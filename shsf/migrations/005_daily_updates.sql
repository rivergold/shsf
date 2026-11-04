CREATE TABLE IF NOT EXISTS arxiv_releases (
    release_day TEXT PRIMARY KEY,
    categories_json TEXT NOT NULL,
    raw_response_path TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS arxiv_release_items (
    release_day TEXT NOT NULL REFERENCES arxiv_releases(release_day),
    arxiv_id TEXT NOT NULL REFERENCES papers(arxiv_id),
    PRIMARY KEY(release_day, arxiv_id)
);
CREATE INDEX IF NOT EXISTS arxiv_release_items_paper ON arxiv_release_items(arxiv_id);
CREATE TABLE IF NOT EXISTS daily_update_runs (
    run_id TEXT PRIMARY KEY,
    day TEXT NOT NULL,
    status TEXT NOT NULL,
    stage TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    summary_json TEXT NOT NULL,
    error TEXT
);
CREATE INDEX IF NOT EXISTS daily_update_runs_day ON daily_update_runs(day, started_at);
