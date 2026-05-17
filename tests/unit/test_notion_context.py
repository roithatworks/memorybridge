"""
Unit tests for notion_context.py — Notion AI context page refresher.

Uses unittest.mock to avoid real DB access and Notion API calls.

Run: python -m pytest tests/unit/test_notion_context.py -v
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, call

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "ingestion"))

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE_MEMORIES = [
    {
        "category": "constraint",
        "content": "Never use em dashes in writing",
        "importance": "high",
        "project_id": None,
        "tags": "[]",
    },
    {
        "category": "preference",
        "content": "Architecture-first approach — decisions locked before code",
        "importance": "high",
        "project_id": None,
        "tags": "[]",
    },
    {
        "category": "fact",
        "content": "USAF veteran, 8 years",
        "importance": "medium",
        "project_id": None,
        "tags": "[]",
    },
]

SAMPLE_PASSPORT = """\
# Memory Passport
Profile: default
Generated: 2026-05-10
Name: Cale
Role: Director-level BizOps/PMO professional

## Constraints

! Never use em dashes in writing

## Preferences

! Architecture-first approach

## Facts

- USAF veteran, 8 years
"""


# ---------------------------------------------------------------------------
# _passport_to_blocks
# ---------------------------------------------------------------------------

class TestPassportToBlocks:
    def test_heading1(self):
        from notion_context import _passport_to_blocks
        blocks = _passport_to_blocks("# My Title")
        assert len(blocks) == 1
        assert blocks[0]["type"] == "heading_1"
        assert blocks[0]["heading_1"]["rich_text"][0]["text"]["content"] == "My Title"

    def test_heading2(self):
        from notion_context import _passport_to_blocks
        blocks = _passport_to_blocks("## Section")
        assert blocks[0]["type"] == "heading_2"

    def test_important_line_is_bold_paragraph(self):
        from notion_context import _passport_to_blocks
        blocks = _passport_to_blocks("! Critical constraint here")
        assert blocks[0]["type"] == "paragraph"
        assert blocks[0]["paragraph"]["rich_text"][0]["annotations"]["bold"] is True
        assert "Critical constraint here" in blocks[0]["paragraph"]["rich_text"][0]["text"]["content"]

    def test_bullet_line(self):
        from notion_context import _passport_to_blocks
        blocks = _passport_to_blocks("- Regular fact")
        assert blocks[0]["type"] == "bulleted_list_item"

    def test_quote_line(self):
        from notion_context import _passport_to_blocks
        blocks = _passport_to_blocks("> Quoted text")
        assert blocks[0]["type"] == "quote"

    def test_divider(self):
        from notion_context import _passport_to_blocks
        blocks = _passport_to_blocks("---")
        assert blocks[0]["type"] == "divider"
        assert blocks[0]["divider"] == {}

    def test_blank_lines_skipped(self):
        from notion_context import _passport_to_blocks
        blocks = _passport_to_blocks("\n\n\n")
        assert blocks == []

    def test_long_line_chunked(self):
        """Lines > 1900 chars should be split into multiple blocks."""
        from notion_context import _passport_to_blocks
        long_text = "- " + ("x" * 4000)
        blocks = _passport_to_blocks(long_text)
        assert len(blocks) >= 3  # 4000 chars / 1900 = 3 chunks
        for b in blocks:
            content = b["bulleted_list_item"]["rich_text"][0]["text"]["content"]
            assert len(content) <= 1900

    def test_full_passport_produces_multiple_blocks(self):
        from notion_context import _passport_to_blocks
        blocks = _passport_to_blocks(SAMPLE_PASSPORT)
        assert len(blocks) >= 5
        types = {b["type"] for b in blocks}
        assert "heading_1" in types
        assert "heading_2" in types


# ---------------------------------------------------------------------------
# _clear_page_blocks
# ---------------------------------------------------------------------------

class TestClearPageBlocks:
    def test_deletes_all_blocks(self):
        from notion_context import _clear_page_blocks

        mock_client = MagicMock()
        mock_client.blocks.children.list.return_value = {
            "results": [{"id": "block-1"}, {"id": "block-2"}],
            "has_more": False,
        }

        count = _clear_page_blocks(mock_client, "page-abc")
        assert count == 2
        assert mock_client.blocks.delete.call_count == 2

    def test_handles_pagination(self):
        from notion_context import _clear_page_blocks

        mock_client = MagicMock()
        mock_client.blocks.children.list.side_effect = [
            {"results": [{"id": "b1"}, {"id": "b2"}], "has_more": True, "next_cursor": "cur1"},
            {"results": [{"id": "b3"}], "has_more": False},
        ]

        count = _clear_page_blocks(mock_client, "page-abc")
        assert count == 3

    def test_skips_failed_deletes(self, caplog):
        import logging
        from notion_context import _clear_page_blocks

        mock_client = MagicMock()
        mock_client.blocks.children.list.return_value = {
            "results": [{"id": "bad-block"}, {"id": "good-block"}],
            "has_more": False,
        }
        mock_client.blocks.delete.side_effect = [Exception("403 Forbidden"), None]

        with caplog.at_level(logging.WARNING, logger="notion_context"):
            count = _clear_page_blocks(mock_client, "page-abc")

        assert count == 1  # only the successful one counts
        assert any("bad-block" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# refresh_context_page
# ---------------------------------------------------------------------------

class TestRefreshContextPage:
    def _make_db(self, tmp_path):
        """Create a minimal in-memory-like DB at tmp_path/memory.db."""
        import sqlite3
        db = tmp_path / "memory.db"
        conn = sqlite3.connect(str(db))
        conn.execute("""
            CREATE TABLE memories (
                id TEXT, profile TEXT, content TEXT, content_hash TEXT,
                category TEXT, importance TEXT, relevance_score REAL,
                created_at TEXT, last_accessed TEXT, access_count INTEGER,
                tags TEXT, project_id TEXT, token_count INTEGER, archived INTEGER,
                archived_at TEXT, archive_reason TEXT
            )
        """)
        for m in SAMPLE_MEMORIES:
            conn.execute(
                "INSERT INTO memories VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "id1", "default", m["content"], "hash",
                    m["category"], m["importance"], 1.0,
                    "2026-05-10", "2026-05-10", 0,
                    m["tags"], m["project_id"], 20, 0, None, None,
                ),
            )
        conn.commit()
        conn.close()
        return db

    def test_dry_run_returns_block_count_without_api_calls(self, tmp_path):
        from notion_context import refresh_context_page

        db = self._make_db(tmp_path)
        with patch.dict("os.environ", {"NOTION_API_TOKEN": "tok"}):
            result = refresh_context_page(
                profile="default",
                page_id="test-page-id",
                db_path=db,
                dry_run=True,
                max_tokens=2000,
            )

        assert result["dry_run"] is True
        assert result["memories_loaded"] == 3
        assert result["blocks_written"] > 0
        assert result["page_id"] == "test-page-id"

    def test_raises_without_token(self, tmp_path):
        from notion_context import refresh_context_page

        db = self._make_db(tmp_path)
        env = {k: v for k, v in __import__("os").environ.items() if k != "NOTION_API_TOKEN"}
        with patch.dict("os.environ", env, clear=True):
            with pytest.raises(RuntimeError, match="NOTION_API_TOKEN"):
                refresh_context_page(db_path=db, dry_run=False)

    def test_raises_when_db_missing(self, tmp_path):
        from notion_context import refresh_context_page

        missing = tmp_path / "no_such.db"
        with patch.dict("os.environ", {"NOTION_API_TOKEN": "tok"}):
            with pytest.raises(FileNotFoundError):
                refresh_context_page(db_path=missing, page_id="dummy-page-id")

    def test_live_run_calls_clear_and_append(self, tmp_path):
        from notion_context import refresh_context_page

        db = self._make_db(tmp_path)

        mock_client = MagicMock()
        mock_client.blocks.children.list.return_value = {
            "results": [], "has_more": False
        }
        mock_client.blocks.children.append.return_value = {}

        with patch.dict("os.environ", {"NOTION_API_TOKEN": "tok"}):
            with patch("notion_client.Client", return_value=mock_client):
                result = refresh_context_page(
                    profile="default",
                    page_id="live-page",
                    db_path=db,
                    dry_run=False,
                    max_tokens=2000,
                )

        assert result["dry_run"] is False
        assert result["memories_loaded"] == 3
        assert result["blocks_written"] > 0
        mock_client.blocks.children.append.assert_called()

    def test_only_non_archived_memories_loaded(self, tmp_path):
        """Archived memories should not appear in the passport."""
        import sqlite3
        from notion_context import refresh_context_page

        db = tmp_path / "memory.db"
        conn = sqlite3.connect(str(db))
        conn.execute("""
            CREATE TABLE memories (
                id TEXT, profile TEXT, content TEXT, content_hash TEXT,
                category TEXT, importance TEXT, relevance_score REAL,
                created_at TEXT, last_accessed TEXT, access_count INTEGER,
                tags TEXT, project_id TEXT, token_count INTEGER, archived INTEGER,
                archived_at TEXT, archive_reason TEXT
            )
        """)
        conn.execute(
            "INSERT INTO memories VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("a1", "default", "Active fact", "h1", "fact", "high", 1.0,
             "2026-05-10", "2026-05-10", 0, "[]", None, 10, 0, None, None),
        )
        conn.execute(
            "INSERT INTO memories VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("a2", "default", "Archived fact", "h2", "fact", "high", 1.0,
             "2026-05-10", "2026-05-10", 0, "[]", None, 10, 1, "2026-05-10", "pruned"),
        )
        conn.commit()
        conn.close()

        with patch.dict("os.environ", {"NOTION_API_TOKEN": "tok"}):
            result = refresh_context_page(
                profile="default",
                page_id="pg",
                db_path=db,
                dry_run=True,
            )

        assert result["memories_loaded"] == 1  # only the active one
