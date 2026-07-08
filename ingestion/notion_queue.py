"""
MemoryBridge — Notion Flagged Queue writer.

Writes flagged ingestion items to a Notion database so they can be
reviewed and acted on directly in Notion.

Opt-in: only runs when NOTION_API_TOKEN and NOTION_FLAGGED_DB_ID are set
in the environment. If either is missing, write_flagged_to_notion() is a
no-op and the standard flagged_queue.json write still happens.

The target database is configured via NOTION_FLAGGED_DB_ID; no URL is hardcoded.
"""

import logging
import os
import time

logger = logging.getLogger("notion_queue")

# Retry budget for transient Notion 429 rate-limit responses (#127).
_NOTION_MAX_RETRIES = 4

try:
    from notion_client import Client
except ImportError:
    Client = None  # type: ignore


# Valid select option values — must match what's in the Notion database schema
_VALID_CATEGORIES = {
    "preference", "fact", "skill", "insight",
    "constraint", "decision", "project_status", "relationship",
}
_VALID_IMPORTANCES = {"low", "medium", "high", "critical"}
_VALID_SOURCES = {"claude", "chatgpt", "gemini", "hermes", "perplexity"}


def build_notion_client():
    """
    Build a Notion API client from environment variables.

    Returns a Client instance if NOTION_API_TOKEN is set, else None.
    """
    if Client is None:
        logger.warning("notion-client not installed — pip install notion-client")
        return None

    token = os.environ.get("NOTION_API_TOKEN", "")
    if not token:
        return None

    return Client(auth=token)


def _build_properties(item: dict, source: str, profile: str) -> dict:
    """Build the Notion page properties dict for a single flagged item."""
    fact = item.get("fact", "")
    confidence = item.get("confidence", 0.0)
    reason = item.get("reason", "")
    category = item.get("category", "fact")
    importance = item.get("importance", "medium")
    conv_id = item.get("source_conversation_id", "")

    # Clamp to known select values
    if category not in _VALID_CATEGORIES:
        category = "fact"
    if importance not in _VALID_IMPORTANCES:
        importance = "medium"
    # Preserve real provenance — never silently relabel as "claude" (that
    # mislabels every hermes/perplexity fact). Unknown sources pass through as
    # "unknown" so the mistake is visible, not disguised.
    if source not in _VALID_SOURCES:
        source = source or "unknown"

    return {
        "Fact": {
            "title": [{"text": {"content": fact[:2000]}}]
        },
        "Status": {
            "select": {"name": "Pending"}
        },
        "Confidence": {
            "number": round(float(confidence), 4)
        },
        "Category": {
            "select": {"name": category}
        },
        "Importance": {
            "select": {"name": importance}
        },
        "Source": {
            "select": {"name": source}
        },
        "Reason": {
            "rich_text": [{"text": {"content": reason[:2000]}}]
        },
        "Profile": {
            "rich_text": [{"text": {"content": profile[:200]}}]
        },
        "Source Conversation ID": {
            "rich_text": [{"text": {"content": conv_id[:200]}}]
        },
    }


def write_flagged_to_notion(
    flagged: list,
    source: str,
    profile: str,
    client,
    database_id: str,
) -> int:
    """
    Write flagged items to a Notion database.

    Args:
        flagged:     List of flagged fact dicts from ingestion router.
        source:      Source AI (claude, chatgpt, gemini).
        profile:     Memory profile name.
        client:      Notion Client instance.
        database_id: Target Notion database ID.

    Returns:
        Number of items successfully written.
    """
    if not flagged:
        return 0

    written = 0
    failed = 0
    for item in flagged:
        properties = _build_properties(item, source, profile)
        if _create_with_retry(client, database_id, properties, item):
            written += 1
        else:
            failed += 1

    if failed:
        # These remain in the flagged_queue.json fallback, so the next sync can
        # re-attempt them — but flag the incomplete sync loudly (#127).
        logger.warning(
            "%d/%d flagged items could not be written to Notion after retries; "
            "they stay in the JSON fallback for the next sync.", failed, len(flagged))
    logger.info("Wrote %d/%d flagged items to Notion", written, len(flagged))
    return written


def _is_rate_limited(e) -> bool:
    """True if a Notion exception looks like an HTTP 429 rate-limit."""
    status = getattr(e, "status", None) or getattr(e, "code", None)
    if status in (429, "429", "rate_limited"):
        return True
    return "429" in str(e) or "rate_limited" in str(e).lower()


def _retry_after_seconds(e, attempt: int) -> float:
    """Honor a Retry-After header if present, else exponential backoff."""
    headers = getattr(e, "headers", None) or {}
    try:
        delay = float(headers.get("Retry-After", 0))
    except (TypeError, ValueError, AttributeError):
        delay = 0.0
    return delay or float(2 ** attempt)


def _create_with_retry(client, database_id, properties, item) -> bool:
    """Create one Notion page, retrying on 429 with backoff. Returns success."""
    fact_preview = item.get("fact", "")[:50]
    for attempt in range(_NOTION_MAX_RETRIES):
        try:
            client.pages.create(parent={"database_id": database_id}, properties=properties)
            return True
        except Exception as e:  # noqa: BLE001 — notion_client raises its own types
            if _is_rate_limited(e) and attempt < _NOTION_MAX_RETRIES - 1:
                delay = _retry_after_seconds(e, attempt)
                logger.warning("Notion 429 for '%s' — backing off %.1fs (attempt %d/%d)",
                               fact_preview, delay, attempt + 1, _NOTION_MAX_RETRIES)
                time.sleep(delay)
                continue
            logger.warning("Notion write failed for '%s': %s", fact_preview, e)
            return False
    return False
