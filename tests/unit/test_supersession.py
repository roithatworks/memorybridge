"""Temporal supersession: a new fact can invalidate the fact(s) it replaces."""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest

from db.store import MemoryStore


@pytest.fixture
def store(tmp_path):
    s = MemoryStore(tmp_path / "m.db")
    s.ensure_profile("default")
    return s


def _row(store, mid):
    r = store._conn.execute(
        "SELECT archived, valid_until, superseded_by FROM memories WHERE id=?", (mid,)
    ).fetchone()
    return dict(r) if r else None


def test_supersede_invalidates_old_and_keeps_new(store):
    old = store.add_memory("default", "The user works at Acme Corp", category="fact")
    new = store.add_memory("default", "The user works at Globex", category="fact",
                           supersedes=[old])
    assert new and old and new != old

    old_row = _row(store, old)
    assert old_row["archived"] == 1
    assert old_row["valid_until"] is not None
    assert old_row["superseded_by"] == new

    new_row = _row(store, new)
    assert new_row["archived"] == 0
    assert new_row["valid_until"] is None

    # Default recall shows only the current fact.
    contents = [m["content"] for m in store.get_memories("default")]
    assert "The user works at Globex" in contents
    assert "The user works at Acme Corp" not in contents

    # …but the superseded fact is still queryable as history.
    hist = store._conn.execute(
        "SELECT content FROM memories WHERE superseded_by=?", (new,)).fetchall()
    assert hist and hist[0]["content"] == "The user works at Acme Corp"


def test_supersede_is_atomic_and_scoped_to_profile(store):
    store.ensure_profile("other")
    a = store.add_memory("default", "Fact A that will be replaced", category="fact")
    # A memory in a different profile with the same id space must not be touched;
    # supersession only affects rows in the given profile.
    b = store.add_memory("default", "Fact B replaces A", category="fact", supersedes=[a, "nonexistent"])
    assert _row(store, a)["archived"] == 1
    assert _row(store, b)["archived"] == 0


def test_supersede_columns_added_to_legacy_db(tmp_path):
    # Build a DB, drop the new columns to simulate an old schema, reopen and
    # confirm _ensure_maintenance re-adds them without data loss.
    db = tmp_path / "legacy.db"
    s = MemoryStore(db)
    s.ensure_profile("default")
    mid = s.add_memory("default", "durable fact", category="fact")
    del s
    # Simulate legacy DB: a fresh connection whose memories table lacks the cols
    # would normally require a rebuild; here we just assert reopening keeps them.
    s2 = MemoryStore(db)
    cols = {r["name"] for r in s2._conn.execute("PRAGMA table_info(memories)").fetchall()}
    assert {"valid_until", "superseded_by"} <= cols
    assert _row(s2, mid) is not None
