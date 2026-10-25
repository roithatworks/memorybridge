"""
Knowledge Graph page — interactive multi-hop neighborhood traversal,
bi-temporal epistemic time travel, and relation mining visualization (Memory Engineering v5.2).
"""
import os
import sys
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

_DATA_DIR = Path(os.environ.get("MEMORYBRIDGE_DATA", Path.home() / "memorybridge"))

# Visual styles and colors for relations
RELATION_STYLES: Dict[str, Dict[str, str]] = {
    "depends_on": {"color": "#E65100", "fontcolor": "#E65100", "style": "solid", "badge": "🟧 depends_on"},
    "implements": {"color": "#6A1B9A", "fontcolor": "#6A1B9A", "style": "solid", "badge": "🟪 implements"},
    "contradicts": {"color": "#C62828", "fontcolor": "#C62828", "style": "dashed", "badge": "🟥 contradicts"},
    "part_of": {"color": "#0277BD", "fontcolor": "#0277BD", "style": "solid", "badge": "🟦 part_of"},
    "relates_to": {"color": "#455A64", "fontcolor": "#455A64", "style": "solid", "badge": "⬜ relates_to"},
    "supersedes": {"color": "#AD1457", "fontcolor": "#AD1457", "style": "solid", "badge": "🟫 supersedes"},
    "shares_entities": {"color": "#00695C", "fontcolor": "#00695C", "style": "dotted", "badge": "🟩 shares_entities"},
    "semantically_similar": {"color": "#1565C0", "fontcolor": "#1565C0", "style": "dashed", "badge": "🔷 semantically_similar"},
}

DEFAULT_EDGE_STYLE = {"color": "#78909C", "fontcolor": "#78909C", "style": "solid", "badge": "⚪ edge"}

CATEGORY_COLORS: Dict[str, str] = {
    "decision": "#EDE7F6",       # light purple
    "fact": "#E1F5FE",           # light blue
    "insight": "#FFF9C4",        # light yellow
    "preference": "#E8F5E9",     # light green
    "project_status": "#FFF3E0",  # light orange
    "relationship": "#FCE4EC",   # light pink
    "constraint": "#FFEBEE",     # light red
    "procedural": "#F3E5F5",     # light violet
    "episodic": "#E0F2F1",       # light teal
    "skill": "#E8EAF6",          # light indigo
}
DEFAULT_CATEGORY_COLOR = "#ECEFF1"


def sanitize_dot_string(text: str, max_chars: int = 50) -> str:
    """Sanitize arbitrary text for safe inclusion in Graphviz DOT labels."""
    if not text:
        return ""
    # Strip HTML tags
    clean = re.sub(r"<[^>]+>", "", str(text))
    # Replace backslashes and double quotes, collapse newlines and whitespace
    clean = clean.replace("\\", "\\\\").replace('"', '\\"').replace("\r", " ").replace("\n", " ").strip()
    if len(clean) > max_chars:
        clean = clean[: max_chars - 3] + "..."
    return clean


def build_graphviz_dot(
    nodes: List[Dict[str, Any]],
    edges: List[Dict[str, Any]],
    root_id: Optional[str] = None,
    graph_name: str = "KnowledgeGraph",
) -> str:
    """Generate clean Graphviz digraph DOT syntax for a set of nodes and edges."""
    lines = [
        f'digraph "{graph_name}" {{',
        '    rankdir=LR;',
        '    bgcolor="transparent";',
        '    node [shape=box, style="rounded,filled", fontname="Helvetica", fontsize=10, margin="0.2,0.1"];',
        '    edge [fontname="Helvetica", fontsize=9];',
    ]

    seen_nodes: Set[str] = set()

    for n in nodes:
        nid = str(n.get("id", "")).strip()
        if not nid:
            continue
        seen_nodes.add(nid)
        cat = str(n.get("category", "fact")).lower()
        fillcolor = CATEGORY_COLORS.get(cat, DEFAULT_CATEGORY_COLOR)

        # Content snippet
        raw_content = n.get("content") or ""
        snippet = sanitize_dot_string(raw_content, max_chars=36)

        # Shortened ID or label
        short_id = sanitize_dot_string(nid[:14])
        hop_info = ""
        if "hop_distance" in n and n["hop_distance"] is not None:
            hop_info = f" [Hop {n['hop_distance']}]"

        label = f"{short_id}{hop_info}\\n({cat})\\n{snippet}"

        if root_id and nid == root_id:
            lines.append(
                f'    "{nid}" [label="{label}", fillcolor="{fillcolor}", color="#FF6F00", penwidth=3.0, fontcolor="#0D47A1"];'
            )
        else:
            lines.append(f'    "{nid}" [label="{label}", fillcolor="{fillcolor}", color="#90A4AE", penwidth=1.0];')

    # Add edges
    for e in edges:
        src = str(e.get("source_id", "")).strip()
        tgt = str(e.get("target_id", "")).strip()
        if not src or not tgt:
            continue

        for endpoint in (src, tgt):
            if endpoint not in seen_nodes:
                seen_nodes.add(endpoint)
                short = sanitize_dot_string(endpoint[:14])
                lines.append(
                    f'    "{endpoint}" [label="{short}", fillcolor="#ECEFF1", color="#B0BEC5", style="dashed,rounded,filled"];'
                )

        rel = str(e.get("relation", "relates_to")).strip()
        style_info = RELATION_STYLES.get(rel, DEFAULT_EDGE_STYLE)
        color = style_info["color"]
        fontcolor = style_info["fontcolor"]
        style = style_info["style"]
        rel_label = sanitize_dot_string(rel, max_chars=20)

        lines.append(
            f'    "{src}" -> "{tgt}" [label="{rel_label}", color="{color}", fontcolor="{fontcolor}", style="{style}"];'
        )

    lines.append("}")
    return "\n".join(lines)


def get_graph_edges(
    store: Any,
    profile: Optional[str] = None,
    relation_filter: Optional[str] = None,
    as_of: Optional[str] = None,
    limit: int = 200,
) -> List[Dict[str, Any]]:
    """Fetch edges with source and target memory metadata, respecting profile and as_of."""
    sql = """
        SELECT e.id, e.source_id, e.target_id, e.relation, e.created_at,
               m1.content as source_content, m1.category as source_cat, m1.profile as source_profile,
               m2.content as target_content, m2.category as target_cat, m2.profile as target_profile
        FROM memory_edges e
        JOIN memories m1 ON e.source_id = m1.id
        JOIN memories m2 ON e.target_id = m2.id
        WHERE 1=1
    """
    params: List[Any] = []
    if profile:
        sql += " AND (m1.profile = ? OR m2.profile = ?)"
        params.extend([profile, profile])
    if relation_filter and relation_filter != "(all)":
        sql += " AND e.relation = ?"
        params.append(relation_filter)
    if as_of:
        sql += " AND e.created_at <= ?"
        params.append(as_of.strip())

    sql += " ORDER BY e.created_at DESC LIMIT ?"
    params.append(limit)

    rows = store._conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def traverse_neighborhood(
    store: Any,
    root_id: str,
    profile: Optional[str] = None,
    max_hops: int = 2,
    max_nodes: int = 15,
    directed: bool = False,
    as_of: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Traverse multi-hop neighborhood from root_id and reconstruct nodes and edges for visualization."""
    discovered = store.traverse_graph(
        start_ids=root_id,
        profile=profile,
        max_hops=max_hops,
        max_nodes=max_nodes,
        directed=directed,
        as_of=as_of,
    )

    root_row = store._conn.execute("SELECT * FROM memories WHERE id=?", (root_id,)).fetchone()
    nodes: List[Dict[str, Any]] = []
    if root_row:
        root_dict = store._row_to_dict(root_row)
        root_dict["hop_distance"] = 0
        root_dict["graph_score"] = 1.0
        nodes.append(root_dict)

    nodes.extend(discovered)
    node_ids = {n["id"] for n in nodes}

    edges: List[Dict[str, Any]] = []
    if len(node_ids) > 1:
        placeholders = ",".join("?" for _ in node_ids)
        params: List[Any] = list(node_ids) + list(node_ids)
        edge_sql = f"""
            SELECT id, source_id, target_id, relation, created_at
            FROM memory_edges
            WHERE source_id IN ({placeholders}) AND target_id IN ({placeholders})
        """
        if as_of:
            edge_sql += " AND created_at <= ?"
            params.append(as_of.strip())
        edge_rows = store._conn.execute(edge_sql, params).fetchall()
        edges = [dict(r) for r in edge_rows]

    return nodes, edges


def render():
    import streamlit as st
    from db.store import MemoryStore

    MEMORY_DB = _DATA_DIR / "memory.db"
    store = MemoryStore(MEMORY_DB)

    st.header("🕸️ Knowledge Graph & Multi-Hop Traversal")
    st.caption("Autonomous knowledge graph construction, relation mining & epistemic time travel (v5.2)")

    profiles = store.list_profiles()
    if not profiles:
        st.info("No profiles found. Add memories via MCP tools or CLI first.")
        return

    col_prof, col_as_of = st.columns([2, 3])
    with col_prof:
        profile = st.selectbox("Profile", profiles, index=0)
    with col_as_of:
        use_time_travel = st.checkbox("🕰️ Epistemic Time Travel (`as_of`)", value=False)
        as_of_val: Optional[str] = None
        if use_time_travel:
            as_of_val = st.text_input(
                "Filter graph state as of (ISO-8601)",
                placeholder="2026-10-20T00:00:00Z",
                help="Reconstructs historical topology: excludes memories and edges created after this timestamp.",
            )
            if as_of_val and not as_of_val.strip():
                as_of_val = None

    tab_traverse, tab_global, tab_tools = st.tabs([
        "🔍 Multi-Hop Neighborhood Traversal",
        "🌐 Global Edge Graph",
        "⚡ Relation Mining & Edge Tools",
    ])

    # -------------------------------------------------------------------------
    # Tab 1: Multi-Hop Neighborhood Traversal
    # -------------------------------------------------------------------------
    with tab_traverse:
        memories = store.get_memories(profile, limit=100, as_of=as_of_val)
        if not memories:
            st.info(f"No memories found in profile `{profile}`.")
        else:
            mem_options = {
                f"[{m['id'][:8]}] {m.get('category', 'fact')}: {m.get('content', '')[:50]}": m["id"]
                for m in memories
            }
            selected_label = st.selectbox("Select Root Memory", list(mem_options.keys()))
            root_id = mem_options[selected_label]

            c1, c2, c3 = st.columns(3)
            with c1:
                hops = st.slider("Depth (Hops)", min_value=1, max_value=3, value=2, help="BFS traversal depth (1 to 3 hops)")
            with c2:
                max_nodes = st.slider("Max Nodes", min_value=5, max_value=50, value=15, step=5)
            with c3:
                directed = st.radio("Direction", ["Bidirectional", "Directed"], index=0, horizontal=True) == "Directed"

            nodes, edges = traverse_neighborhood(
                store,
                root_id=root_id,
                profile=profile,
                max_hops=hops,
                max_nodes=max_nodes,
                directed=directed,
                as_of=as_of_val,
            )

            m_col1, m_col2, m_col3 = st.columns(3)
            m_col1.metric("Neighborhood Nodes", len(nodes))
            m_col2.metric("Connected Edges", len(edges))
            deepest_hop = max([n.get("hop_distance", 0) for n in nodes] or [0])
            m_col3.metric("Deepest Hop Reached", f"Hop {deepest_hop}")

            # Visual Digraph
            dot = build_graphviz_dot(nodes, edges, root_id=root_id)
            try:
                st.graphviz_chart(dot, use_container_width=True)
            except Exception as e:
                st.warning(f"Graphviz rendering note: {e}")

            # Discovered Nodes Breakdown
            with st.expander("📋 Neighborhood Nodes & Path Details", expanded=False):
                for n in nodes:
                    h_dist = n.get("hop_distance", 0)
                    score = n.get("graph_score", 1.0)
                    rel = n.get("edge_relation", "root")
                    badge = RELATION_STYLES.get(rel, DEFAULT_EDGE_STYLE)["badge"]
                    st.markdown(
                        f"**Hop {h_dist}** (`score: {score:.2f}`) · {badge} · ID: `{n['id']}` · Category: `{n.get('category', 'fact')}`"
                    )
                    st.text(n.get("content", ""))
                    if n.get("traversal_path"):
                        st.caption(f"Path: {' -> '.join(str(p) for p in n['traversal_path'])}")
                    st.divider()

    # -------------------------------------------------------------------------
    # Tab 2: Global Edge Graph
    # -------------------------------------------------------------------------
    with tab_global:
        all_edges = get_graph_edges(store, profile=profile, as_of=as_of_val, limit=300)
        relations = sorted(list({e["relation"] for e in all_edges}))
        rel_filter = st.selectbox("Filter by Predicate", ["(all)"] + relations, index=0)

        filtered_edges = get_graph_edges(store, profile=profile, relation_filter=rel_filter, as_of=as_of_val, limit=200)

        st.metric("Graph Edges Matching Filter", len(filtered_edges))

        if filtered_edges:
            # Build node list for filtered edges
            node_map: Dict[str, Dict[str, Any]] = {}
            for e in filtered_edges:
                node_map[e["source_id"]] = {
                    "id": e["source_id"],
                    "content": e.get("source_content", ""),
                    "category": e.get("source_cat", "fact"),
                }
                node_map[e["target_id"]] = {
                    "id": e["target_id"],
                    "content": e.get("target_content", ""),
                    "category": e.get("target_cat", "fact"),
                }

            global_dot = build_graphviz_dot(list(node_map.values()), filtered_edges)
            try:
                st.graphviz_chart(global_dot, use_container_width=True)
            except Exception as e:
                st.warning(f"Graphviz rendering note: {e}")

            st.divider()
            for r in filtered_edges[:50]:
                with st.container():
                    col1, col2, col3, col_del = st.columns([4, 2, 4, 1])
                    with col1:
                        st.markdown(f"**Source (`{r.get('source_cat', 'fact')}`)**")
                        st.caption(f"ID: `{r['source_id']}`")
                        st.text((r.get("source_content") or "Deleted memory")[:120])
                    with col2:
                        badge = RELATION_STYLES.get(r["relation"], DEFAULT_EDGE_STYLE)["badge"]
                        st.markdown(f"**{badge}**")
                        st.caption(f"Created: {r['created_at'][:10]}")
                    with col3:
                        st.markdown(f"**Target (`{r.get('target_cat', 'fact')}`)**")
                        st.caption(f"ID: `{r['target_id']}`")
                        st.text((r.get("target_content") or "Deleted memory")[:120])
                    with col_del:
                        if st.button("🗑", key=f"deledge_{r['id']}", help="Delete edge"):
                            store.delete_edge(r["id"])
                            st.rerun()
                    st.divider()
        else:
            st.info("No knowledge graph edges found matching the current filters.")

    # -------------------------------------------------------------------------
    # Tab 3: Relation Mining & Edge Tools
    # -------------------------------------------------------------------------
    with tab_tools:
        st.subheader("➕ Add Manual Edge")
        with st.form("manual_edge_form"):
            ac1, ac2, ac3 = st.columns(3)
            with ac1:
                src_input = st.text_input("Source Memory ID")
            with ac2:
                valid_rels = list(RELATION_STYLES.keys())
                rel_choice = st.selectbox("Relation Predicate", valid_rels, index=0)
            with ac3:
                tgt_input = st.text_input("Target Memory ID")

            if st.form_submit_button("Create Edge"):
                if src_input and tgt_input and rel_choice:
                    try:
                        eid = store.add_edge(src_input.strip(), tgt_input.strip(), rel_choice.strip())
                        st.success(f"Edge `{eid}` created successfully!")
                        st.rerun()
                    except Exception as ex:
                        st.error(f"Failed to create edge: {ex}")
                else:
                    st.error("All fields are required.")

        st.divider()
        st.subheader("⛏️ Autonomous Relation & Triple Mining")
        mine_mem_id = st.text_input("Memory ID to Mine Relations For", placeholder="mem_...")
        if st.button("Mine Relations for Memory"):
            if mine_mem_id and mine_mem_id.strip():
                try:
                    with st.spinner("Mining relations..."):
                        mined = store.mine_relations(mine_mem_id.strip(), profile=profile, auto_link=True)
                    if mined:
                        st.success(f"Discovered and linked {len(mined)} relation(s):")
                        for m in mined:
                            badge = RELATION_STYLES.get(m["relation"], DEFAULT_EDGE_STYLE)["badge"]
                            st.markdown(f"- {badge} -> Target: `{m['target_id']}` (confidence: {m.get('confidence', 1.0):.2f})")
                            st.caption(f"Reason: {m.get('reason', 'N/A')}")
                    else:
                        st.info("No explicit domain relations found for this memory among candidates.")
                except Exception as ex:
                    st.error(f"Mining failed: {ex}")
            else:
                st.error("Please enter a valid Memory ID.")

        st.divider()
        st.subheader("🔄 Backfill Knowledge Graph Edges")
        st.caption(
            "Scans profile for supersession links, shared entity tags (>=2), semantic vector similarities (>=0.85), and relation triples."
        )
        if st.button("Run Full Graph Backfill"):
            try:
                with st.spinner("Backfilling graph edges..."):
                    res = store.backfill_graph_edges(profile=profile)
                st.success("Graph edge backfill completed:")
                bc1, bc2, bc3, bc4 = st.columns(4)
                bc1.metric("Supersedes", res.get("supersedes", 0))
                bc2.metric("Shared Entities", res.get("shares_entities", 0))
                bc3.metric("Semantic Similar", res.get("semantically_similar", 0))
                bc4.metric("Mined Relations", res.get("mined_relations", 0))
                st.rerun()
            except Exception as ex:
                st.error(f"Backfill failed: {ex}")
