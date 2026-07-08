"""Parse ChatGPT conversations.json export into normalized format."""

import json
from datetime import datetime, timezone
from pathlib import Path


def _extract_messages_from_mapping(mapping: dict) -> list:
    """Walk the mapping tree in chronological order and extract user/assistant messages."""
    # Build parent->children adjacency and find root
    children_map = {}
    root_id = None
    for node_id, node in mapping.items():
        parent = node.get("parent")
        if parent is None or parent not in mapping:
            root_id = node_id
        else:
            children_map.setdefault(parent, []).append(node_id)

    if root_id is None and mapping:
        root_id = next(iter(mapping))

    messages = []
    # Track visited nodes: a truncated/corrupted export can contain a parent
    # cycle (A->B->A with no true root), and without this the DFS would loop
    # forever, hanging the run until the watcher's timeout.
    visited = set()
    stack = [root_id] if root_id else []
    while stack:
        node_id = stack.pop()
        if node_id in visited:
            continue
        visited.add(node_id)
        node = mapping.get(node_id, {})
        msg = node.get("message")
        if msg:
            role_raw = (msg.get("author") or {}).get("role", "")
            if role_raw in ("user", "assistant"):
                content_obj = msg.get("content", {})
                parts = content_obj.get("parts", []) if isinstance(content_obj, dict) else []
                text = " ".join(
                    p if isinstance(p, str) else (p.get("text", "") if isinstance(p, dict) else "")
                    for p in parts
                ).strip()
                if text:
                    messages.append({"role": role_raw, "content": text})

        for child_id in reversed(children_map.get(node_id, [])):
            stack.append(child_id)

    return messages


def parse(file_path: str, days: int = None) -> dict:
    """
    Parse a ChatGPT conversations.json export.

    Args:
        file_path: Path to conversations.json
        days: If set, only include conversations from the last N days
    Returns:
        Normalized conversation dict
    """
    path = Path(file_path).expanduser()
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    cutoff = None
    if days:
        cutoff = datetime.now(timezone.utc).timestamp() - (days * 86400)

    conversations = []
    for item in raw:
        create_time = item.get("create_time")
        # Only drop a conversation for age when we actually KNOW its age. A
        # missing/unparseable timestamp used to become 0 (1970) and get silently
        # excluded by any --days filter — include it instead (#79).
        if cutoff and isinstance(create_time, (int, float)) and create_time > 0 \
                and create_time < cutoff:
            continue

        try:
            date_str = datetime.fromtimestamp(create_time, tz=timezone.utc).strftime("%Y-%m-%d")
        except Exception:
            date_str = "unknown"

        mapping = item.get("mapping", {})
        messages = _extract_messages_from_mapping(mapping)

        if not messages:
            continue

        conversations.append({
            "id": item.get("id", ""),
            "title": item.get("title", "Untitled"),
            "date": date_str,
            "messages": messages,
        })

    return {
        "source": "chatgpt",
        "exported_at": datetime.now().strftime("%Y-%m-%d"),
        "conversations": conversations,
    }
