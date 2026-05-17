"""
Memory Browser page — filter, sort, search, and delete memories across profiles.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from server import delete_memory as _del_tool, get_token_stats as _stats_tool
delete_memory = _del_tool.fn
get_token_stats = _stats_tool.fn

MAX_TOTAL_TOKENS = 50_000


def render():
    import streamlit as st
    from db.store import MemoryStore

    MEMORY_DB = Path.home() / "memorybridge" / "memory.db"
    store = MemoryStore(MEMORY_DB)

    st.header("🧠 Memory Browser")

    # Profile selector
    profiles = store.list_profiles()
    if not profiles:
        st.info("No profiles found.")
        return

    profile = st.selectbox("Profile", profiles, index=0)

    # Token usage bar
    stats = store.token_stats(profile)
    used = stats["total_tokens"]
    pct = min(used / MAX_TOTAL_TOKENS, 1.0)
    st.metric("Memories", stats["memory_count"], delta=None)
    st.progress(pct, text=f"Token budget: {used:,} / {MAX_TOTAL_TOKENS:,} ({pct:.1%})")

    # Filters
    col1, col2, col3 = st.columns(3)
    with col1:
        cats = ["(all)", "preference", "fact", "insight", "decision",
                "project_status", "relationship", "skill", "constraint"]
        category_filter = st.selectbox("Category", cats)
    with col2:
        importance_filter = st.selectbox("Importance", ["(all)", "low", "medium", "high", "critical"])
    with col3:
        sort_by = st.selectbox("Sort by", ["relevance_score", "created_at", "access_count"])

    search_query = st.text_input("Search content", placeholder="Type to filter…")

    # Fetch memories
    cat_arg = None if category_filter == "(all)" else category_filter
    memories = store.get_memories(profile, category=cat_arg)

    # Client-side filters
    if importance_filter != "(all)":
        memories = [m for m in memories if m.get("importance") == importance_filter]
    if search_query:
        q = search_query.lower()
        memories = [m for m in memories if q in m.get("content", "").lower()]

    # Sort
    reverse = sort_by in ("relevance_score", "access_count")
    memories = sorted(memories, key=lambda m: m.get(sort_by, 0), reverse=reverse)

    st.caption(f"{len(memories)} memories shown")
    st.divider()

    if not memories:
        st.info("No memories match the current filters.")
        return

    for mem in memories:
        with st.container():
            col_text, col_del = st.columns([8, 1])
            with col_text:
                badge_cat = f"`{mem.get('category', '')}`"
                badge_imp = f"`{mem.get('importance', '')}`"
                st.markdown(f"{mem['content']}  {badge_cat} {badge_imp}")
                st.caption(
                    f"ID: {mem['id']} · "
                    f"Score: {mem.get('relevance_score', 0):.2f} · "
                    f"Accessed: {mem.get('access_count', 0)}× · "
                    f"Created: {mem.get('created_at', '')}"
                )
            with col_del:
                if st.button("🗑", key=f"del_{mem['id']}", help="Delete memory"):
                    delete_memory(memory_id=mem["id"], profile=profile)
                    st.rerun()
            st.divider()
