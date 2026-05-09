"""
MemoryBridge Review UI — Streamlit app.

Run with:
    streamlit run ui/app.py

Pages:
  - Flagged Queue  — review ingestion extractions with 60-84% confidence
  - Memory Browser — filter, search, delete memories
  - Analytics      — token usage trends and operation breakdown
  - Portability    — import exports, generate model-specific exports
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import streamlit as st

st.set_page_config(
    page_title="MemoryBridge",
    page_icon="🧠",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Navigation
PAGES = {
    "🚩 Flagged Queue": "flagged_queue",
    "🧠 Memory Browser": "memory_browser",
    "📊 Analytics": "analytics",
    "🔄 Portability": "portability",
}

with st.sidebar:
    st.title("🧠 MemoryBridge")
    st.caption("Local-first AI memory")
    st.divider()
    selection = st.radio("Navigate", list(PAGES.keys()), label_visibility="collapsed")

module_name = PAGES[selection]

if module_name == "flagged_queue":
    from ui.pages.flagged_queue import render
elif module_name == "memory_browser":
    from ui.pages.memory_browser import render
elif module_name == "analytics":
    from ui.pages.analytics import render
elif module_name == "portability":
    from ui.pages.portability import render

render()
