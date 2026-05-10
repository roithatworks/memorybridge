# Notion Flagged Queue Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Write flagged ingestion items to a live Notion database so they can be reviewed and acted on directly in Notion.

**Architecture:** `ingestion/notion_queue.py` wraps the Notion API (`notion-client` library) and writes each flagged item as a database row. `ingestion/run.py` calls it after the existing `_write_flagged()` JSON write, when `NOTION_API_TOKEN` is set. Opt-in via env var — no Notion token means behavior is unchanged. The Notion database already exists; its ID is baked into the env config.

**Tech Stack:** Python `notion-client` library, existing `ingestion/run.py` pipeline, pytest with unittest.mock.

---

## Notion database (already created)

- **Page:** https://www.notion.so/35c9e1173e5c81fea2bedf2abb2ce111 (MemoryBridge)
- **Database URL:** https://www.notion.so/dbf5ac1a599b4b9b800f07cf98e7ebc2
- **Database ID:** `dbf5ac1a-599b-4b9b-800f-07cf98e7ebc2`

Schema (already live):
| Property | Type | Values |
|---|---|---|
| Fact | Title | free text |
| Status | Select | Pending / Accepted / Rejected |
| Confidence | Number | 0.0–1.0 |
| Category | Select | preference, fact, skill, insight, constraint, decision, project_status, relationship |
| Importance | Select | low, medium, high, critical |
| Source | Select | claude, chatgpt, gemini |
| Reason | Rich Text | free text |
| Profile | Rich Text | free text |
| Source Conversation ID | Rich Text | free text |

---

## Task 1: Add notion-client to requirements and .env

**Files:**
- Modify: `requirements.txt`
- Modify: `~/memorybridge/.env` (production — not in repo)

### Step 1: Add dependency

In `requirements.txt`, add after `anthropic`:

```
notion-client>=2.2.0
```

### Step 2: Install it

```bash
/usr/local/bin/python3 -m pip install notion-client
```

Expected: `Successfully installed notion-client-...`

### Step 3: Add env vars to production .env

```bash
echo "" >> ~/memorybridge/.env
echo "NOTION_API_TOKEN=your_token_here" >> ~/memorybridge/.env
echo "NOTION_FLAGGED_DB_ID=dbf5ac1a-599b-4b9b-800f-07cf98e7ebc2" >> ~/memorybridge/.env
```

**Where to get the token:**
1. Go to https://www.notion.so/profile/integrations
2. Create a new integration named "MemoryBridge"
3. Copy the "Internal Integration Secret"
4. Replace `your_token_here` with that value
5. In Notion, open the MemoryBridge page → click ••• → Connections → add "MemoryBridge" integration

### Step 4: Commit requirements change

```bash
git add requirements.txt
git commit -m "feat: add notion-client dependency for Notion flagged queue"
```

---

## Task 2: Write notion_queue.py with tests

**Files:**
- Create: `ingestion/notion_queue.py`
- Create: `tests/unit/test_notion_queue.py`

### Step 1: Write the failing tests first

Create `tests/unit/test_notion_queue.py`:

```python
"""
Unit tests for Notion flagged queue writer.

Uses unittest.mock to avoid real Notion API calls.

Run: python -m pytest tests/unit/test_notion_queue.py -v
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, call

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

    # Title
    assert props["Fact"]["title"][0]["text"]["content"] == "Prefers dark mode in all apps"
    # Status defaults to Pending
    assert props["Status"]["select"]["name"] == "Pending"
    # Confidence
    assert props["Confidence"]["number"] == 0.72
    # Category
    assert props["Category"]["select"]["name"] == "preference"
    # Source
    assert props["Source"]["select"]["name"] == "claude"
    # Profile
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
        write_flagged_to_notion(
            flagged=SAMPLE_FLAGGED,
            source="claude",
            profile="default",
            client=mock_client,
            database_id="test-db-id",
        )

    # Both items attempted
    assert mock_client.pages.create.call_count == 2
    # Error logged
    assert "API error" in caplog.text or "failed" in caplog.text.lower()


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
```

### Step 2: Run to verify failure

```bash
/usr/local/bin/python3 -m pytest tests/unit/test_notion_queue.py -v 2>&1 | tail -10
```

Expected: `ModuleNotFoundError: No module named 'notion_queue'`

### Step 3: Implement ingestion/notion_queue.py

Create `ingestion/notion_queue.py`:

```python
"""
MemoryBridge — Notion Flagged Queue writer.

Writes flagged ingestion items to a Notion database so they can be
reviewed and acted on directly in Notion.

Opt-in: only runs when NOTION_API_TOKEN and NOTION_FLAGGED_DB_ID are set
in the environment. If either is missing, write_flagged_to_notion() is a
no-op and the standard flagged_queue.json write still happens.

Usage (called from ingestion/run.py):
    from notion_queue import build_notion_client, write_flagged_to_notion
    client = build_notion_client()
    if client:
        write_flagged_to_notion(flagged, source, profile, client, database_id)
"""

import logging
import os

logger = logging.getLogger("notion_queue")

try:
    from notion_client import Client
except ImportError:
    Client = None  # type: ignore


def build_notion_client():
    """
    Build a Notion API client from environment variables.

    Returns a Client instance if NOTION_API_TOKEN is set, else None.
    """
    if Client is None:
        logger.warning("notion-client not installed — skipping Notion queue write")
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

    # Clamp category and importance to known select values
    valid_categories = {
        "preference", "fact", "skill", "insight",
        "constraint", "decision", "project_status", "relationship"
    }
    valid_importances = {"low", "medium", "high", "critical"}
    valid_sources = {"claude", "chatgpt", "gemini"}

    if category not in valid_categories:
        category = "fact"
    if importance not in valid_importances:
        importance = "medium"
    if source not in valid_sources:
        source = "claude"

    return {
        "Fact": {
            "title": [{"text": {"content": fact[:2000]}}]  # Notion title limit
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
```

### Step 4: Run tests

```bash
/usr/local/bin/python3 -m pytest tests/unit/test_notion_queue.py -v
```

Expected: 6 passed.

### Step 5: Commit

```bash
git add ingestion/notion_queue.py tests/unit/test_notion_queue.py
git commit -m "feat: Notion flagged queue writer with unit tests"
```

---

## Task 3: Wire into ingestion/run.py

**Files:**
- Modify: `ingestion/run.py`

### Step 1: Add Notion write after existing JSON write

In `ingestion/run.py`, find the `_write_flagged()` call (around line 174) and add the Notion write immediately after:

```python
    # 6. Write flagged queue and log (skip in preview)
    if not args.preview:
        _write_flagged(flagged, args.source)
        log_path = _write_log(report, len(flagged), len(escalated))
        print(f"  Log written to {log_path}")

        # Write to Notion if configured
        notion_db_id = os.environ.get("NOTION_FLAGGED_DB_ID", "")
        if notion_db_id and flagged:
            try:
                from notion_queue import build_notion_client, write_flagged_to_notion
                notion_client = build_notion_client()
                if notion_client:
                    written = write_flagged_to_notion(
                        flagged=flagged,
                        source=args.source,
                        profile=args.profile,
                        client=notion_client,
                        database_id=notion_db_id,
                    )
                    print(f"  Notion: {written} flagged items added to database")
            except Exception as e:
                print(f"  Notion write skipped: {e}", file=sys.stderr)
```

Also add `import os` at the top of `run.py` if not already present (it is — it's imported via `dotenv`). Actually, `os` isn't imported directly in run.py — add it:

At the top imports section, add `import os` after `import argparse`.

### Step 2: Verify the import chain works

```bash
cd /Users/cale/Documents/GitHub/memorybridge && /usr/local/bin/python3 -c "
import sys
sys.path.insert(0, 'ingestion')
from notion_queue import build_notion_client
print('import OK')
client = build_notion_client()
print('client:', 'connected' if client else 'no token set')
"
```

Expected (with token set): `client: connected`
Expected (without token): `client: no token set`

### Step 3: Commit

```bash
git add ingestion/run.py
git commit -m "feat: wire Notion flagged queue into ingestion pipeline"
```

---

## Task 4: Full suite + sync + verify

### Step 1: Run full test suite

```bash
/usr/local/bin/python3 -m pytest tests/ -v 2>&1 | tail -10
```

Expected: 92 passed (86 + 6 new).

### Step 2: Sync to production

```bash
cp /Users/cale/Documents/GitHub/memorybridge/ingestion/notion_queue.py ~/memorybridge/ingestion/notion_queue.py
cp /Users/cale/Documents/GitHub/memorybridge/ingestion/run.py ~/memorybridge/ingestion/run.py
cp /Users/cale/Documents/GitHub/memorybridge/requirements.txt ~/memorybridge/requirements.txt
```

### Step 3: Install notion-client in production Python

```bash
/usr/local/bin/python3 -m pip install notion-client
```

### Step 4: Push to main

```bash
git push origin main
```

### Step 5: End-to-end smoke test

Drop a test file with extractable content into the inbox and verify items appear in Notion:

```bash
# The watcher will pick it up — or run ingestion manually:
/usr/local/bin/python3 ~/memorybridge/ingestion/run.py \
  --source claude \
  --file ~/Downloads/conversations.json \
  --days 1 \
  --preview
```

Then open https://www.notion.so/dbf5ac1a599b4b9b800f07cf98e7ebc2 and verify rows appear with Status = Pending.

---

## Verification checklist

- [ ] `notion-client` installed (`python3 -c "import notion_client; print('ok')"`)
- [ ] `NOTION_API_TOKEN` set in `~/memorybridge/.env`
- [ ] `NOTION_FLAGGED_DB_ID=dbf5ac1a-599b-4b9b-800f-07cf98e7ebc2` set in `.env`
- [ ] MemoryBridge integration added to MemoryBridge Notion page (Connections)
- [ ] 92/92 tests pass
- [ ] Ingestion run writes to Notion when token present; skips gracefully when absent
- [ ] Rows appear in https://www.notion.so/dbf5ac1a599b4b9b800f07cf98e7ebc2

---

## Notes

**Integration token:** must be added to the MemoryBridge Notion page's Connections. Without this, the API will return 404 on database writes even with a valid token.

**Opt-in design:** if `NOTION_API_TOKEN` is not set, nothing breaks. `build_notion_client()` returns None, the code path skips silently. The `flagged_queue.json` file still gets written as before.

**Notion API rate limits:** 3 requests/second. For large flagged batches, add a `time.sleep(0.35)` between writes if you hit 429 errors.
