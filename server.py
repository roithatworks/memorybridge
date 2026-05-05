"""MemoryBridge MCP Server v1.3
Local-first, cross-model memory portability

Token Management Features:
1. Token counting - each memory has token_count field
2. Budget-aware retrieval - get_memory enforces max_tokens (ALL response fields counted)
3. Compression on export - condense verbose memories
4. Usage tracking - tokens_served logged per operation
5. Memory pruning - auto-archive low-score memories
6. Analytics dashboard - track token savings over time (buffered writes)

Run with: fastmcp run server.py
"""

import json
import uuid
import re
import fcntl
import atexit
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional
from fastmcp import FastMCP

try:
    import tiktoken
    _enc = tiktoken.get_encoding("cl100k_base")
    def _count_tokens_impl(text: str) -> int:
        return len(_enc.encode(text))
except ImportError:
    # Fallback to regex estimate if tiktoken isn't installed yet
    def _count_tokens_impl(text: str) -> int:
        words = len(re.findall(r'\b\w+\b', text))
        punctuation = len(re.findall(r'[^\w\s]', text))
        return int(words * 1.3 + punctuation * 0.5) or 1

# Initialize MCP server
mcp = FastMCP("MemoryBridge")

# Config
MEMORY_FILE = Path.home() / "memorybridge" / "memory.json"
ANALYTICS_FILE = Path.home() / "memorybridge" / "analytics.json"
DEFAULT_PROFILE = "default"
MAX_TOKENS_DEFAULT = 4000
SEARCH_LIMIT_DEFAULT = 5       # max results returned by search_memory
SEARCH_MAX_TOKENS_DEFAULT = 800  # token cap per search_memory call
MAX_TOTAL_TOKENS = 50000  # Threshold for auto-pruning
ARCHIVE_SCORE_THRESHOLD = 0.15  # Auto-archive below this
ANALYTICS_FLUSH_EVERY = 10  # Write analytics to disk every N events

# Valid categories
VALID_CATEGORIES = [
    "preference", "fact", "insight", "decision",
    "project_status", "relationship", "skill", "constraint"
]
IMPORTANCE_LEVELS = ["low", "medium", "high", "critical"]

# =============================================================================
# FIX 3: ANALYTICS BUFFER — accumulate in memory, flush every N events
# Previously: every get_memory/search_memory call did a full analytics read+write
# Now: buffer events in-process, flush to disk every ANALYTICS_FLUSH_EVERY calls
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
                "savings_estimate": {
                    "baseline_tokens_per_session": 8000
                }
            }

        for session in _analytics_buffer:
            analytics["sessions"].append(session)

            date_key = session["date"]
            if date_key not in analytics["daily_stats"]:
                analytics["daily_stats"][date_key] = {"tokens_served": 0, "sessions": 0, "memories_returned": 0}
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

        # Cap session history
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


# Flush on clean shutdown so we don't lose buffered events
atexit.register(_flush_analytics)


def log_to_analytics(
    tokens_served: int,
    memories_returned: int,
    model: str = "claude",
    profile: str = "default",
    operation: str = "get_memory"
) -> None:
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


def get_profile_token_stats(profile_data: dict) -> dict:
    memories = profile_data.get("memories", [])
    total_tokens = sum(m.get("token_count", count_memory_tokens(m)) for m in memories)
    by_category = {}
    for mem in memories:
        cat = mem.get("category", "other")
        tokens = mem.get("token_count", count_memory_tokens(mem))
        by_category[cat] = by_category.get(cat, 0) + tokens
    return {
        "total_tokens": total_tokens,
        "memory_count": len(memories),
        "avg_tokens_per_memory": total_tokens // len(memories) if memories else 0,
        "by_category": by_category
    }


def should_prune(memory: dict, profile: str) -> bool:
    profile_data = memory.get("profiles", {}).get(profile, {})
    stats = get_profile_token_stats(profile_data)
    return stats["total_tokens"] > MAX_TOTAL_TOKENS


def auto_prune(memory: dict, profile: str) -> list:
    profile_data = memory.get("profiles", {}).get(profile, {})
    memories = profile_data.get("memories", [])
    decay_config = memory.get("schema", {}).get("decay_config", {})
    memories = apply_decay(memories, decay_config)
    to_archive = []
    to_keep = []
    for mem in memories:
        if mem.get("effective_score", 1.0) < ARCHIVE_SCORE_THRESHOLD:
            to_archive.append(mem)
        else:
            to_keep.append(mem)
    if to_archive:
        if "archived" not in profile_data:
            profile_data["archived"] = []
        for mem in to_archive:
            mem["archived_at"] = datetime.now().strftime("%Y-%m-%d")
            mem["archive_reason"] = "auto_prune_low_score"
            profile_data["archived"].append(mem)
        profile_data["memories"] = to_keep
    return [m["id"] for m in to_archive]


# =============================================================================
# CORE UTILITIES
# =============================================================================

# Lock file used to serialise concurrent readers/writers (e.g. Claude Desktop
# running alongside Claude Code).  We use a dedicated .lock file so that the
# lock fd stays open for the full read-modify-write cycle regardless of whether
# memory.json itself exists yet.
LOCK_FILE = MEMORY_FILE.parent / "memory.lock"


from contextlib import contextmanager

@contextmanager
def _memory_lock():
    """Exclusive POSIX file lock over the full read-modify-write cycle.

    Both processes must acquire this lock before touching memory.json, so a
    concurrent pair of Claude instances can't interleave their reads and writes
    and silently drop each other's data.
    """
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    lf = open(LOCK_FILE, "a")      # 'a' creates the file if absent, never truncates
    try:
        fcntl.flock(lf, fcntl.LOCK_EX)   # blocks until we own the lock
        yield
    finally:
        fcntl.flock(lf, fcntl.LOCK_UN)
        lf.close()


def load_memory() -> dict:
    if not MEMORY_FILE.exists():
        return {
            "version": "1.3",
            "last_updated": datetime.now().strftime("%Y-%m-%d"),
            "access_log": [],
            "token_usage": {
                "total_served": 0,
                "by_profile": {}
            },
            "profiles": {},
            "schema": {
                "memory_categories": VALID_CATEGORIES,
                "importance_levels": IMPORTANCE_LEVELS,
                "decay_config": {
                    "enabled": True,
                    "half_life_days": 30,
                    "min_score": 0.1,
                    "boost_on_access": 0.1
                },
                "token_config": {
                    "max_total_tokens": MAX_TOTAL_TOKENS,
                    "archive_threshold": ARCHIVE_SCORE_THRESHOLD,
                    "auto_prune": True
                }
            }
        }
    with open(MEMORY_FILE, "r") as f:
        data = json.load(f)
    if data.get("version") not in ("1.2", "1.3"):
        data["version"] = "1.3"
        if "token_usage" not in data:
            data["token_usage"] = {"total_served": 0, "by_profile": {}}
    return data


def save_memory(data: dict) -> None:
    MEMORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    data["last_updated"] = datetime.now().strftime("%Y-%m-%d")
    # Atomic write: write to temp file then rename to avoid corruption
    tmp = MEMORY_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    tmp.replace(MEMORY_FILE)


def log_access(memory: dict, action: str, profile: str, details: str = "", tokens_served: int = 0) -> None:
    if "access_log" not in memory:
        memory["access_log"] = []
    log_entry = {
        "timestamp": datetime.now().isoformat(),
        "action": action,
        "profile": profile,
        "details": details,
        "tokens_served": tokens_served
    }
    memory["access_log"].append(log_entry)
    if tokens_served > 0:
        if "token_usage" not in memory:
            memory["token_usage"] = {"total_served": 0, "by_profile": {}}
        memory["token_usage"]["total_served"] += tokens_served
        profile_usage = memory["token_usage"].get("by_profile", {})
        profile_usage[profile] = profile_usage.get(profile, 0) + tokens_served
        memory["token_usage"]["by_profile"] = profile_usage
    if len(memory["access_log"]) > 1000:
        memory["access_log"] = memory["access_log"][-1000:]


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


def boost_on_access(memory: dict, mem_id: str, profile: str) -> None:
    profile_data = memory.get("profiles", {}).get(profile, {})
    memories = profile_data.get("memories", [])
    decay_config = memory.get("schema", {}).get("decay_config", {})
    boost = decay_config.get("boost_on_access", 0.1)
    for mem in memories:
        if mem.get("id") == mem_id:
            mem["last_accessed"] = datetime.now().strftime("%Y-%m-%d")
            mem["access_count"] = mem.get("access_count", 0) + 1
            mem["relevance_score"] = min(mem.get("relevance_score", 1.0) + boost, 1.0)
            break


def generate_memory_id() -> str:
    return f"mem_{uuid.uuid4().hex[:8]}"


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

    FIX 2: Budget now accounts for ALL response fields (identity, projects,
    model_preferences) not just memories. Previously these fields were returned
    outside the budget, silently overflowing the caller's context window.

    Args:
        profile: Memory profile to load
        context_hint: Optional hint to filter relevant memory
        category: Optional category filter
        max_tokens: Maximum tokens to return (default 4000, enforced across entire response)
        compress: Whether to compress memories to fit budget (default True)
    Returns:
        JSON with memories, token stats, and budget info
    """
    with _memory_lock():
        memory = load_memory()
        if profile not in memory.get("profiles", {}):
            return json.dumps({"error": f"Profile '{profile}' not found"})

        profile_data = memory["profiles"][profile]
        decay_config = memory.get("schema", {}).get("decay_config", {})
        memories = profile_data.get("memories", [])
        memories = apply_decay([m.copy() for m in memories], decay_config)

        for mem in memories:
            if "token_count" not in mem:
                mem["token_count"] = count_memory_tokens(mem)

        if category:
            if category not in VALID_CATEGORIES:
                return json.dumps({"error": f"Invalid category. Valid: {VALID_CATEGORIES}"})
            memories = [m for m in memories if m.get("category") == category]

        if context_hint:
            hint_lower = context_hint.lower()
            memories = [
                m for m in memories
                if hint_lower in m.get("content", "").lower()
                or hint_lower in str(m.get("tags", [])).lower()
                or hint_lower in str(m.get("project_id", "")).lower()
            ]

        memories.sort(key=lambda m: m.get("effective_score", 0), reverse=True)

        # FIX 2: Measure overhead of ALL non-memory fields first, then budget memories
        # into whatever remains. Previously only identity was measured; projects and
        # model_preferences were added to the response without any token accounting.
        identity = profile_data.get("identity", {})
        projects = profile_data.get("projects", [])
        model_preferences = profile_data.get("model_preferences", {})

        overhead_tokens = (
            count_tokens(json.dumps(identity)) +
            count_tokens(json.dumps(projects)) +
            count_tokens(json.dumps(model_preferences)) +
            200  # structural JSON overhead
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

        log_access(
            memory, "get_memory", profile,
            f"hint={context_hint}, cat={category}, budget={max_tokens}",
            tokens_served=total_tokens_served
        )
        save_memory(memory)

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
    Add a new memory with automatic token counting.
    Triggers auto-prune if over budget.

    Args:
        content: The memory content to store
        category: Category type
        importance: Importance level
        tags: Optional tags
        project_id: Optional project association
        profile: Memory profile
    Returns:
        Confirmation with memory ID and token count
    """
    if category not in VALID_CATEGORIES:
        return json.dumps({"error": f"Invalid category. Valid: {VALID_CATEGORIES}"})
    if importance not in IMPORTANCE_LEVELS:
        return json.dumps({"error": f"Invalid importance. Valid: {IMPORTANCE_LEVELS}"})

    with _memory_lock():
        memory = load_memory()
        if profile not in memory.get("profiles", {}):
            memory["profiles"][profile] = {
                "identity": {},
                "memories": [],
                "projects": [],
                "model_preferences": {}
            }

        profile_data = memory["profiles"][profile]
        if "memories" not in profile_data:
            profile_data["memories"] = []

        now = datetime.now().strftime("%Y-%m-%d")
        mem_id = generate_memory_id()
        token_count = count_tokens(content) + count_tokens(" ".join(tags or [])) + 20

        new_memory = {
            "id": mem_id,
            "content": content,
            "category": category,
            "relevance_score": 1.0,
            "importance": importance,
            "created_at": now,
            "last_accessed": now,
            "access_count": 0,
            "tags": tags or [],
            "project_id": project_id,
            "token_count": token_count
        }
        profile_data["memories"].append(new_memory)

        pruned = []
        if should_prune(memory, profile):
            pruned = auto_prune(memory, profile)

        log_access(memory, "add_memory", profile, f"id={mem_id}, tokens={token_count}")
        save_memory(memory)

    result = {
        "status": "added",
        "memory_id": mem_id,
        "category": category,
        "importance": importance,
        "token_count": token_count,
        "profile": profile
    }
    if pruned:
        result["auto_pruned"] = pruned
        result["prune_reason"] = f"Total tokens exceeded {MAX_TOTAL_TOKENS}"
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
    Batch-add multiple facts in a single save operation.

    FIX 1: Previously called add_memory() in a loop, causing N file reads and
    N file writes for N facts. Now validates all facts upfront, appends all
    memories to the in-memory dict, then saves exactly once.

    Args:
        facts: List of facts to remember
        category: Category for all facts
        importance: Importance level for all facts
        project: Optional project association
        profile: Memory profile
    Returns:
        Summary with all added memory IDs and total tokens
    """
    # Validate inputs before touching disk
    if category not in VALID_CATEGORIES:
        return json.dumps({"error": f"Invalid category. Valid: {VALID_CATEGORIES}"})
    if importance not in IMPORTANCE_LEVELS:
        return json.dumps({"error": f"Invalid importance. Valid: {IMPORTANCE_LEVELS}"})
    if not facts:
        return json.dumps({"error": "facts list is empty"})

    with _memory_lock():
        # Single load
        memory = load_memory()
        if profile not in memory.get("profiles", {}):
            memory["profiles"][profile] = {
                "identity": {},
                "memories": [],
                "projects": [],
                "model_preferences": {}
            }

        profile_data = memory["profiles"][profile]
        if "memories" not in profile_data:
            profile_data["memories"] = []

        now = datetime.now().strftime("%Y-%m-%d")
        changes = []
        total_tokens = 0

        # Append all facts in memory — no disk I/O per fact
        for fact in facts:
            mem_id = generate_memory_id()
            token_count = count_tokens(fact) + 20
            profile_data["memories"].append({
                "id": mem_id,
                "content": fact,
                "category": category,
                "relevance_score": 1.0,
                "importance": importance,
                "created_at": now,
                "last_accessed": now,
                "access_count": 0,
                "tags": [],
                "project_id": project,
                "token_count": token_count
            })
            total_tokens += token_count
            changes.append({
                "memory_id": mem_id,
                "tokens": token_count,
                "preview": fact[:60] + ("…" if len(fact) > 60 else "")
            })

        # Check prune once after all facts are added
        pruned = []
        if should_prune(memory, profile):
            pruned = auto_prune(memory, profile)

        log_access(memory, "update_memory", profile, f"added {len(changes)} memories, {total_tokens} tokens")

        # Single save
        save_memory(memory)

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
    Search memories with optional token budget.
    """
    with _memory_lock():
        memory = load_memory()
        if profile not in memory.get("profiles", {}):
            return json.dumps({"error": f"Profile '{profile}' not found"})

        profile_data = memory["profiles"][profile]
        decay_config = memory.get("schema", {}).get("decay_config", {})
        memories = profile_data.get("memories", [])
        memories = apply_decay([m.copy() for m in memories], decay_config)

        if category:
            if category not in VALID_CATEGORIES:
                return json.dumps({"error": f"Invalid category. Valid: {VALID_CATEGORIES}"})
            memories = [m for m in memories if m.get("category") == category]

        query_lower = query.lower()
        query_terms = query_lower.split()
        results = []
        for mem in memories:
            content_lower = mem.get("content", "").lower()
            tags_str = " ".join(mem.get("tags", [])).lower()
            project_id = str(mem.get("project_id", "")).lower()
            searchable = f"{content_lower} {tags_str} {project_id}"
            match_count = sum(1 for term in query_terms if term in searchable)
            if match_count > 0:
                mem["match_score"] = match_count / len(query_terms)
                mem["combined_score"] = mem["match_score"] * mem.get("effective_score", 1.0)
                if "token_count" not in mem:
                    mem["token_count"] = count_memory_tokens(mem)
                results.append(mem)

        results.sort(key=lambda m: m.get("combined_score", 0), reverse=True)

        if max_tokens:
            budget_results = []
            tokens_used = 0
            for mem in results:
                mem_tokens = mem.get("token_count", 0)
                if tokens_used + mem_tokens <= max_tokens:
                    budget_results.append(mem)
                    tokens_used += mem_tokens
                if len(budget_results) >= limit:
                    break
            results = budget_results
        else:
            results = results[:limit]

        for mem in results:
            boost_on_access(memory, mem["id"], profile)

        tokens_served = sum(m.get("token_count", 0) for m in results)
        log_access(memory, "search_memory", profile, f"query='{query}', results={len(results)}", tokens_served)
        save_memory(memory)

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
        "results": results,
        "total_matches": len(results),
        "tokens_served": tokens_served
    }, indent=2)


@mcp.tool()
def delete_memory(
    memory_id: str,
    profile: str = DEFAULT_PROFILE
) -> str:
    """Delete a specific memory by ID."""
    with _memory_lock():
        memory = load_memory()
        if profile not in memory.get("profiles", {}):
            return json.dumps({"error": f"Profile '{profile}' not found"})

        profile_data = memory["profiles"][profile]
        memories = profile_data.get("memories", [])
        deleted_mem = None
        for m in memories:
            if m.get("id") == memory_id:
                deleted_mem = m
                break

        if not deleted_mem:
            return json.dumps({"error": f"Memory '{memory_id}' not found"})

        tokens_freed = deleted_mem.get("token_count", count_memory_tokens(deleted_mem))
        profile_data["memories"] = [m for m in memories if m.get("id") != memory_id]
        log_access(memory, "delete_memory", profile, f"id={memory_id}, freed={tokens_freed} tokens")
        save_memory(memory)

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
    memory = load_memory()
    global_usage = memory.get("token_usage", {"total_served": 0, "by_profile": {}})

    if profile == "all":
        all_profiles = {}
        total_stored = 0
        for p_name, p_data in memory.get("profiles", {}).items():
            stats = get_profile_token_stats(p_data)
            all_profiles[p_name] = stats
            total_stored += stats["total_tokens"]
        return json.dumps({
            "global": {
                "total_tokens_stored": total_stored,
                "total_tokens_served": global_usage["total_served"],
                "max_budget": MAX_TOTAL_TOKENS,
                "utilization": f"{(total_stored / MAX_TOTAL_TOKENS) * 100:.1f}%"
            },
            "by_profile": all_profiles,
            "served_by_profile": global_usage.get("by_profile", {})
        }, indent=2)

    if profile not in memory.get("profiles", {}):
        return json.dumps({"error": f"Profile '{profile}' not found"})

    profile_data = memory["profiles"][profile]
    stats = get_profile_token_stats(profile_data)
    archived = profile_data.get("archived", [])
    archived_tokens = sum(m.get("token_count", count_memory_tokens(m)) for m in archived)

    return json.dumps({
        "profile": profile,
        "active": stats,
        "archived": {
            "count": len(archived),
            "tokens": archived_tokens
        },
        "served_total": global_usage.get("by_profile", {}).get(profile, 0),
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
    with _memory_lock():
        memory = load_memory()
        if profile not in memory.get("profiles", {}):
            return json.dumps({"error": f"Profile '{profile}' not found"})

        profile_data = memory["profiles"][profile]
        memories = profile_data.get("memories", [])
        decay_config = memory.get("schema", {}).get("decay_config", {})
        threshold = threshold or ARCHIVE_SCORE_THRESHOLD
        memories = apply_decay([m.copy() for m in memories], decay_config)
        to_prune = [m for m in memories if m.get("effective_score", 1.0) < threshold]
        tokens_to_free = sum(m.get("token_count", count_memory_tokens(m)) for m in to_prune)

        if dry_run:
            return json.dumps({
                "dry_run": True,
                "would_prune": len(to_prune),
                "tokens_would_free": tokens_to_free,
                "memories": [{"id": m["id"], "score": m.get("effective_score"), "content": m["content"][:50]} for m in to_prune]
            }, indent=2)

        pruned_ids = auto_prune(memory, profile)
        log_access(memory, "prune_memories", profile, f"pruned {len(pruned_ids)}, freed ~{tokens_to_free} tokens")
        save_memory(memory)

    return json.dumps({
        "status": "pruned",
        "pruned_count": len(pruned_ids),
        "tokens_freed": tokens_to_free,
        "pruned_ids": pruned_ids,
        "profile": profile
    }, indent=2)


@mcp.tool()
def switch_profile(profile_name: str) -> str:
    """Switch active persona context."""
    with _memory_lock():
        memory = load_memory()
        if profile_name not in memory.get("profiles", {}):
            available = list(memory.get("profiles", {}).keys())
            return json.dumps({
                "error": f"Profile '{profile_name}' not found",
                "available_profiles": available
            })

        profile_data = memory["profiles"][profile_name]
        stats = get_profile_token_stats(profile_data)
        log_access(memory, "switch_profile", profile_name, "")
        save_memory(memory)

    return json.dumps({
        "status": "switched",
        "profile": profile_name,
        "identity": profile_data.get("identity", {}),
        "memory_count": len(profile_data.get("memories", [])),
        "project_count": len(profile_data.get("projects", [])),
        "token_stats": stats
    }, indent=2)


@mcp.tool()
def list_projects(profile: str = DEFAULT_PROFILE) -> str:
    """List all projects with status."""
    with _memory_lock():
        memory = load_memory()
        if profile not in memory.get("profiles", {}):
            return json.dumps({"error": f"Profile '{profile}' not found"})

        projects = memory["profiles"][profile].get("projects", [])
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
        log_access(memory, "list_projects", profile, "")
        save_memory(memory)

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
    """
    memory = load_memory()
    log = memory.get("access_log", [])
    recent = log[-limit:] if len(log) > limit else log
    result = {
        "entries": list(reversed(recent)),
        "total_logged": len(log),
        "returned": len(recent)
    }
    if include_tokens:
        result["token_summary"] = {
            "total_served": memory.get("token_usage", {}).get("total_served", 0),
            "by_profile": memory.get("token_usage", {}).get("by_profile", {})
        }
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
    Compresses content to fit.

    Args:
        model: Target model (chatgpt, gemini, ollama)
        profile: Memory profile to export
        depth: Export depth (full, summary, minimal)
        max_tokens: Token budget for export (default 2000)
    """
    with _memory_lock():
        memory = load_memory()
        if profile not in memory.get("profiles", {}):
            return json.dumps({"error": f"Profile '{profile}' not found"})

        profile_data = memory["profiles"][profile]
        identity = profile_data.get("identity", {})
        memories = profile_data.get("memories", [])
        projects = profile_data.get("projects", [])
        prefs = profile_data.get("model_preferences", {}).get(model, {})
        decay_config = memory.get("schema", {}).get("decay_config", {})
        memories = apply_decay([m.copy() for m in memories], decay_config)
        memories.sort(key=lambda m: m.get("effective_score", 0), reverse=True)

        budgets = {
            "full": max_tokens,
            "summary": max_tokens // 2,
            "minimal": max_tokens // 4
        }
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
        log_access(memory, "export_for_model", profile, f"model={model}, tokens={final_tokens}", final_tokens)
        save_memory(memory)

    log_to_analytics(
        tokens_served=final_tokens,
        memories_returned=len([m for m in memories if m.get("effective_score", 0) > 0.3]),
        model=model,
        profile=profile,
        operation="export_for_model"
    )
    return export_text


if __name__ == "__main__":
    mcp.run()
