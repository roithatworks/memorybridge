"""MemoryBridge MCP Server v2.0 — SQLite + WAL edition
Local-first, cross-model memory portability

Phase 3 changes (zero breaking API changes):
- Persistence: memory.json → SQLite (WAL mode) via db/store.py
- Search: FTS5 BM25 replaces keyword overlap
- Access log: direct SQLite INSERT (WAL = fast) replaces buffered JSON writes
- Concurrency: SQLite locking replaces POSIX fcntl file locking
- Phase 2.5 wins retained: stripped search metadata, compact-equivalent I/O,
  content-hash dedup now enforced at DB level via UNIQUE index

Run with: fastmcp run server.py
"""

import json
import logging
import re
import os
import sys
import signal
import atexit
import time
import secrets
import threading
from pathlib import Path
from datetime import datetime
from typing import Optional
from contextvars import ContextVar
from fastmcp import FastMCP
import workspace
from db.pruner import run_auto_prune, record_outcome, get_pruner_report  # noqa: F401
from db.constants import VALID_CATEGORIES, IMPORTANCE_LEVELS, _content_hash, _count_tokens, effective_score  # noqa: F401
# Token counting, recency decay, and the model-export logic live in a
# store-free module so the UI can reuse them without importing this server
# module (which would build a second store + register atexit) — see #91.
from exports import (  # noqa: E402,F401
    count_tokens, apply_decay, DECAY_CONFIG,
    export_for_model as _export_for_model_impl,
    export_passport as _export_passport_impl,
    UNTRUSTED_NOTICE,
)

# Initialize MCP server
mcp = FastMCP("MemoryBridge")

# =============================================================================
# CONFIG
# =============================================================================
# Code/data split: code lives in the git repo (this file's directory);
# mutable state (db, analytics, inbox, .env, pid) lives in DATA_DIR.
# Override with MEMORYBRIDGE_DATA env var; defaults to ~/memorybridge.
CODE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("MEMORYBRIDGE_DATA", Path.home() / "memorybridge"))

# Allow `server.py` to remain a top-level file while serving as the package
# parent for `server.middleware` and `server.tools.*` submodules (#215).
__path__ = [str(CODE_DIR / "server")]
sys.modules.setdefault("server", sys.modules[__name__])

# Load .env from DATA_DIR so API keys live with the data, not the repo.
try:
    from dotenv import load_dotenv
    load_dotenv(DATA_DIR / ".env", override=False)
except (ImportError, OSError):
    pass

MEMORY_DB              = DATA_DIR / "memory.db"
# The active profile is a config contract (config.py): MEMORYBRIDGE_PROFILE env
# -> `profile:` in memorybridge.yaml -> "default". Resolve it, never hardcode the
# literal — a pinned profile silently hides every other one (#197). Imported here,
# after the .env load above, so a MEMORYBRIDGE_PROFILE set there still applies.
import config as _config  # noqa: E402
DEFAULT_PROFILE        = _config.profile()
_current_profile       = DEFAULT_PROFILE
# True only while serving over the HTTP bridge (remote clients). Gates the
# auto-pruner's delete path so a remote-origin write can never destroy a
# memory — candidates are routed to the review queue instead. See #37.
_REMOTE_MODE           = False
_BOUND_PROFILE: ContextVar[Optional[str]] = ContextVar("bound_profile", default=None)


def _active_profile() -> str:
    """Resolve the profile for a call that omitted one.

    If the current request is bound to a specific token profile via _BOUND_PROFILE,
    that profile is always returned.
    Over the HTTP bridge (`_REMOTE_MODE`), NEVER resolve through the mutable
    process-global `_current_profile`: it is shared across all concurrent
    remote requests, so one client's `switch_profile` would silently retarget
    every other client's reads/writes (#70). Remote requests default to
    DEFAULT_PROFILE; a remote client that needs a specific profile passes it
    explicitly. Local stdio (single session) keeps the switchable global.
    """
    bound = _BOUND_PROFILE.get()
    if bound:
        return bound
    return DEFAULT_PROFILE if _REMOTE_MODE else _current_profile


def _resolve_profile(profile: Optional[str] = None) -> str:
    """Resolve and enforce profile scoping for the current context.

    If the current request is authenticated with a token bound to a specific profile,
    any attempt to access a different profile raises PermissionError.
    If no explicit profile is passed, defaults to the bound profile (or _active_profile()).
    """
    bound = _BOUND_PROFILE.get()
    if bound:
        if profile and profile.strip() != bound:
            raise PermissionError(
                f"Access denied: token is bound to profile '{bound}', "
                f"cannot access profile '{profile}'"
            )
        return bound
    return profile or _active_profile()


def _caller_model() -> str:
    """Best-effort caller identity for analytics/provenance (#180)."""
    return "remote" if _REMOTE_MODE else "claude"


def _sanitize_client_name(client_name: Optional[str]) -> Optional[str]:
    """Clean a caller-supplied client_name (e.g. "hermes") before storage."""
    if not client_name:
        return None
    cleaned = re.sub(r"[^a-z0-9_-]", "", client_name.strip().lower())[:32]
    return cleaned or None


def _resolve_client_name(client_name: Optional[str]) -> Optional[str]:
    """Resolve the client_name to persist for an add_memory/add_memories call."""
    explicit = _sanitize_client_name(client_name)
    if explicit:
        return explicit
    return _sanitize_client_name(os.environ.get("MEMORYBRIDGE_CLIENT_NAME"))


_PROFILE_RE = re.compile(r"^[a-zA-Z0-9_.-]{1,64}$")


def _validate_profile(p: Optional[str]) -> str:
    """Validate and normalize a profile name."""
    bound = _BOUND_PROFILE.get()
    if bound:
        if p and p.strip().lower() != bound:
            raise PermissionError(
                f"Access denied: token is bound to profile '{bound}', "
                f"cannot access profile '{p}'"
            )
        return bound
    if not p:
        return _active_profile()
    cleaned = p.strip().lower()
    if not _PROFILE_RE.fullmatch(cleaned) or cleaned.startswith((".", "-")):
        return _active_profile()
    return cleaned


def _sanitize_why_it_matters(val: Optional[str]) -> Optional[str]:
    """Sanitize belief injection instruction text."""
    if not val:
        return None
    val = val.strip()
    if not val:
        return None
    val = val.replace("<<<", "").replace(">>>", "").strip()
    return val[:500] if val else None


MAX_TOKENS_DEFAULT        = 4000
SEARCH_LIMIT_DEFAULT      = 5
SEARCH_MAX_TOKENS_DEFAULT = 800
MAX_TOTAL_TOKENS          = _config.max_total_tokens()
ARCHIVE_SCORE_THRESHOLD   = 0.15

# PID file for duplicate-instance awareness
PID_DIR = DATA_DIR
_PID_FILE = PID_DIR / "instance.pid"


def _write_pid() -> None:
    """Write current PID to file, replacing any old one."""
    PID_DIR.mkdir(parents=True, exist_ok=True)
    _PID_FILE.write_text(str(os.getpid()))


def _cleanup_pid() -> None:
    """Remove PID file if we're still the one in it."""
    if _PID_FILE.exists() and _PID_FILE.read_text().strip() == str(os.getpid()):
        _PID_FILE.unlink(missing_ok=True)


def _sigterm_handler(signum, frame) -> None:
    """Handle SIGTERM: log clearly, exit cleanly."""
    pid_was = os.getpid()
    current_owner = _PID_FILE.read_text().strip() if _PID_FILE.exists() else None

    if current_owner and current_owner != str(pid_was):
        print(f"[memorybridge] Received SIGTERM — superseded by instance PID={current_owner}, exiting gracefully", file=sys.stderr)
    else:
        print("[memorybridge] Received SIGTERM — shutting down", file=sys.stderr)

    _cleanup_pid()
    sys.stderr.flush()
    _store.drain_embeds(timeout=3.0)
    os._exit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _sigterm_handler)
atexit.register(_cleanup_pid)


# =============================================================================
# STORE — SQLite singleton
# =============================================================================
from db.store import MemoryStore, GuardrailRejection, DuplicateContentError  # noqa: E402,F401
from db.entities import EntityExtractor  # noqa: E402

_entities_path = DATA_DIR / "entities.json"
_entity_extractor = EntityExtractor(
    config_path=_entities_path if _entities_path.exists() else None
)
_recency_decay_days = int(os.environ.get("MEMORYBRIDGE_RECENCY_DAYS", "30"))
_store = MemoryStore(
    MEMORY_DB,
    entity_extractor=_entity_extractor,
    recency_decay_days=_recency_decay_days,
)


def log_to_analytics(tokens_served: int, memories_returned: int,
                     model: str = "claude", profile: str = DEFAULT_PROFILE,
                     operation: str = "get_memory") -> None:
    """Write one analytics event directly to SQLite (issue #8: replaces buffered JSON)."""
    _store.log_analytics_event(
        tokens_served=tokens_served,
        memories_returned=memories_returned,
        model=model,
        profile=profile,
        operation=operation,
    )


# =============================================================================
# TOKEN MANAGEMENT & RESULT CLEANING
# =============================================================================

def count_memory_tokens(mem: dict) -> int:
    content = mem.get("content", "")
    tags = " ".join(mem.get("tags", []))
    return count_tokens(content) + count_tokens(tags) + 20


def compress_memory(mem: dict, target_tokens: int = 50) -> dict:
    compressed = mem.copy()
    content = compressed.get("content", "")
    current_tokens = count_tokens(content)
    if current_tokens <= target_tokens:
        return compressed
    sentences = re.split(r'(?<=[.!?])\s+', content)
    if len(sentences) > 1:
        truncated = sentences[0]
        if count_tokens(truncated) <= target_tokens:
            compressed["content"] = truncated
            compressed["compressed"] = True
            compressed["token_count"] = count_tokens(truncated) + 20
            compressed["content_hash"] = _content_hash(truncated)
            return compressed
    words = content.split()
    lo, hi = 3, len(words)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if count_tokens(" ".join(words[:mid])) <= target_tokens - 1:
            lo = mid
        else:
            hi = mid - 1
    new_content = " ".join(words[:lo]) + "…"
    compressed["content"] = new_content
    compressed["compressed"] = True
    compressed["token_count"] = count_tokens(new_content) + 20
    compressed["content_hash"] = _content_hash(new_content)
    return compressed


_RESULT_FIELDS = {"id", "content", "category", "importance",
                  "project_id", "tags", "token_count", "created_at", "client_name",
                  "confidence", "expires_at", "status", "source",
                  "why_it_matters", "origin_type", "origin_file",
                  "related", "edge_relation", "hop_distance", "graph_score",
                  "traversal_path", "connected_from"}


def _clean_result(mem: dict) -> dict:
    """Return only fields Claude needs — strip internal scoring metadata."""
    return {k: v for k, v in mem.items() if k in _RESULT_FIELDS}


def _start_parent_watchdog() -> None:
    """Exit if our parent (Claude Desktop's launcher) dies."""
    def _watch() -> None:
        while True:
            if os.getppid() == 1:
                print("[memorybridge] Parent process gone (reparented to PID 1) — exiting",
                      file=sys.stderr)
                _cleanup_pid()
                sys.stderr.flush()
                os._exit(0)
            time.sleep(5.0)

    threading.Thread(target=_watch, daemon=True, name="parent-watchdog").start()


# =============================================================================
# SUBMODULE REGISTRATION & RE-EXPORTS (#215)
# =============================================================================
from server.middleware import (  # noqa: E402,F401
    REMOTE_ALLOWED_TOOLS,
    _gate_tools_for_remote,
    _send_plain,
    _RateLimitAuthMiddleware,
    _run_http,
)
from server.tools.memory import (  # noqa: E402,F401
    get_memory,
    add_memory,
    add_memories,
    edit_memory,
    search_memory,
    reflect,
    consolidate_session,
    consolidate_memories,
    delete_memory,
    bulk_delete,
    get_token_stats,
    prune_memories,
    list_profiles,
    switch_profile,
    list_projects,
    get_access_log,
    get_prune_queue,
    resolve_prune_queue,
)
from server.tools.graph import (  # noqa: E402,F401
    add_memory_edge,
    get_memory_edges,
    traverse_memory_graph,
    mine_memory_relations,
    get_memory_provenance,
)
from server.tools.workspace import (  # noqa: E402,F401
    ws_status,
    ws_list,
    ws_read,
    ws_write,
    ws_search,
)
from server.tools.ingestion import (  # noqa: E402,F401
    export_for_model,
    export_passport,
    ingest_from_inbox,
)

# Backward compatibility for .fn access in tests and modules across FastMCP 2.x and 3.x
for _tool_name, _tool_obj in list(globals().items()):
    if callable(_tool_obj) and hasattr(_tool_obj, "__fastmcp__") and not hasattr(_tool_obj, "fn"):
        setattr(_tool_obj, "fn", _tool_obj)


if __name__ == "__main__":
    transport = os.environ.get("MEMORYBRIDGE_TRANSPORT", "stdio").lower()
    _write_pid()
    if transport == "http":
        try:
            _run_http()
        finally:
            print("[memorybridge] HTTP bridge stopped", file=sys.stderr)
            _cleanup_pid()
            sys.stderr.flush()
            os._exit(0)
    else:
        _start_parent_watchdog()
        try:
            mcp.run()
        finally:
            print("[memorybridge] MCP loop ended — exiting", file=sys.stderr)
            _cleanup_pid()
            sys.stderr.flush()
            os._exit(0)
