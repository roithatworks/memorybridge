"""Route extracted facts into accepted / flagged / escalated buckets."""

import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from server import _store  # noqa: E402

logger = logging.getLogger(__name__)

# Confidence thresholds
ACCEPT_THRESHOLD = 0.85
FLAG_THRESHOLD = 0.60

# Categories that always require escalation regardless of confidence
_ALWAYS_ESCALATE_CATEGORIES = {"relationship", "project_status"}

# search_memory match_score above this triggers conflict escalation
_CONFLICT_SIMILARITY = 0.80


def _detect_conflict(fact: dict, profile: str) -> str | None:
    """
    Call search_hybrid on the database store to find existing memories similar to this fact.
    Returns the conflicting memory content string if found, else None.
    """
    query = fact.get("fact", "")
    if not query:
        return None
    try:
        results = _store.search_hybrid(profile=profile, query=query, limit=5)
        for mem in results:
            if mem.get("match_score", 0) > _CONFLICT_SIMILARITY:
                return mem.get("content", "")
    except Exception as e:
        # search degrades to keyword search on embed failure, so reaching here is
        # a real failure — conflict detection is silently off. Make it visible.
        logger.error("search_hybrid failed during conflict detection "
                     "(conflict check DEGRADED): %s", e)
    return None


def route(facts: list, profile: str = "default") -> dict:
    """
    Route facts into three buckets.

    Args:
        facts: List of extracted fact dicts from extractor.py
        profile: Memory profile to check conflicts against
    Returns:
        {"accepted": [...], "flagged": [...], "escalated": [...]}
    """
    accepted = []
    flagged = []
    escalated = []

    for fact in facts:
        confidence = fact.get("confidence", 0.0)
        category = fact.get("category", "fact")

        # Run conflict detection and stamp the fact
        if fact.get("conflicts_with") is None:
            conflict = _detect_conflict(fact, profile)
            if conflict:
                fact["conflicts_with"] = conflict

        has_conflict = bool(fact.get("conflicts_with"))
        always_escalate = category in _ALWAYS_ESCALATE_CATEGORIES
        low_confidence_identity = (
            category == "identity" and confidence < 0.90
        )

        if has_conflict or always_escalate or low_confidence_identity or confidence < FLAG_THRESHOLD:
            escalated.append(fact)
        elif confidence < ACCEPT_THRESHOLD:
            flagged.append(fact)
        else:
            accepted.append(fact)

    logger.info(
        "Routing complete — accepted: %d, flagged: %d, escalated: %d",
        len(accepted), len(flagged), len(escalated),
    )
    return {"accepted": accepted, "flagged": flagged, "escalated": escalated}
