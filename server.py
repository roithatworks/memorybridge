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
from fastmcp import FastMCP
import workspace
from db.pruner import run_auto_prune, record_outcome, get_pruner_report
from db.constants import VALID_CATEGORIES, IMPORTANCE_LEVELS, _content_hash, _count_tokens, effective_score  # noqa: F401
# Token counting, recency decay, and the model-export logic live in a
# store-free module so the UI can reuse them without importing this server
# module (which would build a second store + register atexit) — see #91.
from exports import (  # noqa: E402
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

# Load .env from DATA_DIR so API keys live with the data, not the repo.
try:
    from dotenv import load_dotenv
    load_dotenv(DATA_DIR / ".env", override=False)
except (ImportError, OSError):
    pass

MEMORY_DB              = DATA_DIR / "memory.db"
DEFAULT_PROFILE        = "default"
_current_profile       = DEFAULT_PROFILE
# True only while serving over the HTTP bridge (remote clients). Gates the
# auto-pruner's delete path so a remote-origin write can never destroy a
# memory — candidates are routed to the review queue instead. See #37.
_REMOTE_MODE           = False


def _active_profile() -> str:
    """Resolve the profile for a call that omitted one.

    Over the HTTP bridge (`_REMOTE_MODE`), NEVER resolve through the mutable
    process-global `_current_profile`: it is shared across all concurrent
    remote requests, so one client's `switch_profile` would silently retarget
    every other client's reads/writes (#70). Remote requests default to
    DEFAULT_PROFILE; a remote client that needs a specific profile passes it
    explicitly. Local stdio (single session) keeps the switchable global.
    """
    return DEFAULT_PROFILE if _REMOTE_MODE else _current_profile


def _caller_model() -> str:
    """Best-effort caller identity for analytics/provenance (#180).

    The capability-URL auth scheme (see _RateLimitAuthMiddleware) is one
    shared secret for every remote client — there is no per-client credential
    to distinguish ChatGPT from Gemini from Perplexity. _REMOTE_MODE is the
    only real signal available today, so this can only say "claude" (stdio —
    provably true) or "remote" (HTTP bridge — true that it's non-Claude-Code,
    unknown which one). Previously every analytics event hardcoded "claude"
    regardless of transport, so the analytics table had no record that a
    non-Claude client had ever touched the system even when one demonstrably
    had. True per-model attribution needs F2 (per-client bearer tokens,
    docs/MULTI_MODEL_FEATURES.md) — this is the honest ceiling until then.
    """
    return "remote" if _REMOTE_MODE else "claude"


def _sanitize_client_name(client_name: Optional[str]) -> Optional[str]:
    """Clean a caller-supplied client_name (e.g. "hermes") before storage.

    Unlike _caller_model()'s source, this is self-reported and unverified —
    any caller can claim any name. Sanitizing keeps it a short identifying
    label rather than arbitrary text: lowercased, [a-z0-9_-] only, <=32 chars.
    Empty/whitespace-only/None all collapse to None (no label).
    """
    if not client_name:
        return None
    cleaned = re.sub(r"[^a-z0-9_-]", "", client_name.strip().lower())[:32]
    return cleaned or None


def _resolve_client_name(client_name: Optional[str]) -> Optional[str]:
    """Resolve the client_name to persist for an add_memory/add_memories call.

    An explicit per-call argument always wins. Otherwise falls back to
    MEMORYBRIDGE_CLIENT_NAME, a process-level default set on this specific
    spawned server.py instance's environment (e.g. via `hermes mcp add
    memorybridge --env MEMORYBRIDGE_CLIENT_NAME=hermes`).

    This is the durable alternative to relying on a prompt instruction
    (e.g. a HERMES.md rule telling an LLM caller to always pass
    client_name="hermes"): a prompt only takes effect if it's actually
    loaded for a given invocation and the LLM remembers to follow it on
    every call. An env var set once on the process is unconditional — every
    write from that spawned instance is labeled, with no cooperation
    required from whatever is calling the tool.
    """
    explicit = _sanitize_client_name(client_name)
    if explicit:
        return explicit
    return _sanitize_client_name(os.environ.get("MEMORYBRIDGE_CLIENT_NAME"))


MAX_TOKENS_DEFAULT     = 4000
SEARCH_LIMIT_DEFAULT   = 5
SEARCH_MAX_TOKENS_DEFAULT = 800
# Total-token ceiling is now configurable (config file `max_total_tokens` or
# MEMORYBRIDGE_MAX_TOKENS env); defaults to 50000 for a fresh install (#7).
import config as _config  # noqa: E402
MAX_TOTAL_TOKENS       = _config.max_total_tokens()
ARCHIVE_SCORE_THRESHOLD = 0.15

# DECAY_CONFIG, count_tokens, and apply_decay are imported from exports.py (#91).
# PID file for duplicate-instance awareness
PID_DIR = DATA_DIR
_PID_FILE = PID_DIR / "instance.pid"


def _write_pid() -> None:
    """Write current PID to file, replacing any old one.

    TOCTOU NOTE (Issue #17): this write is NOT atomic. Two instances starting
    simultaneously can both reach this line and overwrite each other's PID.
    That is acceptable — this file serves supersede-logging only (see
    _sigterm_handler), NOT mutual exclusion. SQLite's busy_timeout is the real
    arbiter for concurrent access. Never use the PID file for exclusion
    decisions.
    """
    PID_DIR.mkdir(parents=True, exist_ok=True)
    _PID_FILE.write_text(str(os.getpid()))


def _cleanup_pid() -> None:
    """Remove PID file if we're still the one in it."""
    if _PID_FILE.exists() and _PID_FILE.read_text().strip() == str(os.getpid()):
        _PID_FILE.unlink(missing_ok=True)


def _sigterm_handler(signum, frame) -> None:
    """Handle SIGTERM: log clearly, exit cleanly.

    Checks if a replacement instance has started (common when Claude Desktop
    spawns a new memorybridge for a new session). If this process has been
    superseded, we exit gracefully. Otherwise we log why we're shutting down.
    """
    pid_was = os.getpid()
    current_owner = _PID_FILE.read_text().strip() if _PID_FILE.exists() else None

    if current_owner and current_owner != str(pid_was):
        print(f"[memorybridge] Received SIGTERM — superseded by instance PID={current_owner}, exiting gracefully", file=sys.stderr)
    else:
        print(f"[memorybridge] Received SIGTERM — shutting down", file=sys.stderr)

    _cleanup_pid()
    sys.stderr.flush()
    # Drain pending embed threads so their SQLite writes land before
    # os._exit. Without this, daemon threads get killed mid-write and
    # the next startup backfills orphaned embeddings (issue #5).
    _store.drain_embeds(timeout=3.0)
    # os._exit, not sys.exit: sys.exit raises SystemExit and runs interpreter
    # finalizers, which deadlock on the stdin reader thread's buffer lock
    # (fatal "_enter_buffered_busy" crash seen 2026-06-03). State is already
    # flushed above; exit immediately without finalizers.
    os._exit(0)


# Register SIGTERM handler at module level (guarded for non-main imports
# such as Streamlit UI pages that import server functions).
if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _sigterm_handler)
atexit.register(_cleanup_pid)


# =============================================================================
# STORE — SQLite singleton
# =============================================================================
from db.store import MemoryStore, GuardrailRejection, DuplicateContentError  # noqa: E402
from db.entities import EntityExtractor  # noqa: E402

# Entity config: DATA_DIR/entities.json overrides defaults
_entities_path = DATA_DIR / "entities.json"
_entity_extractor = EntityExtractor(
    config_path=_entities_path if _entities_path.exists() else None
)
# Recency decay: env var or 30-day default
_recency_decay_days = int(os.environ.get("MEMORYBRIDGE_RECENCY_DAYS", "30"))
_store = MemoryStore(
    MEMORY_DB,
    entity_extractor=_entity_extractor,
    recency_decay_days=_recency_decay_days,
)


def log_to_analytics(tokens_served: int, memories_returned: int,
                     model: str = "claude", profile: str = "default",
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
# TOKEN MANAGEMENT
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
    # Fix #2: binary search the trim point instead of one-word-at-a-time O(n²) loop
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
    # Fix #1: recompute content_hash so round-trips don't create duplicates
    compressed["content_hash"] = _content_hash(new_content)
    return compressed


# =============================================================================
# RESULT CLEANING — Phase 2.5: strip internal scoring metadata
# =============================================================================

_RESULT_FIELDS = {"id", "content", "category", "importance",
                  "project_id", "tags", "token_count", "created_at", "client_name"}


def _clean_result(mem: dict) -> dict:
    """Return only fields Claude needs — strip internal scoring metadata."""
    return {k: v for k, v in mem.items() if k in _RESULT_FIELDS}


# =============================================================================
# MCP TOOLS
# =============================================================================

@mcp.tool()
def get_memory(
    profile: str = None,
    context_hint: Optional[str] = None,
    category: Optional[str] = None,
    max_tokens: int = MAX_TOKENS_DEFAULT,
    compress: bool = True
) -> str:
    """
    Retrieve relevant memory slice within token budget.

    Args:
        profile: Memory profile to load
        context_hint: Optional hint to filter relevant memory
        category: Optional category filter
        max_tokens: Maximum tokens to return (default 4000)
        compress: Whether to compress memories to fit budget (default True)
    Returns:
        JSON with memories, token stats, and budget info
    """
    profile = profile or _active_profile()
    _store.ensure_profile(profile)
    profile_data = _store.get_profile(profile)
    if profile_data is None:
        return json.dumps({"error": f"Profile '{profile}' not found"})

    if context_hint:
        # Use hybrid BM25+semantic search for context_hint so phrasing
        # variants (e.g. "job search" vs "Director+ PM role") are matched.
        # Merge with a full get_memories pull so non-hint memories fill the
        # remaining token budget in decay-score order.
        hint_results = _store.search_hybrid(
            profile, context_hint, category=category,
            limit=20, max_tokens=MAX_TOKENS_DEFAULT
        )
        hint_ids = {m["id"] for m in hint_results}

        # Full list for budget fill — apply decay, exclude hint hits (added first)
        all_memories = _store.get_memories(profile, category=category)
        all_memories = apply_decay([m.copy() for m in all_memories], DECAY_CONFIG)
        remainder = [m for m in all_memories if m["id"] not in hint_ids]
        remainder.sort(key=lambda m: m.get("effective_score", 0), reverse=True)

        memories = hint_results + remainder
    else:
        memories = _store.get_memories(profile, category=category)
        memories = apply_decay([m.copy() for m in memories], DECAY_CONFIG)
        memories.sort(key=lambda m: m.get("effective_score", 0), reverse=True)

    identity = profile_data["identity"]
    projects = profile_data["projects"]
    model_preferences = profile_data["model_preferences"]

    # #179: measure the real serialized cost of everything fixed in the
    # response besides the memories array, rather than summing
    # identity/projects/model_preferences measured compact (no indent, no
    # surrounding keys) plus a flat 200-token guess for the gap. That guess
    # was occasionally too small against real profile data — e.g. an 8-token
    # overshoot at max_tokens=1000 against production identity/projects data,
    # even after the per-memory fix below closed the much larger ~1.5x gap.
    # Build the actual empty-memories skeleton and measure it directly; the
    # +50 covers only the small digit-width variance in token_stats' own
    # placeholder fields, not a structural guess.
    skeleton = {
        "profile": profile,
        "identity": identity,
        "memories": [],
        "projects": projects,
        "model_preferences": model_preferences,
        "token_stats": {
            "budget": max_tokens,
            "served": 0,
            "remaining": 0,
            "memories_returned": 0,
            "memories_available": len(memories),
            "compressed_count": 0,
            "overhead_tokens": 0,
        },
    }
    overhead_tokens = (count_tokens(json.dumps(skeleton, indent=2)) +
                       count_tokens(UNTRUSTED_NOTICE) + 50)
    available_for_memories = max(max_tokens - overhead_tokens, 0)

    # #179: budget against the REAL serialized cost of each memory as it will
    # actually appear in the response — id/category/importance/project_id/
    # tags/token_count/created_at fields plus indent=2 whitespace — not the
    # DB's content-only token_count. Summing per-memory estimates that don't
    # account for JSON structure or tags under-counted the true payload by up
    # to ~1.5x at the default budget; measuring what will actually be
    # serialized fixes that at the source instead of patching the estimate.
    selected_memories = []
    tokens_used = 0
    for mem in memories:
        mem_tokens = count_tokens(json.dumps(_clean_result(mem), indent=2))
        if tokens_used + mem_tokens <= available_for_memories:
            selected_memories.append(mem)
            tokens_used += mem_tokens
        elif compress and tokens_used < available_for_memories:
            remaining = available_for_memories - tokens_used
            compressed = compress_memory(mem, target_tokens=remaining - 20)
            compressed_tokens = count_tokens(json.dumps(_clean_result(compressed), indent=2))
            if compressed_tokens <= remaining:
                selected_memories.append(compressed)
                tokens_used += compressed_tokens
                break
        else:
            break

    def _build_response(mems):
        r = {
            "profile": profile,
            "identity": identity,
            "memories": [_clean_result(m) for m in mems],
            "projects": projects,
            "model_preferences": model_preferences,
            "token_stats": {
                "budget": max_tokens,
                "served": 0,       # filled in below from the real serialized size
                "remaining": 0,
                "memories_returned": len(mems),
                "memories_available": len(memories),
                "compressed_count": sum(1 for m in mems if m.get("compressed")),
                "overhead_tokens": overhead_tokens
            }
        }
        # Untrusted-data framing (#178): the memories above are content, not
        # instructions — user-written notes, ingested excerpts, or memories
        # written by another model over the HTTP bridge. Added as a field
        # rather than mutating each memory's "content" (the Streamlit UI
        # displays that value verbatim; literal delimiter tags would leak
        # into it).
        if mems:
            r["_security_notice"] = UNTRUSTED_NOTICE
        return r

    response = _build_response(selected_memories)

    # #179 backstop: the greedy loop above measures each memory's cost in
    # isolation, but its REAL marginal cost once embedded in the memories
    # array differs slightly — one more level of indent nesting, the array's
    # comma separators — which compounds across many selected items (measured
    # 49 tokens over budget at max_tokens=8000 with 21 memories, even after
    # the per-item fix above). Trim the lowest-ranked (last-selected) memory
    # until the TRUE fully-assembled response actually fits, rather than
    # trusting the isolated-item estimate to have gotten it exactly right.
    while selected_memories and count_tokens(json.dumps(response, indent=2)) > max_tokens:
        selected_memories.pop()
        response = _build_response(selected_memories)

    # #179: report what we're ACTUALLY about to return, measured directly,
    # rather than a sum of the same per-memory estimates used for selection.
    # served/remaining start as placeholders above; filling in their real
    # digit-width can shift the token count by one (a run of digit characters
    # can cross a BPE merge boundary), so re-measure once against the filled-in
    # response rather than trusting the placeholder-based measurement — this
    # must be exact, not merely close, since it's what the caller is told.
    total_tokens_served = count_tokens(json.dumps(response, indent=2))
    response["token_stats"]["served"] = total_tokens_served
    response["token_stats"]["remaining"] = max(max_tokens - total_tokens_served, 0)
    total_tokens_served = count_tokens(json.dumps(response, indent=2))
    response["token_stats"]["served"] = total_tokens_served
    response["token_stats"]["remaining"] = max(max_tokens - total_tokens_served, 0)

    _store.log_access("get_memory", profile,
                      f"hint={context_hint}, cat={category}, budget={max_tokens}",
                      tokens_served=total_tokens_served)
    log_to_analytics(
        tokens_served=total_tokens_served,
        memories_returned=len(selected_memories),
        model=_caller_model(),
        profile=profile,
        operation="get_memory"
    )
    return json.dumps(response, indent=2)


@mcp.tool()
def add_memory(
    content: str,
    category: str = "fact",
    importance: str = "medium",
    tags: list[str] = None,
    project_id: Optional[str] = None,
    profile: str = None,
    supersedes: list[str] = None,
    client_name: Optional[str] = None
) -> str:
    """
    Add a new memory with automatic token counting and content-hash dedup.

    Args:
        content: The memory content to store
        category: Category type
        importance: Importance level
        tags: Optional tags
        project_id: Optional project association
        profile: Memory profile
        supersedes: Memory IDs this new fact REPLACES because the underlying
            fact changed (e.g. a job change, a moved deadline). Each is archived
            and stamped with a valid_until timestamp so it leaves normal recall
            but remains as history. Use for facts that changed, not rewordings.
        client_name: Optional self-reported caller label (e.g. "hermes") for
            multi-agent setups sharing this store over stdio. Distinct from
            the transport-derived source field — unverified, sanitized to
            lowercase [a-z0-9_-], max 32 chars. If omitted, falls back to
            the MEMORYBRIDGE_CLIENT_NAME env var set on this process (a
            durable per-instance default that doesn't depend on the caller
            remembering to pass this argument).
    Returns:
        Confirmation with memory ID and token count, or duplicate status
    """
    profile = profile or _active_profile()
    if category not in VALID_CATEGORIES:
        return json.dumps({"error": f"Invalid category. Valid: {VALID_CATEGORIES}"})
    if importance not in IMPORTANCE_LEVELS:
        return json.dumps({"error": f"Invalid importance. Valid: {IMPORTANCE_LEVELS}"})

    try:
        mid = _store.add_memory(profile, content,
                                category=category, importance=importance,
                                tags=tags, project_id=project_id,
                                supersedes=supersedes, source=_caller_model(),
                                client_name=_resolve_client_name(client_name))
    except GuardrailRejection as e:
        # Document-shaped content: return the structured error contract every
        # other validation path uses, instead of surfacing an unhandled MCP error.
        return json.dumps({"status": "rejected", "reason": str(e)})
    if mid is None:
        return json.dumps({"status": "duplicate", "reason": "identical content already exists"})

    # Report exactly what the store persisted (db.constants._count_tokens on the
    # content), not a different formula — the two used to diverge (#55).
    token_count = _count_tokens(content)

    # Budget-based prune (existing behaviour)
    stats = _store.token_stats(profile)
    budget_pruned = []
    if stats["total_tokens"] > MAX_TOTAL_TOKENS:
        budget_pruned = _store.auto_prune(profile, threshold=ARCHIVE_SCORE_THRESHOLD)

    # Adaptive dedup/staleness prune. Over the remote bridge, never auto-delete:
    # route candidates to the review queue so a remote write can't destroy data.
    prune_result = run_auto_prune(_store._conn, profile, _store.delete_memory,
                                  allow_auto_delete=not _REMOTE_MODE)

    _store.log_access("add_memory", profile, f"id={mid}, tokens={token_count}")

    result = {
        "status": "added",
        "memory_id": mid,
        "category": category,
        "importance": importance,
        "token_count": token_count,
        "profile": profile
    }
    if budget_pruned:
        result["budget_pruned"] = budget_pruned
        result["prune_reason"] = f"Total tokens exceeded {MAX_TOTAL_TOKENS}"
    if prune_result["auto_executed"]:
        result["auto_pruned"] = prune_result["auto_executed"]
    if prune_result["queued"]:
        result["prune_queued"] = prune_result["queued"]
    return json.dumps(result, indent=2)


@mcp.tool()
def add_memories(
    facts: list[str],
    category: str = "fact",
    importance: str = "medium",
    project: Optional[str] = None,
    profile: str = None,
    client_name: Optional[str] = None
) -> str:
    """
    BATCH-ADD operation -- inserts multiple new memory rows. This does NOT edit
    or mutate existing memories. Each fact in the list is inserted as a new row;
    duplicate content (same content_hash) is silently skipped.

    To edit an existing memory in place, use edit_memory(memory_id=...) instead.

    Args:
        facts: List of facts to remember (each becomes a new memory row)
        category: Category for all facts
        importance: Importance level for all facts
        project: Optional project association
        profile: Memory profile
        client_name: Optional self-reported caller label (e.g. "hermes") —
            see add_memory's client_name for details.
    Returns:
        Summary with all added memory IDs and total tokens
    """
    profile = profile or _active_profile()
    if category not in VALID_CATEGORIES:
        return json.dumps({"error": f"Invalid category. Valid: {VALID_CATEGORIES}"})
    if importance not in IMPORTANCE_LEVELS:
        return json.dumps({"error": f"Invalid importance. Valid: {IMPORTANCE_LEVELS}"})
    if not facts:
        return json.dumps({"error": "facts list is empty"})

    changes = []
    rejected = []
    duplicates = 0
    total_tokens = 0

    # Per-fact isolation: a single guardrail rejection must NOT abort the batch
    # and silently drop the remaining facts. Catch it, record it, keep going, and
    # report added/duplicate/rejected counts honestly so callers (e.g. the
    # ingestion merger) never see a false "0 added" on a partial success.
    for fact in facts:
        try:
            mid = _store.add_memory(profile, fact,
                                    category=category, importance=importance,
                                    project_id=project, source=_caller_model(),
                                    client_name=_resolve_client_name(client_name))
        except GuardrailRejection as e:
            rejected.append({
                "reason": str(e),
                "preview": fact[:60] + ("…" if len(fact) > 60 else "")
            })
            continue
        if mid is None:
            duplicates += 1
            continue
        token_count = count_tokens(fact) + 20
        total_tokens += token_count
        changes.append({
            "memory_id": mid,
            "tokens": token_count,
            "preview": fact[:60] + ("…" if len(fact) > 60 else "")
        })

    # Auto-prune if over budget
    pruned = []
    stats = _store.token_stats(profile)
    if stats["total_tokens"] > MAX_TOTAL_TOKENS:
        pruned = _store.auto_prune(profile, threshold=ARCHIVE_SCORE_THRESHOLD)

    _store.log_access(
        "add_memories", profile,
        f"added {len(changes)}, duplicate {duplicates}, rejected {len(rejected)}, "
        f"{total_tokens} tokens")

    return json.dumps({
        "status": "updated",
        "profile": profile,
        "count": len(changes),
        "duplicate_count": duplicates,
        "rejected_count": len(rejected),
        "total_tokens_added": total_tokens,
        "changes": changes,
        "rejected": rejected if rejected else None,
        "auto_pruned": pruned if pruned else None,
        "timestamp": datetime.now().isoformat()
    }, indent=2)


@mcp.tool()
def edit_memory(
    memory_id: str,
    content: Optional[str] = None,
    importance: Optional[str] = None,
    category: Optional[str] = None,
    project: Optional[str] = None,
    profile: str = None
) -> str:
    """
    Edit an existing memory in place by memory_id.

    Only the fields you supply are changed — omitted fields are left untouched.
    If content is updated, content_hash and token_count are recomputed automatically.

    Args:
        memory_id: ID of the memory to edit (e.g. "mem_abc12345")
        content: New content text (optional)
        importance: New importance level — low / medium / high / critical (optional)
        category: New category (optional)
        project: New project association (optional)
        profile: Memory profile the memory belongs to
    Returns:
        JSON confirmation, or {"error": ...} if memory_id not found / validation fails
    """
    profile = profile or _active_profile()
    if importance is not None and importance not in IMPORTANCE_LEVELS:
        return json.dumps({"error": f"Invalid importance. Valid: {IMPORTANCE_LEVELS}"})
    if category is not None and category not in VALID_CATEGORIES:
        return json.dumps({"error": f"Invalid category. Valid: {VALID_CATEGORIES}"})

    kwargs = {}
    if content is not None:
        kwargs["content"] = content
    if importance is not None:
        kwargs["importance"] = importance
    if category is not None:
        kwargs["category"] = category
    if project is not None:
        kwargs["project_id"] = project

    try:
        updated = _store.edit_memory(profile, memory_id, **kwargs)
    except DuplicateContentError as e:
        return json.dumps({"error": f"duplicate content: {e}"})
    if not updated:
        return json.dumps({"error": f"memory_id '{memory_id}' not found in profile '{profile}'"})

    _store.log_access("edit_memory", profile, f"id={memory_id}, fields={list(kwargs.keys())}")
    return json.dumps({
        "status": "updated",
        "memory_id": memory_id,
        "profile": profile,
        "fields_changed": list(kwargs.keys())
    }, indent=2)


@mcp.tool()
def search_memory(
    query: str,
    category: Optional[str] = None,
    limit: int = SEARCH_LIMIT_DEFAULT,
    max_tokens: int = SEARCH_MAX_TOKENS_DEFAULT,
    profile: str = None,
    recency_boost: bool = True,
    include_related: bool = False,
) -> str:
    """
    Search memories using FTS5 BM25 with optional token budget.

    Args:
        query: Search query
        category: Optional category filter
        limit: Max results (default 5)
        max_tokens: Token cap (default 800)
        profile: Memory profile
        recency_boost: Apply recency weighting (default: true when configured)
        include_related: Include related memories by entity tag overlap (default: false)
    Returns:
        JSON with ranked results (internal fields stripped)
    """
    profile = profile or _active_profile()
    _store.ensure_profile(profile)

    if category and category not in VALID_CATEGORIES:
        return json.dumps({"error": f"Invalid category. Valid: {VALID_CATEGORIES}"})

    # Phase 4: hybrid BM25 + semantic search (falls back to FTS5 if no embeddings built)
    results = _store.search_hybrid(profile, query, category=category,
                                   limit=limit, max_tokens=max_tokens,
                                   recency_boost=recency_boost,
                                   include_related=include_related)

    # Boost relevance score for all returned memories in a single commit (issue #12)
    _store.boost_batch(profile, [m["id"] for m in results],
                       boost=DECAY_CONFIG.get("boost_on_access", 0.1))

    response = {
        "query": query,
        "profile": profile,
        "results": [_clean_result(m) for m in results],
        "total_matches": len(results),
        "tokens_served": 0,     # filled in below from the real serialized size
    }
    # Untrusted-data framing (#178) — see the matching comment in get_memory.
    if results:
        response["_security_notice"] = UNTRUSTED_NOTICE

    # #179: same fix as get_memory — report the actual serialized cost
    # (fields + tags + indent=2 whitespace + the security notice), not a sum
    # of the DB's content-only token_count per result. Two passes: filling in
    # the real digit width after the first measurement can shift the count by
    # one (see the matching comment in get_memory).
    tokens_served = count_tokens(json.dumps(response, indent=2))
    response["tokens_served"] = tokens_served
    tokens_served = count_tokens(json.dumps(response, indent=2))
    response["tokens_served"] = tokens_served

    _store.log_access("search_memory", profile,
                      f"query='{query}', results={len(results)}", tokens_served)
    log_to_analytics(
        tokens_served=tokens_served,
        memories_returned=len(results),
        model=_caller_model(),
        profile=profile,
        operation="search_memory"
    )
    return json.dumps(response, indent=2)


@mcp.tool()
def reflect(
    question: str,
    profile: str = None,
    limit: int = 15,
    max_tokens: int = 3000,
) -> str:
    """
    Synthesize a reasoned answer from memories.

    Retrieves relevant memories, groups by entity tag, and produces a
    structured synthesis (key facts, dates, preferences, contradictions,
    confidence). Uses keyword-based fallback when no LLM is configured.

    Args:
        question: The question to reflect on
        profile: Memory profile (default: current)
        limit: Max memories to consider (default 15)
        max_tokens: Token cap for memory context (default 3000)
    Returns:
        JSON with structured synthesis
    """
    profile = profile or _active_profile()
    _store.ensure_profile(profile)

    result = _store.reflect(profile, question, limit=limit, max_tokens=max_tokens)
    return json.dumps(result, indent=2, default=str)


@mcp.tool()
def delete_memory(
    memory_id: str,
    profile: str = None
) -> str:
    """Delete a specific memory by ID."""
    profile = profile or _active_profile()
    tokens_freed = _store.delete_memory(profile, memory_id)
    if tokens_freed == 0:
        # Check if profile even exists
        if _store.get_profile(profile) is None:
            return json.dumps({"error": f"Profile '{profile}' not found"})
        return json.dumps({"error": f"Memory '{memory_id}' not found"})

    _store.log_access("delete_memory", profile,
                      f"id={memory_id}, freed={tokens_freed} tokens")
    return json.dumps({
        "status": "deleted",
        "memory_id": memory_id,
        "tokens_freed": tokens_freed,
        "profile": profile
    }, indent=2)


@mcp.tool()
def get_token_stats(profile: str = None) -> str:
    """
    Get comprehensive token usage statistics.

    Args:
        profile: Profile to analyze (or 'all' for global stats)
    Returns:
        Token usage breakdown
    """
    if profile is None:
        profile = _current_profile
    if profile == "all":
        all_profiles = {}
        total_stored = 0
        for p_name in _store.list_profiles():
            stats = _store.token_stats(p_name)
            all_profiles[p_name] = stats
            total_stored += stats["total_tokens"]

        token_summary = _store.get_access_log_token_summary()

        return json.dumps({
            "global": {
                "total_tokens_stored": total_stored,
                "total_tokens_served": token_summary["total_served"],
                "max_budget": MAX_TOTAL_TOKENS,
                "utilization": f"{(total_stored / MAX_TOTAL_TOKENS) * 100:.1f}%"
            },
            "by_profile": all_profiles,
            "served_by_profile": token_summary["by_profile"]
        }, indent=2)

    _store.ensure_profile(profile)
    stats = _store.token_stats(profile)
    archived_row = _store.get_archived_stats(profile)
    total_served = _store.get_tokens_served(profile)

    return json.dumps({
        "profile": profile,
        "active": stats,
        "archived": {
            "count": archived_row["count"],
            "tokens": archived_row["tokens"]
        },
        "served_total": total_served,
        "budget": {
            "max": MAX_TOTAL_TOKENS,
            "used": stats["total_tokens"],
            "remaining": MAX_TOTAL_TOKENS - stats["total_tokens"],
            "utilization": f"{(stats['total_tokens'] / MAX_TOTAL_TOKENS) * 100:.1f}%"
        }
    }, indent=2)


@mcp.tool()
def prune_memories(
    profile: str = None,
    threshold: Optional[float] = None,
    dry_run: bool = False
) -> str:
    """
    Manually prune low-score memories to free token budget.

    Args:
        profile: Profile to prune
        threshold: Score threshold (default 0.15)
        dry_run: If True, show what would be pruned without doing it
    Returns:
        List of pruned/would-prune memories
    """
    profile = profile or _active_profile()
    _store.ensure_profile(profile)
    threshold = threshold or ARCHIVE_SCORE_THRESHOLD

    if dry_run:
        memories = _store.get_memories(profile)
        memories = apply_decay([m.copy() for m in memories], DECAY_CONFIG)
        to_prune = [m for m in memories if m.get("effective_score", 1.0) < threshold]
        tokens_to_free = sum(m.get("token_count", count_memory_tokens(m)) for m in to_prune)
        return json.dumps({
            "dry_run": True,
            "would_prune": len(to_prune),
            "tokens_would_free": tokens_to_free,
            "memories": [
                {"id": m["id"], "score": m.get("effective_score"),
                 "content": m["content"][:50]}
                for m in to_prune
            ]
        }, indent=2)

    pruned_ids = _store.auto_prune(profile, threshold=threshold)
    tokens_freed = 0  # store archives them; exact freed tokens not tracked here
    _store.log_access("prune_memories", profile,
                      f"pruned {len(pruned_ids)}")
    return json.dumps({
        "status": "pruned",
        "pruned_count": len(pruned_ids),
        "pruned_ids": pruned_ids,
        "profile": profile
    }, indent=2)


@mcp.tool()
def list_profiles() -> str:
    """
    List available profile names. Read-only — does not switch the active
    profile (see switch_profile for that; it stays local-only).

    Remote clients are pinned to the default profile when no `profile`
    argument is given, but tools like get_memory/search_memory DO honor an
    explicit profile= argument remotely. This lets a remote client discover
    what profile names exist to pass, instead of guessing blind (#180).

    Returns:
        JSON with the list of profile names and which one is the default.
    """
    return json.dumps({
        "profiles": _store.list_profiles(),
        "default_profile": DEFAULT_PROFILE,
    }, indent=2)


@mcp.tool()
def switch_profile(profile_name: str) -> str:
    """Switch active persona context."""
    global _current_profile
    profile_data = _store.get_profile(profile_name)
    if profile_data is None:
        available = _store.list_profiles()
        return json.dumps({
            "error": f"Profile '{profile_name}' not found",
            "available_profiles": available
        })

    _current_profile = profile_name
    stats = _store.token_stats(profile_name)
    _store.log_access("switch_profile", profile_name, "")
    return json.dumps({
        "status": "switched",
        "profile": profile_name,
        "identity": profile_data["identity"],
        "memory_count": stats["memory_count"],
        "project_count": len(profile_data["projects"]),
        "token_stats": stats
    }, indent=2)


@mcp.tool()
def list_projects(profile: str = None) -> str:
    """List all projects with status."""
    profile = profile or _active_profile()
    _store.ensure_profile(profile)
    profile_data = _store.get_profile(profile)
    if profile_data is None:
        return json.dumps({"error": f"Profile '{profile}' not found"})

    projects = profile_data.get("projects", [])
    # Guard against malformed project entries: the projects column is free-form
    # JSON, so a bare string would make p.get(...) raise AttributeError and 500
    # the whole tool. Skip non-dict entries instead (#124).
    summary = [
        {
            "id": p.get("id"),
            "name": p.get("name"),
            "status": p.get("status"),
            "phase": p.get("phase"),
            "last_updated": p.get("last_updated")
        }
        for p in projects
        if isinstance(p, dict)
    ]
    _store.log_access("list_projects", profile, "")
    return json.dumps({
        "profile": profile,
        "projects": summary,
        "count": len(summary)
    }, indent=2)


@mcp.tool()
def get_access_log(limit: int = 50, include_tokens: bool = True) -> str:
    """
    Retrieve access log with token usage.

    Args:
        limit: Number of entries (default 50)
        include_tokens: Include token stats summary
    Returns:
        Recent access log entries and optional token summary
    """
    entries = _store.get_access_log(limit=limit)
    result = {
        "entries": entries,
        "total_logged": _store.get_access_log_count(),
        "returned": len(entries)
    }
    if include_tokens:
        token_summary = _store.get_access_log_token_summary()
        result["token_summary"] = token_summary
    return json.dumps(result, indent=2)


@mcp.tool()
def get_prune_queue(
    profile: str = None,
    include_report: bool = True
) -> str:
    """
    Return pending prune queue items awaiting human review, plus pruner health report.

    Args:
        profile: Memory profile
        include_report: Include full pruner activity report (default True)
    Returns:
        JSON with pending queue items and optional pruner report
    """
    profile = profile or _active_profile()
    _store.ensure_profile(profile)
    from db.pruner import get_pruner_report
    report = get_pruner_report(_store._conn, since_days=7) if include_report else {}

    return json.dumps({
        "profile": profile,
        "pending_count": report.get("pending_queue_count", 0),
        "pending_queue": report.get("pending_queue", []),
        "pruner_report": report if include_report else None,
    }, indent=2)


@mcp.tool()
def resolve_prune_queue(
    queue_id: str,
    approved: bool,
    profile: str = None
) -> str:
    """
    Approve or reject a queued prune candidate.
    Approval deletes the memory. Either outcome updates rule confidence.

    Args:
        queue_id: The prune_queue item ID (starts with 'pq_')
        approved: True to delete the memory, False to keep it
        profile: Memory profile
    Returns:
        Outcome with tokens freed and updated confidence info
    """
    profile = profile or _active_profile()
    _store.ensure_profile(profile)
    result = record_outcome(_store._conn, queue_id, approved, _store.delete_memory)

    if "error" in result:
        return json.dumps(result)

    # Return updated rule confidence after recalibration
    from db.pruner import AUTO_EXECUTE_THRESHOLD
    rule_row = _store.get_rule_confidence_after(queue_id)

    result["rule_confidence_after"] = round(rule_row["confidence"], 3) if rule_row else None
    result["auto_executes_now"] = (
        rule_row["confidence"] >= AUTO_EXECUTE_THRESHOLD if rule_row else None
    )
    _store.log_access("resolve_prune_queue", profile,
                      f"queue_id={queue_id}, approved={approved}")
    return json.dumps(result, indent=2)


@mcp.tool()
def export_for_model(
    model: str,
    profile: str = None,
    depth: str = "full",
    max_tokens: int = 2000
) -> str:
    """
    Export memory formatted for specific model, within token budget.

    Args:
        model: Target model (chatgpt, gemini, ollama)
        profile: Memory profile to export
        depth: Export depth (full, summary, minimal)
        max_tokens: Token budget for export (default 2000)
    """
    profile = profile or _active_profile()
    return _export_for_model_impl(
        _store, model, profile, depth=depth, max_tokens=max_tokens,
        log_analytics=log_to_analytics,
    )


@mcp.tool()
def export_passport(
    profile: str = None,
    max_tokens: int = 2000,
) -> str:
    """
    Export memory as a portable plain-text Memory Passport.

    The passport is model-agnostic — suitable for pasting into any AI's system
    prompt or context window.  Format: plain text, category sections, no JSON,
    no code fences.

    Args:
        profile:    Memory profile to export (default: "default")
        max_tokens: Token ceiling for the passport (default: 2000)
    Returns:
        Plain-text Memory Passport string.
    """
    profile = profile or _active_profile()
    return _export_passport_impl(
        _store, profile, max_tokens=max_tokens, log_analytics=log_to_analytics,
    )


# --------------------------------------------------------------------------- #
# ws_* workspace tools (F1) — sandboxed scratch/reference filesystem for
# non-Claude-Code MCP clients (ChatGPT, Gemini, Perplexity). See F1/F4 in
# docs/MULTI_MODEL_FEATURES.md. Implementation lives in workspace.py; these
# are thin @mcp.tool() wrappers, same pattern as get_memory/add_memory above.
# --------------------------------------------------------------------------- #

@mcp.tool()
def ws_status() -> str:
    """
    Report the workspace root, file count, and current write allowlist.

    Returns:
        JSON with root path, file_count, and write_allowed prefixes.
    """
    return json.dumps(workspace.ws_status(), indent=2)


@mcp.tool()
def ws_list(path: str = "", recursive: bool = False) -> str:
    """
    List entries under the workspace (or a subdirectory of it).

    Args:
        path: Subdirectory relative to the workspace root (default: root)
        recursive: If True, list all nested entries, not just the top level
    Returns:
        JSON with the listed path and its entries.
    """
    return json.dumps(workspace.ws_list(path, recursive=recursive), indent=2)


@mcp.tool()
def ws_read(path: str) -> str:
    """
    Read a text file from the workspace.

    Args:
        path: File path relative to the workspace root
    Returns:
        JSON with the file content, or a structured error (not found, binary).
    """
    return json.dumps(workspace.ws_read(path), indent=2)


@mcp.tool()
def ws_write(path: str, content: str, overwrite: bool = False) -> str:
    """
    Write a text file to the workspace. Deny-by-default: only paths under a
    configured `workspace_write_allowed` prefix may be written.

    Over the HTTP bridge (remote mode), overwrite=True is required even for
    a brand-new file — see F4 in docs/MULTI_MODEL_FEATURES.md.

    Args:
        path: File path relative to the workspace root
        content: Text content to write
        overwrite: Required to replace an existing file (or, remotely, to write at all)
    Returns:
        JSON with bytes_written, or a structured error.
    """
    return json.dumps(workspace.ws_write(path, content, overwrite=overwrite), indent=2)


@mcp.tool()
def ws_search(query: str, path: str = "") -> str:
    """
    Search workspace text files for a substring match.

    Args:
        query: Plain substring to search for
        path: Subdirectory relative to the workspace root (default: whole root)
    Returns:
        JSON with the query and the list of matching file paths.
    """
    return json.dumps(workspace.ws_search(query, path), indent=2)


@mcp.tool()
def ingest_from_inbox(
    profile: str = None,
    preview: bool = False
) -> str:
    """
    Process any export files sitting in ~/memorybridge/inbox/.

    Drop a ChatGPT conversations.json, Gemini MyActivity.json, or Claude
    export into that folder, then call this tool to ingest it.  Files are
    auto-detected by format, ingested via the standard pipeline, and moved
    to inbox/processed/ on success or inbox/failed/ on error.

    Args:
        profile: Memory profile to write to (default: "default")
        preview: If True, detect and report files without writing memories
    Returns:
        JSON with counts of processed/failed files and per-file results
    """
    import subprocess
    import sys

    # Normalize like every other tool — otherwise a default call passes
    # profile=None straight into the subprocess argv (TypeError / a profile
    # literally named "None").
    profile = profile or _active_profile()

    inbox = DATA_DIR / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)

    # Detect eligible files first so we can report even if watcher errors
    files = sorted(f for f in inbox.iterdir()
                   if f.is_file() and f.suffix.lower() == ".json"
                   and f.parent == inbox)  # skip processed/ and failed/ subdirs

    if not files:
        return json.dumps({
            "status": "empty",
            "message": f"No files in inbox. Drop a ChatGPT, Gemini, or Claude export into {inbox}/ and call this again.",
            "inbox": str(inbox)
        }, indent=2)

    # Watcher lives with the code (this repo), not the data dir
    watcher_script = CODE_DIR / "ingestion" / "watcher.py"
    cmd = [
        sys.executable,
        str(watcher_script),
        "--inbox", str(inbox),
        "--profile", profile,
    ]
    if preview:
        cmd.append("--preview")

    start = datetime.now()
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=600,
            cwd=str(CODE_DIR),
            env={
                **os.environ,
                "HOME": str(Path.home()),
                "MEMORYBRIDGE_DATA": str(DATA_DIR),
                # Include homebrew + user bins (Apple Silicon brew is /opt/homebrew)
                "PATH": os.environ.get(
                    "PATH",
                    "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
                ),
            }
        )
        elapsed = (datetime.now() - start).total_seconds()

        stdout_lines = result.stdout.strip().splitlines() if result.stdout else []
        stderr_lines = result.stderr.strip().splitlines() if result.stderr else []

        # Parse processed/failed/skipped counts from watcher JSON stdout
        processed = failed = skipped = 0
        for line in stdout_lines:
            try:
                summary = json.loads(line)
                if isinstance(summary, dict) and "processed" in summary:
                    processed = summary.get("processed", 0)
                    failed = summary.get("failed", 0)
                    skipped = summary.get("skipped", 0)
                    break
            except (json.JSONDecodeError, ValueError):
                continue

        _store.log_access("ingest_from_inbox", profile,
                          f"files={len(files)}, processed={processed}, failed={failed}")

        return json.dumps({
            "status": "ok" if result.returncode == 0 else "error",
            "files_found": len(files),
            "files_processed": processed,
            "files_failed": failed,
            "elapsed_seconds": round(elapsed, 1),
            "preview": preview,
            "profile": profile,
            "log": stdout_lines[-20:] if stdout_lines else [],
            "errors": stderr_lines[-10:] if stderr_lines else [],
            "exit_code": result.returncode,
        }, indent=2)

    except subprocess.TimeoutExpired:
        return json.dumps({
            "status": "timeout",
            "message": "Ingestion timed out after 600s — large export? Try running manually: python ingestion/watcher.py",
            "files_found": len(files),
        }, indent=2)
    except Exception as e:
        return json.dumps({
            "status": "error",
            "message": str(e),
            "files_found": len(files),
        }, indent=2)


def _start_parent_watchdog() -> None:
    """Exit if our parent (Claude Desktop's launcher) dies.

    A stdio MCP server must not outlive its client. Two failure modes leave
    orphans: (1) client crashes without sending SIGTERM, (2) stdio loop stops
    on stdin EOF but non-daemon threads (e.g. ONNX/FastEmbed workers) keep the
    process alive. This watchdog polls PPID; if we've been reparented to
    launchd/init (PPID 1), the client is gone — flush and exit hard.
    """
    import threading
    import time

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
# REMOTE BRIDGE (HTTP transport) — ChatGPT / Perplexity / Gemini CLI
# =============================================================================
# Remote clients get read + add only. A prompt-injected or confused remote
# model must not be able to destroy or overwrite memories; destructive and
# subprocess-spawning tools stay stdio/Claude-local.
#   - edit_memory: removed — it overwrites arbitrary memory content by id
#     (destruction-equivalent for a confused/hostile remote model).
#   - add_memories: removed — it is a batch wrapper over add_memory with no
#     added remote value and the same side effects.
#   - add_memory: kept (remote clients need to write), but its auto-prune
#     delete path is disabled for remote writes via _REMOTE_MODE — candidates
#     are routed to the review queue instead of deleted (issue #37).
REMOTE_ALLOWED_TOOLS = {
    "get_memory", "search_memory", "reflect", "add_memory",
    "list_projects", "export_passport",
    # export_for_model (#180): the ChatGPT/Gemini/Ollama-formatted exports
    # exist specifically for non-Claude remote clients to consume, but were
    # never reachable from the bridge those clients connect through — only
    # Claude stdio and the Streamlit UI could call it, so using it remotely
    # meant generating a blob locally and pasting it in by hand, the exact
    # manual workflow the bridge exists to replace. Read-only, no new risk.
    "export_for_model",
    # list_profiles (#180): switch_profile stays excluded — it mutates
    # _current_profile, global state a remote model shouldn't control. But
    # get_memory/search_memory/etc. all accept an explicit profile= argument
    # that IS honored remotely (remote mode only pins the *default* when one
    # isn't given), so a remote client was access-unblocked but
    # discovery-blind: it could reach a non-default profile if it happened to
    # guess the name, with no way to learn what names exist. This is a
    # read-only enumeration of names only — no identity/content/memory data.
    "list_profiles",
    # F1 workspace read tools — ws_write intentionally excluded here; it is
    # added to this allowlist only once the F4 remote-write guard exists
    # (it now does, in workspace.py, but per the F1/F4 doc the allowlist
    # change and the guard must land in the same commit as a deliberate
    # decision, not a byproduct of ws_write existing). See F4 acceptance
    # criteria in docs/MULTI_MODEL_FEATURES.md.
    "ws_status", "ws_list", "ws_read", "ws_search",
}


def _gate_tools_for_remote() -> list[str]:
    """Remove non-allowlisted tools from the MCP server. Returns removed names.

    Fails CLOSED: if the tool set cannot be enumerated (or comes back empty),
    the process exits rather than risk serving destructive tools remotely.
    """
    try:
        import asyncio
        tool_names = list(asyncio.run(mcp.get_tools()).keys())
    except Exception as e:
        # Do NOT fall back to a private FastMCP attribute — a rename there
        # would silently yield an empty set and serve every tool (fail-open).
        print(f"[memorybridge] FATAL: could not enumerate tools for remote "
              f"gating ({e}). Refusing to serve HTTP.", file=sys.stderr)
        os._exit(1)

    if not tool_names:
        print("[memorybridge] FATAL: tool enumeration returned empty; refusing "
              "to serve HTTP (fail-closed).", file=sys.stderr)
        os._exit(1)

    removed = []
    for name in tool_names:
        if name not in REMOTE_ALLOWED_TOOLS:
            try:
                mcp.remove_tool(name)
                removed.append(name)
            except Exception as e:
                print(f"[memorybridge] FATAL: could not remove tool {name}: {e}",
                      file=sys.stderr)
                os._exit(1)  # never serve destructive tools remotely by accident
    return removed


async def _send_plain(send, status: int, text: str) -> None:
    """Emit a minimal ASGI plain-text response."""
    body = text.encode()
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [
            (b"content-type", b"text/plain; charset=utf-8"),
            (b"content-length", str(len(body)).encode()),
        ],
    })
    await send({"type": "http.response.body", "body": body})


class _RateLimitAuthMiddleware:
    """ASGI middleware guarding the HTTP bridge (issue #69).

    - Per-client-IP fixed-window rate limiting -> 429 when exceeded, so the
      path-embedded capability token cannot be brute-forced without backoff.
    - Constant-time comparison of the leading path segment against the expected
      token (`secrets.compare_digest`); a mismatch returns a uniform 404 with no
      timing signal and never reaches the MCP app.
    Behind the Cloudflare tunnel the real client IP arrives in CF-Connecting-IP /
    X-Forwarded-For, so those are honored before the transport peer address.
    """

    def __init__(self, app, expected_token: str, limit: int, window: int):
        self.app = app
        self.expected_token = expected_token
        self.limit = limit
        self.window = window
        self._hits: dict[str, list] = {}
        self._lock = threading.Lock()

    def _client_ip(self, scope) -> str:
        headers = {k.decode().lower(): v.decode()
                   for k, v in scope.get("headers", [])}
        for h in ("cf-connecting-ip", "x-forwarded-for"):
            val = headers.get(h, "").strip()
            if val:
                return val.split(",")[0].strip()
        client = scope.get("client")
        return client[0] if client else "unknown"

    def _rate_ok(self, ip: str) -> bool:
        now = time.monotonic()
        with self._lock:
            window_start, count = self._hits.get(ip, (now, 0))
            if now - window_start >= self.window:
                window_start, count = now, 0
            count += 1
            self._hits[ip] = [window_start, count]
            if len(self._hits) > 4096:  # bound memory; drop stale windows
                for k in [k for k, (s, _) in self._hits.items()
                          if now - s >= self.window]:
                    self._hits.pop(k, None)
            return count <= self.limit

    def _token_ok(self, scope) -> bool:
        segment = scope.get("path", "").lstrip("/").split("/", 1)[0]
        # secrets.compare_digest raises TypeError on a non-ASCII str rather
        # than returning False (issue #177) — uvicorn decodes a percent-encoded
        # non-ASCII path segment to UTF-8 before scope reaches here, so an
        # unauthenticated request could crash this pre-auth check with a 500
        # instead of the uniform 404 every other bad token gets.
        if not segment.isascii():
            return False
        return secrets.compare_digest(segment, self.expected_token)

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        # This is a pre-auth boundary parsing attacker-controlled input (path,
        # headers) from the open internet. Any unexpected exception here must
        # not become an unauthenticated 500 (issue #177) — that both discloses
        # more than a uniform 404 and, since it's logged, is a free remote log
        # amplification vector. Fail closed to 404, log the exception type only.
        try:
            rate_ok = self._rate_ok(self._client_ip(scope))
            token_ok = self._token_ok(scope) if rate_ok else True
        except Exception as e:
            print(f"[memorybridge] auth middleware error (denying request): "
                  f"{type(e).__name__}", file=sys.stderr)
            await _send_plain(send, 404, "not found")
            return
        if not rate_ok:
            await _send_plain(send, 429, "rate limit exceeded")
            return
        if not token_ok:
            await _send_plain(send, 404, "not found")
            return
        await self.app(scope, receive, send)


def _run_http() -> None:
    """Serve over streamable HTTP for remote MCP clients.

    - Bound to 127.0.0.1; exposure to the internet happens only via the
      Cloudflare tunnel in front of it.
    - The MCP path embeds a secret (capability URL): ChatGPT's no-auth
      connector mode and Perplexity's open mode can both use it, and the
      token never appears in server logs ChatGPT/Perplexity side.
    - Parent watchdog is NOT started: under launchd our PPID is legitimately
      1, and the watchdog would kill the server 5s after boot.
    """
    # Mark remote mode so the auto-pruner's delete path is disabled for
    # writes that arrive over this bridge (see add_memory / issue #37).
    global _REMOTE_MODE
    _REMOTE_MODE = True
    workspace.set_remote_mode(True)  # F4: ws_write now requires overwrite=True

    token = os.environ.get("MEMORYBRIDGE_TOKEN", "").strip()
    if len(token) < 32:
        print("[memorybridge] FATAL: MEMORYBRIDGE_TOKEN missing or under 32 chars "
              "(set it in DATA_DIR/.env). Refusing to serve HTTP without a secret.",
              file=sys.stderr)
        os._exit(1)

    removed = _gate_tools_for_remote()
    port = int(os.environ.get("MEMORYBRIDGE_PORT", "8484"))

    # The secret token is embedded in the request path (capability URL). The
    # uvicorn access logger records the full path on every request, which would
    # write the secret to stdout/stderr and any tunnel/proxy log. Disable it so
    # the token never lands in a log. Never print even a prefix of the token.
    import logging
    logging.getLogger("uvicorn.access").disabled = True

    rate_limit = int(os.environ.get("MEMORYBRIDGE_RATE_LIMIT", "120"))
    rate_window = int(os.environ.get("MEMORYBRIDGE_RATE_WINDOW", "60"))

    # Wrap the FastMCP ASGI app with rate-limiting + constant-time token auth
    # (issue #69). Only take this path if we can build the app at the SAME
    # capability path clients expect; otherwise fall back to mcp.run so a
    # FastMCP API change can never leave the bridge serving on the wrong path.
    mcp_path = f"/{token}/mcp"
    # FastMCP 2.x exposes http_app(path=...); older builds used streamable_http_app.
    app_factory = getattr(mcp, "http_app", None) or getattr(mcp, "streamable_http_app", None)
    app = None
    if callable(app_factory):
        try:
            app = app_factory(path=mcp_path)
        except TypeError:
            app = None  # can't pin the path safely -> fall back

    if app is not None:
        import uvicorn
        wrapped = _RateLimitAuthMiddleware(app, token, rate_limit, rate_window)
        print(f"[memorybridge] HTTP bridge on 127.0.0.1:{port} "
              f"path=/<redacted>/mcp | tools gated: removed {len(removed)} "
              f"({', '.join(sorted(removed))}) | rate limit {rate_limit}/{rate_window}s per IP",
              file=sys.stderr)
        # lifespan="on": the streamable-HTTP session manager is started by the
        # Starlette lifespan; the ASGI wrapper forwards lifespan scopes, and
        # forcing it on (vs "auto") makes a lifespan failure loud instead of
        # degrading every request to a 500.
        uvicorn.run(wrapped, host="127.0.0.1", port=port,
                    access_log=False, log_level="warning", lifespan="on")
    else:
        print(f"[memorybridge] WARNING: could not attach rate-limiting middleware "
              f"(FastMCP app factory unavailable); serving without per-IP rate "
              f"limiting. HTTP bridge on 127.0.0.1:{port} path=/<redacted>/mcp | "
              f"tools gated: removed {len(removed)} ({', '.join(sorted(removed))})",
              file=sys.stderr)
        mcp.run(transport="http", host="127.0.0.1", port=port, path=mcp_path)


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
            # stdio loop ended (stdin EOF / client disconnect) — never linger.
            # Worker threads (FastEmbed/ONNX) are non-daemon and would otherwise
            # keep the process alive as an orphan.
            print("[memorybridge] MCP loop ended — exiting", file=sys.stderr)
            _cleanup_pid()
            sys.stderr.flush()
            os._exit(0)
