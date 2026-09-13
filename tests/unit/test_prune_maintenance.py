"""Tests for prune maintenance paths: embedding purge guard (#192) and
autonomous reversal wiring (#194).

Run: python -m pytest tests/unit/test_prune_maintenance.py -v
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest

import config
from db.store import MemoryStore
from db.pruner import autonomous_reverse_prune


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


def _add_embedding(store, mid, profile="default"):
    """Insert a stand-in vector. Real embedding generation is disabled under
    MEMORYBRIDGE_NO_EMBED, so tests seed the row directly."""
    store._conn.execute(
        "INSERT OR REPLACE INTO memory_embeddings(id, profile, vector) VALUES(?,?,?)",
        (mid, profile, "[0.0, 0.0, 0.0]"),
    )
    store._conn.commit()


def _has_embedding(store, mid):
    return store._conn.execute(
        "SELECT 1 FROM memory_embeddings WHERE id=?", (mid,)
    ).fetchone() is not None


def _set_archived_at(store, mid, days_ago):
    stamp = (datetime.now() - timedelta(days=days_ago)).isoformat()
    store._conn.execute(
        "UPDATE memories SET archived_at=? WHERE id=?", (stamp, mid)
    )
    store._conn.commit()


def _log_prune(store, mid, rule="stale_project_status", outcome="auto_archived",
               profile="default", created_at=None):
    store._conn.execute(
        """INSERT INTO pruner_log
           (id, profile, rule_name, action, candidate_id, superseded_by,
            outcome, tokens_freed, triggered_by, created_at, content)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (f"pl_test_{mid[-6:]}", profile, rule, "delete", mid, None,
         outcome, 10, "auto_pruner",
         created_at or datetime.now().isoformat(), "content at decision time"),
    )
    store._conn.commit()


# ---------------------------------------------------------------------------
# #192 — purge_archived_embeddings must respect a reversal window
# ---------------------------------------------------------------------------

def test_recently_archived_keeps_embedding(store):
    mid = store.add_memory("default", "recently archived fact")
    _add_embedding(store, mid)
    store.archive_memory("default", mid)

    deleted = store.purge_archived_embeddings(older_than_days=30)

    assert deleted == 0
    assert _has_embedding(store, mid), (
        "a recently-archived memory must keep its vector so a reversal is lossless"
    )


def test_old_archived_embedding_is_purged(store):
    mid = store.add_memory("default", "long archived fact")
    _add_embedding(store, mid)
    store.archive_memory("default", mid)
    _set_archived_at(store, mid, days_ago=40)

    deleted = store.purge_archived_embeddings(older_than_days=30)

    assert deleted == 1
    assert not _has_embedding(store, mid)


def test_live_memory_embedding_never_purged(store):
    mid = store.add_memory("default", "live fact")
    _add_embedding(store, mid)

    store.purge_archived_embeddings(older_than_days=30)

    assert _has_embedding(store, mid), "live memories must never lose their vector"


def test_purge_none_ignores_age(store):
    mid = store.add_memory("default", "recent but forcibly purged")
    _add_embedding(store, mid)
    store.archive_memory("default", mid)

    deleted = store.purge_archived_embeddings(older_than_days=None)

    assert deleted == 1
    assert not _has_embedding(store, mid)


# ---------------------------------------------------------------------------
# #194 — autonomous_reverse_prune
# ---------------------------------------------------------------------------

def test_reverse_unarchives_memory(store):
    mid = store.add_memory("default", "pruned but actually still relevant")
    store.archive_memory("default", mid, reason="pruned: auto")
    _log_prune(store, mid)

    result = autonomous_reverse_prune(store._conn, "default", mid, "turned out relevant")

    assert result["status"] == "reversed"
    assert result["rule_name"] == "stale_project_status"
    row = store._conn.execute(
        "SELECT archived, archived_at, archive_reason FROM memories WHERE id=?", (mid,)
    ).fetchone()
    assert row["archived"] == 0
    assert row["archived_at"] is None, "un-archive must not leave a half-archived row"
    assert row["archive_reason"] is None


def test_reverse_is_profile_scoped(store):
    """A reversal targeting the wrong profile must not touch the row."""
    mid = store.add_memory("default", "belongs to default profile")
    store.archive_memory("default", mid)
    _log_prune(store, mid)

    result = autonomous_reverse_prune(store._conn, "some-other-profile", mid, "wrong profile")

    assert "error" in result
    archived = store._conn.execute(
        "SELECT archived FROM memories WHERE id=?", (mid,)
    ).fetchone()["archived"]
    assert archived == 1, "reversal must not mutate another profile's memory"


def test_reverse_rejects_unarchived_memory(store):
    mid = store.add_memory("default", "still live")
    result = autonomous_reverse_prune(store._conn, "default", mid, "not archived")
    assert "error" in result


def test_reverse_logs_auto_reversed(store):
    mid = store.add_memory("default", "pruned then restored in test")
    store.archive_memory("default", mid)
    _log_prune(store, mid)

    autonomous_reverse_prune(store._conn, "default", mid, "reversal under test")

    logged = store._conn.execute(
        "SELECT outcome, triggered_by FROM pruner_log WHERE candidate_id=? AND outcome='auto_reversed'",
        (mid,),
    ).fetchone()
    assert logged is not None, "the reversal must be logged for the weekly audit"
    assert logged["triggered_by"] == "autonomous_reversal"


def test_second_reversal_penalizes_rule(store):
    """>1 reversal in 30 days drops the rule below the auto-execute bar."""
    store._conn.execute(
        "UPDATE pruner_rules SET confidence=0.9 WHERE rule_name='stale_project_status'"
    )
    store._conn.commit()

    for i in (1, 2):
        mid = store.add_memory("default", f"pruned memory number {i}")
        store.archive_memory("default", mid)
        _log_prune(store, mid)
        autonomous_reverse_prune(store._conn, "default", mid, f"reversal {i}")

    conf = store._conn.execute(
        "SELECT confidence FROM pruner_rules WHERE rule_name='stale_project_status'"
    ).fetchone()["confidence"]
    assert conf < 0.85, "two reversals should hold the rule below auto-execute"
    assert conf == pytest.approx(0.75)


def test_reverse_matches_pre_195_outcome_spelling(store):
    """Old 'auto_deleted' rows must still be reversible, not just 'auto_archived'."""
    mid = store.add_memory("default", "pruned under the old hard-delete code path")
    store.archive_memory("default", mid)
    _log_prune(store, mid, outcome="auto_deleted")

    result = autonomous_reverse_prune(store._conn, "default", mid, "legacy spelling")

    assert result["status"] == "reversed"
