CREATE TABLE IF NOT EXISTS abstract_translations (
    translation_key TEXT PRIMARY KEY,
    owner_key TEXT NOT NULL REFERENCES seed_workspaces(owner_key),
    arxiv_id TEXT NOT NULL REFERENCES papers(arxiv_id),
    source_hash TEXT NOT NULL,
    route_key TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    model TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('running','succeeded','failed')),
    translated_text TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    retry_after TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS abstract_translations_source ON abstract_translations(owner_key,arxiv_id,source_hash,status);
