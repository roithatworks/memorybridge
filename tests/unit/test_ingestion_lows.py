"""Regression tests for the ingestion low-severity batch.

  #119  extractor._truncate_conversation keeps an oversized single message
        (truncated) instead of emitting an empty conversation.
  #127  notion_queue._create_with_retry backs off and retries on 429, and gives
        up cleanly (returning failure) when the limit persists.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "ingestion"))

import extractor
import notion_queue


# --------------------------------------------------------------------------- #119
def test_truncate_keeps_oversized_single_message():
    huge = "x" * (extractor._MAX_CONV_CHARS * 2)
    conv = {"id": "c1", "messages": [{"role": "user", "content": huge}]}
    out = extractor._truncate_conversation(conv)
    msgs = out["messages"]
    assert len(msgs) == 1, "oversized single message must be kept, not dropped"
    assert msgs[0]["content"] != huge, "message should be truncated"
    assert len(msgs[0]["content"]) <= extractor._MAX_CONV_CHARS
    assert msgs[0]["content"].endswith("[truncated]")


def test_truncate_normal_conversation_unchanged_shape():
    conv = {"id": "c2", "messages": [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there"},
    ]}
    out = extractor._truncate_conversation(conv)
    assert [m["content"] for m in out["messages"]] == ["hello", "hi there"]


# --------------------------------------------------------------------------- #127
class _RateLimitError(Exception):
    def __init__(self):
        super().__init__("rate_limited")
        self.status = 429


class _FakeNotionClient:
    """Raises 429 for the first `fail_times` calls, then succeeds (or always)."""
    def __init__(self, fail_times, always=False):
        self.fail_times = fail_times
        self.always = always
        self.calls = 0
        self.pages = self

    def create(self, **kwargs):
        self.calls += 1
        if self.always or self.calls <= self.fail_times:
            raise _RateLimitError()
        return {"id": "page"}


def test_create_retries_then_succeeds(monkeypatch):
    monkeypatch.setattr(notion_queue.time, "sleep", lambda *_: None)
    client = _FakeNotionClient(fail_times=2)
    ok = notion_queue._create_with_retry(client, "db", {}, {"fact": "x"})
    assert ok is True
    assert client.calls == 3  # two 429s + one success


def test_create_gives_up_after_max_retries(monkeypatch):
    monkeypatch.setattr(notion_queue.time, "sleep", lambda *_: None)
    client = _FakeNotionClient(fail_times=0, always=True)
    ok = notion_queue._create_with_retry(client, "db", {}, {"fact": "x"})
    assert ok is False
    assert client.calls == notion_queue._NOTION_MAX_RETRIES


def test_is_rate_limited_detection():
    assert notion_queue._is_rate_limited(_RateLimitError())
    assert notion_queue._is_rate_limited(Exception("HTTP 429 Too Many Requests"))
    assert not notion_queue._is_rate_limited(Exception("validation_error: bad property"))
