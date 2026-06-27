"""
Flagged Queue page — review and act on facts that scored 0.60-0.84 confidence
during ingestion.

Business logic (accept_item, reject_item, batch_accept, get_counts) is
separated from Streamlit rendering so it can be unit tested without a browser.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from server import add_memory as _add_memory_tool
add_memory = _add_memory_tool.fn

FLAGGED_QUEUE_PATH = Path.home() / "memorybridge" / "flagged_queue.json"


# =============================================================================
# Business logic — testable, Streamlit-free
# =============================================================================

def load_queue(path: Path = None) -> dict:
    p = path or FLAGGED_QUEUE_PATH
    if not p.exists():
        return {"generated": "", "items": []}
    return json.loads(p.read_text())


def save_queue(data: dict, path: Path = None) -> None:
    p = path or FLAGGED_QUEUE_PATH
    p.write_text(json.dumps(data, indent=2))


def get_counts(path: Path = None) -> dict:
    data = load_queue(path)
    counts = {"pending": 0, "accepted": 0, "rejected": 0}
    for item in data.get("items", []):
        status = item.get("status", "pending")
        if status in counts:
            counts[status] += 1
    return counts


def accept_item(item_id: str, queue_path: Path = None) -> bool:
    """
    Accept a flagged fact: call add_memory and mark status=accepted.
    Returns True if item was found and processed, False otherwise.
    """
    data = load_queue(queue_path)
    item = next((i for i in data.get("items", []) if i["id"] == item_id), None)
    if item is None:
        return False

    add_memory(
        content=item["fact"],
        category=item.get("category", "fact"),
        importance=item.get("importance", "medium"),
        project_id=item.get("project"),
        profile=item.get("profile", "default"),
    )
    item["status"] = "accepted"
    save_queue(data, queue_path)
    return True


def reject_item(item_id: str, queue_path: Path = None) -> bool:
    """
    Reject a flagged fact: mark status=rejected, do NOT call add_memory.
    Returns True if item was found, False otherwise.
    """
    data = load_queue(queue_path)
    item = next((i for i in data.get("items", []) if i["id"] == item_id), None)
    if item is None:
        return False

    item["status"] = "rejected"
    save_queue(data, queue_path)
    return True


def batch_accept(queue_path: Path = None) -> int:
    """
    Accept all pending items in the queue.
    Returns count of items accepted.
    """
    data = load_queue(queue_path)
    count = 0
    for item in data.get("items", []):
        if item.get("status") == "pending":
            add_memory(
                content=item["fact"],
                category=item.get("category", "fact"),
                importance=item.get("importance", "medium"),
                project_id=item.get("project"),
                profile=item.get("profile", "default"),
            )
            item["status"] = "accepted"
            count += 1
    save_queue(data, queue_path)
    return count


# =============================================================================
# Streamlit rendering
# =============================================================================

def render():
    import streamlit as st

    st.header("🚩 Flagged Queue")
    st.caption("Facts extracted with 60-84% confidence — review before they enter memory.")

    data = load_queue()
    items = data.get("items", [])
    pending = [i for i in items if i.get("status") == "pending"]
    counts = get_counts()

    st.markdown(
        f"**{counts['pending']} pending** · {counts['accepted']} accepted · {counts['rejected']} rejected"
    )

    if not pending:
        st.success("Queue is clear. Nothing pending review.")
        return

    # Batch accept button
    if st.button(f"✓ Accept all {len(pending)} pending", type="primary"):
        n = batch_accept()
        st.success(f"Accepted {n} facts into memory.")
        st.rerun()

    st.divider()

    for item in pending:
        with st.container():
            col1, col2 = st.columns([6, 1])
            with col1:
                st.markdown(f"**{item['fact']}**")
                conf = item.get("confidence", 0)
                st.progress(conf, text=f"Confidence: {conf:.0%}  ·  {item.get('category', '')}  ·  {item.get('importance', '')}")
                if item.get("source"):
                    st.caption(f"Source: {item['source']}")
            with col2:
                if st.button("✓", key=f"acc_{item['id']}", help="Accept"):
                    accept_item(item["id"])
                    st.rerun()
                if st.button("✗", key=f"rej_{item['id']}", help="Reject"):
                    reject_item(item["id"])
                    st.rerun()
            st.divider()
