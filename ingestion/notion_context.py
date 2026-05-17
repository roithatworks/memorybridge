"""
MemoryBridge — Notion AI Context page refresher.

Reads memories from the local MemoryBridge DB, builds a Memory Passport,
and updates the configured Notion page with the latest content.

Usage:
    python ingestion/notion_context.py [--profile default] [--dry-run]

Environment:
    NOTION_API_TOKEN        — required; Notion integration token
    NOTION_CONTEXT_PAGE_ID  — required; target Notion page ID
"""

import argparse
import logging
import os
import sqlite3
import sys
from pathlib import Path

from dotenv import load_dotenv

# Load repo .env first, then production .env (without overwriting)
load_dotenv(Path(__file__).parent.parent / ".env")
load_dotenv(Path.home() / "memorybridge" / ".env", override=False)

logger = logging.getLogger("notion_context")

# Page ID must be set via NOTION_CONTEXT_PAGE_ID env var
DEFAULT_PAGE_ID = ""  # set NOTION_CONTEXT_PAGE_ID in .env
DEFAULT_DB_PATH = Path.home() / "memorybridge" / "memory.db"

# Notion API max chars per rich_text block; stay under the 2000-char hard limit
_MAX_BLOCK_CHARS = 1900


def _load_memories(profile: str, db_path: Path) -> list:
    """Load non-archived memories for the given profile from SQLite."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT category, content, importance, project_id, tags
            FROM memories
            WHERE profile = ? AND archived = 0
            ORDER BY category, importance DESC
            """,
            (profile,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _load_identity(profile: str, db_path: Path) -> dict:
    """Load identity fields from the profiles table. Returns {} if not found."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM profiles WHERE name = ? LIMIT 1", (profile,)
        ).fetchone()
        if row is None:
            return {}
        return {k: row[k] for k in row.keys() if row[k] is not None}
    except Exception:
        return {}
    finally:
        conn.close()


def _passport_to_blocks(passport_text: str) -> list:
    """
    Convert plain-text Memory Passport into Notion block objects.

    Passport line conventions:
        # Title       → heading_1
        ## Section    → heading_2
        ! Important   → paragraph (bold)
        - Bullet      → bulleted_list_item
        > Quote       → quote
        ---           → divider
        (blank)       → skipped
        (other)       → paragraph
    """

    def _text_block(block_type: str, text: str, bold: bool = False) -> list:
        """Chunk a long text string into ≤ _MAX_BLOCK_CHARS blocks of block_type."""
        chunks = []
        while text:
            part, text = text[:_MAX_BLOCK_CHARS], text[_MAX_BLOCK_CHARS:]
            rich_text = [
                {
                    "type": "text",
                    "text": {"content": part},
                    "annotations": {"bold": bold},
                }
            ]
            chunks.append({"type": block_type, block_type: {"rich_text": rich_text}})
        return chunks

    blocks: list = []

    for line in passport_text.splitlines():
        s = line.strip()
        if not s:
            continue
        if s == "---":
            blocks.append({"type": "divider", "divider": {}})
        elif s.startswith("# "):
            blocks.extend(_text_block("heading_1", s[2:]))
        elif s.startswith("## "):
            blocks.extend(_text_block("heading_2", s[3:]))
        elif s.startswith("! "):
            blocks.extend(_text_block("paragraph", s[2:], bold=True))
        elif s.startswith("- "):
            blocks.extend(_text_block("bulleted_list_item", s[2:]))
        elif s.startswith("> "):
            blocks.extend(_text_block("quote", s[2:]))
        else:
            blocks.extend(_text_block("paragraph", s))

    return blocks


def _clear_page_blocks(client, page_id: str) -> int:
    """Delete all child blocks from a Notion page. Returns count deleted."""
    deleted = 0
    cursor = None

    while True:
        kwargs = {"block_id": page_id, "page_size": 100}
        if cursor:
            kwargs["start_cursor"] = cursor

        response = client.blocks.children.list(**kwargs)
        block_ids = [b["id"] for b in response.get("results", [])]

        for bid in block_ids:
            try:
                client.blocks.delete(block_id=bid)
                deleted += 1
            except Exception as exc:
                logger.warning("Could not delete block %s: %s", bid, exc)

        if not response.get("has_more"):
            break
        cursor = response["next_cursor"]

    return deleted


def _append_blocks(client, page_id: str, blocks: list) -> int:
    """Append blocks to a Notion page in chunks of 100 (API limit)."""
    written = 0
    for i in range(0, len(blocks), 100):
        client.blocks.children.append(block_id=page_id, children=blocks[i : i + 100])
        written += len(blocks[i : i + 100])
    return written


def refresh_context_page(
    profile: str = "default",
    page_id: str = DEFAULT_PAGE_ID,
    db_path: Path = DEFAULT_DB_PATH,
    dry_run: bool = False,
    max_tokens: int = 4000,
) -> dict:
    """
    Refresh the Notion AI context page with the latest MemoryBridge memories.

    Args:
        profile:    Memory profile to export (default: "default").
        page_id:    Notion page ID to update.
        db_path:    Path to the MemoryBridge SQLite database.
        dry_run:    If True, parse and convert without writing to Notion.
        max_tokens: Token budget passed to build_passport().

    Returns:
        dict with keys: memories_loaded, blocks_written, dry_run, page_id.

    Raises:
        RuntimeError:    NOTION_API_TOKEN not set or notion-client not installed.
        FileNotFoundError: db_path does not exist.
    """
    token = os.environ.get("NOTION_API_TOKEN", "")
    if not token:
        raise RuntimeError("NOTION_API_TOKEN not set — cannot update Notion page")

    if not page_id:
        raise RuntimeError(
            "NOTION_CONTEXT_PAGE_ID not set — run notion_context.py --page-id <id> "
            "or set NOTION_CONTEXT_PAGE_ID in your .env"
        )

    if not db_path.exists():
        raise FileNotFoundError(f"MemoryBridge DB not found: {db_path}")

    # Load memories
    memories = _load_memories(profile, db_path)
    logger.info("Loaded %d memories for profile '%s'", len(memories), profile)

    # Build passport text
    sys.path.insert(0, str(Path(__file__).parent))
    from passport import build_passport  # local import — passport.py lives alongside this file

    identity = _load_identity(profile, db_path)
    passport_text = build_passport(
        memories, identity=identity, profile=profile, max_tokens=max_tokens
    )

    # Convert to Notion blocks
    blocks = _passport_to_blocks(passport_text)
    logger.info("Passport → %d Notion blocks", len(blocks))

    if dry_run:
        return {
            "memories_loaded": len(memories),
            "blocks_written": len(blocks),
            "dry_run": True,
            "page_id": page_id,
        }

    # Build Notion client
    try:
        from notion_client import Client
    except ImportError:
        raise RuntimeError("notion-client not installed — pip install notion-client")

    client = Client(auth=token)

    # Clear and rewrite page content
    deleted = _clear_page_blocks(client, page_id)
    logger.info("Cleared %d old blocks", deleted)

    written = _append_blocks(client, page_id, blocks)
    logger.info("Wrote %d blocks to page %s", written, page_id)

    return {
        "memories_loaded": len(memories),
        "blocks_written": written,
        "dry_run": False,
        "page_id": page_id,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Refresh Notion AI context page from MemoryBridge memories"
    )
    parser.add_argument("--profile", default="default", help="Memory profile to export")
    parser.add_argument(
        "--page-id",
        default=os.environ.get("NOTION_CONTEXT_PAGE_ID", DEFAULT_PAGE_ID),
        help="Notion page ID to update",
    )
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="Path to memory.db")
    parser.add_argument(
        "--max-tokens", type=int, default=4000, help="Token budget for passport"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and convert without writing to Notion",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    try:
        result = refresh_context_page(
            profile=args.profile,
            page_id=args.page_id,
            db_path=Path(args.db),
            dry_run=args.dry_run,
            max_tokens=args.max_tokens,
        )
    except (RuntimeError, FileNotFoundError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    verb = "[DRY RUN] Would write" if result["dry_run"] else "Wrote"
    print("MemoryBridge → Notion context refresh complete")
    print(f"  Memories loaded: {result['memories_loaded']}")
    print(f"  {verb}: {result['blocks_written']} blocks")
    if not result["dry_run"]:
        print(f"  Page: https://www.notion.so/{result['page_id'].replace('-', '')}")


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).parent))
    main()
