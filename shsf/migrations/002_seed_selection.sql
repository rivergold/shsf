CREATE TABLE IF NOT EXISTS seed_workspaces (
    owner_key TEXT PRIMARY KEY,
    revision INTEGER NOT NULL DEFAULT 0,
    confirmed_revision INTEGER NOT NULL DEFAULT -1,
    confirmed_at TEXT
);
CREATE TABLE IF NOT EXISTS seed_items (
    seed_id TEXT PRIMARY KEY,
    identity_key TEXT UNIQUE NOT NULL,
    arxiv_id TEXT REFERENCES papers(arxiv_id),
    claimed_arxiv_id TEXT,
    doi TEXT,
    title TEXT NOT NULL,
    abstract TEXT NOT NULL,
    authors_json TEXT NOT NULL,
    published_year INTEGER,
    match_status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS seed_items_arxiv ON seed_items(arxiv_id);
CREATE INDEX IF NOT EXISTS seed_items_doi ON seed_items(doi);
CREATE TABLE IF NOT EXISTS seed_sources (
    source_id TEXT PRIMARY KEY,
    seed_id TEXT NOT NULL REFERENCES seed_items(seed_id),
    source_kind TEXT NOT NULL,
    library_id TEXT NOT NULL,
    external_key TEXT NOT NULL,
    collections_json TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    UNIQUE(source_kind,library_id,external_key)
);
CREATE TABLE IF NOT EXISTS seed_selection (
    owner_key TEXT NOT NULL REFERENCES seed_workspaces(owner_key),
    seed_id TEXT NOT NULL REFERENCES seed_items(seed_id),
    selected_at TEXT NOT NULL,
    PRIMARY KEY(owner_key,seed_id)
);
CREATE TABLE IF NOT EXISTS confirmed_seeds (
    owner_key TEXT NOT NULL REFERENCES seed_workspaces(owner_key),
    seed_id TEXT NOT NULL REFERENCES seed_items(seed_id),
    metadata_snapshot_json TEXT NOT NULL,
    paper_input_hash TEXT,
    confirmed_at TEXT NOT NULL,
    PRIMARY KEY(owner_key,seed_id)
);
