"""
Knowledge Graph page — browse memory relationships and directed graph edges.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

_DATA_DIR = Path(os.environ.get("MEMORYBRIDGE_DATA", Path.home() / "memorybridge"))


def render():
    import streamlit as st
    from db.store import MemoryStore

    MEMORY_DB = _DATA_DIR / "memory.db"
    store = MemoryStore(MEMORY_DB)

    st.header("🕸️ Knowledge Graph")
    st.caption("Directed relations between memories (Memory Engineering v5)")

    with st.expander("➕ Add New Edge"):
        with st.form("add_edge_form"):
            c1, c2, c3 = st.columns(3)
            with c1:
                src = st.text_input("Source Memory ID")
            with c2:
                rel = st.text_input("Relation", value="relates_to")
            with c3:
                tgt = st.text_input("Target Memory ID")
            
            if st.form_submit_button("Create Edge"):
                if src and tgt and rel:
                    import sqlite3
                    try:
                        store.add_edge(src.strip(), tgt.strip(), rel.strip())
                        st.success("Edge created!")
                        st.rerun()
                    except sqlite3.IntegrityError:
                        st.error("Invalid Source or Target Memory ID (must exist in active memories).")
                    except Exception as e:
                        st.error(f"Failed to create edge: {e}")
                else:
                    st.error("All fields are required.")

    rows = store._conn.execute(
        """SELECT e.*, m1.content as source_content, m1.category as source_cat,
                  m2.content as target_content, m2.category as target_cat
           FROM memory_edges e
           LEFT JOIN memories m1 ON e.source_id = m1.id
           LEFT JOIN memories m2 ON e.target_id = m2.id
           ORDER BY e.created_at DESC"""
    ).fetchall()

    if not rows:
        st.info("No knowledge graph edges recorded yet. Use `add_memory_edge` MCP tool to link memories.")
        return

    relations = sorted(list({r["relation"] for r in rows}))
    rel_filter = st.selectbox("Filter by relation", ["(all)"] + relations)

    if rel_filter != "(all)":
        rows = [r for r in rows if r["relation"] == rel_filter]

    st.metric("Graph Edges", len(rows))
    st.divider()

    for r in rows:
        with st.container():
            col1, col2, col3, col_del = st.columns([4, 2, 4, 1])
            with col1:
                st.markdown(f"**Source Memory (`{r.get('source_cat', 'fact')}`)**")
                st.caption(f"ID: `{r['source_id']}`")
                st.text((r.get("source_content") or "Deleted memory")[:120])
            with col2:
                st.markdown(f"↔ **`{r['relation']}`**")
                st.caption(f"Created: {r['created_at'][:10]}")
            with col3:
                st.markdown(f"**Target Memory (`{r.get('target_cat', 'fact')}`)**")
                st.caption(f"ID: `{r['target_id']}`")
                st.text((r.get("target_content") or "Deleted memory")[:120])
            with col_del:
                if st.button("🗑", key=f"deledge_{r['id']}", help="Delete edge"):
                    store.delete_edge(r["id"])
                    st.rerun()
            st.divider()
