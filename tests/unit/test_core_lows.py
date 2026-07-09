"""Regression tests for the core low-severity batch.

  #124  update_profile validates project shape on write; list_projects tolerates
        a malformed (non-dict) project element instead of 500-ing.
  #126  build_passport budgets against a caller-supplied real token counter so
        the emitted passport respects max_tokens as measured by that counter.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import pytest

from db.store import MemoryStore
from ingestion.passport import build_passport


# --------------------------------------------------------------------------- #124
def test_update_profile_rejects_non_dict_projects(tmp_path):
    st = MemoryStore(tmp_path / "m.db")
    st.ensure_profile("default")
    with pytest.raises(ValueError, match="projects must be a list of objects"):
        st.update_profile("default", projects=["not-a-dict"])
    # A well-formed list is accepted and round-trips.
    st.update_profile("default", projects=[{"id": "p1", "name": "Proj"}])
    prof = st.get_profile("default")
    assert prof["projects"] == [{"id": "p1", "name": "Proj"}]


def test_list_projects_tolerates_malformed_entry(tmp_path, monkeypatch):
    # Bypass write validation to simulate legacy/hand-written bad data, then make
    # sure the read path skips it rather than raising AttributeError (#124).
    st = MemoryStore(tmp_path / "m.db")
    st.ensure_profile("default")
    st._conn.execute(
        "UPDATE profiles SET projects=? WHERE name=?",
        (json.dumps([{"id": "ok", "name": "Good"}, "bare-string"]), "default"),
    )
    st._conn.commit()

    import server
    monkeypatch.setattr(server, "_store", st)
    out = json.loads(server.list_projects.fn(profile="default"))
    ids = [p["id"] for p in out["projects"]]
    assert ids == ["ok"]            # bad element skipped, no crash
    assert out["count"] == 1


# --------------------------------------------------------------------------- #126
def _mems(n, text):
    return [{"content": text, "category": "fact", "importance": "medium"} for _ in range(n)]


def test_passport_respects_real_counter_budget():
    # A counter that treats every character as a token (much larger than the
    # 4-char heuristic). The emitted passport, measured by that same counter,
    # must stay within max_tokens.
    counter = len
    max_tokens = 300
    passport = build_passport(
        _mems(50, "a memory line with several words in it"),
        identity={"name": "Cale", "role": "PM"},
        profile="default",
        max_tokens=max_tokens,
        token_counter=counter,
    )
    assert counter(passport) <= max_tokens


def test_passport_counter_affects_how_much_fits():
    mems = _mems(40, "a memory line with several words in it")
    small = build_passport(mems, max_tokens=300, token_counter=len)        # chars-as-tokens
    big = build_passport(mems, max_tokens=300)                             # 4-char heuristic
    # The stricter (larger) counter must fit no more content than the heuristic.
    assert len(small) <= len(big)
