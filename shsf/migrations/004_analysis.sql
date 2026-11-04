CREATE TABLE IF NOT EXISTS analysis_jobs (
    job_id TEXT PRIMARY KEY,
    owner_key TEXT NOT NULL REFERENCES seed_workspaces(owner_key),
    arxiv_id TEXT NOT NULL REFERENCES papers(arxiv_id),
    kind TEXT NOT NULL CHECK(kind IN ('explain','read')),
    cache_key TEXT NOT NULL,
    paper_input_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('queued','running','succeeded','failed')),
    stage TEXT NOT NULL,
    progress INTEGER NOT NULL DEFAULT 0,
    channel TEXT NOT NULL,
    model TEXT NOT NULL,
    request_json TEXT NOT NULL,
    partial_json TEXT NOT NULL DEFAULT '{}',
    report_json TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(owner_key,cache_key)
);
CREATE INDEX IF NOT EXISTS analysis_jobs_paper ON analysis_jobs(owner_key,arxiv_id,kind,created_at);
