"""Dedup and write accepted facts to memory via server tools."""

import json
import logging
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

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
    """Return search results for a fact string."""
    try:
        return _store.search_hybrid(profile=profile, query=fact_text, limit=5)
    except Exception as e:
        logger.warning("search_hybrid failed: %s", e)
        return []


def _write_fact(fact: dict, profile: str, preview: bool) -> str:
    """
    Write a single fact via add_memory. Returns "added" or "error".
    Safe to call during preview=False only.
    """
    try:
        add_memory(
            content=fact["fact"],
            category=fact.get("category", "fact"),
            importance=fact.get("importance", "medium"),
            tags=[],
            project_id=fact.get("project"),
            profile=profile,
        )
        return "added"
    except Exception as e:
        logger.error("add_memory failed: %s", e)
        return "error"


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
            added = add_memories(
                facts=fact_strings,
                category=category,
                importance=importance,
                project=project,
                profile=profile,
            )
            written += added if isinstance(added, int) else 0
            # add_memories records guardrail-skipped facts on the store.
            rejected.extend(getattr(_store, "last_rejected", []) or [])
        except Exception as e:
            logger.error("add_memories failed for category '%s': %s", category, e)
    return {"written": written, "rejected": rejected}


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

    # Dedup: check each fact against existing memory before writing
    write_queue: list[dict] = []
    for fact in all_to_write:
        fact_text = fact.get("fact", "")
        if not fact_text:
            skipped_count += 1
            continue

        existing = _search_existing(fact_text, profile)

        # Check exact-ish match
        skip = False
        for mem in existing:
            score = mem.get("match_score", 0)
            if score >= _EXACT_MATCH_THRESHOLD:
                # Already exists — skip
                skipped_count += 1
                changes.append({"action": "skipped", "fact": fact_text[:80], "category": fact.get("category", "")})
                skip = True
                break

            # Keyword overlap check for near-duplicates
            overlap = _keyword_overlap(fact_text, mem.get("content", ""))
            if overlap >= _KEYWORD_MERGE_THRESHOLD:
                # Keep the newer fact (the one being ingested)
                merged_count += 1
                changes.append({"action": "merged", "fact": fact_text[:80], "category": fact.get("category", "")})
                write_queue.append(fact)
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

    guardrail_rejected = []
    if not preview:
        # Group by category for efficient batch writes
        by_category: dict[str, list] = defaultdict(list)
        for fact in write_queue:
            by_category[fact.get("category", "fact")].append(fact)

        batch = _batch_write(dict(by_category), profile)
        added_count = batch["written"]
        guardrail_rejected = batch["rejected"]
        for fact in write_queue:
            changes.append({"action": "added", "fact": fact.get("fact", "")[:80], "category": fact.get("category", "")})
        for fact_text, reason in guardrail_rejected:
            changes.append({"action": "guardrail_rejected",
                            "fact": str(fact_text)[:80], "reason": reason})
    else:
        # Preview mode: count what would be added
        added_count = len(write_queue)
        for fact in write_queue:
            changes.append({"action": "would_add", "fact": fact.get("fact", "")[:80], "category": fact.get("category", "")})

    return {
        "timestamp": datetime.now().isoformat(),
        "source": source,
        "profile": profile,
        "added": added_count,
        "skipped_duplicate": skipped_count,
        "merged": merged_count,
        "rejected": rejected_count,
        "guardrail_rejected": len(guardrail_rejected),
        "changes": changes,
    }
