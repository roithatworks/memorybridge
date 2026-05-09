# Backlog Completion — Inbox Watcher, Parser Tests, Passport UI

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Ship three backlog items: (1) drag-and-drop inbox watcher with launchd integration, (2) unit-tested ChatGPT/Gemini parsers with fixture data, (3) Memory Passport tab wired into the Streamlit Portability page.

**Architecture:** Inbox watcher runs as a single-shot script triggered by launchd WatchPaths — launchd handles the filesystem watch, the script processes whatever's in the inbox and exits. Parser tests use synthetic JSON fixtures derived from real export schemas. Passport UI adds a third tab to the existing portability page calling the already-wired `export_passport` tool.

**Tech Stack:** Python 3.14, launchd (macOS WatchPaths), pytest, Streamlit, FastMCP `.fn` pattern.

---

## Task 1: Passport tab in Portability UI

**Files:**
- Modify: `ui/pages/portability.py`

### Step 1: Add passport tab alongside existing Import/Export tabs

Open `ui/pages/portability.py`. The current `tab_import, tab_export = st.tabs(...)` line becomes three tabs. Add the passport tab after export.

Replace the existing tabs line and the `render()` imports:

```python
def render():
    import streamlit as st
    from server import export_for_model as _export_tool, export_passport as _passport_tool
    export_for_model = _export_tool.fn
    export_passport = _passport_tool.fn

    st.header("🔄 Portability")

    tab_import, tab_export, tab_passport = st.tabs(["⬆ Import", "⬇ Export", "🛂 Passport"])
```

### Step 2: Add the passport tab content

After the existing `tab_export` block (around line 135, after the `except Exception` block), add:

```python
    # =========================================================================
    # PASSPORT TAB
    # =========================================================================
    with tab_passport:
        st.subheader("Memory Passport")
        st.caption(
            "A plain-text snapshot of your memory — paste into any AI's system prompt. "
            "No JSON, no code fences. Works with Claude, ChatGPT, Gemini, Ollama, anything."
        )

        col1, col2 = st.columns(2)
        with col1:
            passport_profile = st.text_input("Profile", value="default", key="pp_profile")
        with col2:
            passport_tokens = st.slider("Token budget", 500, 4000, 2000, step=100,
                                        key="pp_tokens")

        if st.button("Generate Passport", type="primary", key="pp_generate"):
            with st.spinner("Building passport…"):
                try:
                    text = export_passport(
                        profile=passport_profile,
                        max_tokens=passport_tokens,
                    )
                    st.success("Passport ready.")
                    st.text_area("Passport text", value=text, height=400, key="pp_text")
                    st.download_button(
                        label="⬇ Download passport",
                        data=text,
                        file_name=f"memorybridge_passport_{passport_profile}.txt",
                        mime="text/plain",
                        key="pp_download",
                    )
                except Exception as e:
                    st.error(f"Passport generation failed: {e}")
```

### Step 3: Smoke-test in the browser

```bash
streamlit run ui/app.py
# Navigate to Portability → Passport tab
# Enter profile "default", click Generate Passport
# Verify text area fills with plain text starting "# Memory Passport"
```

### Step 4: Commit

```bash
git add ui/pages/portability.py
git commit -m "feat: add Memory Passport tab to Portability UI"
```

---

## Task 2: Parser unit tests — ChatGPT and Gemini

**Files:**
- Create: `tests/unit/test_parsers.py`

The parsers exist (`ingestion/parse_chatgpt.py`, `ingestion/parse_gemini.py`) but have zero tests. These tests use synthetic JSON fixtures — no real export files needed.

### Step 1: Write the failing test file

Create `tests/unit/test_parsers.py`:

```python
"""
Unit tests for ChatGPT and Gemini export parsers.

Uses synthetic fixtures derived from real export schemas.
Run: python -m pytest tests/unit/test_parsers.py -v
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "ingestion"))

import pytest


# ---------------------------------------------------------------------------
# ChatGPT fixtures
# ---------------------------------------------------------------------------

CHATGPT_MINIMAL = [
    {
        "id": "conv-001",
        "title": "Test Conversation",
        "create_time": 1700000000.0,
        "mapping": {
            "root": {
                "id": "root",
                "parent": None,
                "message": None,
                "children": ["msg-user-1"],
            },
            "msg-user-1": {
                "id": "msg-user-1",
                "parent": "root",
                "message": {
                    "author": {"role": "user"},
                    "content": {"parts": ["Hello, can you help me?"]},
                },
                "children": ["msg-asst-1"],
            },
            "msg-asst-1": {
                "id": "msg-asst-1",
                "parent": "msg-user-1",
                "message": {
                    "author": {"role": "assistant"},
                    "content": {"parts": ["Of course! What do you need?"]},
                },
                "children": [],
            },
        },
    }
]

CHATGPT_MULTI_CONV = CHATGPT_MINIMAL + [
    {
        "id": "conv-002",
        "title": "Another Chat",
        "create_time": 1700086400.0,
        "mapping": {
            "root2": {
                "id": "root2",
                "parent": None,
                "message": None,
                "children": ["msg-u2"],
            },
            "msg-u2": {
                "id": "msg-u2",
                "parent": "root2",
                "message": {
                    "author": {"role": "user"},
                    "content": {"parts": ["What is Python?"]},
                },
                "children": [],
            },
        },
    }
]


# ---------------------------------------------------------------------------
# Gemini fixtures
# ---------------------------------------------------------------------------

GEMINI_FORMAT_A = {
    "conversations": [
        {
            "conversation_id": "gemini-001",
            "conversation": [
                {"role": "user", "text": "Explain machine learning briefly."},
                {"role": "model", "text": "Machine learning is a subset of AI..."},
            ],
        },
        {
            "conversation_id": "gemini-002",
            "conversation": [
                {"role": "user", "text": "What is a neural network?"},
                {"role": "model", "text": "A neural network is..."},
            ],
        },
    ]
}

GEMINI_FORMAT_B = [
    {
        "header": {"title": "Gemini Apps Activity"},
        "title": "What is machine learning?",
        "time": "2024-01-15T10:00:00Z",
        "details": [{"name": "What is machine learning?"}],
    },
    {
        "header": {"title": "Gemini Apps Activity"},
        "title": "Explain neural networks",
        "time": "2024-01-16T09:00:00Z",
        "details": [{"name": "Explain neural networks"}],
    },
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_tmp(data: dict | list) -> Path:
    """Write data to a temp JSON file, return its path."""
    tf = tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w")
    json.dump(data, tf)
    tf.close()
    return Path(tf.name)


# ---------------------------------------------------------------------------
# ChatGPT parser tests
# ---------------------------------------------------------------------------

def test_chatgpt_parse_returns_normalized_structure():
    from parse_chatgpt import parse
    path = _write_tmp(CHATGPT_MINIMAL)
    result = parse(str(path))
    assert result["source"] == "chatgpt"
    assert "conversations" in result
    assert "exported_at" in result


def test_chatgpt_parse_extracts_messages():
    from parse_chatgpt import parse
    path = _write_tmp(CHATGPT_MINIMAL)
    result = parse(str(path))
    assert len(result["conversations"]) == 1
    conv = result["conversations"][0]
    assert conv["id"] == "conv-001"
    msgs = conv["messages"]
    assert len(msgs) == 2
    assert msgs[0]["role"] == "user"
    assert "Hello" in msgs[0]["content"]
    assert msgs[1]["role"] == "assistant"
    assert "Of course" in msgs[1]["content"]


def test_chatgpt_parse_multiple_conversations():
    from parse_chatgpt import parse
    path = _write_tmp(CHATGPT_MULTI_CONV)
    result = parse(str(path))
    assert len(result["conversations"]) == 2


def test_chatgpt_parse_days_filter():
    """Conversations outside the window should be excluded."""
    from parse_chatgpt import parse
    path = _write_tmp(CHATGPT_MINIMAL)
    # create_time = 1700000000 ≈ Nov 2023 — filter to last 1 day excludes it
    result = parse(str(path), days=1)
    assert len(result["conversations"]) == 0


def test_chatgpt_parse_empty_mapping_skipped():
    """Conversations with no extractable messages are dropped."""
    from parse_chatgpt import parse
    data = [{"id": "empty", "title": "Empty", "create_time": 1700000000.0, "mapping": {}}]
    path = _write_tmp(data)
    result = parse(str(path))
    assert len(result["conversations"]) == 0


# ---------------------------------------------------------------------------
# Gemini parser tests
# ---------------------------------------------------------------------------

def test_gemini_format_a_returns_normalized_structure():
    from parse_gemini import parse
    path = _write_tmp(GEMINI_FORMAT_A)
    result = parse(str(path))
    assert result["source"] == "gemini"
    assert "conversations" in result
    assert "exported_at" in result


def test_gemini_format_a_extracts_conversations():
    from parse_gemini import parse
    path = _write_tmp(GEMINI_FORMAT_A)
    result = parse(str(path))
    assert len(result["conversations"]) == 2


def test_gemini_format_a_extracts_messages():
    from parse_gemini import parse
    path = _write_tmp(GEMINI_FORMAT_A)
    result = parse(str(path))
    conv = result["conversations"][0]
    msgs = conv["messages"]
    assert len(msgs) == 2
    assert msgs[0]["role"] == "user"
    assert "machine learning" in msgs[0]["content"].lower()


def test_gemini_format_b_activity_produces_conversations():
    """Activity-only format should produce one conversation per activity entry."""
    from parse_gemini import parse
    path = _write_tmp(GEMINI_FORMAT_B)
    result = parse(str(path))
    assert result["source"] == "gemini"
    assert len(result["conversations"]) == 2


def test_gemini_format_b_activity_has_user_message():
    """Each activity entry should surface as at least one user message."""
    from parse_gemini import parse
    path = _write_tmp(GEMINI_FORMAT_B)
    result = parse(str(path))
    for conv in result["conversations"]:
        roles = [m["role"] for m in conv["messages"]]
        assert "user" in roles, f"No user message in conv: {conv}"


def test_gemini_days_filter():
    """Activities outside the window should be excluded."""
    from parse_gemini import parse
    path = _write_tmp(GEMINI_FORMAT_B)
    # time = 2024-01-15 — filter to last 1 day excludes it
    result = parse(str(path), days=1)
    assert len(result["conversations"]) == 0
```

### Step 2: Run to verify failure

```bash
python -m pytest tests/unit/test_parsers.py -v 2>&1 | tail -20
```

Expected: several failures (ImportError or assertion errors where parser behavior doesn't match fixtures).

### Step 3: Fix any parser gaps found

Run the tests and note failures. Common fixes:

**Gemini Format B** — `parse_gemini.py` around line 100+: ensure the activity-only path produces a `role: "user"` message using the `title` field when `details` is empty or contains no text:

```python
# In parse_gemini.py, Format B handling:
# When building the synthesized user message, fall back to item["title"] if
# details extraction yields empty string.
user_text = _extract_text(details[0]) if details else ""
if not user_text:
    user_text = item.get("title", "")
if user_text:
    messages.append({"role": "user", "content": user_text})
```

### Step 4: Run until all pass

```bash
python -m pytest tests/unit/test_parsers.py -v
```

Expected: 12 passed.

### Step 5: Run full suite

```bash
python -m pytest tests/ -v 2>&1 | tail -5
```

Expected: 73 passed (61 + 12).

### Step 6: Commit

```bash
git add tests/unit/test_parsers.py ingestion/parse_gemini.py
git commit -m "test: parser unit tests for ChatGPT and Gemini with synthetic fixtures"
```

---

## Task 3: Inbox Watcher — format detector

**Files:**
- Create: `ingestion/watcher.py`
- Create: `tests/unit/test_watcher.py`

### Step 1: Write the failing format-detection tests

Create `tests/unit/test_watcher.py`:

```python
"""
Unit tests for inbox watcher — format detection and file processing logic.

Run: python -m pytest tests/unit/test_watcher.py -v
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "ingestion"))

import pytest


def _write_json(data, suffix=".json") -> Path:
    tf = tempfile.NamedTemporaryFile(suffix=suffix, delete=False, mode="w")
    json.dump(data, tf)
    tf.close()
    return Path(tf.name)


# ---------------------------------------------------------------------------
# Format detection tests
# ---------------------------------------------------------------------------

def test_detect_claude():
    from watcher import detect_source
    data = [{"uuid": "abc", "chat_messages": [{"sender": "human", "text": "Hi"}], "created_at": "2024-01-01T00:00:00Z"}]
    path = _write_json(data)
    assert detect_source(path) == "claude"


def test_detect_chatgpt():
    from watcher import detect_source
    data = [{"id": "c1", "title": "T", "create_time": 1700000000.0, "mapping": {}}]
    path = _write_json(data)
    assert detect_source(path) == "chatgpt"


def test_detect_gemini_format_a():
    from watcher import detect_source
    data = {"conversations": [{"conversation_id": "g1", "conversation": []}]}
    path = _write_json(data)
    assert detect_source(path) == "gemini"


def test_detect_gemini_format_b():
    from watcher import detect_source
    data = [{"header": {"title": "Gemini Apps Activity"}, "title": "Hello", "time": "2024-01-01T00:00:00Z"}]
    path = _write_json(data)
    assert detect_source(path) == "gemini"


def test_detect_unknown_returns_none():
    from watcher import detect_source
    data = {"random": "structure", "no_known_keys": True}
    path = _write_json(data)
    assert detect_source(path) is None


def test_detect_non_json_returns_none():
    from watcher import detect_source
    tf = tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w")
    tf.write("this is not json {{{{")
    tf.close()
    assert detect_source(Path(tf.name)) is None


# ---------------------------------------------------------------------------
# File move tests
# ---------------------------------------------------------------------------

def test_move_to_processed(tmp_path):
    from watcher import move_to_processed
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    processed = tmp_path / "inbox" / "processed"
    f = inbox / "test.json"
    f.write_text("{}")
    dest = move_to_processed(f, processed_dir=processed)
    assert dest.exists()
    assert not f.exists()
    assert dest.parent == processed


def test_move_to_failed(tmp_path):
    from watcher import move_to_failed
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    failed = tmp_path / "inbox" / "failed"
    f = inbox / "bad.json"
    f.write_text("{}")
    dest = move_to_failed(f, failed_dir=failed)
    assert dest.exists()
    assert not f.exists()
    assert dest.parent == failed


def test_move_handles_name_collision(tmp_path):
    """If dest file already exists, the move should not overwrite — append a suffix."""
    from watcher import move_to_processed
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    processed = tmp_path / "inbox" / "processed"
    processed.mkdir()
    f = inbox / "test.json"
    f.write_text("{}")
    # Pre-create collision
    (processed / "test.json").write_text("{}")
    dest = move_to_processed(f, processed_dir=processed)
    assert dest.exists()
    assert dest.name != "test.json"  # got a unique name
    assert not f.exists()
```

### Step 2: Run to verify failure

```bash
python -m pytest tests/unit/test_watcher.py -v 2>&1 | tail -15
```

Expected: `ModuleNotFoundError: No module named 'watcher'`

### Step 3: Implement `ingestion/watcher.py`

Create `ingestion/watcher.py`:

```python
"""
MemoryBridge Inbox Watcher.

Scans ~/memorybridge/inbox/ for JSON export files, auto-detects source
format, runs ingestion via subprocess, and moves files to processed/ or failed/.

Designed to be called by launchd WatchPaths — runs once and exits.

Usage:
    python ingestion/watcher.py                    # process inbox once, exit
    python ingestion/watcher.py --inbox ~/custom/inbox  # override inbox path
    python ingestion/watcher.py --profile work     # target profile
    python ingestion/watcher.py --preview          # dry run, no memory writes
"""

import argparse
import json
import logging
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("watcher")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s watcher: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

INGESTION_SCRIPT = Path(__file__).parent / "run.py"
DEFAULT_INBOX = Path.home() / "memorybridge" / "inbox"


# ---------------------------------------------------------------------------
# Format detection
# ---------------------------------------------------------------------------

def detect_source(file_path: Path) -> str | None:
    """
    Fingerprint a JSON export file to determine its AI source.

    Returns 'claude', 'chatgpt', 'gemini', or None if unrecognized.
    Returns None on any parse error — caller should move to failed/.
    """
    try:
        with open(file_path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None

    # Gemini Format A: dict with "conversations" key
    if isinstance(data, dict) and "conversations" in data:
        return "gemini"

    if not isinstance(data, list) or not data:
        return None

    first = data[0]
    if not isinstance(first, dict):
        return None

    # Gemini Format B: list with items containing a "header" dict
    if isinstance(first.get("header"), dict):
        header_title = first["header"].get("title", "").lower()
        if "gemini" in header_title:
            return "gemini"

    # Claude: has "chat_messages" (list of message objects)
    if "chat_messages" in first:
        return "claude"

    # ChatGPT: has "mapping" (tree of message nodes)
    if "mapping" in first:
        return "chatgpt"

    return None


# ---------------------------------------------------------------------------
# File movement helpers
# ---------------------------------------------------------------------------

def _unique_dest(dest_dir: Path, filename: str) -> Path:
    """Return a path in dest_dir that doesn't already exist."""
    candidate = dest_dir / filename
    if not candidate.exists():
        return candidate
    stem = Path(filename).stem
    suffix = Path(filename).suffix
    ts = datetime.now().strftime("%H%M%S%f")
    return dest_dir / f"{stem}_{ts}{suffix}"


def move_to_processed(file_path: Path, processed_dir: Path = None) -> Path:
    """Move a successfully processed file to the processed/ subdirectory."""
    processed_dir = processed_dir or (file_path.parent / "processed")
    processed_dir.mkdir(parents=True, exist_ok=True)
    dest = _unique_dest(processed_dir, file_path.name)
    file_path.rename(dest)
    return dest


def move_to_failed(file_path: Path, failed_dir: Path = None) -> Path:
    """Move an unprocessable file to the failed/ subdirectory."""
    failed_dir = failed_dir or (file_path.parent / "failed")
    failed_dir.mkdir(parents=True, exist_ok=True)
    dest = _unique_dest(failed_dir, file_path.name)
    file_path.rename(dest)
    return dest


# ---------------------------------------------------------------------------
# Ingestion runner
# ---------------------------------------------------------------------------

def run_ingestion(source: str, file_path: Path, profile: str,
                  preview: bool = False) -> bool:
    """
    Invoke ingestion/run.py as a subprocess.

    Returns True on success (exit code 0), False otherwise.
    """
    cmd = [
        sys.executable,
        str(INGESTION_SCRIPT),
        "--source", source,
        "--file", str(file_path),
        "--profile", profile,
    ]
    if preview:
        cmd.append("--preview")

    logger.info("Running: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if result.stdout:
        for line in result.stdout.strip().splitlines():
            logger.info("[run.py] %s", line)
    if result.stderr:
        for line in result.stderr.strip().splitlines():
            logger.warning("[run.py stderr] %s", line)

    return result.returncode == 0


# ---------------------------------------------------------------------------
# Main scan loop
# ---------------------------------------------------------------------------

def scan_inbox(inbox: Path, profile: str = "default", preview: bool = False) -> dict:
    """
    Scan inbox for .json files, ingest each, move to processed/ or failed/.

    Returns a summary dict with counts.
    """
    inbox.mkdir(parents=True, exist_ok=True)
    files = sorted(f for f in inbox.iterdir()
                   if f.is_file() and f.suffix.lower() == ".json")

    if not files:
        logger.info("Inbox empty — nothing to process.")
        return {"processed": 0, "failed": 0, "skipped": 0}

    processed = 0
    failed = 0
    skipped = 0

    for f in files:
        source = detect_source(f)
        if source is None:
            logger.warning("Cannot detect source for %s — moving to failed/", f.name)
            move_to_failed(f)
            failed += 1
            continue

        logger.info("Detected %s → %s", f.name, source)
        success = run_ingestion(source, f, profile=profile, preview=preview)

        if success:
            dest = move_to_processed(f)
            logger.info("Processed → %s", dest)
            processed += 1
        else:
            dest = move_to_failed(f)
            logger.warning("Ingestion failed → %s", dest)
            failed += 1

    return {"processed": processed, "failed": failed, "skipped": skipped}


def main():
    parser = argparse.ArgumentParser(description="MemoryBridge inbox watcher (single-shot)")
    parser.add_argument("--inbox", default=str(DEFAULT_INBOX),
                        help=f"Inbox directory (default: {DEFAULT_INBOX})")
    parser.add_argument("--profile", default="default",
                        help="Target memory profile")
    parser.add_argument("--preview", action="store_true",
                        help="Dry run — detect and log, but do not write memories")
    args = parser.parse_args()

    inbox = Path(args.inbox).expanduser()
    start = time.time()
    summary = scan_inbox(inbox, profile=args.profile, preview=args.preview)
    elapsed = time.time() - start

    logger.info(
        "Done in %.1fs — processed: %d, failed: %d",
        elapsed, summary["processed"], summary["failed"],
    )


if __name__ == "__main__":
    main()
```

### Step 4: Run watcher tests

```bash
python -m pytest tests/unit/test_watcher.py -v
```

Expected: all 9 tests pass.

### Step 5: Commit

```bash
git add ingestion/watcher.py tests/unit/test_watcher.py
git commit -m "feat: inbox watcher with format auto-detection (scan-and-exit for launchd)"
```

---

## Task 4: launchd plist

**Files:**
- Create: `launchd/com.memorybridge.inbox.plist`

launchd's `WatchPaths` fires the job whenever the watched directory's mtime changes (file added, removed, or modified). The script runs once, processes everything in the inbox, and exits. launchd restarts it next time the directory changes.

### Step 1: Create the launchd directory

```bash
mkdir -p /Users/cale/Documents/GitHub/memorybridge/launchd
```

### Step 2: Write the plist

Create `launchd/com.memorybridge.inbox.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
    "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.memorybridge.inbox</string>

    <key>ProgramArguments</key>
    <array>
        <string>/usr/local/bin/python3</string>
        <string>/Users/cale/Documents/GitHub/memorybridge/ingestion/watcher.py</string>
        <string>--profile</string>
        <string>default</string>
    </array>

    <!-- Fire whenever ~/memorybridge/inbox changes (file added/removed) -->
    <key>WatchPaths</key>
    <array>
        <string>/Users/cale/memorybridge/inbox</string>
    </array>

    <key>StandardOutPath</key>
    <string>/Users/cale/memorybridge/logs/watcher.log</string>

    <key>StandardErrorPath</key>
    <string>/Users/cale/memorybridge/logs/watcher_err.log</string>

    <!-- Do not run at login — only on file changes -->
    <key>RunAtLoad</key>
    <false/>

    <!-- Throttle: don't fire more than once per 10 seconds -->
    <key>ThrottleInterval</key>
    <integer>10</integer>
</dict>
</plist>
```

### Step 3: Create the inbox directory and verify watcher runs standalone

```bash
mkdir -p ~/memorybridge/inbox ~/memorybridge/logs

# Test standalone — should log "Inbox empty"
python3 /Users/cale/Documents/GitHub/memorybridge/ingestion/watcher.py --preview
```

Expected output includes: `Inbox empty — nothing to process.`

### Step 4: Install the launchd job

```bash
cp /Users/cale/Documents/GitHub/memorybridge/launchd/com.memorybridge.inbox.plist \
   ~/Library/LaunchAgents/com.memorybridge.inbox.plist

launchctl load ~/Library/LaunchAgents/com.memorybridge.inbox.plist
launchctl list | grep memorybridge
```

Expected: `com.memorybridge.inbox` appears with status `0` (not running, waiting for path event).

### Step 5: Smoke test the full loop

```bash
# Drop a Claude export stub into inbox
echo '[{"uuid":"test","chat_messages":[{"sender":"human","text":"Hello"}],"created_at":"2024-01-01T00:00:00Z"}]' \
  > ~/memorybridge/inbox/test_claude.json

# Wait ~2 seconds for launchd to fire, then check
sleep 3
ls ~/memorybridge/inbox/processed/   # should contain test_claude.json
cat ~/memorybridge/logs/watcher.log  # should show detection + ingestion attempt
```

Note: ingestion will fail on this stub (no real facts to extract), so the file may land in `failed/` — that's expected. The point is the watcher fired and ran.

### Step 6: Commit

```bash
git add launchd/com.memorybridge.inbox.plist
git commit -m "feat: launchd WatchPaths agent for zero-CLI inbox ingestion"
```

---

## Task 5: Full suite + push

### Step 1: Run full test suite

```bash
python -m pytest tests/ -v 2>&1 | tail -10
```

Expected: 73+ tests passing (61 + 12 parser tests + 9 watcher tests = 82 total, minus any that were already there).

### Step 2: Push to main

```bash
git push origin main
```

---

## Verification checklist

- [ ] Portability UI has 3 tabs; Passport tab generates and downloads passport
- [ ] `parse_chatgpt` tests pass with synthetic ChatGPT fixture (5 tests)
- [ ] `parse_gemini` tests pass with both Format A and Format B fixtures (7 tests)
- [ ] `detect_source()` correctly identifies all 3 formats + unknown
- [ ] `move_to_processed` / `move_to_failed` handle name collisions
- [ ] `watcher.py --preview` runs without error on empty inbox
- [ ] launchd job installed and listed: `launchctl list | grep memorybridge`
- [ ] Dropping a file into inbox triggers the watcher within ~10 seconds
- [ ] Full test suite passes

---

## Notes for executor

**Gemini Format B parser fix** (Task 2, Step 3): The existing `parse_gemini.py` probably handles Format B but may produce empty message lists if the `details` field is empty or missing. The fix is a one-liner fallback to `item["title"]` — don't restructure the whole parser.

**launchd install** (Task 4, Step 4): `launchctl load` will succeed silently even if the plist has errors. Always follow with `launchctl list | grep memorybridge` to confirm the job registered. If it doesn't appear, check with `plutil -lint ~/Library/LaunchAgents/com.memorybridge.inbox.plist`.

**Token budget note**: `export_passport` is already implemented in server.py and ingestion/passport.py — the UI task is purely wiring, no backend changes.
