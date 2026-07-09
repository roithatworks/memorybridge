"""Dedup and write accepted facts to memory via server tools."""

import json
import logging
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from profile_router import route_profile  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent.parent))
from server import add_memory as _add_memory_tool, add_memories as _add_memories_tool, _store  # noqa: E402
add_memory = _add_memory_tool.fn
add_memories = _add_memories_tool.fn

logger = logging.getLogger(__name__)

# Semantic match_score (embedding cosine via search_hybrid) is the reliable
# duplicate signal — reworded dups like "$126 million in business impact" vs
# "$126M in cumulative impact" share meaning but few exact words, so the keyword
# path below can't catch them without also wrongly merging distinct facts.
# Lowered 0.80 -> 0.72 so semantically-near facts collapse on the embedding
# score. The keyword path stays HIGH (0.85) as a conservative exact-ish backstop
# that never fires on merely-similar wording.
_EXACT_MATCH_THRESHOLD = 0.72   # semantic match_score for "already exists / merge"
_KEYWORD_MERGE_THRESHOLD = 0.85  # keyword overlap backstop (conservative)


def _keyword_overlap(a: str, b: str) -> float:
    """Jaccard-style keyword overlap between two strings."""
    words_a = set(a.lower().split())
    words_b = set(b.lower().split())
    if not words_a or not words_b:
        return 0.0
    return len(words_a & words_b) / max(len(words_a), len(words_b))


def _search_existing(fact_text: str, profile: str) -> list:
    """Return search results for a fact string.

    Note: search_hybrid/search_semantic already degrade to keyword search when
    the embedding model is unavailable, so reaching this handler means a real
    query failure. Log at ERROR (not WARNING) — a swallowed failure here means
    dedup is silently off and the store will fill with duplicates."""
    try:
        return _store.search_hybrid(profile=profile, query=fact_text, limit=5)
    except Exception as e:
        logger.error("search_hybrid failed for dedup (dedup DEGRADED): %s", e)
        return []


def _batch_write(facts_by_category: dict, profile: str) -> dict:
    """
    Write groups of facts via add_memories (one call per category).

    Returns {"written": int, "rejected": [(fact, reason), ...]}. Counts the
    rows actually inserted (add_memories skips exact duplicates AND
    guardrail-rejected document-shaped facts) rather than the requested count,
    and surfaces guardrail rejections so the backfill report is honest.
    """
    written = 0
    rejected: list = []
    for category, group in facts_by_category.items():
        if not group:
            continue
        # Pick the most common importance in the group
        importances = [f.get("importance", "medium") for f in group]
        importance = max(set(importances), key=importances.count)
        fact_strings = [f["fact"] for f in group]
        project = next((f.get("project") for f in group if f.get("project")), None)
        try:
            result = add_memories(
                facts=fact_strings,
                category=category,
                importance=importance,
                project=project,
                profile=profile,
            )
            written += _count_added(result)
            # add_memories records guardrail-skipped facts on the store.
            rejected.extend(getattr(_store, "last_rejected", []) or [])
        except Exception as e:
            logger.error("add_memories failed for category '%s': %s", category, e)
    return {"written": written, "rejected": rejected}


def _count_added(result) -> int:
    """Extract the real inserted count from add_memories' return value.

    The add_memories MCP tool returns a JSON *string* like
    {"status": "updated", "count": N, ...} — NOT an int. The old code did
    `isinstance(result, int)` which was always False, so `added` was always 0
    (this is why the footer reported "Added: 0" while facts wrote fine).
    """
    if isinstance(result, int):
        return result
    if isinstance(result, dict):
        return int(result.get("count", 0))
    if isinstance(result, str):
        try:
            return int(json.loads(result).get("count", 0))
        except (json.JSONDecodeError, TypeError, ValueError):
            return 0
    return 0


def merge(accepted: list, resolved: list, source: str, profile: str = "default", preview: bool = False) -> dict:
    """
    Dedup and write accepted + resolved facts into memory.

    Args:
        accepted: Auto-accepted facts from router
        resolved: Escalated facts after resolver verdict
        source: "claude" | "chatgpt" | "gemini" (for diff report)
        profile: Memory profile to write to
        preview: If True, no writes are performed
    Returns:
        Diff report dict
    """
    changes = []
    added_count = 0
    skipped_count = 0
    merged_count = 0
    rejected_count = 0

    # Separate resolved into write-eligible and rejected
    to_write_resolved = []
    for fact in resolved:
        verdict = fact.get("verdict", "reject")
        if verdict == "reject":
            rejected_count += 1
            changes.append({"action": "rejected", "fact": fact.get("fact", "")[:80], "category": fact.get("category", "")})
        elif verdict == "merge" and fact.get("merged_fact"):
            # Replace fact text with the merged version
            merged_copy = {**fact, "fact": fact["merged_fact"]}
            to_write_resolved.append(merged_copy)
            merged_count += 1
        else:  # accept
            to_write_resolved.append(fact)

    all_to_write = accepted + to_write_resolved

    # Dedup: check each fact against existing memory AND against facts already
    # queued in THIS run before writing.
    write_queue: list[dict] = []
    queued_texts: list[str] = []  # for intra-run dedup (#59)
    for fact in all_to_write:
        fact_text = fact.get("fact", "")
        if not fact_text:
            skipped_count += 1
            continue

        # Intra-run dedup (#59): two rewordings of the same fact in one export
        # both used to pass, because dedup only checked the store (neither was
        # written yet). Also compare against what we've already queued.
        if any(_keyword_overlap(fact_text, q) >= _KEYWORD_MERGE_THRESHOLD
               for q in queued_texts):
            skipped_count += 1
            changes.append({"action": "skipped", "fact": fact_text[:80],
                            "category": fact.get("category", "")})
            continue

        existing = _search_existing(fact_text, profile)

        # Check exact-ish match against the store. Near-duplicates are NOT
        # special-cased here anymore: the old branch counted a "merge" but wrote
        # a second copy and merged nothing (#58). Let near-dups flow to the write
        # path — the store's add_memory does the real fuzzy merge on write.
        skip = False
        for mem in existing:
            score = mem.get("match_score", 0)
            if score >= _EXACT_MATCH_THRESHOLD:
                skipped_count += 1
                changes.append({"action": "skipped", "fact": fact_text[:80], "category": fact.get("category", "")})
                skip = True
                break

        if not skip:
            # Potential direct contradiction that slipped through — escalate instead of write
            has_conflict = bool(fact.get("conflicts_with"))
            if has_conflict and fact not in to_write_resolved:
                logger.warning("Late-caught conflict for fact '%s...' — skipping", fact_text[:40])
                skipped_count += 1
                changes.append({"action": "skipped", "fact": fact_text[:80], "category": fact.get("category", "")})
            else:
                write_queue.append(fact)
                queued_texts.append(fact_text)

    # Domain routing: assign each fact to a profile (job_search/consulting/
    # teaching/personal/default) by content. Done for both preview and write so
    # the preview report shows the routing. base_profile = the run's --profile.
    routed_by_profile: dict[str, int] = defaultdict(int)
    for fact in write_queue:
        dest = route_profile(fact, base_profile=profile)
        fact["_dest_profile"] = dest
        routed_by_profile[dest] += 1

    guardrail_rejected = []
    if not preview:
        # Group by (profile, category, importance, project) so each group is
        # homogeneous — otherwise the batch write collapsed every fact to the
        # group's most-common importance and the first project it found, losing
        # per-fact importance and smearing one project across unrelated facts
        # (#57). With homogeneous groups, _batch_write's per-group values are the
        # facts' real values.
        by_group: dict[tuple, list] = defaultdict(list)
        for fact in write_queue:
            key = (fact["_dest_profile"], fact.get("category", "fact"),
                   fact.get("importance", "medium"), fact.get("project"))
            by_group[key].append(fact)

        total_written = 0
        for (dest_profile, category, _imp, _proj), group in by_group.items():
            batch = _batch_write({category: group}, dest_profile)
            total_written += batch["written"]
            guardrail_rejected.extend(batch["rejected"])
        added_count = total_written

        for fact in write_queue:
            changes.append({"action": "added", "fact": fact.get("fact", "")[:80],
                            "category": fact.get("category", ""),
                            "profile": fact["_dest_profile"]})
        for fact_text, reason in guardrail_rejected:
            changes.append({"action": "guardrail_rejected",
                            "fact": str(fact_text)[:80], "reason": reason})
    else:
        # Preview mode: count what would be added, with routed profile shown.
        added_count = len(write_queue)
        for fact in write_queue:
            changes.append({"action": "would_add", "fact": fact.get("fact", "")[:80],
                            "category": fact.get("category", ""),
                            "profile": fact["_dest_profile"]})

    return {
        "timestamp": datetime.now().isoformat(),
        "source": source,
        "profile": profile,
        "routed_by_profile": dict(routed_by_profile),
        "added": added_count,
        "skipped_duplicate": skipped_count,
        "merged": merged_count,
        "rejected": rejected_count,
        "guardrail_rejected": len(guardrail_rejected),
        "changes": changes,
    }
