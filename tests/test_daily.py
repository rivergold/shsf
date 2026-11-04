import fcntl
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from shsf.arxiv import parse_announcements
from shsf.daily import run_daily
from shsf.database import Database
from shsf.embeddings import EmbeddingSpec
from shsf.ingestion import Ingester, category_set
from shsf.models import digest, json_text
from shsf.profile import Profile
from test_pipeline import FakeClient


def rss(day='Mon, 26 Oct 2026 00:00:00 -0400', items=True, kind='new', aid='2610.00001', abstract='Robot learning'):
    item = f'''<item><title>Robot study</title><guid>oai:arXiv.org:{aid}v1</guid>
      <description>arXiv:{aid}v1 Announce Type: {kind}\nAbstract: {abstract}</description>
      <category>cs.RO</category><pubDate>{day}</pubDate>
      <arxiv:announce_type>{kind}</arxiv:announce_type></item>''' if items else ''
    return f'''<rss xmlns:arxiv="http://arxiv.org/schemas/atom"><channel>
      <pubDate>{day}</pubDate>{item}</channel></rss>'''.encode()


def atom(aid='2610.00001'):
    return f'''<feed xmlns="http://www.w3.org/2005/Atom"><entry>
      <id>http://arxiv.org/abs/{aid}v1</id><title>Robot study</title><summary>Robot learning</summary>
      <published>2026-10-23T10:00:00Z</published><updated>2026-10-23T10:00:00Z</updated>
      <author><name>A Researcher</name></author><category term="cs.RO"/>
      </entry></feed>'''.encode()


class DailyFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database('sqlite:///' + self.tmp.name + '/daily.sqlite')
        self.db.migrate()
        self.spec = EmbeddingSpec()
        self.profile = Profile(Path(__file__).resolve().parents[1] / 'config/robotics.json')

    def ingester(self, bodies):
        return Ingester(self.db, self.profile, self.spec, FakeClient(bodies), self.tmp.name)


class AnnouncementTests(DailyFixture):
    def test_release_day_is_announcement_not_submission_and_replay_is_safe(self):
        ingest = self.ingester([rss(), atom(), rss(), atom()])
        self.assertEqual(ingest.import_announcements()['new_papers'], 1)
        self.assertEqual(ingest.import_announcements()['inserted'], 0)
        with self.db.transaction() as tx:
            self.assertEqual(tx.execute('SELECT release_day FROM arxiv_release_items').fetchone()['release_day'], '2026-10-26')
            self.assertEqual(tx.execute('SELECT published_at FROM papers').fetchone()['published_at'], '2026-10-23T10:00:00Z')

    def test_replacements_and_cross_listings_are_not_new_papers(self):
        for kind in ['replace', 'replace-cross', 'cross']:
            self.assertEqual(parse_announcements(rss(kind=kind))[1], [])
        with self.assertRaises(ValueError):
            parse_announcements(rss(kind='unexpected'))

    def test_empty_weekend_retains_last_real_batch(self):
        ingest = self.ingester([rss(), atom(), rss(day='Sat, 31 Oct 2026 00:00:00 -0400', items=False)])
        ingest.import_announcements()
        self.assertTrue(ingest.import_announcements()['empty_feed'])
        with self.db.transaction() as tx:
            self.assertEqual(tx.execute('SELECT MAX(release_day) AS day FROM arxiv_releases').fetchone()['day'], '2026-10-26')

    def test_incomplete_metadata_never_publishes_a_complete_batch(self):
        ingest = self.ingester([rss(), b'<feed xmlns="http://www.w3.org/2005/Atom"/>'])
        with self.assertRaises(ValueError):
            ingest.import_announcements()
        with self.db.transaction() as tx:
            self.assertEqual(tx.execute('SELECT COUNT(*) AS n FROM arxiv_releases').fetchone()['n'], 0)

    def test_bad_xml_and_missing_announcement_date_rejected(self):
        for body in [b'<html/>', b'<rss><channel/></rss>']:
            with self.assertRaises((ValueError, TypeError)):
                parse_announcements(body)


class DailyTests(DailyFixture):
    def run_update(self, ingest, worker=None, **kwargs):
        worker = worker or {'claimed': 0, 'completed': 0, 'superseded': 0, 'failed': 0}
        with patch('shsf.daily.today', return_value='2026-10-26'), \
             patch('shsf.daily.run_worker', return_value=worker):
            return run_daily(self.db, ingest, self.spec, self.tmp.name, **kwargs)

    def test_daily_success_is_idempotent_and_no_ready_seeds_is_explicit(self):
        ingest = self.ingester([rss(kind='replace')])
        result = self.run_update(ingest)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['recommendations']['reason'], 'no_ready_confirmed_seeds')
        self.assertEqual(self.run_update(ingest)['status'], 'already_completed')
        self.assertEqual(len(ingest.client.calls), 1)

    def test_failed_ingestion_records_stage_and_retry_recovers(self):
        ingest = self.ingester([b'<html>upstream failure</html>', rss(kind='replace')])
        with self.assertRaisesRegex(RuntimeError, 'announcements'):
            self.run_update(ingest)
        with self.db.transaction() as tx:
            row = tx.execute('SELECT status,stage FROM daily_update_runs').fetchone()
            self.assertEqual((row['status'], row['stage']), ('failed', 'announcements'))
        self.assertEqual(self.run_update(ingest)['status'], 'completed')

    def test_missing_embeddings_blocks_recommendation_generation(self):
        ingest = self.ingester([rss(), atom()])
        with patch('shsf.daily.Recommendations') as recs:
            with self.assertRaisesRegex(RuntimeError, 'embeddings'):
                self.run_update(ingest)
            recs.assert_not_called()

    def test_failed_encoder_blocks_generation(self):
        ingest = self.ingester([rss(kind='replace')])
        with self.assertRaisesRegex(RuntimeError, 'embeddings'):
            self.run_update(ingest, {'failed': 1})

    def test_lock_prevents_duplicate_work(self):
        ingest = self.ingester([])
        with (Path(self.tmp.name) / 'daily-update.lock').open('a+') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.run_update(ingest)['status'], 'already_running')
        self.assertEqual(ingest.client.calls, [])

    def test_incomplete_oai_resumes_and_never_generates(self):
        ingest = self.ingester([rss(kind='replace')])
        key = digest(json_text(['oai-raw-v1', category_set('cs.RO'), self.profile.fingerprint]))
        with self.db.transaction() as tx:
            self.db.save_state(tx, key, self.profile.fingerprint, category_set('cs.RO'),
                               {'mode': 'incremental', 'complete': False})
        with patch.object(ingest, 'harvest', return_value={'state': {'complete': False}}) as harvest:
            with self.assertRaisesRegex(RuntimeError, 'incremental'):
                self.run_update(ingest)
            harvest.assert_called_once_with('cs.RO', 50, incremental=True)

    def test_full_pipeline_embeds_new_paper_and_saves_latest_quota(self):
        from test_seeds import StoreTests
        from shsf.seeds import SeedStore
        from shsf.recommendations import Recommendations

        fixture = StoreTests()
        fixture.tmp, fixture.db = self.tmp, self.db
        fixture.addpaper('2303.00001', 'Seed paper')
        store = SeedStore(self.db)
        store.add_local('2303.00001', 0)
        store.confirm(1)

        class Encoder:
            def load(self): pass
            def encode(self, jobs): return [[1.] + [0.] * 767 for _ in jobs]

        ingest = self.ingester([rss(), atom()])
        with patch('shsf.daily.today', return_value='2026-10-26'):
            result = run_daily(self.db, ingest, self.spec, self.tmp.name, encoder=Encoder())
        self.assertEqual(result['embeddings']['completed'], 2)
        self.assertEqual(result['recommendations']['items'], 1)
        batch = Recommendations(self.db).get('2026-10-26')['batch']
        self.assertEqual(batch['context']['latest_selected'], 1)
        self.assertTrue(batch['items'][0]['is_latest_release'])
