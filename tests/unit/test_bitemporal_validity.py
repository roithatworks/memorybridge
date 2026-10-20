"""Unit tests for Bi-Temporal Validity Windows and as_of Epistemic Time Travel (v5.2 - Group 3)."""

import json
from datetime import datetime, timedelta
from pathlib import Path
import pytest

from db.store import MemoryStore
import server
import cli


def test_bitemporal_schema_and_columns(tmp_path: Path):
    db_file = tmp_path / "test_bitemporal.db"
    store = MemoryStore(db_file)
    store.ensure_profile("default")

    cols = {r["name"] for r in store._conn.execute("PRAGMA table_info(memories)").fetchall()}
    assert "valid_from" in cols
    assert "valid_until" in cols

    indexes = {r["name"] for r in store._conn.execute("PRAGMA index_list(memories)").fetchall()}
    assert "idx_memories_temporal" in indexes


def test_bitemporal_active_recall_filtering(tmp_path: Path):
    db_file = tmp_path / "test_temporal_filter.db"
    store = MemoryStore(db_file)
    store.ensure_profile("default")

    now = datetime.now()
    past = (now - timedelta(days=10)).isoformat()
    future = (now + timedelta(days=10)).isoformat()

    # Memory already expired in reality
    m_expired = store.add_memory(
        "default",
        "Temporary construction on 5th avenue",
        valid_from=past,
        valid_until=past,
        category="fact"
    )
    # Memory only valid in future
    m_future = store.add_memory(
        "default",
        "Upcoming project launch party",
        valid_from=future,
        valid_until=None,
        category="fact"
    )
    # Currently valid memory
    m_current = store.add_memory(
        "default",
        "User prefers dark mode and vim keybindings",
        valid_from=past,
        valid_until=future,
        category="preference"
    )

    # Active recall: get_memories
    active = store.get_memories("default")
    active_ids = {m["id"] for m in active}
    assert m_current in active_ids
    assert m_expired not in active_ids
    assert m_future not in active_ids

    # Active search (BM25)
    search_res = store.search("default", "construction launch vim keybindings")
    search_ids = {m["id"] for m in search_res}
    assert m_current in search_ids
    assert m_expired not in search_ids
    assert m_future not in search_ids


def test_bitemporal_epistemic_time_travel(tmp_path: Path):
    db_file = tmp_path / "test_time_travel.db"
    store = MemoryStore(db_file)
    store.ensure_profile("default")

    t1_created = "2026-01-01T10:00:00"
    t1_valid_from = "2026-01-01T10:00:00"

    t2_swap = "2026-06-01T12:00:00"
    t2_valid_from = "2026-06-01T12:00:00"

    # 1. Add original fact at T1
    mid1 = store.add_memory(
        "default",
        "Alice is a Principal Engineer at Acme Aerospace",
        category="fact",
        valid_from=t1_valid_from
    )
    # Manually back-stamp created_at for test historical simulation
    store._conn.execute("UPDATE memories SET created_at=? WHERE id=?", (t1_created, mid1))
    store._conn.commit()

    # 2. Add replacement fact at T2, superseding mid1
    mid2 = store.add_memory(
        "default",
        "Alice is a VP of Engineering at Beta Robotics",
        category="fact",
        supersedes=[mid1],
        valid_from=t2_valid_from
    )
    # Stamp T2 times
    store._conn.execute(
        "UPDATE memories SET created_at=?, valid_from=? WHERE id=?",
        (t2_swap, t2_valid_from, mid2)
    )
    store._conn.execute(
        "UPDATE memories SET valid_until=?, archived_at=? WHERE id=?",
        (t2_swap, t2_swap, mid1)
    )
    store._conn.commit()

    # Scenario A: Current recall (as_of is None)
    current_mems = store.get_memories("default")
    curr_ids = {m["id"] for m in current_mems}
    assert mid2 in curr_ids
    assert mid1 not in curr_ids

    current_search = store.search("default", "Alice Engineer")
    curr_search_ids = {m["id"] for m in current_search}
    assert mid2 in curr_search_ids
    assert mid1 not in curr_search_ids

    # Scenario B: Historical Time Travel to 2026-03-15 (between T1 and T2)
    t_mid = "2026-03-15T00:00:00"
    past_mems = store.get_memories("default", as_of=t_mid)
    past_ids = {m["id"] for m in past_mems}
    assert mid1 in past_ids
    assert mid2 not in past_ids

    past_search = store.search("default", "Alice Engineer", as_of=t_mid)
    past_search_ids = {m["id"] for m in past_search}
    assert mid1 in past_search_ids
    assert mid2 not in past_search_ids

    # Scenario C: Historical Time Travel to before T1 (2025-11-01)
    t_before = "2025-11-01T00:00:00"
    ancient_mems = store.get_memories("default", as_of=t_before)
    assert len(ancient_mems) == 0

    ancient_search = store.search("default", "Alice Engineer", as_of=t_before)
    assert len(ancient_search) == 0


def test_bitemporal_invalid_as_of_raises():
    store = MemoryStore(Path(":memory:"))
    with pytest.raises(ValueError, match="Invalid ISO timestamp"):
        store.get_memories("default", as_of="yesterday-at-noon")

    with pytest.raises(ValueError, match="Invalid ISO timestamp"):
        store.search("default", "test query", as_of="not-a-date")


def test_traverse_graph_with_as_of(tmp_path: Path):
    db_file = tmp_path / "test_traverse_time.db"
    store = MemoryStore(db_file)
    store.ensure_profile("default")

    t1 = "2026-01-01T10:00:00"
    t2 = "2026-06-01T10:00:00"

    id1 = store.add_memory("default", "Core Authentication Service", category="fact")
    id2 = store.add_memory("default", "Legacy Token Service", category="fact")
    id3 = store.add_memory("default", "OAuth2 Modern Gateway", category="fact")

    edge1 = store.add_edge(id1, id2, relation="depends_on")
    edge2 = store.add_edge(id1, id3, relation="implements")

    # Stamp timestamps: id1, id2, edge1 exist at t1. id3 and edge2 added at t2.
    store._conn.execute("UPDATE memories SET created_at=?, valid_from=? WHERE id IN (?, ?)", (t1, t1, id1, id2))
    store._conn.execute("UPDATE memory_edges SET created_at=? WHERE id=?", (t1, edge1))

    store._conn.execute("UPDATE memories SET created_at=?, valid_from=? WHERE id=?", (t2, t2, id3))
    store._conn.execute("UPDATE memory_edges SET created_at=? WHERE id=?", (t2, edge2))
    store._conn.commit()

    # Traversal as of 2026-03-01 should ONLY discover id2 via edge1, not id3
    discovered_past = store.traverse_graph(id1, profile="default", as_of="2026-03-01T00:00:00")
    disc_ids_past = {n["id"] for n in discovered_past}
    assert id2 in disc_ids_past
    assert id3 not in disc_ids_past

    # Current traversal discovers both
    discovered_now = store.traverse_graph(id1, profile="default")
    disc_ids_now = {n["id"] for n in discovered_now}
    assert id2 in disc_ids_now
    assert id3 in disc_ids_now


def test_mcp_tools_support_bitemporal_as_of(tmp_path: Path):
    db_file = tmp_path / "test_mcp_bitemporal.db"
    store = MemoryStore(db_file)
    server._store = store

    t1 = "2026-01-01T00:00:00"
    t2 = "2026-06-01T00:00:00"

    add_res = json.loads(server.add_memory(
        "Bob lives in Seattle",
        valid_from=t1,
        profile="default"
    ))
    mid1 = add_res["memory_id"]
    store._conn.execute("UPDATE memories SET created_at=? WHERE id=?", (t1, mid1))
    store._conn.commit()

    add_res2 = json.loads(server.add_memory(
        "Bob moved to Tokyo",
        supersedes=[mid1],
        valid_from=t2,
        profile="default"
    ))
    mid2 = add_res2["memory_id"]
    store._conn.execute("UPDATE memories SET created_at=? WHERE id=?", (t2, mid2))
    store._conn.execute("UPDATE memories SET valid_until=?, archived_at=? WHERE id=?", (t2, t2, mid1))
    store._conn.commit()

    # get_memory tool as_of past
    past_get = json.loads(server.get_memory(profile="default", as_of="2026-03-01T00:00:00"))
    past_ids = {m["id"] for m in past_get["memories"]}
    assert mid1 in past_ids
    assert mid2 not in past_ids

    # search_memory tool as_of past
    past_search = json.loads(server.search_memory("Bob Seattle Tokyo", profile="default", as_of="2026-03-01T00:00:00"))
    past_search_ids = {m["id"] for m in past_search["results"]}
    assert mid1 in past_search_ids
    assert mid2 not in past_search_ids


def test_cli_search_and_recall_with_as_of(tmp_path: Path, monkeypatch, capsys):
    db_file = tmp_path / "memory.db"
    store = MemoryStore(db_file)
    store.ensure_profile("default")

    t1 = "2026-01-01T00:00:00"
    t2 = "2026-06-01T00:00:00"

    id1 = store.add_memory("default", "Charlie works on compiler optimization", valid_from=t1)
    id2 = store.add_memory("default", "Charlie leads AI agents architecture team", supersedes=[id1], valid_from=t2)
    store._conn.execute("UPDATE memories SET created_at=? WHERE id=?", (t1, id1))
    store._conn.execute("UPDATE memories SET created_at=? WHERE id=?", (t2, id2))
    store._conn.execute("UPDATE memories SET valid_until=?, archived_at=? WHERE id=?", (t2, t2, id1))
    store._conn.commit()

    import config
    monkeypatch.setattr(config, "data_dir", lambda: tmp_path)

    # 1. CLI search past
    ret = cli.main(["search", "Charlie", "--as-of", "2026-03-01T00:00:00", "--profile", "default"])
    assert ret == 0
    out = capsys.readouterr().out
    assert "compiler optimization" in out
    assert "AI agents" not in out

    # 2. CLI recall past
    ret = cli.main(["recall", "--as-of", "2026-03-01T00:00:00", "--profile", "default"])
    assert ret == 0
    out = capsys.readouterr().out
    assert "compiler optimization" in out
    assert "AI agents" not in out
