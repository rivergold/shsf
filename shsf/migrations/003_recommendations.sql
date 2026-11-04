CREATE TABLE IF NOT EXISTS recommendation_batches (
    batch_id TEXT PRIMARY KEY,
    owner_key TEXT NOT NULL REFERENCES seed_workspaces(owner_key),
    day TEXT NOT NULL,
    profile_key TEXT NOT NULL,
    model_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    context_json TEXT NOT NULL,
    UNIQUE(owner_key,day,profile_key,model_key)
);
CREATE TABLE IF NOT EXISTS recommendation_items (
    batch_id TEXT NOT NULL REFERENCES recommendation_batches(batch_id),
    rank INTEGER NOT NULL,
    arxiv_id TEXT NOT NULL REFERENCES papers(arxiv_id),
    snapshot_json TEXT NOT NULL,
    PRIMARY KEY(batch_id,rank),
    UNIQUE(batch_id,arxiv_id)
);
CREATE INDEX IF NOT EXISTS recommendation_items_paper ON recommendation_items(arxiv_id);
CREATE TABLE IF NOT EXISTS paper_feedback (
    owner_key TEXT NOT NULL REFERENCES seed_workspaces(owner_key),
    arxiv_id TEXT NOT NULL REFERENCES papers(arxiv_id),
    interest TEXT NOT NULL DEFAULT 'none' CHECK(interest IN ('none','like','dislike')),
    is_read INTEGER NOT NULL DEFAULT 0 CHECK(is_read IN (0,1)),
    updated_at TEXT NOT NULL,
    PRIMARY KEY(owner_key,arxiv_id)
);
