"""Concurrency tests for the shared-DB story (#96).

The multi-writer design (Claude Desktop + Claude Code + HTTP bridge + watcher +
Streamlit, all against one SQLite file) rests on three mechanisms:

  * ``check_same_thread=False`` + ``_LockedConnection`` — serializes every
    statement issued through a *single* connection across threads.
  * WAL journal mode — lets a *second* connection read while the first writes.
  * ``busy_timeout`` — a second writer waits (up to N ms) for the lock instead
    of immediately raising ``sqlite3.OperationalError("database is locked")``.

These tests exercise both the single-connection (threads) and the
multi-connection (separate MemoryStore instances on the same file) paths and
assert no "database is locked" errors and consistent final counts.
"""
import sqlite3
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import pytest

from db.store import MemoryStore


def _stub_embeddings(store):
    """Replace the embedder with a cheap deterministic stub so embed-on-write
    doesn't try to load/download a model during the test."""
    store._embed_texts = lambda texts: [[0.0, 1.0, 0.0, 0.0] for _ in texts]


def _count_memories(db_path):
    con = sqlite3.connect(str(db_path))
    con.execute("PRAGMA busy_timeout=5000")
    try:
        return con.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    finally:
        con.close()


@pytest.fixture(autouse=True)
def _no_backfill(monkeypatch):
    # Skip the startup embedding-backfill thread (it would try to load a model).
    monkeypatch.setenv("MEMORYBRIDGE_NO_EMBED", "1")


def test_many_threads_one_connection_no_lock_errors(tmp_path):
    """N threads sharing ONE store do interleaved add/search/delete. The
    _LockedConnection must serialize them: no 'database is locked', and every
    successfully-added memory is accounted for."""
    store = MemoryStore(tmp_path / "shared.db")
    _stub_embeddings(store)
    store.ensure_profile("default")

    n_threads, per_thread = 8, 25
    errors: list[BaseException] = []
    added_ids: list[str] = []
    lock = threading.Lock()

    def worker(tid: int):
        try:
            for i in range(per_thread):
                mid = store.add_memory(
                    "default", f"thread {tid} memory {i} about topic {i}",
                    category="general", importance="low")
                if mid:
                    with lock:
                        added_ids.append(mid)
                # interleave reads and an occasional delete
                store.search("default", "topic", limit=3)
                if i % 10 == 0 and added_ids:
                    with lock:
                        victim = added_ids.pop() if added_ids else None
                    if victim:
                        store.delete_memory("default", victim)
        except BaseException as e:  # noqa: BLE001 — capture for the assert
            with lock:
                errors.append(e)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert not any(t.is_alive() for t in threads), "a worker thread hung"
    assert not errors, f"concurrent workers raised: {errors!r}"

    # Final row count must equal the ids we believe are still present.
    store._drain_pending_embeds() if hasattr(store, "_drain_pending_embeds") else None
    final = _count_memories(tmp_path / "shared.db")
    assert final == len(added_ids), f"expected {len(added_ids)} rows, DB has {final}"


def test_two_connections_same_file_no_lock_errors(tmp_path):
    """Two MemoryStore instances (separate connections) on the SAME file write
    concurrently. WAL + busy_timeout must let both proceed without raising
    'database is locked', and the union of writes must be consistent."""
    db = tmp_path / "shared2.db"
    store_a = MemoryStore(db)
    store_b = MemoryStore(db)
    _stub_embeddings(store_a)
    _stub_embeddings(store_b)
    store_a.ensure_profile("default")

    per_store = 40
    errors: list[BaseException] = []
    lock = threading.Lock()

    def writer(store, tag):
        try:
            for i in range(per_store):
                store.add_memory("default", f"{tag} unique memory {i}",
                                 category="general", importance="low")
        except BaseException as e:  # noqa: BLE001
            with lock:
                errors.append(e)

    ta = threading.Thread(target=writer, args=(store_a, "A"))
    tb = threading.Thread(target=writer, args=(store_b, "B"))
    ta.start(); tb.start()
    ta.join(timeout=60); tb.join(timeout=60)

    assert not errors, f"concurrent connections raised: {errors!r}"
    # All 80 rows have distinct content -> none dup-skipped -> exactly 80.
    final = _count_memories(db)
    assert final == per_store * 2, f"expected {per_store * 2} rows, DB has {final}"
