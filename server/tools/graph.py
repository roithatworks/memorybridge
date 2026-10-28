"""Knowledge graph edges, multi-hop traversal, relation mining, and provenance MCP tools."""

import json
from typing import Optional

import server as _srv

mcp = _srv.mcp


@mcp.tool()
def add_memory_edge(
    source_id: str,
    target_id: str,
    relation: str = "relates_to"
) -> str:
    """
    Create a directed knowledge graph edge between two memories.

    Args:
        source_id: Origin memory ID
        target_id: Target memory ID
        relation: Relation tag (e.g. 'supersedes', 'depends_on', 'relates_to', 'part_of')
    Returns:
        JSON confirmation with new edge ID
    """
    try:
        edge_id = _srv._store.add_edge(source_id, target_id, relation=relation)
        return json.dumps({
            "status": "edge_created",
            "edge_id": edge_id,
            "source_id": source_id,
            "target_id": target_id,
            "relation": relation,
        }, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def get_memory_edges(
    memory_id: str
) -> str:
    """
    Retrieve knowledge graph edges connected to a memory.

    Args:
        memory_id: Memory ID to query
    Returns:
        JSON list of connected edges
    """
    edges = _srv._store.get_edges(memory_id)
    return json.dumps({"memory_id": memory_id, "edges": edges, "count": len(edges)}, indent=2)


@mcp.tool()
def traverse_memory_graph(
    memory_id: str,
    max_hops: int = 2,
    max_nodes: int = 10,
    profile: str = None,
    directed: bool = False,
    as_of: Optional[str] = None
) -> str:
    """
    Perform a multi-hop graph traversal starting from a specific memory.
    Discovers associative context connected across 1, 2, or 3 relation hops
    with cycle prevention and distance score decay.

    Args:
        memory_id: Starting memory ID
        max_hops: Depth of traversal (1, 2, or 3; default 2)
        max_nodes: Maximum number of connected memories to return (default 10)
        profile: Memory profile name (defaults to active profile)
        directed: Whether to traverse only outgoing edges (default False: bidirectional)
        as_of: Optional ISO timestamp for epistemic time travel (e.g. 2026-03-01T00:00:00)
    Returns:
        JSON object containing start node, discovered nodes with hops, paths, and scores.
    """
    try:
        prof = _srv._resolve_profile(profile)
    except PermissionError as e:
        return json.dumps({"error": str(e), "status": "forbidden"})
    _srv._validate_profile(prof)
    hops = min(max(1, int(max_hops)), 3)
    limit = min(max(1, int(max_nodes)), 50)

    start_row = _srv._store._conn.execute(
        "SELECT id, content, category, tags FROM memories WHERE id=? AND profile=?",
        (memory_id, prof)
    ).fetchone()
    if not start_row:
        return json.dumps({"error": f"Memory '{memory_id}' not found in profile '{prof}'"})

    nodes = _srv._store.traverse_graph(
        [memory_id],
        profile=prof,
        max_hops=hops,
        max_nodes=limit,
        include_archived=False,
        directed=directed,
        as_of=as_of
    )

    cleaned_nodes = []
    for n in nodes:
        cleaned_nodes.append({
            "id": n["id"],
            "content": n.get("content", ""),
            "category": n.get("category", "fact"),
            "hop_distance": n.get("hop_distance", 1),
            "graph_score": n.get("graph_score", 1.0),
            "edge_relation": n.get("edge_relation", "relates_to"),
            "traversal_path": n.get("traversal_path", []),
            "connected_from": n.get("connected_from", "")
        })

    return json.dumps({
        "start_memory_id": memory_id,
        "max_hops": hops,
        "count": len(cleaned_nodes),
        "nodes": cleaned_nodes
    }, indent=2)


@mcp.tool()
def mine_memory_relations(
    memory_id: str,
    candidate_ids: Optional[list[str]] = None,
    profile: Optional[str] = None
) -> str:
    """
    Mine semantic relationships and triples for a memory against candidates or the knowledge graph.
    Extracts relations ('depends_on', 'implements', 'contradicts', 'part_of', 'relates_to', 'supersedes')
    using LLM reasoning or heuristic extraction, and links them into the knowledge graph.

    Args:
        memory_id: The primary memory ID to evaluate
        candidate_ids: Optional list of target memory IDs to check relations against (defaults to auto-discovered candidates)
        profile: Memory profile name (defaults to active profile)
    Returns:
        JSON list of discovered relations and created edges
    """
    try:
        prof = _srv._resolve_profile(profile)
    except PermissionError as e:
        return json.dumps({"error": str(e), "status": "forbidden"})
    _srv._validate_profile(prof)
    try:
        mined = _srv._store.mine_relations(memory_id, candidate_ids=candidate_ids, profile=prof, auto_link=True)
        return json.dumps({
            "status": "success",
            "memory_id": memory_id,
            "profile": prof,
            "relations_mined": len(mined),
            "relations": mined
        }, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def get_memory_provenance(
    memory_id: str
) -> str:
    """
    Return the full provenance chain for a memory: origin, edit history,
    supersession, and related edges. Useful for auditing how and where a
    memory was created, what changed, and what it replaced.

    Args:
        memory_id: Memory ID to query
    Returns:
        JSON with origin, edit_history, supersession chain, and edges
    """
    provenance = _srv._store.get_provenance(memory_id)
    if provenance is None:
        return json.dumps({"error": f"Memory '{memory_id}' not found"})
    return json.dumps(provenance, indent=2)
