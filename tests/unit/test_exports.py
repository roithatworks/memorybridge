"""Tests for the store-free exports module (#91).

Importing `exports` must have no side effects (no MemoryStore, no atexit), and
the export functions must work against any store WITHOUT importing `server`.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import pytest

# NOTE: deliberately do NOT import server here — the point of #91 is that the
# export logic is usable without it.
from db.store import MemoryStore
import exports


@pytest.fixture
def store(tmp_path):
    s = MemoryStore(tmp_path / "m.db")
    s.ensure_profile("default")
    s.update_profile("default", identity={"name": "Cale", "role": "PM"})
    s.add_memory("default", "Cale prefers dark mode", category="preference", importance="high")
    s.add_memory("default", "Ships on Fridays", category="fact", importance="medium")
    return s


def test_export_for_model_chatgpt_includes_identity_and_memories(store):
    text = exports.export_for_model(store, "chatgpt", "default")
    assert "# Memory Chip" in text
    assert "Cale" in text
    assert "dark mode" in text


@pytest.mark.parametrize("model,sep", [("gemini", " | "), ("ollama", ";")])
def test_export_for_model_compact_formats(store, model, sep):
    text = exports.export_for_model(store, model, "default")
    assert "Cale" in text
    assert sep in text


def test_export_for_model_unknown_model(store):
    text = exports.export_for_model(store, "bogus", "default")
    assert "Unknown model" in text


def test_export_for_model_new_profile_is_created_empty(store):
    # ensure_profile runs first, so an unseen profile is created (empty) rather
    # than reported "not found" — faithfully-preserved existing behavior.
    text = exports.export_for_model(store, "chatgpt", "brand-new")
    assert "# Memory Chip" in text
    assert "Unknown" in text          # no identity yet


def test_export_passport_plain_text(store):
    text = exports.export_passport(store, "default")
    assert "Memory Passport" in text
    assert "```" not in text          # never fenced
    assert "dark mode" in text


def test_analytics_callback_invoked(store):
    calls = []
    exports.export_for_model(store, "chatgpt", "default",
                             log_analytics=lambda **kw: calls.append(kw))
    assert calls and calls[0]["operation"] == "export_for_model"
    assert calls[0]["memories_returned"] >= 1


def test_count_tokens_and_apply_decay_available():
    assert exports.count_tokens("hello world") >= 1
    assert exports.count_tokens("") == 0
    mems = [{"content": "x", "created_at": "2026-01-01", "relevance_score": 1.0,
             "importance": "medium", "access_count": 0, "last_accessed": "2026-01-01"}]
    out = exports.apply_decay(mems, exports.DECAY_CONFIG)
    assert "effective_score" in out[0]
