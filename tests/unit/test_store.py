"""
Phase 3 store tests — TDD for db/store.py (MemoryStore).
Run: python -m pytest tests/unit/test_store.py -v
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import pytest
import db.store as store


@pytest.fixture
def db(tmp_path):
    """Fresh store for each test."""
    s = store.MemoryStore(tmp_path / "test.db")
    s.ensure_profile("default")
    return s


def test_add_and_retrieve(db):
    mid = db.add_memory("default", "Cale prefers dark mode",
                        category="preference", importance="medium")
    assert mid is not None
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
    db.add_memory("default", "Cale prefers dark mode",
                  category="preference", importance="medium")
    db.add_memory("default", "Cale is PMP certified",
                  category="skill", importance="high")
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


def test_search_respects_token_budget(db):
    for i in range(10):
        db.add_memory("default", f"dark mode memory number {i} with some extra words to inflate tokens",
                      category="fact", importance="medium")
    results = db.search("default", "dark mode", max_tokens=50)
    total = sum(m["token_count"] for m in results)
    assert total <= 50, f"Token budget exceeded: {total} > 50"


def test_ensure_profile_idempotent(db):
    """ensure_profile should not raise on second call."""
    db.ensure_profile("default")
    db.ensure_profile("default")
    db.ensure_profile("work")
    profile = db.get_profile("work")
    assert profile is not None


def test_token_stats(db):
    db.add_memory("default", "Some content here", category="fact", importance="medium")
    stats = db.token_stats("default")
    assert stats["memory_count"] == 1
    assert stats["total_tokens"] > 0


def test_log_access_writes_to_db(db):
    db.log_access("search_memory", "default", details="query='test'", tokens_served=100)
    count = db._conn.execute("SELECT COUNT(*) FROM access_log").fetchone()[0]
    assert count == 1


def test_category_filter(db):
    db.add_memory("default", "A preference", category="preference", importance="medium")
    db.add_memory("default", "A skill", category="skill", importance="medium")
    prefs = db.get_memories("default", category="preference")
    assert len(prefs) == 1
    assert prefs[0]["content"] == "A preference"


def test_search_populates_match_score(db):
    db.add_memory("default", "Cale prefers dark mode in his IDE", category="preference")
    
    # 1. Test keyword search match_score (Jaccard fallback)
    results_keyword = db.search("default", "dark mode IDE")
    assert len(results_keyword) > 0
    assert "match_score" in results_keyword[0]
    assert results_keyword[0]["match_score"] > 0.0

    # 2. Test semantic/hybrid search match_score
    db.build_embeddings("default")
    results_semantic = db.search_semantic("default", "dark mode IDE")
    assert len(results_semantic) > 0
    assert "match_score" in results_semantic[0]
    assert results_semantic[0]["match_score"] > 0.0

    results_hybrid = db.search_hybrid("default", "dark mode IDE")
    assert len(results_hybrid) > 0
    assert "match_score" in results_hybrid[0]
    assert results_hybrid[0]["match_score"] > 0.0


def test_edit_memory_integrity_error_dedup(db):
    mid1 = db.add_memory("default", "Unique content one", category="fact")
    mid2 = db.add_memory("default", "Unique content two", category="fact")
    
    # Edit mid2 to match mid1's content: should fail safely (return False) instead of raising IntegrityError
    success = db.edit_memory("default", mid2, content="Unique content one")
    assert success is False
    
    # Assert content of mid2 remains unchanged
    mems = db.get_memories("default")
    m2 = next(m for m in mems if m["id"] == mid2)
    assert m2["content"] == "Unique content two"
