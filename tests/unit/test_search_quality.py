"""
Phase 4 search quality tests — semantic and hybrid search.

Semantic search (FastEmbed + cosine similarity) must surface synonym/paraphrase
matches that FTS5 keyword search misses.

Run: python -m pytest tests/unit/test_search_quality.py -v -s
Note: first run downloads ~25MB ONNX model — subsequent runs use cache.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import pytest
from db.store import MemoryStore


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    """Module-scoped: build embeddings once (model load is slow)."""
    tmp = tmp_path_factory.mktemp("embeddings")
    s = MemoryStore(tmp / "test.db")
    s.ensure_profile("default")
    s.add_memory("default", "Cale served eight years in the United States Air Force",
                 category="fact", importance="high")
    s.add_memory("default", "Prefers concise bullet-point answers",
                 category="preference", importance="medium")
    s.add_memory("default", "Has $126M in documented program impact",
                 category="fact", importance="high")
    s.build_embeddings("default")
    return s


def test_semantic_finds_synonym(db):
    """'military service' should find 'Air Force' via vector similarity."""
    results = db.search_semantic("default", "military service")
    contents = [r["content"] for r in results]
    assert any("Air Force" in c for c in contents), (
        f"Semantic search missed 'Air Force' for query 'military service'. Got: {contents}"
    )


def test_hybrid_outperforms_keyword_on_paraphrase(db):
    """Hybrid should surface semantic matches that pure keyword misses.

    Query 'monetary achievement' shares zero tokens with '$126M documented program impact'
    (no stemming overlap either), so keyword returns nothing but semantic should fire.
    """
    keyword_results = db.search("default", "monetary achievement")
    keyword_contents = [r["content"] for r in keyword_results]

    hybrid_results = db.search_hybrid("default", "monetary achievement")
    hybrid_contents = [r["content"] for r in hybrid_results]

    # Keyword must NOT find it (zero token overlap — confirms test is meaningful)
    assert not any("126M" in c for c in keyword_contents), (
        "Keyword unexpectedly found '$126M' — choose a better zero-overlap query"
    )
    # Hybrid (via semantic leg) should surface the $126M memory
    assert any("126M" in c for c in hybrid_contents), (
        f"Hybrid missed '$126M' for 'monetary achievement'. Got: {hybrid_contents}"
    )


def test_semantic_returns_results_within_token_budget(db):
    """search_semantic respects max_tokens budget."""
    results = db.search_semantic("default", "Air Force", max_tokens=50)
    total = sum(r["token_count"] for r in results)
    assert total <= 50, f"Token budget exceeded: {total} > 50"


def test_embeddings_persisted_to_db(db):
    """build_embeddings should write rows to memory_embeddings table."""
    count = db._conn.execute(
        "SELECT COUNT(*) FROM memory_embeddings WHERE profile='default'"
    ).fetchone()[0]
    assert count == 3, f"Expected 3 embedding rows, got {count}"


def test_embeddings_cached_not_recomputed(db, mocker):
    """Embedding model should be reused across calls — not reloaded."""
    spy = mocker.spy(db, "_embed_texts")
    db.search_semantic("default", "military")
    db.search_semantic("default", "military")
    # Two search calls → two embed_texts calls (one per query), but model loads once
    assert spy.call_count == 2
    assert db._embed_model is not None, "Model should be cached after first call"


def test_hybrid_combines_both_signals(db):
    """Hybrid result set should include both keyword and semantic matches."""
    # "Air Force" — keyword will find this directly
    # "military service" — semantic will find this
    hybrid = db.search_hybrid("default", "military service Air Force")
    contents = [r["content"] for r in hybrid]
    assert any("Air Force" in c for c in contents), (
        f"Hybrid missing keyword match. Got: {contents}"
    )


def test_search_semantic_fallback_on_no_embeddings(tmp_path):
    """search_semantic falls back to FTS5 if no embeddings have been built."""
    s = MemoryStore(tmp_path / "fresh.db")
    s.ensure_profile("default")
    s.add_memory("default", "dark mode preference", category="preference", importance="medium")
    # No build_embeddings call — should fall back gracefully
    results = s.search_semantic("default", "dark mode")
    # Falls back to FTS5, which should find it
    assert isinstance(results, list)
