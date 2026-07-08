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

-- Canonical external-content FTS5 sync (issue #3). The previous manual sync
-- passed empty content to the 'delete' command, which leaves ghost tokens in
-- the index (FTS5 needs the ORIGINAL content to locate entries to remove).
-- Triggers make the index track the table exactly; application code never
-- writes to memories_fts directly.
CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, content) VALUES (new.rowid, new.content);
END;
CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content)
    VALUES ('delete', old.rowid, old.content);
END;
CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE OF content ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content)
    VALUES ('delete', old.rowid, old.content);
    INSERT INTO memories_fts(rowid, content) VALUES (new.rowid, new.content);
END;

CREATE TABLE IF NOT EXISTS access_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT NOT NULL,
    action        TEXT NOT NULL,
    profile       TEXT NOT NULL,
    details       TEXT,
    tokens_served INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_access_profile ON access_log(profile);
CREATE INDEX IF NOT EXISTS idx_access_ts ON access_log(ts);

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

-- Issue #8: analytics_events replaces analytics.json
CREATE TABLE IF NOT EXISTS analytics_events (
    id               TEXT PRIMARY KEY,
    session_date     TEXT NOT NULL,
    tokens_served    INTEGER NOT NULL DEFAULT 0,
    memories_returned INTEGER NOT NULL DEFAULT 0,
    model            TEXT NOT NULL DEFAULT 'claude',
    operation        TEXT NOT NULL DEFAULT 'get_memory',
    profile          TEXT NOT NULL DEFAULT 'default',
    created_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_analytics_date ON analytics_events(session_date);
CREATE INDEX IF NOT EXISTS idx_analytics_op   ON analytics_events(operation);
CREATE INDEX IF NOT EXISTS idx_analytics_model ON analytics_events(model);
