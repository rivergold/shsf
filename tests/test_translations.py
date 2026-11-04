import fcntl
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest

from shsf.agent_config import AgentError
from shsf.models import digest, json_text
from shsf.translations import AbstractTranslations, PROMPT_VERSION, source_hash, translated_text
import test_recommendations as recommendation_fixtures


class Route:
    model = 'translation-model'
    key = 'route-one'

    def __init__(self):
        self.calls = []
        self.fail = False

    def fingerprint(self): return self.key

    def complete(self, prompt):
        self.calls.append(prompt)
        if self.fail:
            raise AgentError('provider error containing secret-token')
        paper = json.loads(prompt.split('资料 JSON：\n')[-1])
        return json_text({'abstract_zh': '机器人学习的中文摘要。' + paper['title']})


class TranslationContract:
    def setup_translation_fixture(self, fixture_type):
        fixture = fixture_type()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture, self.db, self.recs = fixture, fixture.db, fixture.recs
        self.route = Route()
        self.agent = SimpleNamespace(execution=lambda task: self.route)
        self.worker = AbstractTranslations(self.db, self.agent, fixture.tmp.name)
        self.batch = self.recs.generate('2026-10-24')['batch']

    def test_background_precomputes_and_reading_never_calls_agent(self):
        original = [p['abstract'] for p in self.batch['items']]
        self.assertEqual(self.worker.run(self.batch)['completed'], 3)
        self.assertEqual(len(self.route.calls), 3)
        translated = self.recs.get('2026-10-24')['batch']
        self.assertEqual([p['abstract'] for p in translated['items']], original)
        self.assertTrue(all(p['abstract_zh'] and p['translation_status']=='succeeded' for p in translated['items']))
        self.recs.get('2026-10-24')
        self.assertEqual(len(self.route.calls), 3)
        self.assertEqual(self.worker.run(self.batch)['cached'], 3)
        self.assertEqual(len(self.route.calls), 3)

    def test_cache_changes_when_abstract_or_model_route_changes(self):
        self.worker.run(self.batch)
        changed = json.loads(json.dumps(self.batch))
        changed['items'][0]['abstract'] += ' Updated experiments.'
        self.assertEqual(self.worker.run(changed)['completed'], 1)
        self.route.key = 'route-two'
        self.route.model = 'other-model'
        self.assertEqual(self.worker.run(changed)['completed'], 3)

    def test_paper_detail_reuses_translation_and_rejects_stale_abstract(self):
        from shsf.analyses import Analyses
        self.worker.run(self.batch)
        agent = SimpleNamespace(auth=SimpleNamespace(store=SimpleNamespace(directory=Path(self.fixture.tmp.name)/'agent')))
        service = Analyses(self.db, agent, html=object())
        self.addCleanup(service.close)
        paper = self.batch['items'][0]
        detail = service.paper(paper['arxiv_id'])['paper']
        self.assertEqual(detail['abstract'], paper['abstract'])
        self.assertEqual(detail['translation_status'], 'succeeded')
        self.assertTrue(detail['abstract_zh'])
        calls = len(self.route.calls)
        with self.db.transaction() as tx:
            tx.execute('UPDATE papers SET abstract=? WHERE arxiv_id=?', (paper['abstract']+' Revised abstract.', paper['arxiv_id']))
        detail = service.paper(paper['arxiv_id'])['paper']
        self.assertIsNone(detail['abstract_zh'])
        self.assertEqual(len(self.route.calls), calls)

    def test_failure_is_visible_bounded_and_does_not_break_english_feed(self):
        self.route.fail = True
        result = self.worker.run(self.batch)
        self.assertEqual(result['failed'], 1)
        feed = self.recs.get('2026-10-24')['batch']
        self.assertEqual(len(feed['items']), 3)
        self.assertTrue(all(p['abstract'] for p in feed['items']))
        failed = next(p for p in feed['items'] if p['translation_status']=='failed')
        self.assertIsNone(failed['abstract_zh'])
        with self.db.transaction() as tx:
            error = tx.execute("SELECT error FROM abstract_translations WHERE status='failed'").fetchone()['error']
        self.assertNotIn('secret-token', error)
        self.route.fail = False
        self.assertEqual(self.worker.run(self.batch)['completed'], 2)
        self.assertEqual(self.worker.run(self.batch)['deferred'], 1)
        self.assertEqual(self.worker.run(self.batch, retry_failed=True)['completed'], 1)

    def test_interrupted_request_is_not_silently_replayed(self):
        self.worker.run(self.batch)
        paper = self.batch['items'][0]
        key = digest(json_text(['local', paper['arxiv_id'], source_hash(paper), self.route.key, PROMPT_VERSION]))
        with self.db.transaction() as tx:
            tx.execute("UPDATE abstract_translations SET status='running',translated_text=NULL WHERE translation_key=?", (key,))
        before = len(self.route.calls)
        self.assertEqual(self.worker.run(self.batch)['deferred'], 1)
        self.assertEqual(len(self.route.calls), before)
        self.assertEqual(self.worker.run(self.batch, retry_failed=True)['completed'], 1)

    def test_missing_agent_configuration_does_not_block_recommendations(self):
        def blocked(task): raise AgentError('model not configured')
        self.agent.execution = blocked
        self.assertEqual(self.worker.run(self.batch)['status'], 'blocked')
        self.assertEqual(len(self.recs.get('2026-10-24')['batch']['items']), 3)

    def test_parallel_worker_is_skipped(self):
        with (Path(self.fixture.tmp.name) / 'abstract-translations.lock').open('a+') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.worker.run(self.batch)['status'], 'already_running')
        self.assertEqual(self.route.calls, [])

    def test_invalid_output_rejected_without_caching_success(self):
        for result in ['bad JSON', '[]', '{"abstract_zh":"English only"}', '{"abstract_zh":""}']:
            with self.assertRaises(AgentError): translated_text(result)
        self.assertEqual(translated_text('```json\n{"abstract_zh":"中文摘要"}\n```'), '中文摘要')


class SQLiteTranslationTests(TranslationContract, unittest.TestCase):
    def setUp(self): self.setup_translation_fixture(recommendation_fixtures.SQLiteRecommendationTests)


@unittest.skipUnless(os.getenv('SHSF_TEST_DATABASE_URL'), 'requires isolated PostgreSQL')
class PostgresTranslationTests(TranslationContract, unittest.TestCase):
    def setUp(self): self.setup_translation_fixture(recommendation_fixtures.PostgresRecommendationTests)
