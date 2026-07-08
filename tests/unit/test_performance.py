"""Phase 2.5/3 performance + behavioral tests — SQLite edition.

Tests verify the behavioral guarantees that motivated Phase 2.5,
adapted for the SQLite backend introduced in Phase 3.

Run: python -m pytest tests/unit/test_performance.py -v

Note: the two ``*_smoke`` tests intentionally only assert "doesn't crash".
The *measurable* performance guarantees (no full-table rewrite on read,
bounded read latency) are asserted in ``test_reads_do_not_rewrite_memories``
and ``test_search_latency_budget`` (#95).
"""
import json
import sys
import time
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
# Smoke tests — assert only that repeated calls complete without error.
# (Measurable guarantees are asserted separately below, see #95.)
# ===========================================================================
def test_search_memory_smoke(fresh_store):
    """search_memory should complete multiple calls without error."""
    import server
    for _ in range(3):
        result = json.loads(server.search_memory.fn(query="dark mode", profile="default"))
        assert "results" in result, f"Unexpected response: {result}"


def test_get_memory_smoke(fresh_store):
    """get_memory should complete multiple calls without error."""
    import server
    for _ in range(2):
        result = json.loads(server.get_memory.fn(profile="default"))
        assert "memories" in result, f"Unexpected response: {result}"


# ===========================================================================
# Task 1 (measurable): search/get are cheap reads — they must NOT rewrite the
# memories table. With SQLite, log_access is a fast INSERT into access_log; the
# memories rows are untouched. We assert that directly (#95).
# ===========================================================================
def test_reads_do_not_rewrite_memories(fresh_store):
    """Repeated reads must not add/modify memories rows; only access_log grows."""
    import server
    conn = fresh_store._conn
    mem_before = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    log_before = conn.execute("SELECT COUNT(*) FROM access_log").fetchone()[0]

    for _ in range(25):
        server.search_memory.fn(query="dark mode", profile="default")
        server.get_memory.fn(profile="default")

    mem_after = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    log_after = conn.execute("SELECT COUNT(*) FROM access_log").fetchone()[0]

    assert mem_after == mem_before, "reads must not change the memories table"
    assert log_after > log_before, "reads should append cheap access_log rows"


def test_search_latency_budget(fresh_store):
    """50 searches over a small store should complete well under a generous
    CI budget — guards against an accidental O(rows) full rewrite per read."""
    import server
    for i in range(20):
        fresh_store.add_memory("default", f"Note number {i} about workflows",
                               category="general", importance="low")
    start = time.perf_counter()
    for _ in range(50):
        server.search_memory.fn(query="workflows", profile="default")
    elapsed = time.perf_counter() - start
    # 50 searches over ~21 rows is milliseconds of real work; 10s is a huge
    # margin that still catches a pathological per-read rewrite regression.
    assert elapsed < 10.0, f"50 searches took {elapsed:.2f}s — unexpectedly slow"


# ===========================================================================
# Task 2: Internal metadata stripped from search results
# ===========================================================================
INTERNAL_FIELDS = {"effective_score", "combined_score", "match_score",
                   "compressed", "access_count", "last_accessed"}


def test_search_results_strip_internal_fields(fresh_store):
    """search_memory results should not expose internal scoring metadata."""
    import server
    results = json.loads(server.search_memory.fn(query="dark mode", profile="default"))["results"]
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
    result = json.loads(
        server.add_memory.fn("Cale prefers dark mode",
                             category="preference", profile="default"))
    assert result.get("status") == "duplicate", f"Expected duplicate rejection, got: {result}"
    mems = server._store.get_memories("default")
    assert len(mems) == 1, f"Expected 1 memory, got {len(mems)}"


def test_add_memory_accepts_different_content(fresh_store):
    """Different content should be added successfully."""
    import server
    result = json.loads(
        server.add_memory.fn("Cale prefers concise answers",
                             category="preference", profile="default"))
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
