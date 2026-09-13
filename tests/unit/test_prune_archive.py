"""Regression tests for issue #195: the prune path must ARCHIVE, not DELETE.

Background: the prune execution path used Store.delete_memory, which issued
`DELETE FROM memories WHERE id=?` — a hard delete with no recovery. That
contradicted the workspace archive-only policy and destroyed 12 memories
unrecoverably (July 2026) plus 5 more on 2026-09-12.

These tests pin the corrected behavior: pruning soft-deletes (archived=1), so a
pruned memory leaves default retrieval but remains in the table and is
recoverable. delete_memory keeps its destructive contract for explicit calls.

Run: python -m pytest tests/unit/test_prune_archive.py -v
"""
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest

import config
from db.store import MemoryStore
from db.pruner import record_outcome


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORYBRIDGE_DATA", str(tmp_path))
    monkeypatch.setenv("MEMORYBRIDGE_NO_EMBED", "1")
    config.reset_cache()
    yield
    config.reset_cache()


@pytest.fixture
def store(tmp_path):
    return MemoryStore(tmp_path / "memory.db")


CONTENT = "Cale prefers dark mode in the terminal"


# ---------------------------------------------------------------------------
# Store.archive_memory
# ---------------------------------------------------------------------------

def test_archive_memory_keeps_row_and_sets_archived(store):
    mid = store.add_memory("default", CONTENT)
    assert mid

    freed = store.archive_memory("default", mid, reason="test-reason")
    assert freed > 0

    row = store._conn.execute(
        "SELECT archived, archived_at, archive_reason FROM memories WHERE id=?",
        (mid,),
    ).fetchone()
    assert row is not None, "archive must NOT remove the row (issue #195)"
    assert row["archived"] == 1
    assert row["archived_at"]
    assert row["archive_reason"] == "test-reason"


def test_archived_memory_drops_out_of_retrieval(store):
    mid = store.add_memory("default", CONTENT)
    assert any(m["id"] == mid for m in store.get_memories("default"))

    store.archive_memory("default", mid)

    ids = [m["id"] for m in store.get_memories("default")]
    assert mid not in ids, "archived memory must leave default retrieval"


def test_archive_memory_unknown_id_returns_zero(store):
    assert store.archive_memory("default", "mem_nonexistent") == 0


def test_archive_memory_is_idempotent(store):
    mid = store.add_memory("default", CONTENT)
    assert store.archive_memory("default", mid) > 0
    # Second call finds archived=0 filter empty -> 0, and must not raise.
    assert store.archive_memory("default", mid) == 0


# ---------------------------------------------------------------------------
# End-to-end: approving a queued prune must archive, not delete
# ---------------------------------------------------------------------------

def test_record_outcome_archives_approved_prune(store):
    mid = store.add_memory("default", "Ops radar source list reconciled 2026-07-31")
    assert mid
    conn = store._conn

    conn.execute(
        """INSERT INTO prune_queue
           (id, profile, rule_name, candidate_id, superseded_by, reason,
            confidence, suggested_action, created_at, resolved)
           VALUES (?,?,?,?,?,?,?,?,?,0)""",
        ("pq_test_archive", "default", "stale_project_status", mid, None,
         "superseded by a newer entry", 0.85, "delete", datetime.now().isoformat()),
    )
    conn.commit()

    result = record_outcome(
        conn, "pq_test_archive", True,
        lambda p, m: store.archive_memory(p, m,
                                          reason="pruned: reviewed queue approval"),
    )

    assert result.get("status") == "user_approved"
    assert result.get("tokens_freed", 0) > 0

    row = conn.execute(
        "SELECT archived, archive_reason FROM memories WHERE id=?", (mid,)
    ).fetchone()
    assert row is not None, (
        "approved prune hard-deleted the memory — regression of issue #195"
    )
    assert row["archived"] == 1


def test_record_outcome_rejection_keeps_memory_live(store):
    mid = store.add_memory("default", "Supervisor certification expires 2027-04")
    conn = store._conn
    conn.execute(
        """INSERT INTO prune_queue
           (id, profile, rule_name, candidate_id, superseded_by, reason,
            confidence, suggested_action, created_at, resolved)
           VALUES (?,?,?,?,?,?,?,?,?,0)""",
        ("pq_test_reject", "default", "stale_project_status", mid, None,
         "superseded by a newer entry", 0.85, "delete", datetime.now().isoformat()),
    )
    conn.commit()

    record_outcome(
        conn, "pq_test_reject", False,
        lambda p, m: store.archive_memory(p, m),
    )

    row = conn.execute(
        "SELECT archived FROM memories WHERE id=?", (mid,)
    ).fetchone()
    assert row is not None
    assert row["archived"] == 0, "a rejected prune must leave the memory live"


# ---------------------------------------------------------------------------
# delete_memory keeps its explicit destructive contract
# ---------------------------------------------------------------------------

def test_delete_memory_still_hard_deletes(store):
    mid = store.add_memory("default", "throwaway note for the explicit delete path")
    assert store.delete_memory("default", mid) > 0
    assert store._conn.execute(
        "SELECT 1 FROM memories WHERE id=?", (mid,)
    ).fetchone() is None
