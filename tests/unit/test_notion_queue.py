"""
Unit tests for Notion flagged queue writer.

Uses unittest.mock to avoid real Notion API calls.

Run: python -m pytest tests/unit/test_notion_queue.py -v
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "ingestion"))

import pytest


SAMPLE_FLAGGED = [
    {
        "fact": "Prefers dark mode in all apps",
        "confidence": 0.72,
        "reason": "Confidence below threshold",
        "category": "preference",
        "importance": "medium",
        "project": None,
        "source_conversation_id": "conv-abc123",
    },
    {
        "fact": "Has a dog named Rex",
        "confidence": 0.65,
        "reason": "Low confidence animal fact",
        "category": "fact",
        "importance": "low",
        "project": None,
        "source_conversation_id": "conv-def456",
    },
]


def test_write_flagged_calls_notion_api():
    """write_flagged_to_notion should call pages.create for each flagged item."""
    from notion_queue import write_flagged_to_notion

    mock_client = MagicMock()
    mock_client.pages.create.return_value = {"id": "page-123"}

    write_flagged_to_notion(
        flagged=SAMPLE_FLAGGED,
        source="claude",
        profile="default",
        client=mock_client,
        database_id="dbf5ac1a-599b-4b9b-800f-07cf98e7ebc2",
    )

    assert mock_client.pages.create.call_count == 2


def test_write_flagged_sets_correct_properties():
    """Each Notion page should have correct property values."""
    from notion_queue import write_flagged_to_notion

    mock_client = MagicMock()
    mock_client.pages.create.return_value = {"id": "page-123"}

    write_flagged_to_notion(
        flagged=[SAMPLE_FLAGGED[0]],
        source="claude",
        profile="default",
        client=mock_client,
        database_id="test-db-id",
    )

    call_kwargs = mock_client.pages.create.call_args[1]
    props = call_kwargs["properties"]

    assert props["Fact"]["title"][0]["text"]["content"] == "Prefers dark mode in all apps"
    assert props["Status"]["select"]["name"] == "Pending"
    assert props["Confidence"]["number"] == 0.72
    assert props["Category"]["select"]["name"] == "preference"
    assert props["Source"]["select"]["name"] == "claude"
    assert props["Profile"]["rich_text"][0]["text"]["content"] == "default"


def test_write_flagged_empty_list_is_noop():
    """Empty flagged list should make zero API calls."""
    from notion_queue import write_flagged_to_notion

    mock_client = MagicMock()
    write_flagged_to_notion(
        flagged=[],
        source="claude",
        profile="default",
        client=mock_client,
        database_id="test-db-id",
    )
    mock_client.pages.create.assert_not_called()


def test_write_flagged_skips_on_api_error(caplog):
    """API error on one item should not stop processing remaining items."""
    import logging
    from notion_queue import write_flagged_to_notion

    mock_client = MagicMock()
    mock_client.pages.create.side_effect = [Exception("API error"), {"id": "page-456"}]

    with caplog.at_level(logging.WARNING, logger="notion_queue"):
        count = write_flagged_to_notion(
            flagged=SAMPLE_FLAGGED,
            source="claude",
            profile="default",
            client=mock_client,
            database_id="test-db-id",
        )

    assert mock_client.pages.create.call_count == 2
    assert count == 1  # only second succeeded
    assert any("api error" in r.message.lower() or "failed" in r.message.lower()
               for r in caplog.records)


def test_write_flagged_returns_written_count():
    """Return value should equal number of successfully written items."""
    from notion_queue import write_flagged_to_notion

    mock_client = MagicMock()
    mock_client.pages.create.return_value = {"id": "page-123"}

    count = write_flagged_to_notion(
        flagged=SAMPLE_FLAGGED,
        source="claude",
        profile="default",
        client=mock_client,
        database_id="test-db-id",
    )
    assert count == 2


def test_build_notion_client_uses_env_token():
    """build_notion_client() should read NOTION_API_TOKEN from environment."""
    from notion_queue import build_notion_client

    with patch.dict("os.environ", {"NOTION_API_TOKEN": "secret-token-xyz"}):
        with patch("notion_queue.Client") as mock_client_class:
            build_notion_client()
            mock_client_class.assert_called_once_with(auth="secret-token-xyz")


def test_build_notion_client_returns_none_without_token():
    """build_notion_client() returns None when NOTION_API_TOKEN is not set."""
    from notion_queue import build_notion_client
    import os

    env = {k: v for k, v in os.environ.items() if k != "NOTION_API_TOKEN"}
    with patch.dict("os.environ", env, clear=True):
        result = build_notion_client()
    assert result is None
