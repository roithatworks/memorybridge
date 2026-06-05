"""
Analytics page — token usage trends, operation breakdown, baseline comparison.

Data source: analytics_events table in SQLite (issue #8).
analytics.json is left untouched if it exists — migration reads live DB only.
"""
import os
from pathlib import Path

BASELINE_TOKENS_PER_SEARCH = 1_740  # pre-v1.4 average


def load_analytics() -> dict:
    """Return analytics summary from SQLite via MemoryStore.get_analytics_summary()."""
    try:
        from db.store import MemoryStore
        data_dir = Path(os.environ.get("MEMORYBRIDGE_DATA", Path.home() / "memorybridge"))
        store = MemoryStore(data_dir / "memory.db")
        return store.get_analytics_summary(since_days=90)
    except Exception:
        return {}


def render():
    import streamlit as st

    st.header("📊 Analytics")

    data = load_analytics()
    if not data or not any(data.get(k) for k in ("daily_stats", "by_model", "by_operation")):
        st.info("No analytics data yet. Use MemoryBridge tools to generate events.")
        return

    # -------------------------------------------------------------------------
    # Key metrics
    # -------------------------------------------------------------------------
    by_op = data.get("by_operation", {})
    search_op = by_op.get("search_memory", {})
    get_op = by_op.get("get_memory", {})

    search_calls = search_op.get("count", 0)
    search_tokens = search_op.get("tokens", 0)
    search_avg = search_tokens // search_calls if search_calls else 0

    get_calls = get_op.get("count", 0)
    get_tokens = get_op.get("tokens", 0)

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("search_memory calls", f"{search_calls:,}")
    col2.metric("avg tokens/search", f"{search_avg:,}",
                delta=f"{search_avg - BASELINE_TOKENS_PER_SEARCH:+,} vs baseline",
                delta_color="inverse")
    col3.metric("get_memory calls", f"{get_calls:,}")
    col4.metric("total tokens served",
                f"{(search_tokens + get_tokens):,}")

    # -------------------------------------------------------------------------
    # Daily token chart
    # -------------------------------------------------------------------------
    daily = data.get("daily_stats", {})
    if daily:
        import pandas as pd
        df = pd.DataFrame([
            {"date": k, "tokens_served": v.get("tokens_served", 0),
             "sessions": v.get("sessions", 0)}
            for k, v in sorted(daily.items())
        ])
        df["date"] = pd.to_datetime(df["date"])

        st.subheader("Tokens served per day")
        st.line_chart(df.set_index("date")["tokens_served"])

        st.subheader("Sessions per day")
        st.bar_chart(df.set_index("date")["sessions"])

    # -------------------------------------------------------------------------
    # By-operation breakdown
    # -------------------------------------------------------------------------
    st.subheader("By operation")
    if by_op:
        import pandas as pd
        op_df = pd.DataFrame([
            {"operation": op, "calls": v.get("count", 0),
             "tokens": v.get("tokens", 0),
             "avg": v.get("tokens", 0) // v.get("count", 1) if v.get("count") else 0}
            for op, v in by_op.items()
        ]).sort_values("tokens", ascending=False)
        st.dataframe(op_df, use_container_width=True, hide_index=True)

    # -------------------------------------------------------------------------
    # Baseline annotation
    # -------------------------------------------------------------------------
    st.divider()
    st.caption(
        f"📍 **Baseline (pre-v1.4):** ~{BASELINE_TOKENS_PER_SEARCH:,} tokens/search_memory call  |  "
        f"**Current avg:** {search_avg:,} tokens/call  |  "
        f"**Reduction:** {max(0, BASELINE_TOKENS_PER_SEARCH - search_avg):,} tokens saved per call"
    )

    # By-model breakdown
    by_model = data.get("by_model", {})
    if by_model:
        st.subheader("By model")
        import pandas as pd
        model_df = pd.DataFrame([
            {"model": m, "sessions": v.get("sessions", 0), "tokens": v.get("tokens", 0)}
            for m, v in by_model.items()
        ])
        st.dataframe(model_df, use_container_width=True, hide_index=True)
