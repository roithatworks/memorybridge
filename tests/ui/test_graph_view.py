"""Unit tests for ui/views/graph_view.py business logic and Graphviz generation (Group 4)."""

from pathlib import Path
import pytest

from db.store import MemoryStore
from ui.views.graph_view import (
    RELATION_STYLES,
    build_graphviz_dot,
    get_graph_edges,
    sanitize_dot_string,
    traverse_neighborhood,
)


def test_sanitize_dot_string():
    raw = 'Hello <script>alert("xss")</script> "world" \n new \\ line'
    clean = sanitize_dot_string(raw, max_chars=40)
    assert "<script>" not in clean
    assert "\n" not in clean
    assert '\\"' in clean


def test_build_graphviz_dot_empty():
    dot = build_graphviz_dot([], [])
    assert dot.startswith('digraph "KnowledgeGraph"')
    assert dot.strip().endswith("}")


def test_build_graphviz_dot_with_nodes_and_edges():
    nodes = [
        {"id": "m1", "category": "fact", "content": "Database service on port 5432", "hop_distance": 0},
        {"id": "m2", "category": "decision", "content": "Worker depends on database", "hop_distance": 1},
    ]
    edges = [
        {"source_id": "m2", "target_id": "m1", "relation": "depends_on"},
    ]
    dot = build_graphviz_dot(nodes, edges, root_id="m1")
    assert '"m1"' in dot
    assert '"m2"' in dot
    assert '"m2" -> "m1"' in dot
    assert "depends_on" in dot
    assert "penwidth=3.0" in dot  # root node highlighted


def test_build_graphviz_dot_styles_all_relation_types():
    nodes = [{"id": f"node_{i}", "content": f"Content {i}"} for i in range(10)]
    edges = [
        {"source_id": "node_0", "target_id": "node_1", "relation": "depends_on"},
        {"source_id": "node_0", "target_id": "node_2", "relation": "implements"},
        {"source_id": "node_0", "target_id": "node_3", "relation": "contradicts"},
        {"source_id": "node_0", "target_id": "node_4", "relation": "part_of"},
        {"source_id": "node_0", "target_id": "node_5", "relation": "relates_to"},
        {"source_id": "node_0", "target_id": "node_6", "relation": "supersedes"},
        {"source_id": "node_0", "target_id": "node_7", "relation": "shares_entities"},
        {"source_id": "node_0", "target_id": "node_8", "relation": "semantically_similar"},
    ]
    dot = build_graphviz_dot(nodes, edges)
    for rel in RELATION_STYLES:
        assert rel in dot


def test_get_graph_edges_and_filtering(tmp_path: Path):
    db_file = tmp_path / "test_ui_graph.db"
    store = MemoryStore(db_file)
    store.ensure_profile("default")

    id1 = store.add_memory("default", "Memory 1")
    id2 = store.add_memory("default", "Memory 2")
    id3 = store.add_memory("default", "Memory 3")
    assert id1 and id2 and id3

    store.add_edge(id1, id2, "depends_on")
    store.add_edge(id2, id3, "relates_to")

    edges = get_graph_edges(store, profile="default")
    assert len(edges) == 2

    filtered = get_graph_edges(store, profile="default", relation_filter="depends_on")
    assert len(filtered) == 1
    assert filtered[0]["relation"] == "depends_on"


def test_traverse_neighborhood(tmp_path: Path):
    db_file = tmp_path / "test_neighborhood.db"
    store = MemoryStore(db_file)
    store.ensure_profile("default")

    id1 = store.add_memory("default", "Root database")
    id2 = store.add_memory("default", "API server")
    id3 = store.add_memory("default", "Web frontend")
    assert id1 and id2 and id3

    store.add_edge(id2, id1, "depends_on")
    store.add_edge(id3, id2, "depends_on")

    # 1 hop from id2 should discover id1 and id3 (bidirectional)
    nodes, edges = traverse_neighborhood(store, root_id=id2, profile="default", max_hops=1)
    node_ids = {n["id"] for n in nodes}
    assert id2 in node_ids
    assert id1 in node_ids or id3 in node_ids
    assert len(edges) >= 1

    # 2 hops from id3 should reach id2 and id1
    nodes_2hop, edges_2hop = traverse_neighborhood(store, root_id=id3, profile="default", max_hops=2)
    node_ids_2hop = {n["id"] for n in nodes_2hop}
    assert id3 in node_ids_2hop
    assert id2 in node_ids_2hop
    assert id1 in node_ids_2hop
    assert len(edges_2hop) == 2


def test_traverse_neighborhood_with_as_of(tmp_path: Path):
    db_file = tmp_path / "test_as_of.db"
    store = MemoryStore(db_file)
    store.ensure_profile("default")

    id1 = store.add_memory("default", "Old architecture")
    id2 = store.add_memory("default", "Modern service")
    assert id1 and id2

    store.add_edge(id2, id1, "depends_on")

    # Manually backdate created_at of id1 and edge to past
    past_iso = "2026-01-01T00:00:00Z"
    future_iso = "2026-12-31T23:59:59Z"
    store._conn.execute("UPDATE memories SET created_at = ? WHERE id = ?", (past_iso, id1))
    store._conn.execute("UPDATE memories SET created_at = ? WHERE id = ?", (future_iso, id2))
    store._conn.execute("UPDATE memory_edges SET created_at = ? WHERE source_id = ?", (future_iso, id2))
    store._conn.commit()

    # Traversing as of past should not find id2 or future edge
    nodes_past, edges_past = traverse_neighborhood(store, root_id=id1, profile="default", max_hops=2, as_of=past_iso)
    assert all(n["id"] != id2 for n in nodes_past)
    assert len(edges_past) == 0

    # Traversing as of future should find both
    nodes_fut, edges_fut = traverse_neighborhood(store, root_id=id1, profile="default", max_hops=2, as_of="2027-01-01T00:00:00Z")
    node_ids_fut = {n["id"] for n in nodes_fut}
    assert id2 in node_ids_fut
    assert len(edges_fut) == 1
