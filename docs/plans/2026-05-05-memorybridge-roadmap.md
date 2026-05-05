# MemoryBridge Roadmap Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Evolve MemoryBridge from a JSON-backed MCP server into the definitive model-agnostic AI memory layer — with SQLite performance, semantic search, a review UI, and cross-model portability that no competitor matches.

**Architecture:** Four phases build on each other: quick wins (no breaking changes) → SQLite migration (new persistence layer, same tool API) → search quality (embeddings + hybrid BM25/vector) → Streamlit UI (visible portability, flagged queue review, analytics). Each phase ships independently and passes its own test suite before the next begins.

**Tech Stack:** Python 3.10+, FastMCP, SQLite (WAL + FTS5), FastEmbed (BAAI/bge-small-en-v1.5), sqlite-vec, Streamlit, pytest, python-dotenv, anthropic, openai

---

## Phase 2.5 — Performance Quick Wins

**Goal:** Eliminate unnecessary file I/O and token bloat with zero breaking changes to the tool API.

**Changes confined to:** `server.py` only  
**Estimated time:** 2-3 hours  
**Test file:** `tests/unit/test_performance.py`

---

### Task 1: Buffer access log writes

**Problem:** Every `search_memory` and `get_memory` call triggers `save_memory()` — a full JSON rewrite — just to append an access log entry and update boost scores. With 235 calls in history, that's 235 full rewrites.

**Fix:** Buffer access log entries the same way analytics are already buffered. Flush every `ACCESS_LOG_FLUSH_EVERY` ops and on shutdown.

**Files:**
- Modify: `server.py` (Config block, `log_access`, `save_memory`, `search_memory`, `get_memory`)
- Create: `tests/unit/test_performance.py`

**Step 1: Write the failing test**

```python
# tests/unit/test_performance.py
import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

def test_search_memory_does_not_save_on_every_call(tmp_path, monkeypatch):
    """search_memory should NOT call save_memory on every invocation."""
    import server
    monkeypatch.setattr(server, "MEMORY_FILE", tmp_path / "memory.json")
    monkeypatch.setattr(server, "LOCK_FILE", tmp_path / "memory.lock")

    # Seed a profile with one memory
    data = server.load_memory()
    data["profiles"]["default"] = {
        "identity": {}, "memories": [
            {"id": "mem_test1", "content": "Cale prefers dark mode",
             "category": "preference", "importance": "medium",
             "relevance_score": 1.0, "created_at": "2026-01-01",
             "last_accessed": "2026-01-01", "access_count": 0,
             "tags": [], "project_id": None, "token_count": 20}
        ], "projects": [], "model_preferences": {}
    }
    server.save_memory(data)

    save_call_count = []
    original_save = server.save_memory
    def counting_save(d):
        save_call_count.append(1)
        original_save(d)

    with patch.object(server, "save_memory", side_effect=counting_save):
        server.search_memory.fn(query="dark mode", profile="default")
        server.search_memory.fn(query="dark mode", profile="default")
        server.search_memory.fn(query="dark mode", profile="default")

    # Should NOT have saved on every call — access log is buffered
    assert len(save_call_count) == 0, \
        f"save_memory called {len(save_call_count)} times — should be 0 (buffered)"
```

**Step 2: Run test to verify it fails**

```bash
cd ~/Documents/GitHub/memorybridge
python -m pytest tests/unit/test_performance.py::test_search_memory_does_not_save_on_every_call -v
```
Expected: FAIL — `save_memory called 3 times — should be 0`

**Step 3: Add access log buffer to server.py**

In the Config block, after `ANALYTICS_FLUSH_EVERY`, add:
```python
ACCESS_LOG_FLUSH_EVERY = 10  # Write access log to disk every N events
```

After `_analytics_flush_count`, add:
```python
_access_log_buffer: list = []
_access_log_flush_count: int = 0
```

Add flush function after `_flush_analytics`:
```python
def _flush_access_log() -> None:
    """Write buffered access log entries and boost updates to disk."""
    global _access_log_buffer, _access_log_flush_count
    if not _access_log_buffer:
        return
    try:
        with _memory_lock():
            memory = load_memory()
            if "access_log" not in memory:
                memory["access_log"] = []
            for entry in _access_log_buffer:
                memory["access_log"].append(entry["log"])
                if entry.get("tokens_served", 0) > 0:
                    if "token_usage" not in memory:
                        memory["token_usage"] = {"total_served": 0, "by_profile": {}}
                    memory["token_usage"]["total_served"] += entry["tokens_served"]
                    profile_usage = memory["token_usage"].get("by_profile", {})
                    profile_usage[entry["profile"]] = \
                        profile_usage.get(entry["profile"], 0) + entry["tokens_served"]
                    memory["token_usage"]["by_profile"] = profile_usage
            if len(memory["access_log"]) > 1000:
                memory["access_log"] = memory["access_log"][-1000:]
            save_memory(memory)
        _access_log_buffer = []
        _access_log_flush_count = 0
    except Exception:
        pass

atexit.register(_flush_access_log)
```

Replace `log_access` with a buffered version:
```python
def log_access(memory: dict, action: str, profile: str,
               details: str = "", tokens_served: int = 0) -> None:
    """Buffer access log entry. Flushes every ACCESS_LOG_FLUSH_EVERY calls."""
    global _access_log_flush_count
    _access_log_buffer.append({
        "log": {
            "timestamp": datetime.now().isoformat(),
            "action": action,
            "profile": profile,
            "details": details,
            "tokens_served": tokens_served,
        },
        "tokens_served": tokens_served,
        "profile": profile,
    })
    _access_log_flush_count += 1
    if _access_log_flush_count >= ACCESS_LOG_FLUSH_EVERY:
        _flush_access_log()
```

Remove the `save_memory(memory)` call from inside `search_memory` and `get_memory` (the ones that follow `log_access`). Actual memory writes (add, update, delete, prune) keep their `save_memory` calls.

**Step 4: Run test to verify it passes**

```bash
python -m pytest tests/unit/test_performance.py::test_search_memory_does_not_save_on_every_call -v
```
Expected: PASS

**Step 5: Commit**

```bash
git add server.py tests/unit/test_performance.py
git commit -m "perf: buffer access log writes — eliminate save on every search/get"
```

---

### Task 2: Strip internal metadata from search results

**Problem:** `search_memory` returns `effective_score`, `combined_score`, `match_score`, `access_count`, `last_accessed`, `compressed` — fields Claude never uses. These inflate token count ~30% per result.

**Files:**
- Modify: `server.py` (search_memory return block)
- Modify: `tests/unit/test_performance.py`

**Step 1: Write the failing test**

```python
# Add to tests/unit/test_performance.py
INTERNAL_FIELDS = {"effective_score", "combined_score", "match_score",
                   "compressed", "access_count", "last_accessed"}

def test_search_results_strip_internal_fields(tmp_path, monkeypatch):
    """search_memory results should not expose internal scoring metadata."""
    import server
    monkeypatch.setattr(server, "MEMORY_FILE", tmp_path / "memory.json")
    monkeypatch.setattr(server, "LOCK_FILE", tmp_path / "memory.lock")

    data = server.load_memory()
    data["profiles"]["default"] = {
        "identity": {}, "memories": [
            {"id": "mem_test1", "content": "Cale prefers dark mode",
             "category": "preference", "importance": "medium",
             "relevance_score": 1.0, "created_at": "2026-01-01",
             "last_accessed": "2026-01-01", "access_count": 5,
             "tags": [], "project_id": None, "token_count": 20}
        ], "projects": [], "model_preferences": {}
    }
    server.save_memory(data)

    raw = server.search_memory.fn(query="dark mode", profile="default")
    results = json.loads(raw)["results"]
    assert len(results) > 0

    for mem in results:
        leaked = INTERNAL_FIELDS & set(mem.keys())
        assert not leaked, f"Internal fields leaked into search results: {leaked}"
```

**Step 2: Run to verify it fails**

```bash
python -m pytest tests/unit/test_performance.py::test_search_results_strip_internal_fields -v
```

**Step 3: Add a `_clean_result` helper in server.py**

Add after `boost_on_access`:
```python
_RESULT_FIELDS = {"id", "content", "category", "importance",
                  "project_id", "tags", "token_count", "created_at"}

def _clean_result(mem: dict) -> dict:
    """Return only fields Claude needs — strip internal scoring metadata."""
    return {k: v for k, v in mem.items() if k in _RESULT_FIELDS}
```

In `search_memory`, replace:
```python
return json.dumps({
    "query": query,
    "profile": profile,
    "results": results,
    ...
```
With:
```python
return json.dumps({
    "query": query,
    "profile": profile,
    "results": [_clean_result(m) for m in results],
    ...
```

**Step 4: Run to verify it passes**

```bash
python -m pytest tests/unit/test_performance.py -v
```
Expected: all tests PASS

**Step 5: Commit**

```bash
git add server.py tests/unit/test_performance.py
git commit -m "perf: strip internal metadata from search results (~30% token reduction)"
```

---

### Task 3: Compact JSON writes + content hash dedup

**Problem:** `save_memory` uses `json.dump(..., indent=2)` — pretty-printing adds ~25% file size. `add_memory` and `update_memory` don't check for duplicate content before writing.

**Files:**
- Modify: `server.py` (save_memory, add_memory, update_memory)
- Modify: `tests/unit/test_performance.py`

**Step 1: Write failing tests**

```python
def test_save_memory_writes_compact_json(tmp_path, monkeypatch):
    """save_memory should write compact JSON, not pretty-printed."""
    import server
    monkeypatch.setattr(server, "MEMORY_FILE", tmp_path / "memory.json")
    data = server.load_memory()
    server.save_memory(data)
    raw = (tmp_path / "memory.json").read_text()
    assert "\n  " not in raw, "save_memory is still pretty-printing JSON"

def test_add_memory_deduplicates_by_content_hash(tmp_path, monkeypatch):
    """add_memory should reject exact duplicate content."""
    import server
    monkeypatch.setattr(server, "MEMORY_FILE", tmp_path / "memory.json")
    monkeypatch.setattr(server, "LOCK_FILE", tmp_path / "memory.lock")
    data = server.load_memory()
    data["profiles"]["default"] = {
        "identity": {}, "memories": [], "projects": [], "model_preferences": {}
    }
    server.save_memory(data)

    server.add_memory.fn("Cale prefers dark mode", category="preference", profile="default")
    result = json.loads(
        server.add_memory.fn("Cale prefers dark mode", category="preference", profile="default")
    )
    assert result.get("status") == "duplicate", \
        f"Expected duplicate rejection, got: {result}"

    mem_data = server.load_memory()
    count = len(mem_data["profiles"]["default"]["memories"])
    assert count == 1, f"Expected 1 memory, got {count}"
```

**Step 2: Run to verify they fail**

```bash
python -m pytest tests/unit/test_performance.py::test_save_memory_writes_compact_json tests/unit/test_performance.py::test_add_memory_deduplicates_by_content_hash -v
```

**Step 3: Implement changes in server.py**

Change `save_memory`:
```python
def save_memory(data: dict) -> None:
    MEMORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    data["last_updated"] = datetime.now().strftime("%Y-%m-%d")
    tmp = MEMORY_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f)  # compact — remove indent=2
    tmp.replace(MEMORY_FILE)
```

Add import at top: `import hashlib`

Add helper:
```python
def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()
```

In `add_memory`, before appending the new memory, add:
```python
h = _content_hash(content)
existing_hashes = {_content_hash(m.get("content", "")) for m in profile_data["memories"]}
if h in existing_hashes:
    return json.dumps({"status": "duplicate", "reason": "identical content already exists"})
```

Apply the same check in the `for fact in facts:` loop inside `update_memory`:
```python
h = _content_hash(fact)
existing_hashes = {_content_hash(m.get("content", "")) for m in profile_data["memories"]}
if h in existing_hashes:
    continue  # skip duplicate silently, don't increment counters
```

**Step 4: Run full test suite**

```bash
python -m pytest tests/unit/test_performance.py -v
```
Expected: all PASS

**Step 5: Deploy and commit**

```bash
cp server.py ~/memorybridge/server.py
launchctl unload ~/Library/LaunchAgents/com.memorybridge.server.plist
launchctl load ~/Library/LaunchAgents/com.memorybridge.server.plist
git add server.py tests/unit/test_performance.py
git commit -m "perf: compact JSON writes + content hash dedup on add/update"
```

---

## Phase 3 — SQLite Migration

**Goal:** Replace `memory.json` with SQLite + WAL mode. Same tool API, zero behavior change for Claude. Enables concurrent access, FTS5 search, and the foundation for vector search in Phase 4.

**Estimated time:** 1 day  
**Key constraint:** `server.py` tool signatures must remain identical. Claude must not notice any change.

---

### Task 4: Schema design + migration script

**Files:**
- Create: `db/schema.sql`
- Create: `db/migrate.py` (one-time JSON → SQLite migration)
- Create: `tests/unit/test_db.py`

**Step 1: Write the schema**

```sql
-- db/schema.sql
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
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,
    action       TEXT NOT NULL,
    profile      TEXT NOT NULL,
    details      TEXT,
    tokens_served INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
INSERT OR IGNORE INTO meta VALUES ('schema_version', '3.0');
```

**Step 2: Write migration script**

```python
# db/migrate.py
"""One-time migration: memory.json → SQLite."""
import json
import sqlite3
import sys
from pathlib import Path

MEMORY_JSON = Path.home() / "memorybridge" / "memory.json"
MEMORY_DB   = Path.home() / "memorybridge" / "memory.db"
SCHEMA_SQL  = Path(__file__).parent / "schema.sql"

def migrate():
    if not MEMORY_JSON.exists():
        print("No memory.json found — nothing to migrate.")
        return

    print(f"Migrating {MEMORY_JSON} → {MEMORY_DB}")
    data = json.loads(MEMORY_JSON.read_text())

    conn = sqlite3.connect(MEMORY_DB)
    conn.executescript(SCHEMA_SQL.read_text())

    for profile_name, profile_data in data.get("profiles", {}).items():
        conn.execute(
            "INSERT OR REPLACE INTO profiles VALUES (?,?,?,?)",
            (profile_name,
             json.dumps(profile_data.get("identity", {})),
             json.dumps(profile_data.get("projects", [])),
             json.dumps(profile_data.get("model_preferences", {})))
        )
        for mem in profile_data.get("memories", []):
            import hashlib
            h = hashlib.sha256(mem.get("content", "").encode()).hexdigest()
            try:
                conn.execute(
                    """INSERT INTO memories
                       (id,profile,content,content_hash,category,importance,
                        relevance_score,created_at,last_accessed,access_count,
                        tags,project_id,token_count)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (mem["id"], profile_name, mem["content"], h,
                     mem["category"], mem["importance"],
                     mem.get("relevance_score", 1.0),
                     mem.get("created_at", "2026-01-01"),
                     mem.get("last_accessed", "2026-01-01"),
                     mem.get("access_count", 0),
                     json.dumps(mem.get("tags", [])),
                     mem.get("project_id"),
                     mem.get("token_count", 20))
                )
            except sqlite3.IntegrityError:
                pass  # duplicate hash — skip

        for mem in profile_data.get("archived", []):
            import hashlib
            h = hashlib.sha256(mem.get("content", "").encode()).hexdigest()
            try:
                conn.execute(
                    """INSERT INTO memories
                       (id,profile,content,content_hash,category,importance,
                        relevance_score,created_at,last_accessed,access_count,
                        tags,project_id,token_count,archived,archived_at,archive_reason)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)""",
                    (mem["id"], profile_name, mem["content"], h,
                     mem["category"], mem["importance"],
                     mem.get("relevance_score", 1.0),
                     mem.get("created_at", "2026-01-01"),
                     mem.get("last_accessed", "2026-01-01"),
                     mem.get("access_count", 0),
                     json.dumps(mem.get("tags", [])),
                     mem.get("project_id"),
                     mem.get("token_count", 20),
                     mem.get("archived_at"),
                     mem.get("archive_reason"))
                )
            except sqlite3.IntegrityError:
                pass

    # Rebuild FTS index
    conn.execute("INSERT INTO memories_fts(memories_fts) VALUES('rebuild')")
    conn.commit()
    conn.close()

    # Backup original
    backup = MEMORY_JSON.with_suffix(".json.pre-sqlite-backup")
    MEMORY_JSON.rename(backup)
    print(f"Migration complete. Original backed up to {backup}")
    print(f"Memories in DB: {sqlite3.connect(MEMORY_DB).execute('SELECT COUNT(*) FROM memories WHERE archived=0').fetchone()[0]}")

if __name__ == "__main__":
    migrate()
```

**Step 3: Write migration tests**

```python
# tests/unit/test_db.py
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

def make_memory_json(tmp_path):
    data = {
        "version": "1.3",
        "profiles": {
            "default": {
                "identity": {"name": "Cale"},
                "memories": [
                    {"id": "mem_001", "content": "Prefers dark mode",
                     "category": "preference", "importance": "medium",
                     "relevance_score": 1.0, "created_at": "2026-01-01",
                     "last_accessed": "2026-01-01", "access_count": 0,
                     "tags": [], "project_id": None, "token_count": 20},
                    {"id": "mem_002", "content": "PMP certified",
                     "category": "skill", "importance": "high",
                     "relevance_score": 0.9, "created_at": "2026-01-01",
                     "last_accessed": "2026-01-01", "access_count": 2,
                     "tags": [], "project_id": None, "token_count": 15}
                ],
                "archived": [],
                "projects": [],
                "model_preferences": {}
            }
        }
    }
    p = tmp_path / "memory.json"
    p.write_text(json.dumps(data))
    return p

def test_migration_preserves_memory_count(tmp_path, monkeypatch):
    import db.migrate as migrate_mod
    monkeypatch.setattr(migrate_mod, "MEMORY_JSON", make_memory_json(tmp_path))
    db_path = tmp_path / "memory.db"
    monkeypatch.setattr(migrate_mod, "MEMORY_DB", db_path)

    migrate_mod.migrate()

    conn = sqlite3.connect(db_path)
    count = conn.execute("SELECT COUNT(*) FROM memories WHERE archived=0").fetchone()[0]
    assert count == 2, f"Expected 2 memories, got {count}"
    conn.close()

def test_migration_deduplicates_identical_content(tmp_path, monkeypatch):
    import db.migrate as migrate_mod
    data = json.loads(make_memory_json(tmp_path).read_text())
    # Add duplicate
    data["profiles"]["default"]["memories"].append(
        {**data["profiles"]["default"]["memories"][0], "id": "mem_003"}
    )
    p = tmp_path / "memory.json"
    p.write_text(json.dumps(data))
    monkeypatch.setattr(migrate_mod, "MEMORY_JSON", p)
    db_path = tmp_path / "memory.db"
    monkeypatch.setattr(migrate_mod, "MEMORY_DB", db_path)

    migrate_mod.migrate()

    conn = sqlite3.connect(db_path)
    count = conn.execute("SELECT COUNT(*) FROM memories WHERE archived=0").fetchone()[0]
    assert count == 2, f"Duplicate not deduplicated — got {count} memories"
    conn.close()

def test_fts_index_is_populated(tmp_path, monkeypatch):
    import db.migrate as migrate_mod
    monkeypatch.setattr(migrate_mod, "MEMORY_JSON", make_memory_json(tmp_path))
    db_path = tmp_path / "memory.db"
    monkeypatch.setattr(migrate_mod, "MEMORY_DB", db_path)
    migrate_mod.migrate()

    conn = sqlite3.connect(db_path)
    results = conn.execute(
        "SELECT rowid FROM memories_fts WHERE memories_fts MATCH 'dark'"
    ).fetchall()
    assert len(results) > 0, "FTS index not populated — 'dark' not findable"
    conn.close()
```

**Step 4: Run migration tests**

```bash
python -m pytest tests/unit/test_db.py -v
```
Expected: all PASS

**Step 5: Run actual migration**

```bash
python db/migrate.py
```
Expected output:
```
Migrating .../memory.json → .../memory.db
Migration complete. Original backed up to memory.json.pre-sqlite-backup
Memories in DB: 127
```

**Step 6: Commit**

```bash
git add db/ tests/unit/test_db.py
git commit -m "feat: SQLite schema + migration script from memory.json"
```

---

### Task 5: Rewrite server.py persistence layer

**Files:**
- Create: `db/store.py` (all SQLite read/write logic — isolated from MCP layer)
- Modify: `server.py` (swap `load_memory`/`save_memory` for `db.store` calls)
- Create: `tests/unit/test_store.py`

**Key principle:** `db/store.py` owns all SQL. `server.py` contains zero SQL. Tool signatures don't change.

**Step 1: Write store tests first (TDD)**

```python
# tests/unit/test_store.py
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import pytest
import db.store as store

@pytest.fixture
def db(tmp_path):
    """Fresh in-memory store for each test."""
    s = store.MemoryStore(tmp_path / "test.db")
    s.ensure_profile("default")
    return s

def test_add_and_retrieve(db):
    mid = db.add_memory("default", "Cale prefers dark mode",
                        category="preference", importance="medium")
    assert mid.startswith("mem_")
    mems = db.get_memories("default")
    assert any(m["content"] == "Cale prefers dark mode" for m in mems)

def test_duplicate_content_rejected(db):
    db.add_memory("default", "Cale prefers dark mode",
                  category="preference", importance="medium")
    result = db.add_memory("default", "Cale prefers dark mode",
                           category="preference", importance="medium")
    assert result is None, "Duplicate should return None"
    assert len(db.get_memories("default")) == 1

def test_search_returns_ranked_results(db):
    db.add_memory("default", "Cale prefers dark mode", category="preference", importance="medium")
    db.add_memory("default", "Cale is PMP certified", category="skill", importance="high")
    results = db.search("default", "dark mode")
    assert len(results) > 0
    assert results[0]["content"] == "Cale prefers dark mode"

def test_delete_memory(db):
    mid = db.add_memory("default", "Temporary fact",
                        category="fact", importance="low")
    db.delete_memory("default", mid)
    mems = db.get_memories("default")
    assert not any(m["id"] == mid for m in mems)

def test_wal_mode_enabled(db):
    mode = db._conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode == "wal", f"WAL mode not enabled — got {mode}"

def test_batch_add_memories(db):
    facts = ["Fact one", "Fact two", "Fact three"]
    added = db.add_memories("default", facts, category="fact", importance="medium")
    assert added == 3
    assert len(db.get_memories("default")) == 3

def test_fts_search_finds_partial_match(db):
    db.add_memory("default", "Enterprise turnaround experience at Dell",
                  category="fact", importance="high")
    results = db.search("default", "turnaround")
    assert len(results) > 0

def test_archive_low_score_memories(db):
    db.add_memory("default", "Old low priority fact",
                  category="fact", importance="low")
    db._conn.execute(
        "UPDATE memories SET relevance_score=0.05 WHERE profile='default'"
    )
    db._conn.commit()
    archived = db.auto_prune("default", threshold=0.15)
    assert len(archived) > 0
    active = db.get_memories("default")
    assert len(active) == 0
```

**Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/unit/test_store.py -v
```
Expected: FAIL — `ModuleNotFoundError: No module named 'db.store'`

**Step 3: Implement `db/store.py`**

```python
# db/store.py
"""SQLite persistence layer for MemoryBridge. Zero MCP coupling."""

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path

SCHEMA_SQL = Path(__file__).parent / "schema.sql"

VALID_CATEGORIES = [
    "preference", "fact", "insight", "decision",
    "project_status", "relationship", "skill", "constraint"
]
IMPORTANCE_LEVELS = ["low", "medium", "high", "critical"]


def _mem_id() -> str:
    return f"mem_{uuid.uuid4().hex[:8]}"

def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()

def _count_tokens(text: str) -> int:
    try:
        import tiktoken
        enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text)) + 20
    except Exception:
        return len(text.split()) + 20


class MemoryStore:
    def __init__(self, db_path: Path):
        self._path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA_SQL.read_text())
        self._conn.commit()

    def ensure_profile(self, name: str) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO profiles(name) VALUES(?)", (name,)
        )
        self._conn.commit()

    def get_profile(self, name: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM profiles WHERE name=?", (name,)
        ).fetchone()
        if not row:
            return None
        return {
            "name": row["name"],
            "identity": json.loads(row["identity"]),
            "projects": json.loads(row["projects"]),
            "model_preferences": json.loads(row["model_prefs"]),
        }

    def add_memory(self, profile: str, content: str, *,
                   category: str = "fact", importance: str = "medium",
                   tags: list = None, project_id: str = None) -> str | None:
        """Returns memory ID on success, None if duplicate."""
        self.ensure_profile(profile)
        h = _content_hash(content)
        mid = _mem_id()
        now = datetime.now().strftime("%Y-%m-%d")
        tc = _count_tokens(content)
        try:
            self._conn.execute(
                """INSERT INTO memories
                   (id,profile,content,content_hash,category,importance,
                    created_at,last_accessed,tags,project_id,token_count)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (mid, profile, content, h, category, importance,
                 now, now, json.dumps(tags or []), project_id, tc)
            )
            self._conn.execute(
                "INSERT INTO memories_fts(rowid,content) "
                "SELECT rowid,content FROM memories WHERE id=?", (mid,)
            )
            self._conn.commit()
            return mid
        except sqlite3.IntegrityError:
            return None  # duplicate

    def add_memories(self, profile: str, facts: list[str], *,
                     category: str = "fact", importance: str = "medium",
                     project_id: str = None) -> int:
        """Batch insert. Returns count of actually inserted rows."""
        added = 0
        for fact in facts:
            if self.add_memory(profile, fact, category=category,
                               importance=importance, project_id=project_id):
                added += 1
        return added

    def get_memories(self, profile: str, category: str = None,
                     max_tokens: int = None) -> list[dict]:
        sql = "SELECT * FROM memories WHERE profile=? AND archived=0"
        params = [profile]
        if category:
            sql += " AND category=?"
            params.append(category)
        sql += " ORDER BY relevance_score DESC"
        rows = self._conn.execute(sql, params).fetchall()
        mems = [dict(r) for r in rows]
        for m in mems:
            m["tags"] = json.loads(m.get("tags", "[]"))
        if max_tokens:
            budget, result = 0, []
            for m in mems:
                if budget + m["token_count"] <= max_tokens:
                    result.append(m)
                    budget += m["token_count"]
            return result
        return mems

    def search(self, profile: str, query: str, category: str = None,
               limit: int = 5, max_tokens: int = 800) -> list[dict]:
        """FTS5 BM25 search with token budget."""
        # Sanitize query for FTS5
        safe_query = " ".join(
            f'"{t}"' for t in query.split() if t.isalnum() or len(t) > 2
        ) or query

        sql = """
            SELECT m.*, bm25(memories_fts) AS bm25_score
            FROM memories_fts
            JOIN memories m ON memories_fts.rowid = m.rowid
            WHERE memories_fts MATCH ?
              AND m.profile = ?
              AND m.archived = 0
        """
        params = [safe_query, profile]
        if category:
            sql += " AND m.category = ?"
            params.append(category)
        sql += " ORDER BY bm25_score LIMIT ?"
        params.append(limit * 3)  # over-fetch, then trim by token budget

        rows = self._conn.execute(sql, params).fetchall()
        results, tokens_used = [], 0
        for row in rows:
            m = dict(row)
            m["tags"] = json.loads(m.get("tags", "[]"))
            m.pop("bm25_score", None)
            if tokens_used + m["token_count"] <= max_tokens:
                results.append(m)
                tokens_used += m["token_count"]
            if len(results) >= limit:
                break
        return results

    def delete_memory(self, profile: str, memory_id: str) -> int:
        """Returns token_count freed, or 0 if not found."""
        row = self._conn.execute(
            "SELECT token_count FROM memories WHERE id=? AND profile=?",
            (memory_id, profile)
        ).fetchone()
        if not row:
            return 0
        self._conn.execute("DELETE FROM memories WHERE id=?", (memory_id,))
        self._conn.execute(
            "INSERT INTO memories_fts(memories_fts,rowid,content) "
            "VALUES('delete',?,'')", (memory_id,)
        )
        self._conn.commit()
        return row["token_count"]

    def auto_prune(self, profile: str, threshold: float = 0.15) -> list[str]:
        now = datetime.now()
        rows = self._conn.execute(
            "SELECT id, relevance_score, importance, access_count, created_at "
            "FROM memories WHERE profile=? AND archived=0", (profile,)
        ).fetchall()

        to_archive = []
        for row in rows:
            try:
                created = datetime.fromisoformat(row["created_at"])
            except Exception:
                created = now
            days = (now - created).days
            decay = 0.5 ** (days / 30)
            boost = {"low": 0.8, "medium": 1.0, "high": 1.2, "critical": 1.5}.get(
                row["importance"], 1.0
            )
            access_boost = 1 + min(row["access_count"] * 0.05, 0.5)
            effective = max(row["relevance_score"] * decay * boost * access_boost, 0.1)
            if effective < threshold:
                to_archive.append(row["id"])

        if to_archive:
            now_str = now.strftime("%Y-%m-%d")
            self._conn.executemany(
                "UPDATE memories SET archived=1, archived_at=?, "
                "archive_reason='auto_prune_low_score' WHERE id=?",
                [(now_str, mid) for mid in to_archive]
            )
            self._conn.commit()
        return to_archive

    def log_access(self, action: str, profile: str,
                   details: str = "", tokens_served: int = 0) -> None:
        self._conn.execute(
            "INSERT INTO access_log(ts,action,profile,details,tokens_served) VALUES(?,?,?,?,?)",
            (datetime.now().isoformat(), action, profile, details, tokens_served)
        )
        self._conn.commit()

    def token_stats(self, profile: str) -> dict:
        row = self._conn.execute(
            """SELECT COUNT(*) as count, SUM(token_count) as total,
                      AVG(token_count) as avg
               FROM memories WHERE profile=? AND archived=0""", (profile,)
        ).fetchone()
        return {
            "memory_count": row["count"] or 0,
            "total_tokens": row["total"] or 0,
            "avg_tokens_per_memory": int(row["avg"] or 0),
        }
```

**Step 4: Run store tests**

```bash
python -m pytest tests/unit/test_store.py -v
```
Expected: all PASS

**Step 5: Update server.py to use MemoryStore**

Replace the `load_memory`/`save_memory` pattern with `MemoryStore` calls. Tool signatures stay identical. See `docs/plans/sqlite-server-refactor-notes.md` for the full diff — this is the longest step and should be done method-by-method, running `pytest tests/` after each tool is migrated.

**Step 6: Integration test — all MCP tools**

```python
# tests/integration/test_mcp_tools.py
"""Verify all MCP tools behave identically after SQLite migration."""
import json, sys, pytest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
import server

@pytest.fixture(autouse=True)
def fresh_store(tmp_path, monkeypatch):
    from db.store import MemoryStore
    s = MemoryStore(tmp_path / "test.db")
    monkeypatch.setattr(server, "_store", s)
    s.ensure_profile("default")
    yield s

def test_add_then_get(fresh_store):
    server.add_memory.fn("Cale prefers dark mode", category="preference", profile="default")
    result = json.loads(server.get_memory.fn(profile="default"))
    contents = [m["content"] for m in result["memories"]]
    assert "Cale prefers dark mode" in contents

def test_update_then_search(fresh_store):
    server.update_memory.fn(
        facts=["Fact A", "Fact B", "Fact C"],
        category="fact", profile="default"
    )
    result = json.loads(server.search_memory.fn(query="Fact A", profile="default"))
    assert result["total_matches"] >= 1

def test_delete_memory(fresh_store):
    r = json.loads(server.add_memory.fn("Delete me", category="fact", profile="default"))
    mid = r["memory_id"]
    server.delete_memory.fn(memory_id=mid, profile="default")
    result = json.loads(server.get_memory.fn(profile="default"))
    assert not any(m["id"] == mid for m in result["memories"])

def test_profile_isolation(fresh_store):
    fresh_store.ensure_profile("work")
    server.add_memory.fn("Personal fact", category="fact", profile="default")
    server.add_memory.fn("Work fact", category="fact", profile="work")
    default_mems = json.loads(server.get_memory.fn(profile="default"))["memories"]
    work_mems = json.loads(server.get_memory.fn(profile="work"))["memories"]
    assert not any("Work" in m["content"] for m in default_mems)
    assert not any("Personal" in m["content"] for m in work_mems)
```

**Step 7: Run full test suite, deploy, commit**

```bash
python -m pytest tests/ -v
cp server.py ~/memorybridge/server.py
launchctl unload ~/Library/LaunchAgents/com.memorybridge.server.plist
launchctl load ~/Library/LaunchAgents/com.memorybridge.server.plist
git add db/ server.py tests/
git commit -m "feat: SQLite + WAL migration — same tool API, zero behavior change"
```

---

## Phase 4 — Search Quality

**Goal:** Replace keyword overlap with FTS5 BM25 (already in Phase 3) then layer in local vector embeddings for semantic search. No cloud API required.

**Estimated time:** 4-6 hours  
**New dependency:** `fastembed` (ONNX-backed, no PyTorch required)

---

### Task 6: FastEmbed integration + hybrid search

**Files:**
- Modify: `db/store.py` (add `embed`, `search_semantic`, `search_hybrid`)
- Create: `tests/unit/test_search_quality.py`

**Step 1: Install dependency**

```bash
pip install fastembed
echo "fastembed>=0.2.0" >> requirements.txt
```

**Step 2: Write quality tests first**

```python
# tests/unit/test_search_quality.py
"""Search quality assertions — semantic search must outperform keyword search."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
import pytest
from db.store import MemoryStore

@pytest.fixture
def db(tmp_path):
    s = MemoryStore(tmp_path / "test.db")
    s.ensure_profile("default")
    # Seed memories with synonym relationships
    s.add_memory("default", "Cale served eight years in the United States Air Force",
                 category="fact", importance="high")
    s.add_memory("default", "Prefers concise bullet-point answers",
                 category="preference", importance="medium")
    s.add_memory("default", "Has $126M in documented program impact",
                 category="fact", importance="high")
    s.build_embeddings("default")
    return s

def test_semantic_finds_synonym(db):
    """'military service' should find 'Air Force' even without keyword match."""
    results = db.search_semantic("default", "military service")
    contents = [r["content"] for r in results]
    assert any("Air Force" in c for c in contents), \
        f"Semantic search missed 'Air Force' for query 'military service'. Got: {contents}"

def test_hybrid_outperforms_keyword_on_paraphrase(db):
    """Hybrid search should find 'financial impact' → '$126M program impact'."""
    keyword_results = db.search("default", "financial impact")
    hybrid_results = db.search_hybrid("default", "financial impact")
    hybrid_contents = [r["content"] for r in hybrid_results]
    assert any("126M" in c for c in hybrid_contents), \
        "Hybrid search missed '$126M' for 'financial impact'"

def test_embeddings_cached_not_recomputed(db, mocker):
    """Embedding model should load once and be reused."""
    spy = mocker.spy(db, "_embed_texts")
    db.search_semantic("default", "military")
    db.search_semantic("default", "military")
    # Model loaded once, not per-query
    assert spy.call_count <= 2
```

**Step 3: Add embedding support to `db/store.py`**

Add to `MemoryStore.__init__`:
```python
self._embed_model = None
self._embed_lock = None
```

Add methods:
```python
def _get_embed_model(self):
    """Lazy-load FastEmbed model once per process."""
    if self._embed_model is None:
        import threading
        if self._embed_lock is None:
            self._embed_lock = threading.Lock()
        with self._embed_lock:
            if self._embed_model is None:
                from fastembed import TextEmbedding
                self._embed_model = TextEmbedding("BAAI/bge-small-en-v1.5")
    return self._embed_model

def _embed_texts(self, texts: list[str]) -> list[list[float]]:
    model = self._get_embed_model()
    return list(model.embed(texts))

def build_embeddings(self, profile: str) -> None:
    """Compute and store embeddings for all memories in a profile."""
    # Requires sqlite-vec extension
    try:
        import sqlite_vec
        self._conn.enable_load_extension(True)
        sqlite_vec.load(self._conn)
    except Exception:
        return  # graceful fallback — semantic search disabled

    rows = self._conn.execute(
        "SELECT id, content FROM memories WHERE profile=? AND archived=0",
        (profile,)
    ).fetchall()
    if not rows:
        return

    ids = [r["id"] for r in rows]
    texts = [r["content"] for r in rows]
    embeddings = self._embed_texts(texts)

    self._conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS memory_vecs USING vec0(
            id TEXT PRIMARY KEY,
            embedding FLOAT[384]
        )
    """)
    for mid, emb in zip(ids, embeddings):
        self._conn.execute(
            "INSERT OR REPLACE INTO memory_vecs(id, embedding) VALUES(?,?)",
            (mid, sqlite_vec.serialize_float32(emb))
        )
    self._conn.commit()

def search_semantic(self, profile: str, query: str,
                    limit: int = 5, max_tokens: int = 800) -> list[dict]:
    """Vector similarity search. Falls back to FTS5 if embeddings unavailable."""
    try:
        import sqlite_vec
        self._conn.enable_load_extension(True)
        sqlite_vec.load(self._conn)
    except Exception:
        return self.search(profile, query, limit=limit, max_tokens=max_tokens)

    q_emb = self._embed_texts([query])[0]
    rows = self._conn.execute(
        """SELECT m.*, v.distance
           FROM memory_vecs v
           JOIN memories m ON v.id = m.id
           WHERE v.embedding MATCH ?
             AND m.profile = ?
             AND m.archived = 0
             AND k = ?
           ORDER BY v.distance""",
        (sqlite_vec.serialize_float32(q_emb), profile, limit * 2)
    ).fetchall()

    results, tokens_used = [], 0
    for row in rows:
        m = dict(row)
        m["tags"] = json.loads(m.get("tags", "[]"))
        m.pop("distance", None)
        if tokens_used + m["token_count"] <= max_tokens:
            results.append(m)
            tokens_used += m["token_count"]
        if len(results) >= limit:
            break
    return results

def search_hybrid(self, profile: str, query: str,
                  limit: int = 5, max_tokens: int = 800) -> list[dict]:
    """Reciprocal Rank Fusion of FTS5 + semantic results."""
    keyword_results = self.search(profile, query, limit=limit * 2)
    semantic_results = self.search_semantic(profile, query, limit=limit * 2)

    # RRF: score = sum(1 / (60 + rank)) across both result lists
    scores: dict[str, float] = {}
    all_mems: dict[str, dict] = {}

    for rank, mem in enumerate(keyword_results):
        mid = mem["id"]
        scores[mid] = scores.get(mid, 0) + 1 / (60 + rank)
        all_mems[mid] = mem

    for rank, mem in enumerate(semantic_results):
        mid = mem["id"]
        scores[mid] = scores.get(mid, 0) + 1 / (60 + rank)
        all_mems[mid] = mem

    ranked = sorted(scores.keys(), key=lambda k: scores[k], reverse=True)

    results, tokens_used = [], 0
    for mid in ranked:
        mem = all_mems[mid]
        if tokens_used + mem["token_count"] <= max_tokens:
            results.append(mem)
            tokens_used += mem["token_count"]
        if len(results) >= limit:
            break
    return results
```

**Step 4: Update requirements.txt**

```
fastembed>=0.2.0
sqlite-vec>=0.1.0
```

**Step 5: Run quality tests**

```bash
python -m pytest tests/unit/test_search_quality.py -v
```

**Step 6: Wire hybrid search into search_memory MCP tool**

In `server.py`, `search_memory` calls `_store.search_hybrid()` instead of `_store.search()`. Falls back to FTS5 if embeddings not built yet.

**Step 7: Commit**

```bash
git add db/store.py requirements.txt tests/unit/test_search_quality.py server.py
git commit -m "feat: hybrid BM25 + semantic search via FastEmbed + sqlite-vec"
```

---

## Phase 5 — Streamlit Review UI

**Goal:** Make memory visible and actionable. Surface the flagged queue, memory browser, analytics, and cross-model portability in a local Streamlit app.

**Estimated time:** 1 day  
**New dependency:** `streamlit>=1.30.0`

---

### Task 7: Core UI scaffold + flagged queue review

**Files:**
- Create: `ui/app.py` (main Streamlit entry point)
- Create: `ui/pages/flagged_queue.py`
- Create: `ui/pages/memory_browser.py`
- Create: `ui/pages/analytics.py`
- Create: `ui/pages/portability.py`
- Create: `tests/ui/test_flagged_queue.py`

**Run with:**

```bash
streamlit run ui/app.py
```

**Flagged queue page spec:**
- Load `~/memorybridge/flagged_queue.json`
- Show each pending fact: text, confidence bar, category, source conversation
- Two buttons per fact: ✓ Accept (calls `add_memory`) | ✗ Reject (marks status=rejected)
- Batch approve button for all pending
- Shows count: "7 pending / 2 accepted / 1 rejected"

**Memory browser page spec:**
- Filter by profile, category, importance, date range
- Sort by relevance_score, created_at, access_count
- Inline delete button (calls `delete_memory`)
- Token usage bar per profile (used / max)

**Analytics page spec:**
- Line chart: tokens served per day (from `analytics.json`)
- By-operation breakdown: get_memory vs search_memory
- Baseline annotation: "Pre-v1.4 average: 1,740 tokens/search | Current: ~800"

**Portability page spec:**
- Import section: drop zone for conversations.json (Claude/ChatGPT/Gemini)
- Calls `ingestion/run.py --preview` first, shows extraction preview
- Confirm button to run live ingestion
- Export section: format selector (ChatGPT / Gemini / Ollama), download button

**Step 1: Flagged queue test**

```python
# tests/ui/test_flagged_queue.py
import json
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

def test_accept_moves_fact_to_memory(tmp_path):
    """Accepting a flagged fact should call add_memory and mark status=accepted."""
    queue = {
        "generated": "2026-05-05T00:00:00",
        "items": [
            {"id": "item-1", "fact": "Cale volunteers for Hopeful Fridays",
             "confidence": 0.72, "category": "fact", "importance": "medium",
             "project": None, "source": "claude", "status": "pending"}
        ]
    }
    queue_path = tmp_path / "flagged_queue.json"
    queue_path.write_text(json.dumps(queue))

    from ui.pages.flagged_queue import accept_item

    mock_add = MagicMock(return_value='{"status":"added","memory_id":"mem_abc"}')
    with patch("ui.pages.flagged_queue.add_memory", mock_add):
        accept_item("item-1", queue_path)

    mock_add.assert_called_once()
    updated = json.loads(queue_path.read_text())
    item = next(i for i in updated["items"] if i["id"] == "item-1")
    assert item["status"] == "accepted"

def test_reject_marks_status_only(tmp_path):
    """Rejecting a flagged fact should NOT call add_memory."""
    queue = {
        "generated": "2026-05-05T00:00:00",
        "items": [
            {"id": "item-2", "fact": "Low confidence guess",
             "confidence": 0.61, "category": "insight", "importance": "low",
             "project": None, "source": "claude", "status": "pending"}
        ]
    }
    queue_path = tmp_path / "flagged_queue.json"
    queue_path.write_text(json.dumps(queue))

    from ui.pages.flagged_queue import reject_item
    mock_add = MagicMock()
    with patch("ui.pages.flagged_queue.add_memory", mock_add):
        reject_item("item-2", queue_path)

    mock_add.assert_not_called()
    updated = json.loads(queue_path.read_text())
    item = next(i for i in updated["items"] if i["id"] == "item-2")
    assert item["status"] == "rejected"
```

**Step 2: Run to verify failure, implement, run to verify pass**

```bash
python -m pytest tests/ui/test_flagged_queue.py -v  # should FAIL
# implement ui/pages/flagged_queue.py
python -m pytest tests/ui/test_flagged_queue.py -v  # should PASS
```

**Step 3: Commit**

```bash
git add ui/ tests/ui/
git commit -m "feat: Streamlit review UI — flagged queue, memory browser, analytics, portability"
```

---

## Phase 6 — Cross-Model Portability (The Differentiator)

**Goal:** Make MemoryBridge's unique value proposition visible and frictionless — import from any AI, memory travels with you.

**Estimated time:** 4-6 hours

---

### Task 8: Memory passport format

A portable, model-agnostic export format any AI can consume as a system prompt chunk.

**Files:**
- Create: `ingestion/passport.py`
- Modify: `server.py` (add `export_passport` MCP tool)
- Create: `tests/unit/test_passport.py`

**Passport format:**
```
# Memory Passport
Generated: 2026-05-05 | Profile: default | Facts: 127

## Identity
Name: Cale Corbett | Role: Program Leader & Consultant

## Preferences (8)
- Prefers dark mode in all apps
- No em dashes in writing
...

## Skills (12)
- PMP certified
- $126M documented program impact
...

## Constraints (4)
- Cannot scrape LinkedIn via automated tools
...
```

**Test:**
```python
def test_passport_fits_token_budget(tmp_path, monkeypatch):
    """export_passport should never exceed max_tokens."""
    # seed 50 memories, request 1000 token passport
    ...
    passport = server.export_passport.fn(profile="default", max_tokens=1000)
    from server import count_tokens
    assert count_tokens(passport) <= 1100  # 10% tolerance

def test_passport_is_parseable_as_text(tmp_path, monkeypatch):
    """Passport must be plain text — no JSON, no markdown fences."""
    passport = server.export_passport.fn(profile="default", max_tokens=2000)
    assert not passport.startswith("{")
    assert "```" not in passport
    assert "# Memory Passport" in passport
```

---

## Testing Strategy Summary

| Phase | Test type | Command | Gate |
|---|---|---|---|
| 2.5 | Unit | `pytest tests/unit/test_performance.py -v` | All pass before deploy |
| 3 | Unit + Integration | `pytest tests/unit/test_db.py tests/unit/test_store.py tests/integration/ -v` | All pass before migration |
| 4 | Quality | `pytest tests/unit/test_search_quality.py -v` | Semantic tests pass |
| 5 | UI unit | `pytest tests/ui/ -v` | Flagged queue tests pass |
| 6 | End-to-end | `pytest tests/e2e/ -v` | Passport format validated |

**Full suite before every deploy:**
```bash
python -m pytest tests/ -v --tb=short
```

**Never deploy if any test is red.**

---

## File Map (final state)

```
memorybridge/
├── server.py               # MCP server — tool API only, no SQL
├── requirements.txt
├── .gitignore
├── db/
│   ├── schema.sql          # SQLite schema with WAL + FTS5
│   ├── store.py            # All persistence logic
│   └── migrate.py          # One-time JSON → SQLite migration
├── ingestion/
│   ├── run.py              # CLI orchestrator
│   ├── parse_claude.py
│   ├── parse_chatgpt.py
│   ├── parse_gemini.py
│   ├── extractor.py        # DeepSeek R1
│   ├── router.py
│   ├── resolver.py
│   ├── merger.py
│   └── passport.py         # Memory passport export
├── ui/
│   ├── app.py              # Streamlit entry point
│   └── pages/
│       ├── flagged_queue.py
│       ├── memory_browser.py
│       ├── analytics.py
│       └── portability.py
├── tests/
│   ├── unit/
│   │   ├── test_performance.py
│   │   ├── test_db.py
│   │   ├── test_store.py
│   │   ├── test_search_quality.py
│   │   └── test_passport.py
│   ├── integration/
│   │   └── test_mcp_tools.py
│   └── ui/
│       └── test_flagged_queue.py
└── docs/
    └── plans/
        └── 2026-05-05-memorybridge-roadmap.md
```
