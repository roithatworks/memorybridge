"""
Phase 3 integration tests — all MCP tools behave identically after SQLite migration.
Run: python -m pytest tests/integration/test_mcp_tools.py -v
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import pytest
from db.store import MemoryStore


@pytest.fixture(autouse=True)
def fresh_store(tmp_path, monkeypatch):
    """Inject a fresh SQLite store so tests don't touch the live DB."""
    import server
    s = MemoryStore(tmp_path / "test.db")
    monkeypatch.setattr(server, "_store", s)
    s.ensure_profile("default")
    yield s


# -------------------------------------------------------------------------
# add_memory + get_memory
# -------------------------------------------------------------------------

def test_add_then_get(fresh_store):
    import server
    server.add_memory.fn("Cale prefers dark mode", category="preference", profile="default")
    result = json.loads(server.get_memory.fn(profile="default"))
    contents = [m["content"] for m in result["memories"]]
    assert "Cale prefers dark mode" in contents


def test_add_returns_memory_id(fresh_store):
    import server
    result = json.loads(
        server.add_memory.fn("Some fact", category="fact", profile="default")
    )
    assert result["status"] == "added"
    assert result["memory_id"].startswith("mem_")


def test_add_duplicate_returns_duplicate_status(fresh_store):
    import server
    server.add_memory.fn("Same content", category="fact", profile="default")
    result = json.loads(
        server.add_memory.fn("Same content", category="fact", profile="default")
    )
    assert result["status"] == "duplicate"


# -------------------------------------------------------------------------
# update_memory + search_memory
# -------------------------------------------------------------------------

def test_update_then_search(fresh_store):
    import server
    server.update_memory.fn(
        facts=["Fact Alpha", "Fact Beta", "Fact Gamma"],
        category="fact", profile="default"
    )
    result = json.loads(server.search_memory.fn(query="Alpha", profile="default"))
    assert result["total_matches"] >= 1
    contents = [m["content"] for m in result["results"]]
    assert any("Alpha" in c for c in contents)


def test_update_deduplicates_within_batch(fresh_store):
    import server
    result = json.loads(server.update_memory.fn(
        facts=["Unique fact A", "Unique fact A"],  # duplicate in same batch
        category="fact", profile="default"
    ))
    # Only one should be written
    assert result["count"] == 1


def test_search_strips_internal_fields(fresh_store):
    import server
    server.add_memory.fn("Enterprise turnaround specialist", category="fact", profile="default")
    result = json.loads(server.search_memory.fn(query="turnaround", profile="default"))
    internal = {"effective_score", "combined_score", "match_score",
                "compressed", "access_count", "last_accessed"}
    for mem in result["results"]:
        leaked = internal & set(mem.keys())
        assert not leaked, f"Internal fields in results: {leaked}"


# -------------------------------------------------------------------------
# delete_memory
# -------------------------------------------------------------------------

def test_delete_memory(fresh_store):
    import server
    r = json.loads(server.add_memory.fn("Delete me", category="fact", profile="default"))
    mid = r["memory_id"]
    del_result = json.loads(server.delete_memory.fn(memory_id=mid, profile="default"))
    assert del_result["status"] == "deleted"

    result = json.loads(server.get_memory.fn(profile="default"))
    assert not any(m["id"] == mid for m in result["memories"])


def test_delete_nonexistent_returns_error(fresh_store):
    import server
    result = json.loads(server.delete_memory.fn(memory_id="mem_fake", profile="default"))
    assert "error" in result


# -------------------------------------------------------------------------
# get_token_stats
# -------------------------------------------------------------------------

def test_token_stats_single_profile(fresh_store):
    import server
    server.add_memory.fn("Some fact", category="fact", profile="default")
    result = json.loads(server.get_token_stats.fn(profile="default"))
    assert result["active"]["memory_count"] == 1
    assert result["active"]["total_tokens"] > 0


def test_token_stats_all(fresh_store):
    import server
    fresh_store.ensure_profile("work")
    server.add_memory.fn("Personal fact", category="fact", profile="default")
    server.add_memory.fn("Work fact", category="fact", profile="work")
    result = json.loads(server.get_token_stats.fn(profile="all"))
    assert "global" in result
    assert "default" in result["by_profile"]
    assert "work" in result["by_profile"]


# -------------------------------------------------------------------------
# prune_memories
# -------------------------------------------------------------------------

def test_prune_dry_run(fresh_store):
    import server
    server.add_memory.fn("Low priority fact", category="fact",
                         importance="low", profile="default")
    result = json.loads(server.prune_memories.fn(profile="default", dry_run=True))
    assert "dry_run" in result
    assert result["dry_run"] is True


# -------------------------------------------------------------------------
# switch_profile
# -------------------------------------------------------------------------

def test_switch_profile_existing(fresh_store):
    import server
    result = json.loads(server.switch_profile.fn(profile_name="default"))
    assert result["status"] == "switched"
    assert result["profile"] == "default"


def test_switch_profile_nonexistent(fresh_store):
    import server
    result = json.loads(server.switch_profile.fn(profile_name="nonexistent"))
    assert "error" in result
    assert "available_profiles" in result


# -------------------------------------------------------------------------
# list_projects
# -------------------------------------------------------------------------

def test_list_projects_empty(fresh_store):
    import server
    result = json.loads(server.list_projects.fn(profile="default"))
    assert result["count"] == 0
    assert result["projects"] == []


# -------------------------------------------------------------------------
# get_access_log
# -------------------------------------------------------------------------

def test_access_log_records_operations(fresh_store):
    import server
    server.add_memory.fn("Logged fact", category="fact", profile="default")
    server.search_memory.fn(query="Logged", profile="default")
    result = json.loads(server.get_access_log.fn(limit=10))
    assert result["returned"] >= 2
    actions = [e["action"] for e in result["entries"]]
    assert "add_memory" in actions
    assert "search_memory" in actions


def test_access_log_token_summary(fresh_store):
    import server
    result = json.loads(server.get_access_log.fn(include_tokens=True))
    assert "token_summary" in result
    assert "total_served" in result["token_summary"]


# -------------------------------------------------------------------------
# Profile isolation
# -------------------------------------------------------------------------

def test_profile_isolation(fresh_store):
    import server
    fresh_store.ensure_profile("work")
    server.add_memory.fn("Personal fact", category="fact", profile="default")
    server.add_memory.fn("Work fact", category="fact", profile="work")
    default_mems = json.loads(server.get_memory.fn(profile="default"))["memories"]
    work_mems = json.loads(server.get_memory.fn(profile="work"))["memories"]
    assert not any("Work" in m["content"] for m in default_mems)
    assert not any("Personal" in m["content"] for m in work_mems)
