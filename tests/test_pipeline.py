from dataclasses import replace
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from shsf.arxiv import ArxivError, Page, parse_atom, parse_oai
from shsf.database import Database
from shsf.embeddings import EmbeddingSpec, run_worker
from shsf.ingestion import Ingester, category_set
from shsf.models import Paper, normalize_id
from shsf.profile import Profile

ROOT = Path(__file__).resolve().parents[1]


def paper(**kwargs):
    values = dict(arxiv_id="2303.04137", version=5, title="Diffusion Policy",
                  abstract="Visuomotor robot learning via action diffusion.", authors=["Cheng Chi"],
                  categories=["cs.RO"], published_at="2023-03-07T18:50:03Z", updated_at="2024-03-14T04:36:31Z")
    return Paper(**dict(values, **kwargs))


def oai_page(token="", title="Diffusion Policy", response="2026-10-23T15:00:00Z", deleted=False):
    payload = f"""<metadata><arXivRaw xmlns="http://arxiv.org/OAI/arXivRaw/">
        <id>2303.04137</id><version version="v1"><date>Tue, 07 Mar 2023 18:50:03 GMT</date></version>
        <version version="v5"><date>Thu, 14 Mar 2024 04:36:31 GMT</date></version>
        <title>{title}</title><abstract>Visuomotor robot learning via action diffusion.</abstract>
        <authors>Cheng Chi, Shuran Song</authors><categories>cs.RO cs.SY</categories>
        <license>http://arxiv.org/licenses/nonexclusive-distrib/1.0/</license>
        </arXivRaw></metadata>""" if not deleted else ""
    return f"""<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">
        <responseDate>{response}</responseDate><ListRecords><record>
        <header {'status="deleted"' if deleted else ''}><identifier>oai:arXiv.org:2303.04137</identifier>
        <datestamp>2026-10-22</datestamp></header>{payload}</record>
        <resumptionToken>{token}</resumptionToken></ListRecords></OAI-PMH>""".encode()


class FakeClient:
    def __init__(self, bodies):
        self.bodies, self.calls = list(bodies), []

    def get(self, endpoint, params):
        self.calls.append(dict(params))
        return self.bodies.pop(0), endpoint


class ParserTests(unittest.TestCase):
    def test_modern_legacy_and_unspecified_versions(self):
        self.assertEqual(normalize_id("https://arxiv.org/pdf/2303.04137v5.pdf"), ("2303.04137", 5))
        self.assertEqual(normalize_id("cond-mat/0610001"), ("cond-mat/0610001", None))
        with self.assertRaises(ValueError):
            normalize_id("bad-id")

    def test_raw_versions_and_alias(self):
        p = parse_oai(oai_page()).papers[0]
        self.assertEqual(p.version, 5)
        self.assertEqual(p.published_at, "2023-03-07T18:50:03Z")
        self.assertEqual(p.categories, ["cs.RO", "eess.SY"])
        self.assertEqual(p.authors, ["Cheng Chi, Shuran Song"])

    def test_atom_old_id_and_html(self):
        feed = b'''<feed xmlns="http://www.w3.org/2005/Atom"><entry>
          <id>http://arxiv.org/abs/cond-mat/0610001v2</id><title>A\n title</title><summary>Abstract</summary>
          <published>2006-10-01T00:00:00Z</published><updated>2007-01-01T00:00:00Z</updated>
          <author><name>A Researcher</name></author><category term="cond-mat.mes-hall"/>
          <link type="text/html" href="https://arxiv.org/html/cond-mat/0610001v2"/>
          </entry></feed>'''
        p = parse_atom(feed).papers[0]
        self.assertEqual(p.arxiv_id, "cond-mat/0610001")
        self.assertEqual(p.title, "A title")
        self.assertTrue(p.html_url.endswith("v2"))

    def test_xml_entity_expansion_rejected(self):
        with self.assertRaises(Exception):
            parse_atom(b'<!DOCTYPE feed [<!ENTITY x "bad">]><feed xmlns="http://www.w3.org/2005/Atom">&x;</feed>')

    def test_error_response_not_mistaken_for_empty_completion(self):
        with self.assertRaises(ArxivError):
            parse_oai(b'<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/"><error code="badResumptionToken">expired</error></OAI-PMH>')
        self.assertEqual(parse_oai(oai_page(deleted=True)).deleted_ids, ["2303.04137"])


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.profile = Profile(ROOT / "config/robotics.json")

    def test_core_recall_and_adjacent_noise(self):
        self.assertTrue(self.profile.classify(paper(title="New robot method", abstract="A new approach"))["admitted"])
        self.assertFalse(self.profile.classify(paper(categories=["cs.LG"], title="Language model agents", abstract="Tool use in finance"))["admitted"])

    def test_systems_have_independent_coverage(self):
        result = self.profile.classify(paper(categories=["cs.DC"], title="ROS 2 real-time scheduling", abstract="Middleware"))
        self.assertIn("robotics_systems", [t["id"] for t in result["topics"]])

    def test_old_foundation_and_acronym_boundaries(self):
        self.assertTrue(self.profile.classify(paper(title="Proximal Policy Optimization", abstract="An algorithm", categories=["cs.LG"], published_at="2010-01-01"))["admitted"])
        result = self.profile.classify(paper(title="Robot examples", abstract="An example with sampling", categories=["cs.LG"]))
        self.assertEqual(result["topics"], [])  # AMP must not match 'example'/'sampling'


class DatabaseContract:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(self.database_url())
        self.db.migrate()
        self.spec = EmbeddingSpec()
        self.profile = Profile(ROOT / "config/robotics.json")
        self.ingest = Ingester(self.db, self.profile, self.spec, FakeClient([]), self.tmp.name)

    def add(self, p=None):
        p = p or paper()
        return self.ingest.import_page(Page([p]), "/test/raw.xml")

    def query(self, sql, args=()):
        with self.db.transaction() as tx:
            return [dict(r) for r in tx.execute(sql, args).fetchall()]

    def test_idempotent_replay_and_unchanged_input(self):
        self.assertEqual(self.add()["inserted"], 1)
        self.assertEqual(self.add()["inserted"], 0)
        self.add(paper(doi="10.1234/test"))
        self.assertEqual(self.db.status()["papers"], 1)
        self.assertEqual(len(self.query("SELECT * FROM embedding_jobs")), 1)

    def test_sparse_atom_import_preserves_richer_oai_fields(self):
        self.add(paper(license="license-url", datestamp="2024-03-15", versions=[{"version": 5}]))
        self.add(paper())
        row = self.query("SELECT license,datestamp,versions_json FROM papers")[0]
        self.assertEqual(row["license"], "license-url")
        self.assertEqual(row["datestamp"], "2024-03-15")
        self.assertEqual(json.loads(row["versions_json"]), [{"version": 5}])

    def test_stale_version_cannot_overwrite_newer(self):
        self.add()
        self.add(paper(version=1, title="Old title", updated_at="2023-03-07T18:50:03Z"))
        row = self.query("SELECT version,title FROM papers")[0]
        self.assertEqual(row, {"version": 5, "title": "Diffusion Policy"})

    def test_changed_input_supersedes_old_lease(self):
        self.add()
        old_job = self.db.claim_embeddings(self.spec.key, 1)[0]
        self.add(paper(version=6, abstract="New robot learning evidence", updated_at="2025-01-01T00:00:00Z"))
        self.assertFalse(self.db.finish_embedding(old_job, [1.0] + [0.0] * 767, 768))
        self.assertEqual(self.db.status()["embedding_jobs"], {"pending": 1, "superseded": 1})

    def test_batch_failure_rolls_back_all_papers_and_jobs(self):
        with self.assertRaises(ValueError):
            self.ingest.import_page(Page([paper(), paper(arxiv_id="2303.04138", title="")]), "/test/raw.xml")
        self.assertEqual(self.db.status()["papers"], 0)
        self.assertEqual(self.db.status()["embedding_jobs"], {})

    def test_claim_is_exclusive_and_expired_owner_cannot_publish(self):
        self.add()
        old_job = self.db.claim_embeddings(self.spec.key, 1)[0]
        self.assertEqual(self.db.claim_embeddings(self.spec.key, 1), [])
        with self.db.transaction() as tx:
            tx.execute("UPDATE embedding_jobs SET lease_until='2000-01-01'")
        new_job = self.db.claim_embeddings(self.spec.key, 1)[0]
        vector = [1.0] + [0.0] * 767
        self.assertFalse(self.db.finish_embedding(old_job, vector, 768))
        self.assertTrue(self.db.finish_embedding(new_job, vector, 768))
        self.assertEqual(self.db.status()["embeddings"], 1)

    def test_invalid_vector_and_retry(self):
        self.add()
        job = self.db.claim_embeddings(self.spec.key, 1)[0]
        with self.assertRaises(ValueError):
            self.db.finish_embedding(job, [float("nan")] * 768, 768)
        self.db.fail_embedding(job, RuntimeError("temporarily unavailable"))
        self.assertEqual(self.db.status()["embedding_jobs"], {"retry": 1})
        self.assertEqual(self.db.claim_embeddings(self.spec.key, 1), [])

    def test_abandoned_last_attempt_becomes_failed(self):
        self.add()
        self.db.claim_embeddings(self.spec.key, 1)
        with self.db.transaction() as tx:
            tx.execute("UPDATE embedding_jobs SET lease_until='2000-01-01',attempts=5")
        self.assertEqual(self.db.claim_embeddings(self.spec.key), [])
        self.assertEqual(self.db.status()["embedding_jobs"], {"failed": 1})

    def test_literal_search_and_migrations_repeatable(self):
        self.add()
        self.db.migrate()
        self.assertEqual(len(self.db.search("diffusion")), 1)
        self.assertEqual(self.db.search("%"), [])

    def test_report_counts_only_current_vectors_and_selected_model(self):
        self.add()
        job = self.db.claim_embeddings(self.spec.key, 1)[0]
        self.db.finish_embedding(job, [1.0] + [0.0] * 767, 768)
        self.add(paper(version=6, abstract="Humanoid whole-body robot reinforcement learning", updated_at="2025-01-01T00:00:00Z"))
        report = self.db.corpus_report(self.spec.key, self.profile.fingerprint)
        self.assertEqual(report["active_papers"], 1)
        self.assertEqual(report["current_embeddings"], 0)
        self.assertEqual(report["topics"]["whole_body_policy"], 1)
        self.assertEqual(report["topics"]["embodied_learning"], 1)
        self.assertEqual(report["current_embedding_jobs"][0]["status"], "pending")
        self.assertEqual(self.db.corpus_report("another-model", self.profile.fingerprint)["current_embeddings"], 0)

    def test_report_does_not_count_deleted_papers(self):
        self.add()
        self.ingest.client = FakeClient([oai_page(deleted=True)])
        self.ingest.harvest()
        report = self.db.corpus_report(self.spec.key, self.profile.fingerprint)
        self.assertEqual(report["active_papers"], 0)
        self.assertEqual(report["profile_matched_papers"], 0)
        self.assertEqual(report["years"], [])

    def test_neighbors_exclude_self_stale_inputs_and_other_models(self):
        self.add()
        self.add(paper(arxiv_id="2303.04138", title="Robot neighbor"))
        self.add(paper(arxiv_id="2303.04139", title="Orthogonal paper"))
        jobs = self.db.claim_embeddings(self.spec.key, 3)
        for job in jobs:
            vector = [0.0] * 768
            vector[1 if job["arxiv_id"] == "2303.04139" else 0] = 1.0
            self.db.finish_embedding(job, vector, 768)
        neighbors = self.db.neighbors("2303.04137", self.spec.key)
        self.assertEqual([r["arxiv_id"] for r in neighbors], ["2303.04138", "2303.04139"])
        self.assertAlmostEqual(neighbors[0]["similarity"], 1)
        self.assertAlmostEqual(neighbors[1]["similarity"], 0)
        self.add(paper(arxiv_id="2303.04138", version=6, title="Changed", updated_at="2025-01-01T00:00:00Z"))
        self.assertEqual(len(self.db.neighbors("2303.04137", self.spec.key)), 1)
        with self.assertRaisesRegex(ValueError, "no current vector"):
            self.db.neighbors("2303.04137", "other-model")

    def test_checkpoint_resume_and_incremental_watermark(self):
        client = FakeClient([oai_page("next+token"), oai_page(response="2026-10-24T15:00:00Z"), oai_page()])
        self.ingest.client = client
        first = self.ingest.harvest(max_pages=1)
        self.assertFalse(first["state"]["complete"])
        self.assertIsNone(first["state"]["watermark"])
        second = self.ingest.harvest(max_pages=1)
        self.assertEqual(client.calls[1], {"verb": "ListRecords", "resumptionToken": "next+token"})
        self.assertEqual(second["state"]["watermark"], "2026-10-23")  # START, not last page
        self.ingest.harvest(incremental=True)
        self.assertEqual(client.calls[2]["from"], "2026-10-21")
        self.assertEqual(self.db.status()["papers"], 1)

    def test_recent_snapshot_is_sorted_and_does_not_advance_harvest_state(self):
        feed = b'''<feed xmlns="http://www.w3.org/2005/Atom"><entry>
          <id>https://arxiv.org/abs/2303.04137v5</id><title>Robot diffusion policy</title>
          <summary>Robot learning.</summary><published>2023-03-07T18:50:03Z</published>
          <updated>2024-03-14T04:36:31Z</updated><category term="cs.RO"/>
          </entry></feed>'''
        client = FakeClient([feed])
        self.ingest.client = client
        result = self.ingest.import_recent("cs.RO", 10)
        self.assertEqual(result["inserted"], 1)
        self.assertFalse(result["advances_incremental_watermark"])
        self.assertEqual(client.calls[0]["sortBy"], "submittedDate")
        self.assertEqual(client.calls[0]["sortOrder"], "descending")
        self.assertEqual(self.db.status()["harvests"], [])
        with self.assertRaises(ValueError):
            self.ingest.import_recent("cs.RO", 1001)
        with self.assertRaises(ValueError):
            self.ingest.import_recent("bad-query", 10)

    def test_expired_token_restarts_same_scope(self):
        error = b'<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/"><error code="badResumptionToken">expired</error></OAI-PMH>'
        client = FakeClient([oai_page("expired"), error, oai_page()])
        self.ingest.client = client
        self.ingest.harvest()
        reset = self.ingest.harvest()
        self.assertIsNone(reset["state"]["token"])
        self.ingest.harvest()
        self.assertNotIn("from", client.calls[-1])
        self.assertEqual(client.calls[-1]["set"], "cs:cs:RO")

    def test_profile_scope_isolation(self):
        self.ingest.client = FakeClient([oai_page("next"), oai_page()])
        self.ingest.harvest(category="cs.RO")
        self.ingest.harvest(category="cs.LG")
        self.assertEqual(len(self.db.status()["harvests"]), 2)

    def test_deleted_metadata_not_recommended_or_embedded(self):
        self.add()
        self.ingest.client = FakeClient([oai_page(deleted=True)])
        self.ingest.harvest()
        self.assertEqual(self.db.search("diffusion"), [])
        self.assertEqual(self.db.claim_embeddings(self.spec.key), [])

    def test_replay_cannot_resurrect_deleted_metadata(self):
        self.add()
        self.ingest.client = FakeClient([oai_page(deleted=True)])
        self.ingest.harvest()
        self.add()
        self.assertEqual(self.db.search("diffusion"), [])
        self.assertEqual(self.db.status()["embedding_jobs"], {"superseded": 1})

    def test_worker_only_publishes_real_provider_result(self):
        self.add()
        class TestEncoder:
            def load(self): pass
            def encode(self, jobs): return [[1.0] + [0.0] * 767 for _ in jobs]
        result = run_worker(self.db, self.spec, TestEncoder())
        self.assertEqual(result["completed"], 1)
        self.assertEqual(self.db.status()["embeddings"], 1)


class SQLiteTests(DatabaseContract, unittest.TestCase):
    def database_url(self):
        return "sqlite:///" + str(Path(self.tmp.name) / "test.sqlite3")


@unittest.skipUnless(os.getenv("SHSF_TEST_DATABASE_URL"), "Set SHSF_TEST_DATABASE_URL for PostgreSQL contract tests")
class PostgreSQLTests(DatabaseContract, unittest.TestCase):
    def database_url(self):
        # This MUST be a dedicated test DB: contract tests truncate application
        # tables and are never pointed at SHSF_DATABASE_URL automatically.
        return os.environ["SHSF_TEST_DATABASE_URL"]

    def tearDown(self):
        with self.db.transaction() as tx:
            tx.execute("TRUNCATE papers,embedding_models,ingestion_jobs CASCADE")


if __name__ == "__main__":
    unittest.main()
