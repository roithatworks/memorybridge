"""Shared constants for MemoryBridge — single source of truth.

Imported by both db/store.py and server.py so definitions are never
duplicated.  Zero dependencies on FastMCP, SQLite, or any project module.
"""

import hashlib
from datetime import datetime


MIN_SCORE = 0.1
HALF_LIFE_DAYS = 30

_IMPORTANCE_BOOSTS = {"low": 0.8, "medium": 1.0, "high": 1.2, "critical": 1.5}


def effective_score(row: dict, now: datetime) -> float:
    """Canonical decay-adjusted score for a memory row.

    Ordering: decay → importance_boost → access_boost → clamp.
    All callers (apply_decay, auto_prune, prune dry-run) must use this.
    """
    try:
        created = datetime.fromisoformat(row.get("created_at", ""))
    except Exception:
        created = now
    days_old = (now - created).days
    decay = 0.5 ** (days_old / HALF_LIFE_DAYS)
    importance_boost = _IMPORTANCE_BOOSTS.get(row.get("importance", "medium"), 1.0)
    access_boost = 1 + min(row.get("access_count", 0) * 0.05, 0.5)
    result = row.get("relevance_score", 1.0) * decay * importance_boost * access_boost
    return max(result, MIN_SCORE)


VALID_CATEGORIES = [
    "preference", "fact", "insight", "decision",
    "project_status", "relationship", "skill", "constraint"
]

IMPORTANCE_LEVELS = ["low", "medium", "high", "critical"]


def _content_hash(content: str) -> str:
    """SHA256 of normalized content — same normalization across all callers."""
    return hashlib.sha256(content.strip().lower().encode()).hexdigest()


def _count_tokens(text: str) -> int:
    """Token-count with tiktoken; falls back to word-count heuristic."""
    try:
        import tiktoken
        enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text)) + 20
    except Exception:
        return len(text.split()) + 20
