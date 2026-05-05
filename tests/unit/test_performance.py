"""
Phase 2.5 performance tests — buffer access log, strip metadata, compact JSON + hash dedup.
Run: python -m pytest tests/unit/test_performance.py -v
"""
import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

# ===========================================================================
# Task 1: Access log writes are buffered (no save_memory on every search/get)
# ===========================================================================

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


def test_get_memory_does_not_save_on_every_call(tmp_path, monkeypatch):
    """get_memory should NOT call save_memory on every invocation."""
    import server
    monkeypatch.setattr(server, "MEMORY_FILE", tmp_path / "memory.json")
    monkeypatch.setattr(server, "LOCK_FILE", tmp_path / "memory.lock")

    data = server.load_memory()
    data["profiles"]["default"] = {
        "identity": {}, "memories": [
            {"id": "mem_test2", "content": "Cale prefers concise answers",
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
        server.get_memory.fn(profile="default")
        server.get_memory.fn(profile="default")

    assert len(save_call_count) == 0, \
        f"save_memory called {len(save_call_count)} times — should be 0 (buffered)"


# ===========================================================================
# Task 2: Internal metadata stripped from search results
# ===========================================================================

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


# ===========================================================================
# Task 3: Compact JSON writes + content hash dedup
# ===========================================================================

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
