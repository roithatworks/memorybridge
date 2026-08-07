"""Regression tests for issue #178 — untrusted-data framing on the retrieval
path. Memory content served back to a consuming model (get_memory,
search_memory, export_for_model, export_passport) previously carried no
signal distinguishing it from trusted instructions, unlike the ingestion path
(ingestion/extractor.py, ingestion/resolver.py, db/reflect.py) which already
delimits this class of data.

Design constraint verified explicitly below: get_memory/search_memory add a
top-level "_security_notice" field rather than mutating each memory's
"content" value, because ui/views/memory_browser.py displays that value
verbatim (st.text(mem["content"])) — literal delimiter tags would leak into
the UI.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import pytest

from db.store import MemoryStore
from exports import export_for_model, export_passport, UNTRUSTED_OPEN, UNTRUSTED_CLOSE
from ingestion.passport import build_passport


@pytest.fixture
def fresh_store(tmp_path, monkeypatch):
    import server
    s = MemoryStore(tmp_path / "test.db")
    monkeypatch.setattr(server, "_store", s)
    s.ensure_profile("default")
    s.add_memory("default", "Cale prefers dark mode",
                 category="preference", importance="medium")
    return s


# --------------------------------------------------------------------------- #
# get_memory / search_memory — non-mutating notice field
# --------------------------------------------------------------------------- #

def test_get_memory_adds_security_notice_when_memories_present(fresh_store):
    import server
    result = json.loads(server.get_memory.fn(profile="default"))
    assert "_security_notice" in result
    assert "untrusted" in result["_security_notice"].lower()


def test_get_memory_omits_security_notice_when_empty(tmp_path, monkeypatch):
    import server
    s = MemoryStore(tmp_path / "empty.db")
    monkeypatch.setattr(server, "_store", s)
    s.ensure_profile("empty_profile")
    result = json.loads(server.get_memory.fn(profile="empty_profile"))
    assert result["token_stats"]["memories_returned"] == 0
    assert "_security_notice" not in result


def test_get_memory_does_not_mutate_content_field(fresh_store):
    # The whole point of using a separate field: content must round-trip
    # verbatim for the UI (memory_browser.py renders it directly).
    import server
    result = json.loads(server.get_memory.fn(profile="default"))
    contents = [m["content"] for m in result["memories"]]
    assert contents == ["Cale prefers dark mode"]
    assert not any(UNTRUSTED_OPEN in c or UNTRUSTED_CLOSE in c for c in contents)


def test_search_memory_adds_security_notice_when_results_present(fresh_store):
    import server
    result = json.loads(server.search_memory.fn(query="dark mode", profile="default"))
    assert "_security_notice" in result


def test_search_memory_omits_security_notice_when_no_results(fresh_store):
    import server
    result = json.loads(server.search_memory.fn(query="nonexistent xyz123", profile="default"))
    assert result["total_matches"] == 0
    assert "_security_notice" not in result


# --------------------------------------------------------------------------- #
# export_for_model — literal delimiter markers (plain text, safe to wrap)
# --------------------------------------------------------------------------- #

def test_export_for_model_chatgpt_wraps_memories(fresh_store):
    text = export_for_model(fresh_store, model="chatgpt", profile="default")
    assert UNTRUSTED_OPEN in text
    assert UNTRUSTED_CLOSE in text
    open_idx = text.index(UNTRUSTED_OPEN)
    close_idx = text.index(UNTRUSTED_CLOSE)
    assert open_idx < text.index("Cale prefers dark mode") < close_idx


def test_export_for_model_gemini_wraps_memories(fresh_store):
    text = export_for_model(fresh_store, model="gemini", profile="default")
    assert UNTRUSTED_OPEN in text
    assert UNTRUSTED_CLOSE in text


def test_export_for_model_ollama_wraps_memories(fresh_store):
    text = export_for_model(fresh_store, model="ollama", profile="default")
    assert UNTRUSTED_OPEN in text
    assert UNTRUSTED_CLOSE in text


def test_export_for_model_no_memories_no_wrapper(tmp_path):
    s = MemoryStore(tmp_path / "empty.db")
    s.ensure_profile("empty_profile")
    text = export_for_model(s, model="chatgpt", profile="empty_profile")
    assert UNTRUSTED_OPEN not in text


# --------------------------------------------------------------------------- #
# export_passport / build_passport
# --------------------------------------------------------------------------- #

def test_export_passport_wraps_memories(fresh_store):
    text = export_passport(fresh_store, profile="default")
    assert UNTRUSTED_OPEN in text
    assert UNTRUSTED_CLOSE in text
    assert text.index(UNTRUSTED_OPEN) < text.index("Cale prefers dark mode") < text.index(UNTRUSTED_CLOSE)


def test_build_passport_no_memories_no_wrapper():
    text = build_passport(memories=[], identity={"name": "Cale"}, profile="default")
    assert UNTRUSTED_OPEN not in text


def test_build_passport_respects_token_budget_with_wrapper_overhead():
    # The wrapper's own tokens must be reserved against max_tokens up front,
    # not added on top of an already-full budget (#126's original concern,
    # now with the wrapper as additional fixed overhead).
    memories = [
        {"content": f"fact number {i} with some extra padding words here", "category": "fact",
         "importance": "medium"}
        for i in range(50)
    ]
    text = build_passport(memories=memories, identity={}, profile="default",
                          max_tokens=300, token_counter=lambda t: len(t.split()))
    assert len(text.split()) <= 300
