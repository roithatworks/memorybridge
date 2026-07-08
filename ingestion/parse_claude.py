"""Parse Claude conversations.json export into normalized format."""

import json
from datetime import datetime, timezone
from pathlib import Path


def parse(file_path: str, days: int = None) -> dict:
    """
    Parse a Claude conversations.json export.

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
        # Claude export uses "created_at" ISO string
        created_str = item.get("created_at", "")
        try:
            created_dt = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
            created_ts = created_dt.timestamp()
            date_str = created_dt.strftime("%Y-%m-%d")
        except Exception:
            created_ts = None  # unknown age — don't let a --days filter drop it
            date_str = "unknown"

        # Only exclude by age when the timestamp is known (#79).
        if cutoff and created_ts is not None and created_ts < cutoff:
            continue

        messages = []
        for msg in item.get("chat_messages", []):
            sender = msg.get("sender", "")
            role = "user" if sender == "human" else "assistant"
            content = msg.get("text", "")
            if isinstance(content, list):
                # Some exports wrap content in a list of dicts
                content = " ".join(
                    part.get("text", "") if isinstance(part, dict) else str(part)
                    for part in content
                )
            if content and content.strip():
                messages.append({"role": role, "content": content.strip()})

        if not messages:
            continue

        conversations.append({
            "id": item.get("uuid", ""),
            "title": item.get("name", "Untitled"),
            "date": date_str,
            "messages": messages,
        })

    return {
        "source": "claude",
        "exported_at": datetime.now().strftime("%Y-%m-%d"),
        "conversations": conversations,
    }
