CREATE TABLE IF NOT EXISTS papers (
    arxiv_id TEXT PRIMARY KEY,
    version INTEGER,
    title TEXT NOT NULL,
    abstract TEXT NOT NULL,
    authors_json TEXT NOT NULL,
    categories_json TEXT NOT NULL,
    published_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    doi TEXT,
    license TEXT,
    comments TEXT,
    journal_ref TEXT,
    datestamp TEXT,
    versions_json TEXT NOT NULL,
    html_url TEXT,
    input_hash TEXT NOT NULL,
    metadata_hash TEXT NOT NULL,
    metadata_deleted INTEGER NOT NULL DEFAULT 0,
    raw_response_path TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS papers_published ON papers(published_at);
CREATE INDEX IF NOT EXISTS papers_doi ON papers(doi);
CREATE TABLE IF NOT EXISTS paper_profile_matches (
    arxiv_id TEXT NOT NULL REFERENCES papers(arxiv_id),
    profile_key TEXT NOT NULL,
    reason TEXT NOT NULL,
    topics_json TEXT NOT NULL,
    priority INTEGER NOT NULL,
    PRIMARY KEY(arxiv_id, profile_key)
);
CREATE TABLE IF NOT EXISTS paper_contents (
    arxiv_id TEXT NOT NULL REFERENCES papers(arxiv_id),
    version_key TEXT NOT NULL,
    html_url TEXT,
    status TEXT NOT NULL DEFAULT 'not_requested',
    raw_html_path TEXT,
    parsed_path TEXT,
    parser_version TEXT,
    content_hash TEXT,
    last_error TEXT,
    PRIMARY KEY(arxiv_id, version_key)
);
CREATE TABLE IF NOT EXISTS ingestion_jobs (
    job_key TEXT PRIMARY KEY,
    profile_key TEXT NOT NULL,
    set_spec TEXT NOT NULL,
    state_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ingestion_pages (
    page_key TEXT PRIMARY KEY,
    job_key TEXT NOT NULL REFERENCES ingestion_jobs(job_key),
    raw_response_path TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    records_seen INTEGER NOT NULL,
    records_admitted INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS embedding_models (
    model_key TEXT PRIMARY KEY,
    spec_json TEXT NOT NULL,
    dimensions INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS embedding_jobs (
    job_key TEXT PRIMARY KEY,
    arxiv_id TEXT NOT NULL REFERENCES papers(arxiv_id),
    model_key TEXT NOT NULL REFERENCES embedding_models(model_key),
    input_hash TEXT NOT NULL,
    priority INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    lease_owner TEXT,
    lease_until TEXT,
    available_at TEXT NOT NULL,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(arxiv_id, model_key, input_hash)
);
CREATE INDEX IF NOT EXISTS embedding_jobs_queue ON embedding_jobs(status, available_at, priority);
CREATE TABLE IF NOT EXISTS paper_embeddings (
    arxiv_id TEXT NOT NULL REFERENCES papers(arxiv_id),
    model_key TEXT NOT NULL REFERENCES embedding_models(model_key),
    input_hash TEXT NOT NULL,
    dimensions INTEGER NOT NULL,
    embedding __VECTOR_TYPE__ NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(arxiv_id, model_key, input_hash)
);
