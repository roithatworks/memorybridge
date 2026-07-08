"""Regression test for #94: a failed merge UPDATE must not fall through to a
duplicate INSERT — it must raise so the anti-duplication guarantee holds."""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import pytest

from db.store import MemoryStore

TAGS = ["entity:dark_mode", "entity:editor"]


def test_merge_write_failure_does_not_double_write(tmp_path):
    st = MemoryStore(tmp_path / "m.db", merge_threshold=0.1, merge_min_tags=2)
    st.ensure_profile("default")

    # First memory establishes a merge candidate (shares 2 entity tags).
    st.add_memory("default", "Cale likes dark mode in editors",
                  tags=list(TAGS), skip_enrichment=True)
    assert _count(st) == 1

    # Make the merge UPDATE fail, leaving the SELECT/candidate path working.
    real_execute = st._conn.execute

    def failing_execute(sql, *args, **kwargs):
        if isinstance(sql, str) and sql.lstrip().upper().startswith("UPDATE MEMORIES"):
            raise sqlite3.OperationalError("simulated lock timeout")
        return real_execute(sql, *args, **kwargs)

    st._conn.execute = failing_execute

    # A near-duplicate would merge into the first row — but the UPDATE fails.
    # add_memory must propagate the error, NOT insert a new duplicate row (#94).
    with pytest.raises(sqlite3.OperationalError):
        st.add_memory("default", "Cale prefers dark mode in editors",
                      tags=list(TAGS), skip_enrichment=True)

    st._conn.execute = real_execute
    assert _count(st) == 1  # still one row — no silent duplicate


def _count(store):
    return store._conn.execute(
        "SELECT COUNT(*) FROM memories WHERE profile='default'").fetchone()[0]
