"""
Unit tests for inbox watcher — format detection and file processing logic.

Run: python -m pytest tests/unit/test_watcher.py -v
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "ingestion"))



def _write_json(data, suffix=".json") -> Path:
    tf = tempfile.NamedTemporaryFile(suffix=suffix, delete=False, mode="w",
                                     encoding="utf-8")
    json.dump(data, tf)
    tf.close()
    return Path(tf.name)


# ---------------------------------------------------------------------------
# Format detection tests
# ---------------------------------------------------------------------------

def test_detect_claude():
    from watcher import detect_source
    data = [{"uuid": "abc",
             "chat_messages": [{"sender": "human", "text": "Hi"}],
             "created_at": "2024-01-01T00:00:00Z"}]
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
    data = [{"header": {"title": "Gemini Apps Activity"},
             "title": "Hello", "time": "2024-01-01T00:00:00Z"}]
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


def test_detect_empty_list_returns_none():
    from watcher import detect_source
    path = _write_json([])
    assert detect_source(path) is None


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
    """If dest file already exists, the move should not overwrite — use a unique name."""
    from watcher import move_to_processed
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    processed = tmp_path / "inbox" / "processed"
    processed.mkdir()
    f = inbox / "test.json"
    f.write_text("{}")
    # Pre-create a collision
    (processed / "test.json").write_text("{}")
    dest = move_to_processed(f, processed_dir=processed)
    assert dest.exists()
    assert dest.name != "test.json"  # got a unique name
    assert not f.exists()


# ---------------------------------------------------------------------------
# scan_inbox tests (no subprocess — uses mock)
# ---------------------------------------------------------------------------

def test_scan_inbox_empty_returns_zero_counts(tmp_path):
    from watcher import scan_inbox
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    summary = scan_inbox(inbox, profile="default", _dry_run=True)
    assert summary["processed"] == 0
    assert summary["failed"] == 0


def test_scan_inbox_unknown_format_goes_to_failed(tmp_path):
    from watcher import scan_inbox
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "mystery.json").write_text('{"unknown": true}')
    summary = scan_inbox(inbox, profile="default", _dry_run=True)
    assert summary["failed"] == 1
    assert (inbox / "failed" / "mystery.json").exists()


def test_scan_inbox_known_format_counts_as_processed_in_dry_run(tmp_path):
    from watcher import scan_inbox
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    data = [{"uuid": "x", "chat_messages": [{"sender": "human", "text": "Hi"}],
             "created_at": "2024-01-01T00:00:00Z"}]
    (inbox / "claude_export.json").write_text(json.dumps(data))
    # _dry_run=True skips subprocess, treats detection success as processed
    summary = scan_inbox(inbox, profile="default", _dry_run=True)
    assert summary["processed"] == 1
    assert (inbox / "processed" / "claude_export.json").exists()
