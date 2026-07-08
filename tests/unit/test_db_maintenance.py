"""Regression tests for the db maintenance / correctness batch.

Covers:
  #54  search_semantic scored via a single vectorized matmul (== reference cosine),
       stale-dimension vectors skipped (#52 preserved).
  #75  log-table retention (access_log/analytics_events/pruner_log/prune_queue)
       with MEMORYBRIDGE_LOG_RETENTION_DAYS, and =0 disables trimming.
  #92  migrate() runs in one transaction: rolls back on mid-loop failure, skips
       malformed rows, is idempotent, and only renames the JSON after success.
  #125 the per-candidate / model indexes exist after construction.
"""
import json
import math
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest

os.environ.setdefault("MEMORYBRIDGE_NO_EMBED", "1")

from db.store import MemoryStore  # noqa: E402
import db.migrate as migrate_mod  # noqa: E402


def _cos(a, b):
    d = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return d / (na * nb) if na and nb else 0.0


# --------------------------------------------------------------------------- #54
def test_search_semantic_matches_reference_cosine(tmp_path):
    st = MemoryStore(tmp_path / "m.db")
    st.ensure_profile("default")
    vec = {
        "alpha": [1.0, 0.0, 0.0, 0.0],
        "beta": [0.9, 0.1, 0.0, 0.0],
        "gamma": [0.0, 1.0, 0.0, 0.0],
        "delta": [0.2, 0.2, 0.9, 0.0],
        "stale": [1.0, 0.0],  # wrong dim -> must be skipped (#52)
    }
    st._embed_texts = lambda texts: [vec[t] for t in texts]
    for name in vec:
        mid = st.add_memory("default", content=name,
                            category="general", importance="medium")
        st._conn.execute(
            "INSERT OR REPLACE INTO memory_embeddings (id,profile,vector) "
            "VALUES (?,?,?)", (mid, "default", json.dumps(vec[name])))
    st._conn.commit()

    q = [1.0, 0.05, 0.0, 0.0]
    st._embed_texts = lambda texts: [q]
    res = st.search_semantic("default", "irrelevant", limit=5, max_tokens=10 ** 6)
    got = [r["content"] for r in res]

    ref = sorted((n for n in vec if len(vec[n]) == 4),
                 key=lambda n: _cos(q, vec[n]), reverse=True)
    assert got == ref
    assert "stale" not in got
    for r in res:
        assert abs(r["match_score"] - _cos(q, vec[r["content"]])) < 1e-5


# --------------------------------------------------------------------------- #75/#125
def test_maintenance_trims_logs_and_creates_indexes(tmp_path, monkeypatch):
    st = MemoryStore(tmp_path / "m.db")
    conn = st._conn
    old = (datetime.now() - timedelta(days=200)).isoformat()
    new = (datetime.now() - timedelta(days=5)).isoformat()
    conn.execute("INSERT INTO access_log (ts,action,profile) VALUES (?,?,?)", (old, "get", "default"))
    conn.execute("INSERT INTO access_log (ts,action,profile) VALUES (?,?,?)", (new, "get", "default"))
    conn.execute("INSERT INTO analytics_events (id,session_date,created_at) VALUES ('e1',?,?)", (old, old))
    conn.execute("INSERT INTO analytics_events (id,session_date,created_at) VALUES ('e2',?,?)", (new, new))
    conn.execute("INSERT INTO pruner_log (id,profile,rule_name,action,candidate_id,outcome,triggered_by,created_at) "
                 "VALUES ('l1','default','r','archive','c1','ok','auto',?)", (old,))
    conn.execute("INSERT INTO pruner_log (id,profile,rule_name,action,candidate_id,outcome,triggered_by,created_at) "
                 "VALUES ('l2','default','r','archive','c2','ok','auto',?)", (new,))
    conn.execute("INSERT INTO prune_queue (id,profile,rule_name,candidate_id,reason,confidence,suggested_action,created_at,resolved,resolved_at) "
                 "VALUES ('q1','default','r','c1','x',0.9,'archive',?,1,?)", (old, old))
    conn.execute("INSERT INTO prune_queue (id,profile,rule_name,candidate_id,reason,confidence,suggested_action,created_at,resolved,resolved_at) "
                 "VALUES ('q2','default','r','c2','x',0.9,'archive',?,0,NULL)", (new,))
    conn.commit()

    st._ensure_maintenance()

    assert conn.execute("SELECT count(*) FROM access_log").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM analytics_events").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM pruner_log").fetchone()[0] == 1
    # q1 (resolved + old) trimmed; q2 (unresolved) kept regardless of age
    assert conn.execute("SELECT count(*) FROM prune_queue").fetchone()[0] == 1

    idx = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    for want in ("idx_access_profile", "idx_access_ts", "idx_analytics_model",
                 "idx_prune_queue_candidate", "idx_pruner_log_candidate"):
        assert want in idx


def test_retention_zero_keeps_everything(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORYBRIDGE_LOG_RETENTION_DAYS", "0")
    st = MemoryStore(tmp_path / "m.db")
    old = (datetime.now() - timedelta(days=500)).isoformat()
    st._conn.execute("INSERT INTO access_log (ts,action,profile) VALUES (?,?,?)", (old, "get", "d"))
    st._conn.commit()
    st._ensure_maintenance()
    assert st._conn.execute("SELECT count(*) FROM access_log").fetchone()[0] == 1


# --------------------------------------------------------------------------- #92
def _run_migrate(tmp_path, profiles):
    j = tmp_path / "memory.json"
    db = tmp_path / "memory.db"
    j.write_text(json.dumps({"profiles": profiles}))
    migrate_mod.MEMORY_JSON = j
    migrate_mod.MEMORY_DB = db
    return j, db


def test_migrate_normal_and_defensive_skip(tmp_path):
    j, db = _run_migrate(tmp_path, {"default": {
        "identity": {}, "projects": [], "memories": [
            {"id": "a", "content": "hello world", "category": "general", "importance": "high"},
            {"id": "b", "content": "second one", "importance": "low"},   # category defaulted
            {"content": "no id"},                                        # skipped
            {"id": "d"},                                                 # no content -> skipped
        ], "archived": [
            {"id": "z", "content": "archived one", "category": "general", "importance": "low"},
        ]}})
    migrate_mod.migrate()
    con = sqlite3.connect(str(db))
    active = con.execute("SELECT id FROM memories WHERE archived=0 ORDER BY id").fetchall()
    arch = con.execute("SELECT id FROM memories WHERE archived=1").fetchall()
    fts = con.execute("SELECT count(*) FROM memories_fts").fetchone()[0]
    con.close()
    assert active == [("a",), ("b",)]
    assert arch == [("z",)]
    assert fts == 3
    assert (tmp_path / "memory.json.pre-sqlite-backup").exists()
    assert not j.exists()


def test_migrate_rolls_back_and_keeps_json_on_failure(tmp_path, monkeypatch):
    j, db = _run_migrate(tmp_path, {"default": {
        "identity": {}, "projects": [],
        "memories": [{"id": "ok", "content": "fine", "category": "general", "importance": "high"}]}})

    def boom(conn, data):
        conn.execute(
            "INSERT INTO memories (id,profile,content,content_hash,category,importance,"
            "created_at,last_accessed,token_count) "
            "VALUES ('x','default','c','h','general','high','2026','2026',5)")
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(migrate_mod, "_migrate_rows", boom)
    with pytest.raises(RuntimeError):
        migrate_mod.migrate()

    con = sqlite3.connect(str(db))
    n = con.execute("SELECT count(*) FROM memories").fetchone()[0]
    con.close()
    assert n == 0                       # rolled back, no half-populated DB
    assert j.exists()                   # JSON not renamed on failure
    assert not (tmp_path / "memory.json.pre-sqlite-backup").exists()


def test_migrate_is_idempotent_dedup_by_hash(tmp_path):
    j, db = _run_migrate(tmp_path, {"default": {
        "memories": [{"id": "a", "content": "same", "category": "general", "importance": "high"}]}})
    migrate_mod.migrate()
    j.write_text(json.dumps({"profiles": {"default": {"memories": [
        {"id": "a2", "content": "same", "category": "general", "importance": "high"},       # dup hash
        {"id": "a3", "content": "different", "category": "general", "importance": "low"},
    ]}}}))
    migrate_mod.migrate()
    con = sqlite3.connect(str(db))
    n = con.execute("SELECT count(*) FROM memories").fetchone()[0]
    con.close()
    assert n == 2
