PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA cache_size=-64000;
PRAGMA temp_store=MEMORY;
PRAGMA busy_timeout=10000;

CREATE TABLE IF NOT EXISTS profiles (
    name        TEXT PRIMARY KEY,
    identity    TEXT NOT NULL DEFAULT '{}',
    projects    TEXT NOT NULL DEFAULT '[]',
    model_prefs TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS memories (
    id             TEXT PRIMARY KEY,
    profile        TEXT NOT NULL REFERENCES profiles(name),
    content        TEXT NOT NULL,
    content_hash   TEXT NOT NULL,
    category       TEXT NOT NULL,
    importance     TEXT NOT NULL,
    relevance_score REAL NOT NULL DEFAULT 1.0,
    created_at     TEXT NOT NULL,
    last_accessed  TEXT NOT NULL,
    access_count   INTEGER NOT NULL DEFAULT 0,
    tags           TEXT NOT NULL DEFAULT '[]',
    project_id     TEXT,
    token_count    INTEGER NOT NULL,
    archived       INTEGER NOT NULL DEFAULT 0,
    archived_at    TEXT,
    archive_reason TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_content_hash
    ON memories(profile, content_hash);
CREATE INDEX IF NOT EXISTS idx_profile_cat
    ON memories(profile, category) WHERE archived = 0;
CREATE INDEX IF NOT EXISTS idx_profile_score
    ON memories(profile, relevance_score DESC) WHERE archived = 0;

CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    content,
    content='memories',
    content_rowid='rowid',
    tokenize='porter ascii'
);

CREATE TABLE IF NOT EXISTS access_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT NOT NULL,
    action        TEXT NOT NULL,
    profile       TEXT NOT NULL,
    details       TEXT,
    tokens_served INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
INSERT OR IGNORE INTO meta VALUES ('schema_version', '4.0');

-- Phase 4: embedding vectors stored as JSON float arrays (no native extension needed)
CREATE TABLE IF NOT EXISTS memory_embeddings (
    id      TEXT PRIMARY KEY REFERENCES memories(id) ON DELETE CASCADE,
    profile TEXT NOT NULL,
    vector  TEXT NOT NULL   -- JSON array of 384 floats (BAAI/bge-small-en-v1.5)
);
CREATE INDEX IF NOT EXISTS idx_embed_profile ON memory_embeddings(profile);
