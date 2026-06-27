"""Parse Hermes local-agent history (state.db) into normalized format.

Hermes stores conversation history in a SQLite `state.db` with a `messages`
table (grouped by `session_id`) and a `sessions` table holding titles/metadata.
This parser reads user+assistant messages only (tool-call rows are noise for
durable-memory extraction) and emits the same normalized shape the other
parsers (parse_claude / parse_chatgpt / parse_gemini) produce.

Contract:
    parse(file_path, days=None) -> {
        "source": "hermes",
        "conversations": [
            {"id": str, "title": str, "messages": [{"role","content"}, ...]},
            ...
        ],
    }

`file_path` points at the Hermes state.db (default ~/.hermes/state.db).
"""

import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

DEFAULT_DB = Path(os.path.expanduser("~/.hermes/state.db"))

# Roles that carry actual conversation. Tool output is excluded — it rarely
# yields durable facts and inflates extraction cost/noise.
_KEEP_ROLES = ("user", "assistant")


def parse(file_path: str = None, days: int = None) -> dict:
    """Parse Hermes state.db into normalized conversations.

    Args:
        file_path: Path to Hermes state.db (defaults to ~/.hermes/state.db).
        days: If set, only include sessions started within the last N days.
    Returns:
        Normalized dict (see module docstring).
    """
    db_path = Path(file_path) if file_path else DEFAULT_DB
    if not db_path.exists():
        raise FileNotFoundError(f"Hermes state.db not found at {db_path}")

    cutoff_ts = None
    if days is not None:
        cutoff_ts = (datetime.now() - timedelta(days=days)).timestamp()

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    # Session titles / start times for labeling + optional date filtering.
    sessions = {}
    for s in conn.execute("SELECT id, title, started_at FROM sessions"):
        try:
            started = float(s["started_at"]) if s["started_at"] is not None else None
        except (TypeError, ValueError):
            started = None
        sessions[s["id"]] = {"title": s["title"], "started_at": started}

    placeholders = ",".join("?" * len(_KEEP_ROLES))
    rows = conn.execute(
        f"""SELECT session_id, role, content, timestamp
            FROM messages
            WHERE role IN ({placeholders})
              AND content IS NOT NULL
              AND TRIM(content) <> ''
            ORDER BY session_id, id""",
        _KEEP_ROLES,
    ).fetchall()
    conn.close()

    # Group messages by session, preserving order.
    grouped: dict[str, list] = {}
    for r in rows:
        grouped.setdefault(r["session_id"], []).append(
            {"role": r["role"], "content": r["content"].strip()}
        )

    conversations = []
    for sid, messages in grouped.items():
        meta = sessions.get(sid, {})

        # Date filter on session start (fall back to first message if needed).
        if cutoff_ts is not None:
            started = meta.get("started_at")
            if started is not None and started < cutoff_ts:
                continue

        # Skip trivial sessions (e.g. a lone "hey") — no durable signal.
        if len(messages) < 2:
            continue

        title = (meta.get("title") or "").strip() or f"Hermes session {sid}"
        conversations.append({
            "id": sid,
            "title": title,
            "messages": messages,
        })

    return {
        "source": "hermes",
        "conversations": conversations,
    }


if __name__ == "__main__":
    import json
    import sys

    path = sys.argv[1] if len(sys.argv) > 1 else None
    result = parse(path)
    convs = result["conversations"]
    total_msgs = sum(len(c["messages"]) for c in convs)
    print(f"Parsed {len(convs)} conversations, {total_msgs} messages")
    print(json.dumps(convs[0], indent=2)[:600] if convs else "(no conversations)")
