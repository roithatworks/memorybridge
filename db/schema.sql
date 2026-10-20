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
    archive_reason TEXT,
    -- Bi-temporal validity (v5.2). valid_from is when fact became true (defaults to created_at).
    -- valid_until is when the fact ceased to be valid in the real world (or when superseded).
    valid_from     TEXT,
    valid_until    TEXT,
    superseded_by  TEXT,
    -- Write provenance (#180). "claude" = written over local stdio
    -- (Claude Code/Desktop) — provably true, since that's the only
    -- transport a stdio process serves. "remote" = written over the HTTP
    -- bridge — provably non-Claude-Code, but NOT which specific model, since
    -- the capability-URL auth scheme is one shared secret for every remote
    -- client (see server.py's _caller_model). NULL for rows written before
    -- this column existed.
    source         TEXT,
    -- Self-reported client label (e.g. "hermes"), distinct from `source`.
    -- `source` is transport-derived and can't be spoofed by the caller
    -- (a stdio process really is local, an HTTP-bridge request really is
    -- remote); `client_name` is whatever the caller says it is, unverified —
    -- useful when you already trust your own multi-agent setup (Hermes,
    -- a future second local agent) but not a substitute for real per-client
    -- auth. Sanitized (lowercase, [a-z0-9_-], <=32 chars) on write.
    client_name    TEXT,
    -- 2026 Memory Engineering additions (v5.0)
    confidence     REAL NOT NULL DEFAULT 1.0,
    expires_at     TEXT,
    status         TEXT NOT NULL DEFAULT 'active',
    -- v5.1: Belief injection — instruction on HOW the model should use this fact.
    -- Distinct from content (the fact itself). Not indexed by FTS5 because
    -- searching instructions would return irrelevant hits.
    why_it_matters TEXT,
    -- v5.1: Enhanced provenance — where the memory originated.
    -- origin_type: "manual" (MCP tool), "conversation" (ingestion pipeline),
    --   "document" (doc/ADR ingestion), "consolidation" (session consolidation).
    origin_type    TEXT,
    origin_file    TEXT,       -- source file path, e.g. "docs/ADR-003.md"
    -- v5.1: Edit history — JSON array of {ts, old_content, editor}.
    -- Appended on every content change via edit_memory.
    edit_history   TEXT NOT NULL DEFAULT '[]'
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_content_hash
    ON memories(profile, content_hash);
CREATE INDEX IF NOT EXISTS idx_profile_cat
    ON memories(profile, category) WHERE archived = 0;
CREATE INDEX IF NOT EXISTS idx_profile_score
    ON memories(profile, relevance_score DESC) WHERE archived = 0;
CREATE INDEX IF NOT EXISTS idx_memories_temporal
    ON memories(profile, created_at, valid_from, valid_until);

CREATE TABLE IF NOT EXISTS memory_edges (
    id           TEXT PRIMARY KEY,
    source_id    TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    target_id    TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    relation     TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_edge_source ON memory_edges(source_id);
CREATE INDEX IF NOT EXISTS idx_edge_target ON memory_edges(target_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_edge_unique ON memory_edges(source_id, target_id, relation);

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
INSERT OR IGNORE INTO meta VALUES ('schema_version', '5.1');
UPDATE meta SET value = '5.1' WHERE key = 'schema_version';

-- Phase 4: embedding vectors stored as packed float32 BLOBs (issue #181 P2-1).
-- Previously JSON text, which cost ~81ms of json.loads() parsing per semantic
-- search across the full profile and inflated DB size (9MB of 12.5MB was
-- vector JSON). np.frombuffer() on a BLOB is a zero-copy view -- no parsing.
CREATE TABLE IF NOT EXISTS memory_embeddings (
    id      TEXT PRIMARY KEY REFERENCES memories(id) ON DELETE CASCADE,
    profile TEXT NOT NULL,
    vector  BLOB NOT NULL   -- packed float32[384] (BAAI/bge-small-en-v1.5), see db/store.py _pack_vector
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
