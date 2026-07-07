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
from pathlib import Path
from datetime import datetime
from typing import Optional
from fastmcp import FastMCP
from db.pruner import run_auto_prune, record_outcome, get_pruner_report
from db.constants import VALID_CATEGORIES, IMPORTANCE_LEVELS, _content_hash, effective_score  # noqa: F401

try:
    import tiktoken
    _enc = tiktoken.get_encoding("cl100k_base")
    def _count_tokens_impl(text: str) -> int:
        return len(_enc.encode(text))
except ImportError:
    def _count_tokens_impl(text: str) -> int:
        words = len(re.findall(r'\b\w+\b', text))
        punctuation = len(re.findall(r'[^\w\s]', text))
        return int(words * 1.3 + punctuation * 0.5) or 1

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
MAX_TOKENS_DEFAULT     = 4000
SEARCH_LIMIT_DEFAULT   = 5
SEARCH_MAX_TOKENS_DEFAULT = 800
MAX_TOTAL_TOKENS       = 50000
ARCHIVE_SCORE_THRESHOLD = 0.15

DECAY_CONFIG = {
    "enabled": True,
    "half_life_days": 30,
    "min_score": 0.1,
    "boost_on_access": 0.1
}
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
from db.store import MemoryStore  # noqa: E402
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

def count_tokens(text: str) -> int:
    if not text:
        return 0
    return _count_tokens_impl(text) or 1


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


def apply_decay(memories: list, decay_config: dict) -> list:
    if not decay_config.get("enabled", True):
        return memories
    today = datetime.now()
    for mem in memories:
        mem["effective_score"] = effective_score(mem, today)
    return memories


# =============================================================================
# RESULT CLEANING — Phase 2.5: strip internal scoring metadata
# =============================================================================

_RESULT_FIELDS = {"id", "content", "category", "importance",
                  "project_id", "tags", "token_count", "created_at"}


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
    profile = profile or _current_profile
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

    overhead_tokens = (
        count_tokens(json.dumps(identity)) +
        count_tokens(json.dumps(projects)) +
        count_tokens(json.dumps(model_preferences)) +
        200
    )
    available_for_memories = max(max_tokens - overhead_tokens, 0)

    selected_memories = []
    tokens_used = 0
    for mem in memories:
        mem_tokens = mem.get("token_count", count_memory_tokens(mem))
        if tokens_used + mem_tokens <= available_for_memories:
            selected_memories.append(mem)
            tokens_used += mem_tokens
        elif compress and tokens_used < available_for_memories:
            remaining = available_for_memories - tokens_used
            compressed = compress_memory(mem, target_tokens=remaining - 20)
            if compressed.get("token_count", mem_tokens) <= remaining:
                selected_memories.append(compressed)
                tokens_used += compressed.get("token_count", 0)
                break
        else:
            break

    total_tokens_served = tokens_used + overhead_tokens

    response = {
        "profile": profile,
        "identity": identity,
        "memories": selected_memories,
        "projects": projects,
        "model_preferences": model_preferences,
        "token_stats": {
            "budget": max_tokens,
            "served": total_tokens_served,
            "remaining": max(max_tokens - total_tokens_served, 0),
            "memories_returned": len(selected_memories),
            "memories_available": len(memories),
            "compressed_count": sum(1 for m in selected_memories if m.get("compressed")),
            "overhead_tokens": overhead_tokens
        }
    }

    _store.log_access("get_memory", profile,
                      f"hint={context_hint}, cat={category}, budget={max_tokens}",
                      tokens_served=total_tokens_served)
    log_to_analytics(
        tokens_served=total_tokens_served,
        memories_returned=len(selected_memories),
        model="claude",
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
    profile: str = None
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
    Returns:
        Confirmation with memory ID and token count, or duplicate status
    """
    profile = profile or _current_profile
    if category not in VALID_CATEGORIES:
        return json.dumps({"error": f"Invalid category. Valid: {VALID_CATEGORIES}"})
    if importance not in IMPORTANCE_LEVELS:
        return json.dumps({"error": f"Invalid importance. Valid: {IMPORTANCE_LEVELS}"})

    mid = _store.add_memory(profile, content,
                            category=category, importance=importance,
                            tags=tags, project_id=project_id)
    if mid is None:
        return json.dumps({"status": "duplicate", "reason": "identical content already exists"})

    token_count = count_tokens(content) + count_tokens(" ".join(tags or [])) + 20

    # Budget-based prune (existing behaviour)
    stats = _store.token_stats(profile)
    budget_pruned = []
    if stats["total_tokens"] > MAX_TOTAL_TOKENS:
        budget_pruned = _store.auto_prune(profile, threshold=ARCHIVE_SCORE_THRESHOLD)

    # Adaptive dedup/staleness prune
    prune_result = run_auto_prune(_store._conn, profile, _store.delete_memory)

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
    profile: str = None
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
    Returns:
        Summary with all added memory IDs and total tokens
    """
    profile = profile or _current_profile
    if category not in VALID_CATEGORIES:
        return json.dumps({"error": f"Invalid category. Valid: {VALID_CATEGORIES}"})
    if importance not in IMPORTANCE_LEVELS:
        return json.dumps({"error": f"Invalid importance. Valid: {IMPORTANCE_LEVELS}"})
    if not facts:
        return json.dumps({"error": "facts list is empty"})

    changes = []
    total_tokens = 0

    for fact in facts:
        mid = _store.add_memory(profile, fact,
                                category=category, importance=importance,
                                project_id=project)
        if mid is not None:
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

    _store.log_access("add_memories", profile,
                      f"added {len(changes)} memories, {total_tokens} tokens")

    return json.dumps({
        "status": "updated",
        "profile": profile,
        "count": len(changes),
        "total_tokens_added": total_tokens,
        "changes": changes,
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
    profile = profile or _current_profile
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

    updated = _store.edit_memory(profile, memory_id, **kwargs)
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
    profile = profile or _current_profile
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

    tokens_served = sum(m.get("token_count", 0) for m in results)
    _store.log_access("search_memory", profile,
                      f"query='{query}', results={len(results)}", tokens_served)
    log_to_analytics(
        tokens_served=tokens_served,
        memories_returned=len(results),
        model="claude",
        profile=profile,
        operation="search_memory"
    )
    return json.dumps({
        "query": query,
        "profile": profile,
        "results": [_clean_result(m) for m in results],
        "total_matches": len(results),
        "tokens_served": tokens_served
    }, indent=2)


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
    profile = profile or _current_profile
    _store.ensure_profile(profile)

    result = _store.reflect(profile, question, limit=limit, max_tokens=max_tokens)
    return json.dumps(result, indent=2, default=str)


@mcp.tool()
def delete_memory(
    memory_id: str,
    profile: str = None
) -> str:
    """Delete a specific memory by ID."""
    profile = profile or _current_profile
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
    profile = profile or _current_profile
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
    profile = profile or _current_profile
    _store.ensure_profile(profile)
    profile_data = _store.get_profile(profile)
    if profile_data is None:
        return json.dumps({"error": f"Profile '{profile}' not found"})

    projects = profile_data.get("projects", [])
    summary = [
        {
            "id": p.get("id"),
            "name": p.get("name"),
            "status": p.get("status"),
            "phase": p.get("phase"),
            "last_updated": p.get("last_updated")
        }
        for p in projects
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
    profile = profile or _current_profile
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
    profile = profile or _current_profile
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
    profile = profile or _current_profile
    _store.ensure_profile(profile)
    profile_data = _store.get_profile(profile)
    if profile_data is None:
        return json.dumps({"error": f"Profile '{profile}' not found"})

    identity = profile_data["identity"]
    projects = profile_data["projects"]
    memories = _store.get_memories(profile)
    prefs = profile_data.get("model_preferences", {}).get(model, {})

    memories = apply_decay([m.copy() for m in memories], DECAY_CONFIG)
    memories.sort(key=lambda m: m.get("effective_score", 0), reverse=True)

    budgets = {"full": max_tokens, "summary": max_tokens // 2, "minimal": max_tokens // 4}
    budget = budgets.get(depth, max_tokens)
    tokens_used = 0

    # --- Build shared base data (Issue #20: all models draw from the same pool) ---
    # Collect exported memories in a list so we can count what was actually included.
    exported_memories: list = []

    # Shared memory-iteration helper used by all three model branches.
    def _collect_memories(lines_or_parts, append_fn, fmt_fn):
        """Iterate memories within budget; populate exported_memories side-effect."""
        nonlocal tokens_used
        for m in memories:
            if tokens_used >= budget - 50:
                break
            content = m.get("content", "")
            mem_tokens = count_tokens(content)
            if tokens_used + mem_tokens > budget - 50:
                remaining = budget - tokens_used - 50
                content = content[:remaining * 3] + "…"
            append_fn(fmt_fn(content))
            tokens_used += count_tokens(content) + 2
            exported_memories.append(m)

    if model == "chatgpt":
        lines = [
            "# Memory Chip",
            f"*Exported: {datetime.now().strftime('%Y-%m-%d %H:%M')}*",
            "",
            "## Identity",
            f"**Name:** {identity.get('name', 'Unknown')}",
            f"**Role:** {identity.get('role', 'Unknown')}",
            ""
        ]
        tokens_used = count_tokens("\n".join(lines))

        if identity.get("communication_style") and tokens_used < budget - 100:
            style = identity["communication_style"]
            style_lines = [
                "## Communication Style",
                f"**Tone:** {style.get('tone', '')}",
            ]
            if style.get("preferences"):
                for pref in style["preferences"][:3]:
                    style_lines.append(f"- {pref}")
            style_lines.append("")
            style_text = "\n".join(style_lines)
            if tokens_used + count_tokens(style_text) < budget:
                lines.extend(style_lines)
                tokens_used += count_tokens(style_text)

        if memories and depth in ("full", "summary") and tokens_used < budget - 100:
            lines.append("## Key Memories")
            _collect_memories(lines, lines.append, lambda c: f"- {c}")
            lines.append("")

        if projects and depth == "full" and tokens_used < budget - 100:
            lines.append("## Active Projects")
            for p in projects:
                if p.get("status") == "active" and tokens_used < budget - 50:
                    proj_line = f"- **{p.get('name', p.get('id'))}**: {p.get('description', '')[:50]}"
                    lines.append(proj_line)
                    tokens_used += count_tokens(proj_line)
            lines.append("")

        export_text = "\n".join(lines)

    elif model == "gemini":
        # Gemini: pipe-separated compact format, but includes ALL memories within
        # budget — not just 3 preference snippets (Issue #20 fix).
        parts = [f"User: {identity.get('name', 'Unknown')} - {identity.get('role', 'Unknown')}"]
        tokens_used = count_tokens(parts[0])

        if identity.get("communication_style", {}).get("tone"):
            tone_part = f"Style: {identity['communication_style']['tone'][:50]}"
            if tokens_used + count_tokens(tone_part) < budget:
                parts.append(tone_part)
                tokens_used += count_tokens(tone_part)

        if memories and depth in ("full", "summary") and tokens_used < budget - 100:
            _collect_memories(parts, parts.append, lambda c: f"Mem: {c}")

        if projects and depth == "full" and tokens_used < budget - 100:
            active = [p.get("name", p["id"]) for p in projects if p.get("status") == "active"]
            if active:
                proj_part = f"Projects: {', '.join(active)}"
                if tokens_used + count_tokens(proj_part) < budget:
                    parts.append(proj_part)
                    tokens_used += count_tokens(proj_part)

        export_text = " | ".join(parts)

    elif model == "ollama":
        # Ollama: semicolon-separated terse format, but includes ALL memories
        # within budget — not just name/role/project ids (Issue #20 fix).
        parts = [
            f"User={identity.get('name', 'Unknown')}",
            f"Role={identity.get('role', 'Unknown')[:30]}"
        ]
        tokens_used = sum(count_tokens(p) for p in parts)

        if memories and depth in ("full", "summary") and tokens_used < budget - 100:
            _collect_memories(parts, parts.append, lambda c: f"Mem={c[:80]}")

        if projects and depth in ("full", "summary") and tokens_used < budget - 100:
            active = [p["id"] for p in projects if p.get("status") == "active"]
            if active:
                proj_part = f"Projects={','.join(active)}"
                if tokens_used + count_tokens(proj_part) < budget:
                    parts.append(proj_part)
                    tokens_used += count_tokens(proj_part)

        export_text = ";".join(parts)

    else:
        return json.dumps({"error": f"Unknown model: {model}. Supported: chatgpt, gemini, ollama"})

    final_tokens = count_tokens(export_text)
    _store.log_access("export_for_model", profile,
                      f"model={model}, tokens={final_tokens}", final_tokens)
    log_to_analytics(
        tokens_served=final_tokens,
        # Fix Issue #20: count memories actually included in the export,
        # not memories that merely have effective_score > 0.3.
        memories_returned=len(exported_memories),
        model=model,
        profile=profile,
        operation="export_for_model"
    )
    return export_text


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
    profile = profile or _current_profile
    from ingestion.passport import build_passport

    _store.ensure_profile(profile)
    profile_data = _store.get_profile(profile)
    if profile_data is None:
        return f"# Memory Passport\nProfile: {profile}\nGenerated: {__import__('datetime').datetime.now().strftime('%Y-%m-%d')}\n\nError: profile not found."

    memories = _store.get_memories(profile)
    identity = profile_data.get("identity", {})

    passport = build_passport(
        memories=memories,
        identity=identity,
        profile=profile,
        max_tokens=max_tokens,
    )

    final_tokens = count_tokens(passport)
    _store.log_access("export_passport", profile,
                      f"tokens={final_tokens}", final_tokens)
    log_to_analytics(
        tokens_served=final_tokens,
        memories_returned=len(memories),
        model="passport",
        profile=profile,
        operation="export_passport",
    )
    return passport


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
# NOTE (issue #37, still open): add_memory below can still trigger the
# auto-pruner's delete path. Fully neutralizing that requires threading a
# no-op delete callback through run_auto_prune for remote-origin writes;
# tracked separately. This allowlist change closes the direct overwrite/
# batch vectors now.
REMOTE_ALLOWED_TOOLS = {
    "get_memory", "search_memory", "reflect", "add_memory",
    "list_projects", "export_passport",
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

    print(f"[memorybridge] HTTP bridge on 127.0.0.1:{port} "
          f"path=/<redacted>/mcp | tools gated: removed {len(removed)} "
          f"({', '.join(sorted(removed))})", file=sys.stderr)
    mcp.run(transport="http", host="127.0.0.1", port=port,
            path=f"/{token}/mcp")


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
