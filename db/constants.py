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
        # Strip tzinfo so subtraction works regardless of whether the stored
        # timestamp has a UTC offset (Python 3.11+ fromisoformat parses "+00:00"
        # as timezone-aware, which can't be subtracted from a naive datetime.now()).
        created = created.replace(tzinfo=None)
    except Exception:
        created = now.replace(tzinfo=None)
    now_naive = now.replace(tzinfo=None)
    days_old = (now_naive - created).days
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


# --- Write-path guardrails -------------------------------------------------
# A memory is a COMPRESSED FACT, not a document. These thresholds reject
# document-shaped content at the write path so reference docs, prompt files,
# and pasted essays never leak into the context-loaded store. Tune here.
MAX_MEMORY_TOKENS = 500        # hard cap per memory; over this = "store as a file"
MAX_MEMORY_NEWLINES = 15       # multi-section docs blow past this
MAX_MEMORY_H2_SECTIONS = 2     # >=2 "## " headers means it's a document


def guardrail_check(content: str) -> tuple[bool, str]:
    """Deterministic gate for the memory write path.

    Returns (ok, reason). ok=False means the content is document-shaped and
    should be stored as a file (or compressed into a fact), NOT inserted as a
    memory. Pure / zero-dependency so it is trivially unit-testable.
    """
    text = (content or "").strip()
    if not text:
        return False, "empty content"

    tc = _count_tokens(text)
    if tc > MAX_MEMORY_TOKENS:
        return False, (
            f"too long ({tc} tokens > {MAX_MEMORY_TOKENS} cap) — looks like a "
            f"document; store as a file or compress into a fact"
        )

    if text.count("\n") >= MAX_MEMORY_NEWLINES:
        return False, (
            f"too many lines ({text.count(chr(10))} >= {MAX_MEMORY_NEWLINES}) — "
            f"document-shaped, not a memory"
        )

    # Leading markdown title is the strongest document fingerprint.
    stripped = text.lstrip()
    if stripped.startswith(("# ", "## ", "### ")):
        return False, "starts with a markdown heading — looks like a document, not a memory"

    h2 = sum(1 for ln in text.splitlines() if ln.lstrip().startswith("## "))
    if h2 >= MAX_MEMORY_H2_SECTIONS:
        return False, (
            f"has {h2} section headers (>= {MAX_MEMORY_H2_SECTIONS}) — "
            f"multi-section document, not a memory"
        )

    return True, ""


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
