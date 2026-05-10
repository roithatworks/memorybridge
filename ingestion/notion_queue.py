"""
MemoryBridge — Notion Flagged Queue writer.

Writes flagged ingestion items to a Notion database so they can be
reviewed and acted on directly in Notion.

Opt-in: only runs when NOTION_API_TOKEN and NOTION_FLAGGED_DB_ID are set
in the environment. If either is missing, write_flagged_to_notion() is a
no-op and the standard flagged_queue.json write still happens.

Notion database: https://www.notion.so/dbf5ac1a599b4b9b800f07cf98e7ebc2
"""

import logging
import os

logger = logging.getLogger("notion_queue")

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
_VALID_SOURCES = {"claude", "chatgpt", "gemini"}


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
    if source not in _VALID_SOURCES:
        source = "claude"

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
    for item in flagged:
        try:
            properties = _build_properties(item, source, profile)
            client.pages.create(
                parent={"database_id": database_id},
                properties=properties,
            )
            written += 1
        except Exception as e:
            fact_preview = item.get("fact", "")[:50]
            logger.warning("Notion write failed for '%s': %s", fact_preview, e)

    logger.info("Wrote %d/%d flagged items to Notion", written, len(flagged))
    return written
