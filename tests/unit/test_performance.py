"""
Phase 2.5/3 performance + behavioral tests — SQLite edition.

Tests verify the behavioral guarantees that motivated Phase 2.5,
adapted for the SQLite backend introduced in Phase 3.

Run: python -m pytest tests/unit/test_performance.py -v
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import pytest
from db.store import MemoryStore


@pytest.fixture
def fresh_store(tmp_path, monkeypatch):
    """Fresh SQLite store for each test — replaces tmp MEMORY_FILE pattern."""
    import server
    s = MemoryStore(tmp_path / "test.db")
    monkeypatch.setattr(server, "_store", s)
    s.ensure_profile("default")
    s.add_memory("default", "Cale prefers dark mode",
                 category="preference", importance="medium")
    return s


# ===========================================================================
# Task 1: search/get don't trigger expensive disk rewrites
# (With SQLite WAL, log_access is a fast INSERT — no full-file rewrite)
# ===========================================================================

def test_search_memory_works_without_error(fresh_store):
    """search_memory should complete multiple calls without error."""
    import server
    for _ in range(3):
        raw = server.search_memory.fn(query="dark mode", profile="default")
        result = json.loads(raw)
        assert "results" in result, f"Unexpected response: {result}"


def test_get_memory_works_without_error(fresh_store):
    """get_memory should complete multiple calls without error."""
    import server
    for _ in range(2):
        raw = server.get_memory.fn(profile="default")
        result = json.loads(raw)
        assert "memories" in result, f"Unexpected response: {result}"


# ===========================================================================
# Task 2: Internal metadata stripped from search results
# ===========================================================================

INTERNAL_FIELDS = {"effective_score", "combined_score", "match_score",
                   "compressed", "access_count", "last_accessed"}


def test_search_results_strip_internal_fields(fresh_store):
    """search_memory results should not expose internal scoring metadata."""
    import server
    raw = server.search_memory.fn(query="dark mode", profile="default")
    results = json.loads(raw)["results"]
    assert len(results) > 0

    for mem in results:
        leaked = INTERNAL_FIELDS & set(mem.keys())
        assert not leaked, f"Internal fields leaked into search results: {leaked}"


# ===========================================================================
# Task 3: Content hash dedup — handled at DB level via UNIQUE index
# ===========================================================================

def test_add_memory_deduplicates_by_content_hash(fresh_store):
    """add_memory should reject exact duplicate content."""
    import server
    # First add (dark mode already added by fixture — exact duplicate)
    result = json.loads(
        server.add_memory.fn("Cale prefers dark mode",
                             category="preference", profile="default")
    )
    assert result.get("status") == "duplicate", \
        f"Expected duplicate rejection, got: {result}"

    # Only 1 memory in store (not 2)
    import server as s
    mems = s._store.get_memories("default")
    assert len(mems) == 1, f"Expected 1 memory, got {len(mems)}"


def test_add_memory_accepts_different_content(fresh_store):
    """Different content should be added successfully."""
    import server
    result = json.loads(
        server.add_memory.fn("Cale prefers concise answers",
                             category="preference", profile="default")
    )
    assert result.get("status") == "added", f"Expected added, got: {result}"
    mems = server._store.get_memories("default")
    assert len(mems) == 2


# ===========================================================================
# WAL mode — performance foundation for Phase 3
# ===========================================================================

def test_wal_mode_active(fresh_store):
    """SQLite store should run in WAL mode for concurrent access performance."""
    mode = fresh_store._conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode == "wal", f"Expected WAL mode, got: {mode}"
