"""Tests for Autonomous Knowledge Graph Construction and Multi-Hop Graph Traversal (v5.1)."""

import json
import pytest
import numpy as np
from datetime import datetime
from db.store import MemoryStore, _pack_vector
import server
import cli


@pytest.fixture
def clean_store(tmp_path):
    db_file = tmp_path / "memory.db"
    store = MemoryStore(db_file)
    store.ensure_profile("default")
    return store


def test_supersession_auto_linking(clean_store):
    """When add_memory supersedes an old fact, a 'supersedes' edge is automatically created."""
    m1 = clean_store.add_memory("default", "Backend uses Flask framework", category="fact")
    assert m1 is not None

    m2 = clean_store.add_memory(
        "default",
        "Backend migrated to FastAPI framework",
        category="decision",
        supersedes=[m1]
    )
    assert m2 is not None

    # Check that memory_edges has the supersedes link
    edges = clean_store.get_edges(m2)
    assert len(edges) == 1
    edge = edges[0]
    assert edge["source_id"] == m2
    assert edge["target_id"] == m1
    assert edge["relation"] == "supersedes"


def test_entity_cooccurrence_auto_linking(clean_store):
    """Memories sharing >= 2 entity tags are automatically linked via 'shares_entities'."""
    # Both mention Cloudflare and SQLite (seed entities in db/entities.py)
    m1 = clean_store.add_memory(
        "default",
        "We deploy on Cloudflare Workers and persist data in SQLite",
        category="fact"
    )
    m2 = clean_store.add_memory(
        "default",
        "Our Cloudflare edge routing connects directly to local SQLite database",
        category="fact"
    )

    # Check edges for m2
    edges = clean_store.get_edges(m2)
    shares_edges = [e for e in edges if e["relation"] == "shares_entities"]
    assert len(shares_edges) == 1
    assert shares_edges[0]["source_id"] == m2
    assert shares_edges[0]["target_id"] == m1


def test_traverse_graph_multi_hop_decay_and_cycles(clean_store):
    """Multi-hop BFS traversal explores connected nodes with score decay and cycle guards."""
    mA = clean_store.add_memory("default", "Memory Node Alpha", category="fact")
    mB = clean_store.add_memory("default", "Memory Node Beta", category="fact")
    mC = clean_store.add_memory("default", "Memory Node Gamma", category="fact")

    # Directed Graph: A --[relates_to]--> B --[depends_on]--> C --[relates_to]--> A (cycle)
    clean_store.add_edge(mA, mB, "relates_to")
    clean_store.add_edge(mB, mC, "depends_on")
    clean_store.add_edge(mC, mA, "relates_to")

    # Traverse starting from A up to 2 hops (directed)
    nodes = clean_store.traverse_graph([mA], profile="default", max_hops=2, max_nodes=10, directed=True)

    # Must discover B (1 hop) and C (2 hops) without re-visiting A
    node_ids = [n["id"] for n in nodes]
    assert node_ids == [mB, mC]

    # Verify B (hop 1)
    b_node = next(n for n in nodes if n["id"] == mB)
    assert b_node["hop_distance"] == 1
    assert b_node["graph_score"] == 1.0
    assert b_node["edge_relation"] == "relates_to"
    assert b_node["traversal_path"] == [mA, "relates_to", mB]

    # Verify C (hop 2)
    c_node = next(n for n in nodes if n["id"] == mC)
    assert c_node["hop_distance"] == 2
    assert c_node["graph_score"] == 0.5
    assert c_node["edge_relation"] == "depends_on"
    assert c_node["traversal_path"] == [mA, "relates_to", mB, "depends_on", mC]


def test_expand_related_multi_hop(clean_store):
    """_expand_related incorporates multi-hop graph nodes into hybrid retrieval."""
    mA = clean_store.add_memory("default", "Root architectural fact", category="fact")
    mB = clean_store.add_memory("default", "1st hop dependency", category="fact")
    mC = clean_store.add_memory("default", "2nd hop dependency", category="fact")

    clean_store.add_edge(mA, mB, "relates_to")
    clean_store.add_edge(mB, mC, "depends_on")

    row_a = clean_store._row_to_dict(
        clean_store._conn.execute("SELECT * FROM memories WHERE id=?", (mA,)).fetchone()
    )

    expanded = clean_store._expand_related([row_a], "default", max_related=3, max_hops=2)
    exp_ids = [m["id"] for m in expanded]

    assert mA in exp_ids
    assert mB in exp_ids
    assert mC in exp_ids


def test_add_edge_guards_and_idempotency(clean_store):
    """Cannot create self-loops, and duplicate edge inserts return existing edge ID."""
    mA = clean_store.add_memory("default", "Node 1", category="fact")
    mB = clean_store.add_memory("default", "Node 2", category="fact")

    # Self-loop rejection
    with pytest.raises(ValueError, match="Cannot create an edge from a memory to itself"):
        clean_store.add_edge(mA, mA, "relates_to")

    # Idempotent insert
    e1 = clean_store.add_edge(mA, mB, "relates_to")
    e2 = clean_store.add_edge(mA, mB, "relates_to")
    assert e1 == e2

    # Database only has 1 row
    count = clean_store._conn.execute("SELECT COUNT(*) FROM memory_edges").fetchone()[0]
    assert count == 1


def test_semantic_auto_linking(clean_store):
    """_auto_link_semantic creates semantically_similar edges when cosine similarity >= threshold."""
    mA = clean_store.add_memory("default", "Vector Candidate A", category="fact")
    mB = clean_store.add_memory("default", "Vector Candidate B", category="fact")

    # Synthetic unit vectors with high cosine similarity (0.95)
    vA = [1.0] + [0.0] * 383
    vB = [0.95, 0.3122] + [0.0] * 382

    # Save vector for mA
    clean_store._conn.execute(
        "INSERT INTO memory_embeddings(id, profile, vector) VALUES (?, ?, ?)",
        (mA, "default", _pack_vector(vA))
    )
    clean_store._conn.commit()

    # Link mB with vB
    created = clean_store._auto_link_semantic(mB, "default", vB, threshold=0.85)
    assert created == 1

    edges = clean_store.get_edges(mB)
    assert len(edges) == 1
    assert edges[0]["relation"] == "semantically_similar"
    assert (edges[0]["source_id"], edges[0]["target_id"]) == (mB, mA)


def test_backfill_graph_edges(clean_store):
    """backfill_graph_edges scans existing memories and populates missing edges."""
    mA = clean_store.add_memory("default", "Old fact", category="fact")
    mB = clean_store.add_memory("default", "New fact replacing old", category="decision")

    # Manually simulate historical supersession without edge
    clean_store._conn.execute(
        "UPDATE memories SET valid_until=?, superseded_by=? WHERE id=?",
        (datetime.now().isoformat(), mB, mA)
    )
    clean_store._conn.commit()

    counts = clean_store.backfill_graph_edges("default")
    assert counts["supersedes"] == 1

    edges = clean_store.get_edges(mB)
    assert any(e["relation"] == "supersedes" and e["target_id"] == mA for e in edges)


def test_server_traverse_memory_graph_tool(clean_store, monkeypatch):
    """traverse_memory_graph tool executes multi-hop graph discovery via MCP."""
    monkeypatch.setattr(server, "_store", clean_store)

    mA = clean_store.add_memory("default", "Deployment Root", category="fact")
    mB = clean_store.add_memory("default", "Cloudflare Subsystem", category="fact")
    clean_store.add_edge(mA, mB, "relates_to")

    fn = getattr(server.traverse_memory_graph, "fn", server.traverse_memory_graph)
    res_raw = fn(mA, max_hops=2, profile="default")
    res = json.loads(res_raw)

    assert res["start_memory_id"] == mA
    assert res["max_hops"] == 2
    assert res["count"] == 1
    assert res["nodes"][0]["id"] == mB
    assert res["nodes"][0]["hop_distance"] == 1
    assert res["nodes"][0]["graph_score"] == 1.0


def test_cli_edges_traverse_and_auto_link(clean_store, monkeypatch, capsys):
    """CLI subcommands 'edges traverse' and 'edges auto-link' run cleanly."""
    mA = clean_store.add_memory("default", "CLI Source Node", category="fact")
    mB = clean_store.add_memory("default", "CLI Target Node", category="fact")
    clean_store.add_edge(mA, mB, "relates_to")

    # Mock data_dir and MemoryStore
    import config
    monkeypatch.setattr(config, "data_dir", lambda: clean_store._path.parent)
    parser = cli.build_parser()

    # 1. Test traverse
    args_trav = parser.parse_args(["edges", "traverse", mA, "--hops", "2", "--profile", "default"])
    rc_trav = cli.cmd_edges(args_trav)
    assert rc_trav == 0
    captured_trav = capsys.readouterr().out
    assert "Discovered 1 graph nodes" in captured_trav
    assert mB in captured_trav

    # 2. Test auto-link
    args_link = parser.parse_args(["edges", "auto-link", "--profile", "default"])
    rc_link = cli.cmd_edges(args_link)
    assert rc_link == 0
    captured_link = capsys.readouterr().out
    assert "Auto-linking complete" in captured_link


def test_traverse_graph_undirected_cycle(clean_store):
    """Undirected traversal handles bidirectional edges and loops gracefully."""
    mA = clean_store.add_memory("default", "Undirected Node A", category="fact")
    mB = clean_store.add_memory("default", "Undirected Node B", category="fact")
    mC = clean_store.add_memory("default", "Undirected Node C", category="fact")

    clean_store.add_edge(mA, mB, "relates_to")
    clean_store.add_edge(mB, mC, "relates_to")
    clean_store.add_edge(mC, mB, "relates_to")  # Bidirectional edge / cycle

    nodes = clean_store.traverse_graph([mA], profile="default", max_hops=2, directed=False)
    node_ids = [n["id"] for n in nodes]
    assert node_ids == [mB, mC]
    assert nodes[0]["hop_distance"] == 1
    assert nodes[1]["hop_distance"] == 2


def test_get_edges_archived_filtering(clean_store):
    """get_edges filters archived memories for non-supersedes relations unless include_archived=True."""
    mA = clean_store.add_memory("default", "Active Node A", category="fact")
    mB = clean_store.add_memory("default", "Active Node B", category="fact")
    clean_store.add_edge(mA, mB, "relates_to")

    # Manually archive mB
    clean_store._conn.execute("UPDATE memories SET archived=1 WHERE id=?", (mB,))
    clean_store._conn.commit()

    # Active only: mB is archived, so relates_to edge is hidden
    active_edges = clean_store.get_edges(mA, include_archived=False)
    assert len(active_edges) == 0

    # Include archived: returns the edge
    all_edges = clean_store.get_edges(mA, include_archived=True)
    assert len(all_edges) == 1
    assert all_edges[0]["relation"] == "relates_to"
