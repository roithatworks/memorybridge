"""
MemoryBridge Review UI — Streamlit app.

Run with:
    streamlit run ui/app.py

Security:
  - The bundled .streamlit/config.toml binds the server to localhost and enables
    XSRF protection, so the UI is not exposed on the LAN by default.
  - Set MEMORYBRIDGE_UI_PASSWORD to require a password before the UI (which can
    delete memories and spawn ingestion subprocesses) will render. If it is unset,
    the UI still binds to localhost but shows an "unauthenticated" warning.

Pages:
  - Flagged Queue  — review ingestion extractions with 60-84% confidence
  - Memory Browser — filter, search, delete memories
  - Analytics      — token usage trends and operation breakdown
  - Portability    — import exports, generate model-specific exports
"""
import os
import sys
import secrets
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import streamlit as st

st.set_page_config(
    page_title="MemoryBridge",
    page_icon="🧠",
    layout="wide",
    initial_sidebar_state="expanded",
)


def _require_auth() -> None:
    """Gate the UI behind MEMORYBRIDGE_UI_PASSWORD when it is set.

    The UI can delete memories and launch ingestion subprocesses, so it must not
    be usable by anyone who can merely reach the port. Uses a constant-time
    comparison and stops rendering until the correct password is supplied.
    """
    password = os.environ.get("MEMORYBRIDGE_UI_PASSWORD", "")
    if not password:
        st.sidebar.warning(
            "UI is unauthenticated (bound to localhost only). "
            "Set MEMORYBRIDGE_UI_PASSWORD to require a password.",
            icon="⚠️",
        )
        return

    if st.session_state.get("_mb_authed"):
        return

    st.title("🧠 MemoryBridge")
    entered = st.text_input("Password", type="password",
                            help="Set via MEMORYBRIDGE_UI_PASSWORD")
    if entered and secrets.compare_digest(entered, password):
        st.session_state["_mb_authed"] = True
        st.rerun()
    elif entered:
        st.error("Incorrect password.")
    st.stop()


_require_auth()

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
