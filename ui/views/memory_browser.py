"""
Memory Browser page — filter, sort, search, and delete memories across profiles.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

MAX_TOTAL_TOKENS = 50_000
# Honor MEMORYBRIDGE_DATA so the browser reads/deletes from the SAME DB the
# server uses, not a hardcoded ~/memorybridge (#89).
_DATA_DIR = Path(os.environ.get("MEMORYBRIDGE_DATA", Path.home() / "memorybridge"))


def render():
    import streamlit as st
    from db.store import MemoryStore

    MEMORY_DB = _DATA_DIR / "memory.db"
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

    # Sort. Use a type-matched default so a row missing the field doesn't mix
    # str and int and raise TypeError (#114): numeric fields default to 0,
    # string date fields to "".
    numeric_sort = sort_by in ("relevance_score", "access_count")
    reverse = numeric_sort
    default = 0 if numeric_sort else ""
    memories = sorted(memories, key=lambda m: m.get(sort_by, default), reverse=reverse)

    st.caption(f"{len(memories)} memories shown")
    st.divider()

    if not memories:
        st.info("No memories match the current filters.")
        return

    for mem in memories:
        with st.container():
            col_text, col_del = st.columns([8, 1])
            with col_text:
                # Memory content is untrusted (LLM-extracted / user-written) —
                # render as plain text to avoid markdown image/link injection
                # (e.g. a beacon image firing on page load) (#86). Badges are
                # our own trusted strings.
                st.text(mem["content"])
                st.markdown(f"`{mem.get('category', '')}` `{mem.get('importance', '')}`")
                st.caption(
                    f"ID: {mem['id']} · "
                    f"Score: {mem.get('relevance_score', 0):.2f} · "
                    f"Accessed: {mem.get('access_count', 0)}× · "
                    f"Created: {mem.get('created_at', '')}"
                )
            with col_del:
                # Two-click confirmation + audit log. delete_memory is a hard
                # DELETE with no undo, so a single misclick must not destroy a
                # memory, and UI deletes must be recorded like the MCP tool's.
                if st.session_state.get("_confirm_del") == mem["id"]:
                    if st.button("✓", key=f"confirmdel_{mem['id']}",
                                 help="Confirm permanent delete"):
                        store.delete_memory(profile, mem["id"])
                        try:
                            store.log_access("delete_memory", profile,
                                             f"id={mem['id']} (via UI)")
                        except Exception:
                            pass
                        st.session_state.pop("_confirm_del", None)
                        st.rerun()
                    if st.button("✕", key=f"canceldel_{mem['id']}", help="Cancel"):
                        st.session_state.pop("_confirm_del", None)
                        st.rerun()
                else:
                    if st.button("🗑", key=f"del_{mem['id']}", help="Delete memory"):
                        st.session_state["_confirm_del"] = mem["id"]
                        st.rerun()
            if st.session_state.get("_confirm_del") == mem["id"]:
                st.warning("Click ✓ to permanently delete this memory, or ✕ to cancel.")
            st.divider()
