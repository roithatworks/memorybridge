"""Tests for two-stage SQL candidate pre-filtering in search_semantic (Issue #211)."""
import json
import pytest
import db.store as store_mod
from db.store import MemoryStore


def test_candidate_prefilter_bounds_large_vector_scans(tmp_path, monkeypatch):
    """When vector count exceeds threshold, candidate pre-filtering bounds matmul size."""
    st = MemoryStore(tmp_path / "scale.db")
    st.ensure_profile("scale")

    st._embed_texts = lambda texts: [[1.0, 0.0, 0.0, 0.0] for _ in texts]

    # Add target memory
    target_id = st.add_memory("scale", "Important Python FastMCP documentation", category="tech", importance="high")

    # Add 15 dummy memories
    dummy_ids = []
    for i in range(15):
        did = st.add_memory("scale", f"Random noise note number {i}", category="misc", importance="low")
        dummy_ids.append(did)

    # Build embeddings
    st.build_embeddings("scale")

    # Mock query embedding
    st._embed_query_cached = lambda q: [1.0, 0.0, 0.0, 0.0]

    # Under normal threshold (1000), all 16 are candidate-scored
    res_normal = st.search_semantic("scale", "Python FastMCP", limit=5)
    assert any(r["id"] == target_id for r in res_normal)

    # Lower threshold to 5 to trigger pre-filtering branch
    monkeypatch.setattr(store_mod, "MAX_EXACT_SEMANTIC_THRESHOLD", 5)

    res_filtered = st.search_semantic("scale", "Python FastMCP", limit=5)
    assert any(r["id"] == target_id for r in res_filtered)
    assert len(res_filtered) <= 5
