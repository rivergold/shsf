from contextlib import contextmanager
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import sqlite3
import uuid

from .models import digest, json_text, utcnow


class Session:
    def __init__(self, connection, postgres):
        self.connection, self.postgres = connection, postgres

    def execute(self, sql, args=()):
        return self.connection.execute(sql.replace("?", "%s") if self.postgres else sql, args)


class Database:
    def __init__(self, url):
        self.url = url
        self.postgres = url.startswith(("postgresql://", "postgres://"))
        if not self.postgres:
            if not url.startswith("sqlite:///"):
                raise ValueError("Database must be postgresql://… or sqlite:///…")
            self.path = Path(url.removeprefix("sqlite:///"))
            self.path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def transaction(self, immediate=False):
        if self.postgres:
            import psycopg
            from psycopg.rows import dict_row
            conn = psycopg.connect(self.url, row_factory=dict_row)
        else:
            conn = sqlite3.connect(self.path, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield Session(conn, self.postgres)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def migrate(self):
        with self.transaction(immediate=True) as db:
            if self.postgres:
                # Serialize migrations from concurrent CLI/worker processes.
                db.execute("SELECT pg_advisory_xact_lock(77120311)")
                db.execute("CREATE EXTENSION IF NOT EXISTS vector")
            db.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, checksum TEXT NOT NULL, applied_at TEXT NOT NULL)")
            for migration in sorted(Path(__file__).with_name("migrations").glob("*.sql")):
                source = migration.read_text()
                checksum = digest(source)
                old = db.execute("SELECT checksum FROM schema_migrations WHERE version=?", (migration.name,)).fetchone()
                if old:
                    if old["checksum"] != checksum:
                        raise RuntimeError(f"Applied migration changed: {migration.name}")
                    continue
                sql = source.replace("__VECTOR_TYPE__", "vector" if self.postgres else "TEXT")
                for statement in sql.split(";"):
                    if statement.strip():
                        db.execute(statement)
                db.execute("INSERT INTO schema_migrations VALUES (?,?,?)", (migration.name, checksum, utcnow()))

    def register_model(self, spec):
        with self.transaction() as db:
            old = db.execute("SELECT spec_json FROM embedding_models WHERE model_key=?", (spec.key,)).fetchone()
            if old and old["spec_json"] != json_text(spec.to_dict()):
                raise ValueError("Model key collision: configuration changed")
            db.execute("INSERT INTO embedding_models VALUES (?,?,?,?) ON CONFLICT(model_key) DO NOTHING",
                       (spec.key, json_text(spec.to_dict()), spec.dimensions, utcnow()))

    @staticmethod
    def enqueue(db, arxiv_id, input_hash, model_key, priority):
        job_key = digest(json_text([arxiv_id, model_key, input_hash]))
        stamp = utcnow()
        db.execute("""INSERT INTO embedding_jobs
            (job_key,arxiv_id,model_key,input_hash,priority,status,available_at,created_at,updated_at)
            VALUES (?,?,?,?,?,'pending',?,?,?) ON CONFLICT(arxiv_id,model_key,input_hash)
            DO UPDATE SET priority=MIN(embedding_jobs.priority,excluded.priority)""".replace(
                "MIN(embedding_jobs.priority,excluded.priority)",
                "LEAST(embedding_jobs.priority,excluded.priority)" if db.postgres else "MIN(embedding_jobs.priority,excluded.priority)"),
            (job_key, arxiv_id, model_key, input_hash, priority, stamp, stamp, stamp))

    def save_paper(self, db, paper, match, profile_key, raw_path, model_key):
        if not paper.title or not paper.abstract or not paper.published_at or not paper.updated_at:
            raise ValueError(f"Incomplete paper metadata: {paper.arxiv_id}")
        old = db.execute("SELECT * FROM papers WHERE arxiv_id=?", (paper.arxiv_id,)).fetchone()
        # Replaying an archived response must not resurrect a record removed
        # from the source. Restoring a tombstoned record is not an implicit
        # consequence of an explicit ID fetch.
        if old and old["metadata_deleted"]:
            return False
        # A stale replay must never replace a newer paper version.
        if old and ((old["version"] or 0) > (paper.version or 0) or
                    ((old["version"] or 0) == (paper.version or 0) and old["updated_at"] > paper.updated_at)):
            return False
        if old:
            # Atom does not carry license/datestamp/version history. A sparse
            # explicit import must not erase richer OAI metadata.
            for name in ("doi", "license", "comments", "journal_ref", "datestamp", "html_url"):
                if getattr(paper, name) is None:
                    setattr(paper, name, old[name])
            if not paper.versions:
                paper.versions = json.loads(old["versions_json"])
        values = (paper.arxiv_id, paper.version, paper.title, paper.abstract,
                  json_text(paper.authors), json_text(paper.categories), paper.published_at, paper.updated_at,
                  paper.doi, paper.license, paper.comments, paper.journal_ref, paper.datestamp,
                  json_text(paper.versions), paper.html_url, paper.input_hash, digest(json_text(paper.to_dict())),
                  0, str(raw_path), utcnow())
        columns = "arxiv_id,version,title,abstract,authors_json,categories_json,published_at,updated_at,doi,license,comments,journal_ref,datestamp,versions_json,html_url,input_hash,metadata_hash,metadata_deleted,raw_response_path,fetched_at"
        names = columns.split(",")
        db.execute(f"INSERT INTO papers ({columns}) VALUES ({','.join('?' for _ in names)}) ON CONFLICT(arxiv_id) DO UPDATE SET " +
                   ",".join(f"{name}=excluded.{name}" for name in names[1:]), values)
        db.execute("""INSERT INTO paper_profile_matches VALUES (?,?,?,?,?)
            ON CONFLICT(arxiv_id,profile_key) DO UPDATE SET reason=excluded.reason,
            topics_json=excluded.topics_json,priority=excluded.priority""",
            (paper.arxiv_id, profile_key, match["reason"], json_text(match["topics"]), match["priority"]))
        db.execute("INSERT INTO paper_contents(arxiv_id,version_key,html_url) VALUES (?,?,?) ON CONFLICT(arxiv_id,version_key) DO NOTHING",
                   (paper.arxiv_id, str(paper.version or "unknown"), paper.html_url))
        self.enqueue(db, paper.arxiv_id, paper.input_hash, model_key, match["priority"])
        if old and old["input_hash"] != paper.input_hash:
            db.execute("UPDATE embedding_jobs SET status='superseded',updated_at=? WHERE arxiv_id=? AND input_hash<>? AND status IN ('pending','retry','running')",
                       (utcnow(), paper.arxiv_id, paper.input_hash))
        return old is None

    def get_state(self, key):
        with self.transaction() as db:
            row = db.execute("SELECT state_json FROM ingestion_jobs WHERE job_key=?", (key,)).fetchone()
            return json.loads(row["state_json"]) if row else None

    @staticmethod
    def save_state(db, key, profile_key, set_spec, state):
        db.execute("""INSERT INTO ingestion_jobs VALUES (?,?,?,?,?) ON CONFLICT(job_key)
            DO UPDATE SET state_json=excluded.state_json,updated_at=excluded.updated_at""",
            (key, profile_key, set_spec, json_text(state), utcnow()))

    def claim_embeddings(self, model_key, limit=8, lease_seconds=1800, max_attempts=5):
        owner = uuid.uuid4().hex
        stamp = utcnow()
        until = (datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)).isoformat(timespec="microseconds")
        with self.transaction(immediate=True) as db:
            db.execute("""UPDATE embedding_jobs SET status='superseded',updated_at=?
                WHERE status IN ('pending','retry','running') AND EXISTS
                (SELECT 1 FROM papers p WHERE p.arxiv_id=embedding_jobs.arxiv_id
                AND (p.input_hash<>embedding_jobs.input_hash OR p.metadata_deleted=1))""", (stamp,))
            db.execute("""UPDATE embedding_jobs SET status='failed',updated_at=?
                WHERE status='running' AND lease_until<? AND attempts>=?""", (stamp, stamp, max_attempts))
            rows = db.execute("""SELECT j.*,p.title,p.abstract FROM embedding_jobs j JOIN papers p ON p.arxiv_id=j.arxiv_id
                WHERE j.model_key=? AND j.attempts<? AND p.metadata_deleted=0 AND p.input_hash=j.input_hash
                AND ((j.status IN ('pending','retry') AND j.available_at<=?) OR (j.status='running' AND j.lease_until<?))
                ORDER BY j.priority,j.created_at LIMIT ?""" + (" FOR UPDATE OF j SKIP LOCKED" if self.postgres else ""),
                (model_key, max_attempts, stamp, stamp, limit)).fetchall()
            result = []
            for row in rows:
                db.execute("UPDATE embedding_jobs SET status='running',lease_owner=?,lease_until=?,attempts=attempts+1,updated_at=? WHERE job_key=?",
                           (owner, until, stamp, row["job_key"]))
                result.append(dict(row, lease_owner=owner, attempts=row["attempts"] + 1))
            return result

    def finish_embedding(self, job, vector, dimensions):
        if len(vector) != dimensions or not all(math.isfinite(v) for v in vector) or sum(v*v for v in vector) == 0:
            raise ValueError("Embedding has wrong dimensions, non-finite values, or zero norm")
        with self.transaction(immediate=True) as db:
            current = db.execute("SELECT j.status,j.lease_owner,p.input_hash,p.metadata_deleted FROM embedding_jobs j JOIN papers p ON p.arxiv_id=j.arxiv_id WHERE j.job_key=?" +
                                 (" FOR UPDATE OF j" if self.postgres else ""), (job["job_key"],)).fetchone()
            if not current or current["status"] != "running" or current["lease_owner"] != job["lease_owner"]:
                return False
            if current["input_hash"] != job["input_hash"] or current["metadata_deleted"]:
                db.execute("UPDATE embedding_jobs SET status='superseded',updated_at=? WHERE job_key=?", (utcnow(), job["job_key"]))
                return False
            placeholder = "CAST(? AS vector)" if self.postgres else "?"
            db.execute(f"""INSERT INTO paper_embeddings VALUES (?,?,?,?,{placeholder},?)
                ON CONFLICT(arxiv_id,model_key,input_hash) DO UPDATE SET embedding=excluded.embedding,created_at=excluded.created_at""",
                (job["arxiv_id"], job["model_key"], job["input_hash"], dimensions, json_text(vector), utcnow()))
            db.execute("UPDATE embedding_jobs SET status='completed',last_error=NULL,lease_owner=NULL,lease_until=NULL,updated_at=? WHERE job_key=?",
                       (utcnow(), job["job_key"]))
            return True

    def fail_embedding(self, job, error, max_attempts=5):
        available = (datetime.now(timezone.utc) + timedelta(seconds=min(3600, 30 * 2 ** job["attempts"]))).isoformat(timespec="microseconds")
        with self.transaction() as db:
            db.execute("""UPDATE embedding_jobs SET status=?,available_at=?,last_error=?,lease_owner=NULL,lease_until=NULL,updated_at=?
                WHERE job_key=? AND lease_owner=? AND status='running'""",
                ("failed" if job["attempts"] >= max_attempts else "retry", available, str(error)[:1000], utcnow(), job["job_key"], job["lease_owner"]))

    def status(self):
        with self.transaction() as db:
            return {"backend": "postgresql+pgvector" if self.postgres else "sqlite-development",
                    "papers": db.execute("SELECT COUNT(*) AS n FROM papers").fetchone()["n"],
                    "embeddings": db.execute("SELECT COUNT(*) AS n FROM paper_embeddings").fetchone()["n"],
                    "embedding_jobs": {r["status"]: r["n"] for r in db.execute("SELECT status,COUNT(*) AS n FROM embedding_jobs GROUP BY status").fetchall()},
                    "harvests": [dict(r) for r in db.execute("SELECT job_key,set_spec,state_json FROM ingestion_jobs").fetchall()]}

    def corpus_report(self, model_key, profile_key):
        """Count usable current vectors, not archived vectors from older inputs.

        Topic counts overlap by design: a whole-body VLA paper can belong to
        several interests. Unclassified core papers remain visible separately.
        """
        with self.transaction() as db:
            if self.postgres:
                # A live harvester can commit between statements. Keep all
                # report counts on one consistent database snapshot.
                db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            total = db.execute("SELECT COUNT(*) AS n FROM papers WHERE metadata_deleted=0").fetchone()["n"]
            current_vectors = db.execute("""SELECT COUNT(*) AS n FROM paper_embeddings e JOIN papers p
                ON p.arxiv_id=e.arxiv_id AND p.input_hash=e.input_hash
                WHERE e.model_key=? AND p.metadata_deleted=0""", (model_key,)).fetchone()["n"]
            years = [dict(r) for r in db.execute("""SELECT SUBSTR(published_at,1,4) AS year,COUNT(*) AS papers
                FROM papers WHERE metadata_deleted=0 GROUP BY SUBSTR(published_at,1,4) ORDER BY year DESC""").fetchall()]
            topics, reasons = Counter(), Counter()
            unclassified, matched = 0, 0
            for row in db.execute("""SELECT m.topics_json,m.reason FROM paper_profile_matches m JOIN papers p
                ON p.arxiv_id=m.arxiv_id WHERE m.profile_key=? AND p.metadata_deleted=0""", (profile_key,)).fetchall():
                entries = json.loads(row["topics_json"])
                topics.update({topic["id"] for topic in entries})
                reasons[row["reason"]] += 1
                unclassified += not bool(entries)
                matched += 1
            jobs = [dict(r) for r in db.execute("""SELECT j.status,j.priority,COUNT(*) AS papers FROM embedding_jobs j
                JOIN papers p ON p.arxiv_id=j.arxiv_id AND p.input_hash=j.input_hash
                WHERE j.model_key=? AND p.metadata_deleted=0 GROUP BY j.status,j.priority ORDER BY j.priority,j.status""",
                (model_key,)).fetchall()]
            harvests = []
            for row in db.execute("SELECT set_spec,state_json,updated_at FROM ingestion_jobs WHERE profile_key=? ORDER BY set_spec", (profile_key,)).fetchall():
                state = json.loads(row["state_json"])
                harvests.append({"set_spec": row["set_spec"], "mode": state["mode"],
                                 "complete": state["complete"], "pages": state["pages"],
                                 "records_scanned": state["seen"], "records_admitted": state["admitted"],
                                 "watermark": state["watermark"], "updated_at": row["updated_at"]})
            return {"generated_at": utcnow(), "model_key": model_key, "profile_key": profile_key,
                    "active_papers": total, "current_embeddings": current_vectors,
                    "embedding_coverage": current_vectors / total if total else 0,
                    "profile_matched_papers": matched, "topics": dict(sorted(topics.items())),
                    "topic_counts_overlap": True, "unclassified_papers": unclassified,
                    "admission_reasons": dict(reasons), "years": years,
                    "current_embedding_jobs": jobs, "harvests": harvests}

    def neighbors(self, arxiv_id, model_key, limit=10):
        """Paper-to-paper cosine similarity, not a personalized score."""
        with self.transaction() as db:
            reference = db.execute("""SELECT e.embedding FROM paper_embeddings e JOIN papers p
                ON p.arxiv_id=e.arxiv_id AND p.input_hash=e.input_hash
                WHERE p.arxiv_id=? AND e.model_key=? AND p.metadata_deleted=0""",
                (arxiv_id, model_key)).fetchone()
            if reference is None:
                raise ValueError("Reference paper has no current vector for this model; import it and run embed first")
            if self.postgres:
                return [dict(row) for row in db.execute("""WITH reference AS (
                    SELECT e.embedding FROM paper_embeddings e JOIN papers p
                    ON p.arxiv_id=e.arxiv_id AND p.input_hash=e.input_hash
                    WHERE p.arxiv_id=? AND e.model_key=? AND p.metadata_deleted=0)
                    SELECT p.arxiv_id,p.title,p.published_at,p.categories_json,
                           1-(e.embedding <=> r.embedding) AS similarity
                    FROM paper_embeddings e JOIN papers p
                    ON p.arxiv_id=e.arxiv_id AND p.input_hash=e.input_hash CROSS JOIN reference r
                    WHERE e.model_key=? AND p.arxiv_id<>? AND p.metadata_deleted=0
                    ORDER BY e.embedding <=> r.embedding,p.arxiv_id LIMIT ?""",
                    (arxiv_id, model_key, model_key, arxiv_id, limit)).fetchall()]
            # Development-only exact scan. Deployment uses pgvector above.
            seed = json.loads(reference["embedding"])
            seed_norm = math.sqrt(sum(v * v for v in seed))
            result = []
            for row in db.execute("""SELECT p.arxiv_id,p.title,p.published_at,p.categories_json,e.embedding
                FROM paper_embeddings e JOIN papers p ON p.arxiv_id=e.arxiv_id AND p.input_hash=e.input_hash
                WHERE e.model_key=? AND p.arxiv_id<>? AND p.metadata_deleted=0""", (model_key, arxiv_id)).fetchall():
                vector = json.loads(row["embedding"])
                norm = math.sqrt(sum(v * v for v in vector))
                result.append({k: row[k] for k in ("arxiv_id", "title", "published_at", "categories_json")} |
                              {"similarity": sum(a * b for a, b in zip(seed, vector)) / (seed_norm * norm)})
            return sorted(result, key=lambda r: (-r["similarity"], r["arxiv_id"]))[:limit]

    def search(self, query, limit=10):
        # Literal lexical search for pipeline inspection. Semantic search/UI is
        # a later phase; do not claim this LIKE query is a vector search.
        escaped = query.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        with self.transaction() as db:
            return [dict(r) for r in db.execute("""SELECT arxiv_id,version,title,published_at,categories_json FROM papers
                WHERE metadata_deleted=0 AND (LOWER(title) LIKE ? ESCAPE '\\' OR LOWER(abstract) LIKE ? ESCAPE '\\')
                ORDER BY published_at DESC LIMIT ?""", (f"%{escaped}%", f"%{escaped}%", limit)).fetchall()]
