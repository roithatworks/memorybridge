"""Core memory CRUD, search, reflection, consolidation, profile, and pruning MCP tools."""

import json
import logging
from datetime import datetime
from typing import Optional

from db.constants import VALID_CATEGORIES, IMPORTANCE_LEVELS, _count_tokens
from db.pruner import run_auto_prune, record_outcome, get_pruner_report, AUTO_EXECUTE_THRESHOLD
from db.store import GuardrailRejection, DuplicateContentError
from exports import count_tokens, apply_decay, DECAY_CONFIG, UNTRUSTED_NOTICE
import server as _srv

logger = logging.getLogger(__name__)
mcp = _srv.mcp


@mcp.tool()
def get_memory(
    profile: str = None,
    context_hint: Optional[str] = None,
    category: Optional[str] = None,
    max_tokens: int = _srv.MAX_TOKENS_DEFAULT,
    compress: bool = True,
    min_confidence: float = 0.0,
    as_of: Optional[str] = None
) -> str:
    """
    Retrieve relevant memory slice within token budget.

    Args:
        profile: Memory profile to load
        context_hint: Optional hint to filter relevant memory
        category: Optional category filter
        max_tokens: Maximum tokens to return (default 4000)
        compress: Whether to compress memories to fit budget (default True)
        min_confidence: Minimum confidence threshold (default 0.0)
        as_of: Optional ISO timestamp for epistemic time travel (e.g. 2026-03-01T00:00:00)
    Returns:
        JSON with memories, token stats, and budget info
    """
    try:
        profile = _srv._resolve_profile(profile)
    except PermissionError as e:
        return json.dumps({"error": str(e), "status": "forbidden"})
    _srv._store.ensure_profile(profile)
    profile_data = _srv._store.get_profile(profile)
    if profile_data is None:
        return json.dumps({"error": f"Profile '{profile}' not found"})

    if context_hint:
        # Use hybrid BM25+semantic search for context_hint so phrasing
        # variants (e.g. "job search" vs "Director+ PM role") are matched.
        # Merge with a full get_memories pull so non-hint memories fill the
        # remaining token budget in decay-score order.
        hint_results = _srv._store.search_hybrid(
            profile, context_hint, category=category,
            limit=20, max_tokens=_srv.MAX_TOKENS_DEFAULT,
            min_confidence=min_confidence,
            as_of=as_of
        )
        hint_ids = {m["id"] for m in hint_results}

        # Full list for budget fill — apply decay, exclude hint hits (added first)
        all_memories = _srv._store.get_memories(profile, category=category, min_confidence=min_confidence, as_of=as_of)
        all_memories = apply_decay([m.copy() for m in all_memories], DECAY_CONFIG)
        remainder = [m for m in all_memories if m["id"] not in hint_ids]
        remainder.sort(key=lambda m: m.get("effective_score", 0), reverse=True)

        memories = hint_results + remainder
    else:
        memories = _srv._store.get_memories(profile, category=category, min_confidence=min_confidence, as_of=as_of)
        memories = apply_decay([m.copy() for m in memories], DECAY_CONFIG)
        memories.sort(key=lambda m: m.get("effective_score", 0), reverse=True)

    identity = profile_data["identity"]
    projects = profile_data["projects"]
    model_preferences = profile_data["model_preferences"]

    # #179: measure the real serialized cost of everything fixed in the
    # response besides the memories array, rather than summing
    # identity/projects/model_preferences measured compact (no indent, no
    # surrounding keys) plus a flat 200-token guess for the gap.
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
                       count_tokens(UNTRUSTED_NOTICE) + 60)
    available_for_memories = max(max_tokens - overhead_tokens, 0)

    selected_memories = []
    tokens_used = 0
    for mem in memories:
        mem_tokens = count_tokens(json.dumps(_srv._clean_result(mem), indent=2))
        if tokens_used + mem_tokens <= available_for_memories:
            selected_memories.append(mem)
            tokens_used += mem_tokens
        elif compress and tokens_used < available_for_memories:
            remaining = available_for_memories - tokens_used
            compressed = _srv.compress_memory(mem, target_tokens=remaining - 20)
            compressed_tokens = count_tokens(json.dumps(_srv._clean_result(compressed), indent=2))
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
            "memories": [_srv._clean_result(m) for m in mems],
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
        if mems:
            r["_security_notice"] = UNTRUSTED_NOTICE
        return r

    response = _build_response(selected_memories)

    while selected_memories and count_tokens(json.dumps(response, indent=2)) > max_tokens:
        selected_memories.pop()
        response = _build_response(selected_memories)

    total_tokens_served = count_tokens(json.dumps(response, indent=2))
    response["token_stats"]["served"] = total_tokens_served
    response["token_stats"]["remaining"] = max(max_tokens - total_tokens_served, 0)
    total_tokens_served = count_tokens(json.dumps(response, indent=2))
    response["token_stats"]["served"] = total_tokens_served
    response["token_stats"]["remaining"] = max(max_tokens - total_tokens_served, 0)

    _srv._store.log_access("get_memory", profile,
                           f"hint={context_hint}, cat={category}, budget={max_tokens}",
                           tokens_served=total_tokens_served)
    _srv.log_to_analytics(
        tokens_served=total_tokens_served,
        memories_returned=len(selected_memories),
        model=_srv._caller_model(),
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
    client_name: Optional[str] = None,
    confidence: float = 1.0,
    expires_at: Optional[str] = None,
    status: str = "active",
    why_it_matters: Optional[str] = None,
    valid_from: Optional[str] = None,
    valid_until: Optional[str] = None
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
        confidence: Confidence score between 0.0 and 1.0 (default 1.0)
        expires_at: Optional ISO timestamp when this memory expires (TTL)
        status: Status indicator (default 'active')
        why_it_matters: Optional instruction on how the model should act on this
            fact (e.g. "Format all responses as bullet lists")
        valid_from: Optional ISO timestamp when fact became valid in reality (defaults to now)
        valid_until: Optional ISO timestamp when fact ceases validity (defaults to expires_at)
    Returns:
        Confirmation with memory ID and token count, or duplicate status
    """
    try:
        profile = _srv._validate_profile(profile)
    except PermissionError as e:
        return json.dumps({"error": str(e), "status": "forbidden"})
    if _srv._REMOTE_MODE:
        existing = set(_srv._store.list_profiles())
        if profile not in existing:
            profile = _srv.DEFAULT_PROFILE
        # Security: remote callers cannot mass-archive memories via supersedes
        if supersedes:
            supersedes = None
        # Security: remote callers cannot inject behavioral instructions/beliefs
        if why_it_matters:
            why_it_matters = None
    elif why_it_matters:
        why_it_matters = _srv._sanitize_why_it_matters(why_it_matters)

    if category not in VALID_CATEGORIES:
        return json.dumps({"error": f"Invalid category. Valid: {VALID_CATEGORIES}"})
    if importance not in IMPORTANCE_LEVELS:
        return json.dumps({"error": f"Invalid importance. Valid: {IMPORTANCE_LEVELS}"})

    try:
        mid = _srv._store.add_memory(profile, content,
                                     category=category, importance=importance,
                                     tags=tags, project_id=project_id,
                                     supersedes=supersedes, source=_srv._caller_model(),
                                     client_name=_srv._resolve_client_name(client_name),
                                     confidence=confidence, expires_at=expires_at, status=status,
                                     why_it_matters=why_it_matters,
                                     origin_type="manual",
                                     valid_from=valid_from,
                                     valid_until=valid_until)
    except GuardrailRejection as e:
        return json.dumps({"status": "rejected", "reason": str(e)})
    if mid is None:
        return json.dumps({"status": "duplicate", "reason": "identical content already exists"})

    token_count = _count_tokens(content)

    # Budget-based prune (existing behaviour)
    stats = _srv._store.token_stats(profile)
    budget_pruned = []
    if stats["total_tokens"] > _srv.MAX_TOTAL_TOKENS:
        budget_pruned = _srv._store.auto_prune(profile, threshold=_srv.ARCHIVE_SCORE_THRESHOLD)

    prune_result = run_auto_prune(
        _srv._store._conn, profile,
        lambda p, mid: _srv._store.archive_memory(p, mid, reason="pruned: auto (dedup/stale)"),
        allow_auto_delete=not _srv._REMOTE_MODE)

    _srv._store.log_access("add_memory", profile, f"id={mid}, tokens={token_count}")

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
        result["prune_reason"] = f"Total tokens exceeded {_srv.MAX_TOTAL_TOKENS}"
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
    try:
        profile = _srv._resolve_profile(profile)
    except PermissionError as e:
        return json.dumps({"error": str(e), "status": "forbidden"})
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

    for fact in facts:
        try:
            mid = _srv._store.add_memory(profile, fact,
                                         category=category, importance=importance,
                                         project_id=project, source=_srv._caller_model(),
                                         client_name=_srv._resolve_client_name(client_name),
                                         origin_type="manual")
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

    pruned = []
    stats = _srv._store.token_stats(profile)
    if stats["total_tokens"] > _srv.MAX_TOTAL_TOKENS:
        pruned = _srv._store.auto_prune(profile, threshold=_srv.ARCHIVE_SCORE_THRESHOLD)

    _srv._store.log_access(
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
    profile: str = None,
    why_it_matters: Optional[str] = None
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
        why_it_matters: Updated instruction on how the model should use this fact (optional)
    Returns:
        JSON confirmation, or {"error": ...} if memory_id not found / validation fails
    """
    try:
        profile = _srv._resolve_profile(profile)
    except PermissionError as e:
        return json.dumps({"error": str(e), "status": "forbidden"})
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
    if why_it_matters is not None:
        kwargs["why_it_matters"] = why_it_matters

    try:
        updated = _srv._store.edit_memory(profile, memory_id, **kwargs)
    except DuplicateContentError as e:
        return json.dumps({"error": f"duplicate content: {e}"})
    if not updated:
        return json.dumps({"error": f"memory_id '{memory_id}' not found in profile '{profile}'"})

    _srv._store.log_access("edit_memory", profile, f"id={memory_id}, fields={list(kwargs.keys())}")
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
    limit: int = _srv.SEARCH_LIMIT_DEFAULT,
    max_tokens: int = _srv.SEARCH_MAX_TOKENS_DEFAULT,
    profile: str = None,
    recency_boost: bool = True,
    include_related: bool = False,
    min_confidence: float = 0.0,
    search_mode: str = "hybrid",
    as_of: Optional[str] = None,
) -> str:
    """
    Search memories using BM25, semantic, or hybrid (RRF) search.

    Args:
        query: Search query
        category: Optional category filter
        limit: Max results (default 5)
        max_tokens: Token cap (default 800)
        profile: Memory profile
        recency_boost: Apply recency weighting (default: true when configured)
        include_related: Include related memories by entity tag overlap (default: false)
        min_confidence: Minimum confidence threshold (default 0.0)
        search_mode: Mode to search with ('hybrid', 'keyword', or 'semantic')
        as_of: Optional ISO timestamp for epistemic time travel (e.g. 2026-03-01T00:00:00)
    Returns:
        JSON with ranked results (internal fields stripped)
    """
    try:
        profile = _srv._resolve_profile(profile)
    except PermissionError as e:
        return json.dumps({"error": str(e), "status": "forbidden"})
    _srv._store.ensure_profile(profile)

    if category and category not in VALID_CATEGORIES:
        return json.dumps({"error": f"Invalid category. Valid: {VALID_CATEGORIES}"})

    if search_mode not in ("hybrid", "keyword", "semantic"):
        return json.dumps({"error": "Invalid search_mode. Use 'hybrid', 'keyword', or 'semantic'"})

    if search_mode == "keyword":
        results = _srv._store.search(profile, query, category=category,
                                     limit=limit, max_tokens=max_tokens,
                                     recency_boost=recency_boost,
                                     include_related=include_related,
                                     min_confidence=min_confidence,
                                     as_of=as_of)
    elif search_mode == "semantic":
        results = _srv._store.search_semantic(profile, query, limit=limit, max_tokens=max_tokens,
                                              min_confidence=min_confidence, as_of=as_of)
    else:
        results = _srv._store.search_hybrid(profile, query, category=category,
                                            limit=limit, max_tokens=max_tokens,
                                            recency_boost=recency_boost,
                                            include_related=include_related,
                                            min_confidence=min_confidence,
                                            as_of=as_of)

    if not as_of:
        _srv._store.boost_batch(profile, [m["id"] for m in results],
                                boost=DECAY_CONFIG.get("boost_on_access", 0.1))

    response = {
        "query": query,
        "profile": profile,
        "results": [_srv._clean_result(m) for m in results],
        "total_matches": len(results),
        "tokens_served": 0,
    }
    if results:
        response["_security_notice"] = UNTRUSTED_NOTICE

    tokens_served = count_tokens(json.dumps(response, indent=2))
    response["tokens_served"] = tokens_served
    tokens_served = count_tokens(json.dumps(response, indent=2))
    response["tokens_served"] = tokens_served

    _srv._store.log_access("search_memory", profile,
                           f"query='{query}', results={len(results)}", tokens_served)
    _srv.log_to_analytics(
        tokens_served=tokens_served,
        memories_returned=len(results),
        model=_srv._caller_model(),
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
    try:
        profile = _srv._resolve_profile(profile)
    except PermissionError as e:
        return json.dumps({"error": str(e), "status": "forbidden"})
    _srv._store.ensure_profile(profile)

    result = _srv._store.reflect(profile, question, limit=limit, max_tokens=max_tokens)
    return json.dumps(result, indent=2, default=str)


@mcp.tool()
def consolidate_session(
    session_notes: str,
    profile: Optional[str] = None,
    project_id: Optional[str] = None
) -> str:
    """
    Session consolidation tool (Memory Promotion System).
    Analyzes raw session notes/events, deduplicates against existing store,
    and synthesizes durable candidate facts, decisions, and procedural rules.

    Args:
        session_notes: Raw events, tool outputs, or bullet points from the session
        profile: Memory profile (default: current active profile)
        project_id: Optional project association
    Returns:
        JSON summary of promoted, duplicate, or rejected candidate memories
    """
    try:
        profile = _srv._resolve_profile(profile)
    except PermissionError as e:
        return json.dumps({"error": str(e), "status": "forbidden"})
    _srv._store.ensure_profile(profile)

    lines = [ln.strip() for ln in session_notes.splitlines() if ln.strip()]
    candidates = []
    for line in lines:
        if line.startswith(("- ", "* ", "1. ", "2. ", "3. ", "4. ", "5. ")):
            clean = line.lstrip("-*0123456789. ").strip()
            if len(clean) > 10:
                candidates.append(clean)
    if not candidates:
        candidates = [session_notes.strip()]

    added = []
    duplicates = []
    rejected = []

    for candidate in candidates[:5]:
        cat = "procedural" if any(w in candidate.lower() for w in ["step", "fix", "workflow", "how to", "use ", "command", "requires"]) else "fact"
        try:
            mid = _srv._store.add_memory(
                profile, candidate,
                category=cat,
                importance="high",
                project_id=project_id,
                source=_srv._caller_model(),
                confidence=0.9
            )
            if mid:
                added.append({"memory_id": mid, "content": candidate, "category": cat})
            else:
                duplicates.append(candidate)
        except GuardrailRejection as e:
            rejected.append({"content": candidate, "reason": str(e)})

    edges_created = 0
    if len(added) >= 2:
        added_ids = [m["memory_id"] for m in added]
        for item in added:
            mid = item["memory_id"]
            other_ids = [oid for oid in added_ids if oid != mid]
            try:
                mined = _srv._store.mine_relations(mid, candidate_ids=other_ids, profile=profile, auto_link=True)
                edges_created += len([m for m in mined if "edge_id" in m])
            except Exception as e:
                logger.debug("Failed relation mining in consolidate_session: %s", e)

    _srv._store.log_access("consolidate_session", profile, f"added={len(added)}, duplicates={len(duplicates)}, rejected={len(rejected)}, edges={edges_created}")
    return json.dumps({
        "status": "consolidated",
        "profile": profile,
        "added_count": len(added),
        "duplicate_count": len(duplicates),
        "rejected_count": len(rejected),
        "edges_created": edges_created,
        "promoted_memories": added,
        "rejected": rejected
    }, indent=2)


@mcp.tool()
def consolidate_memories(
    topic_or_entity: str,
    summary: Optional[str] = None,
    profile: Optional[str] = None,
    min_memories: int = 2,
    max_memories: int = 15,
) -> str:
    """
    Consolidate multiple episodic micro-memories about a topic or entity into a single high-density summary card.
    Constituent memories are atomically archived and linked via 'supersedes' graph edges for provenance.

    Args:
        topic_or_entity: The entity or topic to consolidate (e.g. 'auth_architecture', 'cale', 'deployment')
        summary: Optional custom synthesized summary. If omitted, synthesizes automatically.
        profile: Target profile (default: current active profile)
        min_memories: Minimum number of candidate memories required to perform consolidation (default: 2)
        max_memories: Maximum number of candidate memories to roll up (default: 15)
    Returns:
        JSON response with summary_id, archived_count, and superseded_ids
    """
    try:
        profile = _srv._resolve_profile(profile)
    except PermissionError as e:
        return json.dumps({"error": str(e), "status": "forbidden"})
    _srv._store.ensure_profile(profile)
    res = _srv._store.consolidate_memories(
        profile=profile,
        topic_or_entity=topic_or_entity,
        summary=summary,
        min_memories=min_memories,
        max_memories=max_memories,
    )
    return json.dumps(res, indent=2, default=str)


@mcp.tool()
def delete_memory(
    memory_id: str,
    profile: str = None
) -> str:
    """Delete a specific memory by ID."""
    try:
        profile = _srv._resolve_profile(profile)
    except PermissionError as e:
        return json.dumps({"error": str(e), "status": "forbidden"})
    tokens_freed = _srv._store.delete_memory(profile, memory_id)
    if tokens_freed == 0:
        if _srv._store.get_profile(profile) is None:
            return json.dumps({"error": f"Profile '{profile}' not found"})
        return json.dumps({"error": f"Memory '{memory_id}' not found"})

    _srv._store.log_access("delete_memory", profile,
                           f"id={memory_id}, freed={tokens_freed} tokens")
    return json.dumps({
        "status": "deleted",
        "memory_id": memory_id,
        "tokens_freed": tokens_freed,
        "profile": profile
    }, indent=2)


@mcp.tool()
def bulk_delete(
    ids: list[str],
    profile: str = None
) -> str:
    """
    Delete multiple memories by ID in a single operation.

    Iterates over the supplied IDs, deleting each one. Returns per-id results
    (deleted or not_found) and the total tokens freed across all successful
    deletions. Destructive — local stdio only, never exposed over the HTTP
    bridge (not in REMOTE_ALLOWED_TOOLS), per the security rule in CLAUDE.md.

    Args:
        ids: List of memory IDs to delete (e.g. ["mem_abc123", "mem_def456"])
        profile: Memory profile
    Returns:
        JSON with status, per-id results, counts, and total tokens freed
    """
    profile = profile or _srv._active_profile()
    if not ids:
        return json.dumps({"error": "ids list is empty"})
    if _srv._store.get_profile(profile) is None:
        return json.dumps({"error": f"Profile '{profile}' not found"})

    results = []
    deleted_count = 0
    not_found_count = 0
    total_tokens_freed = 0

    for mid in ids:
        tokens_freed = _srv._store.delete_memory(profile, mid)
        if tokens_freed > 0:
            deleted_count += 1
            total_tokens_freed += tokens_freed
            results.append({"id": mid, "status": "deleted", "tokens_freed": tokens_freed})
        else:
            not_found_count += 1
            results.append({"id": mid, "status": "not_found", "tokens_freed": 0})

    _srv._store.log_access("bulk_delete", profile,
                           f"requested={len(ids)}, deleted={deleted_count}, "
                           f"not_found={not_found_count}, freed={total_tokens_freed} tokens")
    return json.dumps({
        "status": "completed",
        "profile": profile,
        "requested_count": len(ids),
        "deleted_count": deleted_count,
        "not_found_count": not_found_count,
        "total_tokens_freed": total_tokens_freed,
        "results": results
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
        profile = _srv._current_profile
    if profile == "all":
        all_profiles = {}
        total_stored = 0
        for p_name in _srv._store.list_profiles():
            stats = _srv._store.token_stats(p_name)
            all_profiles[p_name] = stats
            total_stored += stats["total_tokens"]

        token_summary = _srv._store.get_access_log_token_summary()

        return json.dumps({
            "global": {
                "total_tokens_stored": total_stored,
                "total_tokens_served": token_summary["total_served"],
                "max_budget": _srv.MAX_TOTAL_TOKENS,
                "utilization": f"{(total_stored / _srv.MAX_TOTAL_TOKENS) * 100:.1f}%"
            },
            "by_profile": all_profiles,
            "served_by_profile": token_summary["by_profile"]
        }, indent=2)

    _srv._store.ensure_profile(profile)
    stats = _srv._store.token_stats(profile)
    archived_row = _srv._store.get_archived_stats(profile)
    total_served = _srv._store.get_tokens_served(profile)

    return json.dumps({
        "profile": profile,
        "active": stats,
        "archived": {
            "count": archived_row["count"],
            "tokens": archived_row["tokens"]
        },
        "served_total": total_served,
        "budget": {
            "max": _srv.MAX_TOTAL_TOKENS,
            "used": stats["total_tokens"],
            "remaining": _srv.MAX_TOTAL_TOKENS - stats["total_tokens"],
            "utilization": f"{(stats['total_tokens'] / _srv.MAX_TOTAL_TOKENS) * 100:.1f}%"
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
    profile = profile or _srv._active_profile()
    _srv._store.ensure_profile(profile)
    threshold = threshold or _srv.ARCHIVE_SCORE_THRESHOLD

    if dry_run:
        memories = _srv._store.get_memories(profile)
        memories = apply_decay([m.copy() for m in memories], DECAY_CONFIG)
        to_prune = [m for m in memories if m.get("effective_score", 1.0) < threshold]
        tokens_to_free = sum(m.get("token_count", _srv.count_memory_tokens(m)) for m in to_prune)
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

    pruned_ids = _srv._store.auto_prune(profile, threshold=threshold)
    _srv._store.log_access("prune_memories", profile,
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
    bound = _srv._BOUND_PROFILE.get()
    if bound:
        return json.dumps({
            "profiles": [bound],
            "default_profile": bound,
            "read_only": True,
        }, indent=2)
    return json.dumps({
        "profiles": _srv._store.list_profiles(),
        "default_profile": _srv.DEFAULT_PROFILE,
    }, indent=2)


@mcp.tool()
def switch_profile(profile_name: str) -> str:
    """Switch active persona context."""
    if _srv._BOUND_PROFILE.get():
        return json.dumps({"error": "Profile switching disabled for scoped token"})
    profile_data = _srv._store.get_profile(profile_name)
    if profile_data is None:
        available = _srv._store.list_profiles()
        return json.dumps({
            "error": f"Profile '{profile_name}' not found",
            "available_profiles": available
        })

    _srv._current_profile = profile_name
    stats = _srv._store.token_stats(profile_name)
    _srv._store.log_access("switch_profile", profile_name, "")
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
    profile = profile or _srv._active_profile()
    _srv._store.ensure_profile(profile)
    profile_data = _srv._store.get_profile(profile)
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
        if isinstance(p, dict)
    ]
    _srv._store.log_access("list_projects", profile, "")
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
    entries = _srv._store.get_access_log(limit=limit)
    result = {
        "entries": entries,
        "total_logged": _srv._store.get_access_log_count(),
        "returned": len(entries)
    }
    if include_tokens:
        token_summary = _srv._store.get_access_log_token_summary()
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
    profile = profile or _srv._active_profile()
    _srv._store.ensure_profile(profile)
    report = get_pruner_report(_srv._store._conn, since_days=7) if include_report else {}

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
    profile = profile or _srv._active_profile()
    _srv._store.ensure_profile(profile)
    result = record_outcome(
        _srv._store._conn, queue_id, approved,
        lambda p, mid: _srv._store.archive_memory(p, mid, reason="pruned: reviewed queue approval"))

    if "error" in result:
        return json.dumps(result)

    rule_row = _srv._store.get_rule_confidence_after(queue_id)

    result["rule_confidence_after"] = round(rule_row["confidence"], 3) if rule_row else None
    result["auto_executes_now"] = (
        rule_row["confidence"] >= AUTO_EXECUTE_THRESHOLD if rule_row else None
    )
    _srv._store.log_access("resolve_prune_queue", profile,
                           f"queue_id={queue_id}, approved={approved}")
    return json.dumps(result, indent=2)
