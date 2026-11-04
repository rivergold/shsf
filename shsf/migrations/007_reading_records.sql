CREATE TABLE IF NOT EXISTS reading_favorites (
    owner_key TEXT NOT NULL REFERENCES seed_workspaces(owner_key),
    arxiv_id TEXT NOT NULL REFERENCES papers(arxiv_id),
    created_at TEXT NOT NULL,
    PRIMARY KEY(owner_key,arxiv_id)
);
CREATE INDEX IF NOT EXISTS analysis_jobs_records ON analysis_jobs(owner_key,kind,arxiv_id,updated_at);
