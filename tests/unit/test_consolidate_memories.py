import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import config
from db.store import MemoryStore
import server


def test_consolidate_memories_basic(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MEMORYBRIDGE_DATA", str(tmp_path))
    config.reset_cache()

    db_path = tmp_path / "test_consolidate.db"
    st = MemoryStore(db_path)
    st.ensure_profile("default")

    # Add 3 episodic micro-memories about Postgres deployment
    m1 = st.add_memory("default", "Postgres primary db deployed on AWS RDS us-east-1",
                       category="fact", tags=["entity:postgres", "infra"])
    m2 = st.add_memory("default", "Postgres connection pooling configured with PgBouncer max 100 conns",
                       category="fact", tags=["entity:postgres", "pooling"])
    m3 = st.add_memory("default", "Postgres read replicas configured for reporting read-only traffic",
                       category="fact", tags=["entity:postgres", "replica"])

    assert m1 and m2 and m3

    # Consolidate memories for entity "postgres"
    res = st.consolidate_memories("default", "postgres", min_memories=2)
    assert res["status"] == "consolidated"
    assert res["archived_count"] == 3
    summary_id = res["summary_id"]
    assert summary_id is not None
    assert summary_id not in [m1, m2, m3]

    # Verify that constituent memories were archived with supersedes status
    archived_rows = st._conn.execute(
        "SELECT id, archived, superseded_by, archive_reason FROM memories WHERE id IN (?, ?, ?)",
        (m1, m2, m3)
    ).fetchall()
    assert len(archived_rows) == 3
    for r in archived_rows:
        assert r["archived"] == 1
        assert r["superseded_by"] == summary_id
        assert f"superseded by {summary_id}" in r["archive_reason"]

    # Verify summary memory is active
    summary_row = st._conn.execute(
        "SELECT id, archived, content, tags FROM memories WHERE id=?",
        (summary_id,)
    ).fetchone()
    assert summary_row["archived"] == 0
    assert "Consolidated summary" in summary_row["content"]
    assert "entity:postgres" in json.loads(summary_row["tags"])

    # Verify supersedes graph edges
    edges = st.get_edges(summary_id, include_archived=True)
    supersede_edges = [e for e in edges if e["relation"] == "supersedes"]
    assert len(supersede_edges) == 3
    target_ids = {e["target_id"] for e in supersede_edges}
    assert target_ids == {m1, m2, m3}

    # Verify normal queries do not return the archived micro-memories
    active_mems = st.get_memories("default")
    active_ids = {m["id"] for m in active_mems}
    assert summary_id in active_ids
    assert m1 not in active_ids
    assert m2 not in active_ids
    assert m3 not in active_ids


def test_consolidate_memories_custom_summary_and_llm(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MEMORYBRIDGE_DATA", str(tmp_path))
    config.reset_cache()

    db_path = tmp_path / "test_custom_llm.db"
    mock_llm = MagicMock(return_value="Unified architecture: Auth is managed via ECDSA JWT tokens.")
    st = MemoryStore(db_path, llm_synthesize=mock_llm)
    st.ensure_profile("default")

    m1 = st.add_memory("default", "Auth service uses JWT tokens signed by Ed25519", category="fact", tags=["entity:auth"])
    m2 = st.add_memory("default", "Tokens expire after 15 minutes with refresh rotation", category="fact", tags=["entity:auth"])

    # Test LLM synthesis
    res = st.consolidate_memories("default", "auth", min_memories=2)
    assert res["status"] == "consolidated"
    assert res["summary"] == "Unified architecture: Auth is managed via ECDSA JWT tokens."
    assert mock_llm.called

    # Test manual summary override on another entity
    m3 = st.add_memory("default", "Payment uses Stripe Elements", category="fact", tags=["entity:billing"])
    m4 = st.add_memory("default", "Stripe webhook verifies signature with secret", category="fact", tags=["entity:billing"])

    manual_summary = "Billing is implemented via Stripe webhook signatures and Elements frontend."
    res2 = st.consolidate_memories("default", "billing", summary=manual_summary, min_memories=2)
    assert res2["status"] == "consolidated"
    assert res2["summary"] == manual_summary


def test_consolidate_memories_insufficient_candidates_no_op(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MEMORYBRIDGE_DATA", str(tmp_path))
    config.reset_cache()

    db_path = tmp_path / "test_noop.db"
    st = MemoryStore(db_path)
    st.ensure_profile("default")

    st.add_memory("default", "Only one memory here about Kubernetes", category="fact", tags=["entity:k8s"])

    # Requires min_memories=2
    res = st.consolidate_memories("default", "k8s", min_memories=2)
    assert res["status"] == "no_op"
    assert "Only found 1 active memories" in res["reason"]


def test_server_consolidate_memories_tool(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MEMORYBRIDGE_DATA", str(tmp_path))
    config.reset_cache()

    db_path = tmp_path / "memory.db"
    st = MemoryStore(db_path)
    st.ensure_profile("default")
    st.add_memory("default", "First note on Apollo telemetry engine", category="fact", tags=["entity:apollo"])
    st.add_memory("default", "Second note on Apollo metric aggregation", category="fact", tags=["entity:apollo"])

    monkeypatch.setattr(server, "_store", st)
    fn = getattr(server.consolidate_memories, "fn", server.consolidate_memories)

    res_raw = fn(topic_or_entity="apollo", profile="default", min_memories=2)
    res = json.loads(res_raw)
    assert res["status"] == "consolidated"
    assert res["archived_count"] == 2
    assert res["summary_id"] is not None


def test_cli_consolidate_command(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setenv("MEMORYBRIDGE_DATA", str(tmp_path))
    config.reset_cache()

    db_path = tmp_path / "memory.db"
    st = MemoryStore(db_path)
    st.ensure_profile("default")
    st.add_memory("default", "Redis cache cluster node 1", category="fact", tags=["entity:redis"])
    st.add_memory("default", "Redis cache cluster node 2", category="fact", tags=["entity:redis"])

    import cli
    rc = cli.main(["consolidate", "redis", "--min", "2"])
    assert rc == 0
    captured = capsys.readouterr()
    assert "Consolidated 2 memories into" in captured.out
    assert "Topic: redis" in captured.out
