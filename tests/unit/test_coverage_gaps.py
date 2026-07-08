"""Regression tests for the historically buggiest ingestion code (#122).

Covers the two spots that already caused production regressions —
  * resolver._parse_verdict  ("every escalation rejected" when Claude wrapped
    its JSON verdict in a ```json fence),
  * extractor fence-stripping ("Added: 0" when DeepSeek fenced its output) —
plus the previously-untested claude/hermes parsers and the watcher's
timeout / preview / unknown-extension paths.
"""
import json
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "ingestion"))

import pytest

import resolver
import extractor
import parse_claude
import parse_hermes
import watcher


# ========================================================================= resolver
def test_parse_verdict_bare_json():
    assert resolver._parse_verdict('{"verdict": "accept"}') == {"verdict": "accept"}


def test_parse_verdict_json_fenced():
    raw = '```json\n{"verdict": "accept", "merged_fact": "x"}\n```'
    assert resolver._parse_verdict(raw) == {"verdict": "accept", "merged_fact": "x"}


def test_parse_verdict_plain_fence():
    assert resolver._parse_verdict('```\n{"verdict": "reject"}\n```') == {"verdict": "reject"}


def test_parse_verdict_preamble_then_object():
    raw = 'Sure! Here is the verdict you asked for:\n{"verdict": "accept"}'
    assert resolver._parse_verdict(raw) == {"verdict": "accept"}


def test_parse_verdict_garbage_and_empty():
    assert resolver._parse_verdict("not json at all") is None
    assert resolver._parse_verdict("") is None
    assert resolver._parse_verdict(None) is None


# ========================================================================= extractor
class _FakeChat:
    def __init__(self, content):
        self._content = content

    @property
    def chat(self):
        return self

    @property
    def completions(self):
        return self

    def create(self, **_kwargs):
        msg = type("M", (), {"content": self._content})
        choice = type("C", (), {"message": msg})
        return type("R", (), {"choices": [choice]})


def _facts():
    return [{"fact": "Cale prefers dark mode", "confidence": 0.9}]


def test_extractor_strips_json_fence():
    client = _FakeChat("```json\n" + json.dumps(_facts()) + "\n```")
    out = extractor._call_deepseek(client, [{"id": "c1", "messages": [{"role": "user", "content": "hi"}]}])
    assert out == _facts()


def test_extractor_strips_plain_fence():
    client = _FakeChat("```\n" + json.dumps(_facts()) + "\n```")
    out = extractor._call_deepseek(client, [{"id": "c1", "messages": [{"role": "user", "content": "hi"}]}])
    assert out == _facts()


def test_extractor_accepts_bare_json():
    client = _FakeChat(json.dumps(_facts()))
    out = extractor._call_deepseek(client, [{"id": "c1", "messages": [{"role": "user", "content": "hi"}]}])
    assert out == _facts()


def test_extractor_returns_empty_on_persistent_bad_json():
    client = _FakeChat("still not json")
    out = extractor._call_deepseek(client, [{"id": "c1", "messages": [{"role": "user", "content": "hi"}]}])
    assert out == []


# ========================================================================= parse_claude
def test_parse_claude_roles_and_list_content(tmp_path):
    export = [{
        "uuid": "u1", "name": "Chat", "created_at": "2026-01-02T00:00:00Z",
        "chat_messages": [
            {"sender": "human", "text": "hello"},
            {"sender": "assistant", "text": [{"text": "hi"}, {"text": "there"}]},
            {"sender": "human", "text": "   "},   # blank -> skipped
        ],
    }]
    p = tmp_path / "conversations.json"
    p.write_text(json.dumps(export))
    out = parse_claude.parse(str(p))
    assert out["source"] == "claude"
    convs = out["conversations"]
    assert len(convs) == 1
    msgs = convs[0]["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[1]["content"] == "hi there"   # list content joined


def test_parse_claude_unknown_date_survives_days_filter(tmp_path):
    export = [{
        "uuid": "u2", "name": "NoDate", "created_at": "",
        "chat_messages": [{"sender": "human", "text": "keep me"}],
    }]
    p = tmp_path / "conversations.json"
    p.write_text(json.dumps(export))
    out = parse_claude.parse(str(p), days=1)   # unknown date must not be dropped (#79)
    assert len(out["conversations"]) == 1
    assert out["conversations"][0]["date"] == "unknown"


# ========================================================================= parse_hermes
def _make_hermes_db(path):
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE sessions (id TEXT, title TEXT, started_at REAL);
        CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT,
                               content TEXT, timestamp REAL);
        """
    )
    conn.execute("INSERT INTO sessions VALUES ('s1','Real Session', 1700000000.0)")
    conn.execute("INSERT INTO sessions VALUES ('s2','Trivial', 1700000000.0)")
    conn.executemany(
        "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?,?,?,?)",
        [
            ("s1", "user", "what is the plan", 1.0),
            ("s1", "assistant", "here is the plan", 2.0),
            ("s1", "system", "ignore me", 3.0),   # non-kept role filtered out
            ("s2", "user", "hey", 1.0),            # lone message -> session skipped
        ],
    )
    conn.commit()
    conn.close()


def test_parse_hermes_groups_and_skips_trivial(tmp_path):
    db = tmp_path / "state.db"
    _make_hermes_db(db)
    out = parse_hermes.parse(str(db))
    assert out["source"] == "hermes"
    convs = out["conversations"]
    assert len(convs) == 1                       # s2 skipped (<2 messages)
    assert convs[0]["id"] == "s1"
    roles = [m["role"] for m in convs[0]["messages"]]
    assert roles == ["user", "assistant"]        # system role filtered


# ========================================================================= watcher
def test_run_ingestion_returns_false_on_timeout(monkeypatch, tmp_path):
    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="run", timeout=1)
    monkeypatch.setattr(watcher.subprocess, "run", boom)
    ok = watcher.run_ingestion("claude", tmp_path / "x.json", profile="default")
    assert ok is False


def test_scan_inbox_logs_and_skips_non_json(tmp_path):
    (tmp_path / "export.zip").write_text("not json")
    result = watcher.scan_inbox(tmp_path, _dry_run=True)
    assert result["skipped"] >= 1
    assert result["processed"] == 0
    assert (tmp_path / "export.zip").exists()    # left in place, not moved


def test_preview_leaves_file_in_inbox(monkeypatch, tmp_path):
    conv = [{"uuid": "u1", "chat_messages": [{"sender": "human", "text": "hi"}]}]
    f = tmp_path / "conversations.json"
    f.write_text(json.dumps(conv))
    monkeypatch.setattr(watcher, "run_ingestion", lambda *a, **k: True)
    result = watcher.scan_inbox(tmp_path, preview=True)
    assert f.exists()                            # preview must not move the file (#84)
    assert result["processed"] == 0
