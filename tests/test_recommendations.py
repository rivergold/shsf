import json
import math
import os
import tempfile
import unittest

from shsf.database import Database
from shsf.embeddings import EmbeddingSpec
from shsf.models import json_text
from shsf.recommendations import Recommendations
import test_seeds as fixtures


def vec(a,b=0):
    norm=math.sqrt(a*a+b*b)
    return [a/norm,b/norm]+[0.0]*766


class RecommendationContract:
    def setup_fixture(self):
        self.fixture=fixtures.StoreTests();self.fixture.tmp=self.tmp;self.fixture.db=self.db
        self.spec=EmbeddingSpec();self.store=fixtures.SeedStore(self.db)
        self.recs=Recommendations(self.db)
        self.add('2303.00001','Seed A',vec(1))
        self.add('2303.00002','Seed B',vec(0,1))
        self.store.add_local('2303.00001',0)
        self.store.add_local('2303.00002',1)
        self.store.confirm(2)
        self.add('2303.00003','Historical candidate',vec(1,1))
        self.add('2303.00004','Recent candidate',vec(1,.5),'2026-10-20')
        self.add('2303.00005','Other candidate',vec(1,.1))

    def add(self,aid,title,v,published='2023-03-07'):
        self.fixture.addpaper(aid,title)
        with self.db.transaction() as tx:
            tx.execute('UPDATE papers SET published_at=? WHERE arxiv_id=?',(published,aid))
        for job in self.db.claim_embeddings(self.spec.key,10):
            self.db.finish_embedding(job,v,768)

    def test_cosine_rank_reasons_and_historical_recall(self):
        result=self.recs.generate('2026-10-24')
        items=result['batch']['items']
        self.assertEqual(len(items),3)
        self.assertEqual(items[0]['arxiv_id'],'2303.00003')
        self.assertAlmostEqual(items[0]['similarity'],1/math.sqrt(2),places=6)
        self.assertEqual(len(items[0]['reasons']),2)
        self.assertFalse(result['context']['trained'])
        self.assertTrue(any(i['published_at'].startswith('2023') for i in items))
        self.assertNotIn('2303.00001',{i['arxiv_id'] for i in items})

    def test_batch_frozen_after_new_embeddings_and_readback(self):
        first=self.recs.generate('2026-10-24')
        self.add('2303.00006','New high match',vec(1,1))
        again=self.recs.generate('2026-10-24')
        self.assertEqual(first['batch'],again['batch'])
        self.assertGreater(again['context']['corpus_vectors'],first['context']['corpus_vectors'])
        self.assertEqual(self.recs.get('2026-10-24')['batch'],first['batch'])

    def test_feedback_persists_is_reversible_and_excludes_next_batch(self):
        self.recs.generate('2026-10-24')
        self.recs.feedback('2303.00003',interest='dislike')
        self.recs.feedback('2303.00003',is_read=True)
        feedback=self.recs.get('2026-10-24')['batch']['items'][0]['feedback']
        self.assertEqual(feedback,{'interest':'dislike','is_read':True})
        later=self.recs.generate('2026-12-01')
        self.assertNotIn('2303.00003',{p['arxiv_id'] for p in later['batch']['items']})
        self.recs.feedback('2303.00003',interest='none',is_read=False)
        self.assertEqual(self.recs.get('2026-10-24')['batch']['items'][0]['feedback'],{'interest':'none','is_read':False})
        with self.assertRaises(ValueError):self.recs.feedback('2303.00001',interest='like')
        with self.assertRaises(ValueError):self.recs.feedback('2303.00004',is_read='yes')

    def test_recently_shown_excluded_from_next_day(self):
        self.recs.generate('2026-10-24')
        self.assertEqual(self.recs.generate('2026-10-25')['batch']['items'],[])

    def test_unconfirmed_draft_does_not_change_profile(self):
        initial=self.recs.generate('2026-10-24')
        self.store.remove(self.store.state()['items'][0]['seed_id'],2)
        current=self.recs.get('2026-10-24')
        self.assertEqual(initial['batch']['batch_id'],current['batch']['batch_id'])
        self.assertTrue(current['context']['draft_changed'])
        self.store.confirm(3)
        self.assertIsNone(self.recs.get('2026-10-24')['batch'])

    def test_stale_deleted_future_and_title_duplicates_excluded(self):
        self.add('2303.00006','Stale candidate',vec(1,1))
        self.add('2303.00007','Deleted candidate',vec(1,1))
        self.add('2303.00008','Future candidate',vec(1,1),'2027-01-01')
        self.add('2303.00009','Seed A',vec(1,1))
        with self.db.transaction() as tx:
            tx.execute("UPDATE papers SET input_hash='changed' WHERE arxiv_id=?",('2303.00006',))
            tx.execute('UPDATE papers SET metadata_deleted=1 WHERE arxiv_id=?',('2303.00007',))
        ids={p['arxiv_id'] for p in self.recs.generate('2026-10-24')['batch']['items']}
        self.assertEqual(ids,{'2303.00003','2303.00004','2303.00005'})

    def test_requires_confirmed_seeds_and_current_seed_vectors(self):
        with self.db.transaction() as tx:tx.execute('DELETE FROM paper_embeddings')
        with self.assertRaises(ValueError):self.recs.generate('2026-10-24')
        with self.db.transaction() as tx:tx.execute('DELETE FROM confirmed_seeds')
        with self.assertRaises(ValueError):self.recs.generate('2026-10-24')

    def test_latest_release_quota_uses_membership_not_submission_date(self):
        for i in range(6, 14):
            self.add(f'2610.{i:05}', f'Latest paper {i}', vec(1, .2), '2026-10-20')
        with self.db.transaction() as tx:
            tx.execute("INSERT INTO arxiv_releases VALUES (?,?,?,?)", ('2026-10-24', '[]', '/raw.xml', '2026-10-24'))
            for i in range(6, 14):
                tx.execute('INSERT INTO arxiv_release_items VALUES (?,?)', ('2026-10-24', f'2610.{i:05}'))
        result = self.recs.generate('2026-10-24', limit=6)
        self.assertEqual(result['batch']['context']['latest_selected'], 6)
        self.assertTrue(all(p['is_latest_release'] for p in result['batch']['items']))
        self.assertTrue(all(p['announcement_day']=='2026-10-24' for p in result['batch']['items']))

    def test_twenty_item_batch_targets_six_even_with_high_scoring_new_papers(self):
        for i in range(6, 36):
            self.add(f'2610.{i:05}', f'Candidate paper {i}', vec(1, .8 if i<20 else .2), '2026-10-20')
        with self.db.transaction() as tx:
            tx.execute('INSERT INTO arxiv_releases VALUES (?,?,?,?)', ('2026-10-24', '[]', '/raw.xml', '2026-10-24'))
            for i in range(6, 20):
                tx.execute('INSERT INTO arxiv_release_items VALUES (?,?)', ('2026-10-24', f'2610.{i:05}'))
        batch=self.recs.generate('2026-10-24')['batch']
        self.assertEqual(len(batch['items']), 20)
        self.assertEqual(batch['context']['latest_selected'], 6)

    def test_latest_shortage_fills_with_other_research_and_survives_weekend(self):
        with self.db.transaction() as tx:
            tx.execute('INSERT INTO arxiv_releases VALUES (?,?,?,?)', ('2026-10-23', '[]', '/raw.xml', '2026-10-23'))
            tx.execute('INSERT INTO arxiv_release_items VALUES (?,?)', ('2026-10-23', '2303.00004'))
        result = self.recs.generate('2026-10-25')
        self.assertEqual(result['batch']['context']['latest_release_day'], '2026-10-23')
        self.assertEqual(result['batch']['context']['latest_selected'], 1)
        self.assertEqual(len(result['batch']['items']), 3)
        self.assertFalse(next(p for p in result['batch']['items'] if p['arxiv_id']=='2303.00003')['is_latest_release'])

    def test_new_announcement_on_same_day_gets_new_batch_without_overwriting_old(self):
        first = self.recs.generate('2026-10-24')['batch']
        with self.db.transaction() as tx:
            tx.execute('INSERT INTO arxiv_releases VALUES (?,?,?,?)', ('2026-10-24', '[]', '/raw.xml', '2026-10-24'))
            tx.execute('INSERT INTO arxiv_release_items VALUES (?,?)', ('2026-10-24', '2303.00004'))
        latest = self.recs.generate('2026-10-24')['batch']
        self.assertNotEqual(first['batch_id'], latest['batch_id'])
        self.assertEqual(latest['context']['latest_selected'], 1)
        with self.db.transaction() as tx:
            self.assertEqual(tx.execute('SELECT COUNT(*) AS n FROM recommendation_batches').fetchone()['n'], 2)

    def test_manual_generation_waits_for_automatic_update(self):
        with self.db.transaction() as tx:
            tx.execute('INSERT INTO daily_update_runs VALUES (?,?,?,?,?,?,?,?)',
                ('test-run', '2026-10-24', 'running', 'embeddings', '2026-10-24', None, '{}', None))
        with self.assertRaisesRegex(ValueError, '每日更新'):
            self.recs.generate('2026-10-24')
        self.assertIsNotNone(self.recs.generate('2026-10-24', during_update=True)['batch'])


class SQLiteRecommendationTests(RecommendationContract,unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.db=Database('sqlite:///'+self.tmp.name+'/feed.sqlite');self.db.migrate();self.setup_fixture()


@unittest.skipUnless(os.getenv('SHSF_TEST_DATABASE_URL'),'requires isolated PostgreSQL database')
class PostgresRecommendationTests(RecommendationContract,unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.db=Database(os.environ['SHSF_TEST_DATABASE_URL']);self.db.migrate()
        def clean():
            with self.db.transaction() as tx:tx.execute('TRUNCATE seed_workspaces,seed_items,papers,embedding_models,ingestion_jobs,arxiv_releases,daily_update_runs CASCADE')
        clean();self.addCleanup(clean);self.setup_fixture()
