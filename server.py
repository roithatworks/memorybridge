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
import re
import atexit
import hashlib
from pathlib import Path
from datetime import datetime
from typing import Optional
from fastmcp import FastMCP
from db.pruner import run_auto_prune, record_outcome, get_pruner_report

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
MEMORY_DB              = Path.home() / "memorybridge" / "memory.db"
ANALYTICS_FILE         = Path.home() / "memorybridge" / "analytics.json"
DEFAULT_PROFILE        = "default"
MAX_TOKENS_DEFAULT     = 4000
SEARCH_LIMIT_DEFAULT   = 5
SEARCH_MAX_TOKENS_DEFAULT = 800
MAX_TOTAL_TOKENS       = 50000
ARCHIVE_SCORE_THRESHOLD = 0.15
ANALYTICS_FLUSH_EVERY  = 10

VALID_CATEGORIES = [
    "preference", "fact", "insight", "decision",
    "project_status", "relationship", "skill", "constraint"
]
IMPORTANCE_LEVELS = ["low", "medium", "high", "critical"]

DECAY_CONFIG = {
    "enabled": True,
    "half_life_days": 30,
    "min_score": 0.1,
    "boost_on_access": 0.1
}

# =============================================================================
# ANALYTICS BUFFER — unchanged from v1.4
# =============================================================================
_analytics_buffer: list = []
_analytics_flush_count: int = 0


def _flush_analytics() -> None:
    """Write buffered analytics events to disk. Called periodically and at shutdown."""
    global _analytics_buffer, _analytics_flush_count
    if not _analytics_buffer:
        return
    try:
        if ANALYTICS_FILE.exists():
            with open(ANALYTICS_FILE, "r") as f:
                analytics = json.load(f)
        else:
            analytics = {
                "version": "1.0",
                "created_at": datetime.now().isoformat(),
                "sessions": [],
                "daily_stats": {},
                "by_model": {},
                "by_operation": {},
                "savings_estimate": {"baseline_tokens_per_session": 8000}
            }

        for session in _analytics_buffer:
            analytics["sessions"].append(session)
            date_key = session["date"]
            if date_key not in analytics["daily_stats"]:
                analytics["daily_stats"][date_key] = {
                    "tokens_served": 0, "sessions": 0, "memories_returned": 0
                }
            analytics["daily_stats"][date_key]["tokens_served"] += session["tokens_served"]
            analytics["daily_stats"][date_key]["sessions"] += 1
            analytics["daily_stats"][date_key]["memories_returned"] += session["memories_returned"]

            model = session["model"]
            if model not in analytics["by_model"]:
                analytics["by_model"][model] = {"tokens": 0, "sessions": 0}
            analytics["by_model"][model]["tokens"] += session["tokens_served"]
            analytics["by_model"][model]["sessions"] += 1

            operation = session["operation"]
            if operation not in analytics["by_operation"]:
                analytics["by_operation"][operation] = {"tokens": 0, "count": 0}
            analytics["by_operation"][operation]["tokens"] += session["tokens_served"]
            analytics["by_operation"][operation]["count"] += 1

        if len(analytics["sessions"]) > 10000:
            analytics["sessions"] = analytics["sessions"][-10000:]

        analytics["last_updated"] = datetime.now().isoformat()
        ANALYTICS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(ANALYTICS_FILE, "w") as f:
            json.dump(analytics, f, indent=2)

        _analytics_buffer = []
        _analytics_flush_count = 0
    except Exception:
        pass


atexit.register(_flush_analytics)


def log_to_analytics(tokens_served: int, memories_returned: int,
                     model: str = "claude", profile: str = "default",
                     operation: str = "get_memory") -> None:
    """Buffer an analytics event. Flushes to disk every ANALYTICS_FLUSH_EVERY calls."""
    global _analytics_flush_count
    _analytics_buffer.append({
        "timestamp": datetime.now().isoformat(),
        "date": datetime.now().strftime("%Y-%m-%d"),
        "tokens_served": tokens_served,
        "memories_returned": memories_returned,
        "model": model,
        "profile": profile,
        "operation": operation
    })
    _analytics_flush_count += 1
    if _analytics_flush_count >= ANALYTICS_FLUSH_EVERY:
        _flush_analytics()


# =============================================================================
# STORE — SQLite singleton
# =============================================================================
from db.store import MemoryStore  # noqa: E402

_store = MemoryStore(MEMORY_DB)


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
            return compressed
    words = content.split()
    while count_tokens(" ".join(words)) > target_tokens - 1 and len(words) > 3:
        words = words[:-1]
    compressed["content"] = " ".join(words) + "…"
    compressed["compressed"] = True
    compressed["token_count"] = count_tokens(compressed["content"]) + 20
    return compressed


def apply_decay(memories: list, decay_config: dict) -> list:
    if not decay_config.get("enabled", True):
        return memories
    half_life = decay_config.get("half_life_days", 30)
    min_score = decay_config.get("min_score", 0.1)
    today = datetime.now()
    for mem in memories:
        try:
            created = datetime.fromisoformat(mem.get("created_at", today.strftime("%Y-%m-%d")))
        except Exception:
            created = today
        days_old = (today - created).days
        decay_factor = 0.5 ** (days_old / half_life)
        base_score = mem.get("relevance_score", 1.0)
        mem["effective_score"] = max(base_score * decay_factor, min_score)
        importance = mem.get("importance", "medium")
        importance_boost = {"low": 0.8, "medium": 1.0, "high": 1.2, "critical": 1.5}
        mem["effective_score"] *= importance_boost.get(importance, 1.0)
        access_count = mem.get("access_count", 0)
        if access_count > 0:
            mem["effective_score"] *= (1 + min(access_count * 0.05, 0.5))
    return memories


# =============================================================================
# RESULT CLEANING — Phase 2.5: strip internal scoring metadata
# =============================================================================

def _content_hash(content: str) -> str:
    return hashlib.sha256(content.strip().lower().encode()).hexdigest()


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
    profile: str = DEFAULT_PROFILE,
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
    _store.ensure_profile(profile)
    profile_data = _store.get_profile(profile)
    if profile_data is None:
        return json.dumps({"error": f"Profile '{profile}' not found"})

    memories = _store.get_memories(profile, category=category)

    # Apply decay scoring
    memories = apply_decay([m.copy() for m in memories], DECAY_CONFIG)

    if context_hint:
        hint_lower = context_hint.lower()
        memories = [
            m for m in memories
            if hint_lower in m.get("content", "").lower()
            or hint_lower in str(m.get("tags", [])).lower()
            or hint_lower in str(m.get("project_id", "")).lower()
        ]

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
    profile: str = DEFAULT_PROFILE
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
def update_memory(
    facts: list[str],
    category: str = "fact",
    importance: str = "medium",
    project: Optional[str] = None,
    profile: str = DEFAULT_PROFILE
) -> str:
    """
    Batch-add multiple facts in a single operation.

    Args:
        facts: List of facts to remember
        category: Category for all facts
        importance: Importance level for all facts
        project: Optional project association
        profile: Memory profile
    Returns:
        Summary with all added memory IDs and total tokens
    """
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

    _store.log_access("update_memory", profile,
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
def search_memory(
    query: str,
    category: Optional[str] = None,
    limit: int = SEARCH_LIMIT_DEFAULT,
    max_tokens: int = SEARCH_MAX_TOKENS_DEFAULT,
    profile: str = DEFAULT_PROFILE
) -> str:
    """
    Search memories using FTS5 BM25 with optional token budget.

    Args:
        query: Search query
        category: Optional category filter
        limit: Max results (default 5)
        max_tokens: Token cap (default 800)
        profile: Memory profile
    Returns:
        JSON with ranked results (internal fields stripped)
    """
    _store.ensure_profile(profile)

    if category and category not in VALID_CATEGORIES:
        return json.dumps({"error": f"Invalid category. Valid: {VALID_CATEGORIES}"})

    # Phase 4: hybrid BM25 + semantic search (falls back to FTS5 if no embeddings built)
    results = _store.search_hybrid(profile, query, category=category,
                                   limit=limit, max_tokens=max_tokens)

    # Boost relevance score for accessed memories
    for mem in results:
        _store.boost_on_access(profile, mem["id"],
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
def delete_memory(
    memory_id: str,
    profile: str = DEFAULT_PROFILE
) -> str:
    """Delete a specific memory by ID."""
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
def get_token_stats(profile: str = DEFAULT_PROFILE) -> str:
    """
    Get comprehensive token usage statistics.

    Args:
        profile: Profile to analyze (or 'all' for global stats)
    Returns:
        Token usage breakdown
    """
    if profile == "all":
        all_profiles = {}
        total_stored = 0
        for p_name in _store.list_profiles():
            stats = _store.token_stats(p_name)
            all_profiles[p_name] = stats
            total_stored += stats["total_tokens"]

        total_served = _store._conn.execute(
            "SELECT COALESCE(SUM(tokens_served), 0) FROM access_log"
        ).fetchone()[0]
        served_by_profile = {}
        for row in _store._conn.execute(
            "SELECT profile, SUM(tokens_served) as t FROM access_log GROUP BY profile"
        ).fetchall():
            served_by_profile[row["profile"]] = row["t"] or 0

        return json.dumps({
            "global": {
                "total_tokens_stored": total_stored,
                "total_tokens_served": total_served,
                "max_budget": MAX_TOTAL_TOKENS,
                "utilization": f"{(total_stored / MAX_TOTAL_TOKENS) * 100:.1f}%"
            },
            "by_profile": all_profiles,
            "served_by_profile": served_by_profile
        }, indent=2)

    _store.ensure_profile(profile)
    stats = _store.token_stats(profile)
    archived_row = _store._conn.execute(
        "SELECT COUNT(*) as c, COALESCE(SUM(token_count), 0) as t "
        "FROM memories WHERE profile=? AND archived=1", (profile,)
    ).fetchone()
    total_served = _store._conn.execute(
        "SELECT COALESCE(SUM(tokens_served), 0) FROM access_log WHERE profile=?",
        (profile,)
    ).fetchone()[0]

    return json.dumps({
        "profile": profile,
        "active": stats,
        "archived": {
            "count": archived_row["c"],
            "tokens": archived_row["t"]
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
    profile: str = DEFAULT_PROFILE,
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
    profile_data = _store.get_profile(profile_name)
    if profile_data is None:
        available = _store.list_profiles()
        return json.dumps({
            "error": f"Profile '{profile_name}' not found",
            "available_profiles": available
        })

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
def list_projects(profile: str = DEFAULT_PROFILE) -> str:
    """List all projects with status."""
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
        "total_logged": _store._conn.execute(
            "SELECT COUNT(*) FROM access_log"
        ).fetchone()[0],
        "returned": len(entries)
    }
    if include_tokens:
        total_served = _store._conn.execute(
            "SELECT COALESCE(SUM(tokens_served), 0) FROM access_log"
        ).fetchone()[0]
        served_by_profile = {}
        for row in _store._conn.execute(
            "SELECT profile, SUM(tokens_served) as t FROM access_log GROUP BY profile"
        ).fetchall():
            served_by_profile[row["profile"]] = row["t"] or 0
        result["token_summary"] = {
            "total_served": total_served,
            "by_profile": served_by_profile
        }
    return json.dumps(result, indent=2)


@mcp.tool()
def get_prune_queue(
    profile: str = DEFAULT_PROFILE,
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
    profile: str = DEFAULT_PROFILE
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
    _store.ensure_profile(profile)
    result = record_outcome(_store._conn, queue_id, approved, _store.delete_memory)

    if "error" in result:
        return json.dumps(result)

    # Return updated rule confidence after recalibration
    from db.pruner import recalibrate_thresholds, AUTO_EXECUTE_THRESHOLD
    rule_row = _store._conn.execute(
        """SELECT rule_name, confidence FROM pruner_rules
           JOIN prune_queue ON pruner_rules.rule_name = prune_queue.rule_name
           WHERE prune_queue.id = ?""",
        (queue_id,)
    ).fetchone()

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
    profile: str = DEFAULT_PROFILE,
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
            for m in memories:
                if tokens_used >= budget - 50:
                    break
                content = m.get("content", "")
                mem_tokens = count_tokens(content)
                if tokens_used + mem_tokens > budget - 50:
                    remaining = budget - tokens_used - 50
                    content = content[:remaining * 3] + "…"
                lines.append(f"- {content}")
                tokens_used += count_tokens(content) + 2
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
        parts = [f"User: {identity.get('name', 'Unknown')} - {identity.get('role', 'Unknown')}"]
        tokens_used = count_tokens(parts[0])

        if identity.get("communication_style", {}).get("tone"):
            tone_part = f"Style: {identity['communication_style']['tone'][:50]}"
            if tokens_used + count_tokens(tone_part) < budget:
                parts.append(tone_part)
                tokens_used += count_tokens(tone_part)

        if memories and depth in ("full", "summary"):
            prefs_mems = [m for m in memories if m.get("category") == "preference"][:3]
            if prefs_mems:
                pref_text = "; ".join(m["content"][:30] for m in prefs_mems)
                if tokens_used + count_tokens(pref_text) < budget:
                    parts.append(f"Prefs: {pref_text}")
                    tokens_used += count_tokens(pref_text)

        export_text = " | ".join(parts)

    elif model == "ollama":
        parts = [
            f"User={identity.get('name', 'Unknown')}",
            f"Role={identity.get('role', 'Unknown')[:30]}"
        ]
        tokens_used = sum(count_tokens(p) for p in parts)

        if depth in ("full", "summary"):
            active = [p["id"] for p in projects if p.get("status") == "active"][:3]
            if active:
                proj_part = f"Projects={','.join(active)}"
                if tokens_used + count_tokens(proj_part) < budget:
                    parts.append(proj_part)

        export_text = ";".join(parts)

    else:
        return json.dumps({"error": f"Unknown model: {model}. Supported: chatgpt, gemini, ollama"})

    final_tokens = count_tokens(export_text)
    _store.log_access("export_for_model", profile,
                      f"model={model}, tokens={final_tokens}", final_tokens)
    log_to_analytics(
        tokens_served=final_tokens,
        memories_returned=len([m for m in memories if m.get("effective_score", 0) > 0.3]),
        model=model,
        profile=profile,
        operation="export_for_model"
    )
    return export_text


@mcp.tool()
def export_passport(
    profile: str = DEFAULT_PROFILE,
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


if __name__ == "__main__":
    mcp.run()
