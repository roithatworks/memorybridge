"""Unit tests for Relation and Triple Mining (v5.2 - Group 2)."""

import json
import pytest
from pathlib import Path
from unittest.mock import MagicMock

from db.relations import (
    VALID_RELATIONS,
    RelationExtractor,
    extract_relations_heuristic,
)
from db.store import MemoryStore


def test_heuristic_relations_depends_on():
    m1 = "api_gateway"
    c1 = "API gateway requires redis cache for rate limiting"
    m2 = "redis_service"
    c2 = "Redis cache instance configured on port 6379"

    rel = extract_relations_heuristic(m1, c1, m2, c2)
    assert rel is not None
    assert rel["relation"] == "depends_on"
    assert rel["source_id"] == m1
    assert rel["target_id"] == m2
    assert rel["confidence"] >= 0.7


def test_heuristic_relations_implements():
    m1 = "auth_handler"
    c1 = "Auth handler implements OAuth2 token validation flow"
    m2 = "oauth_spec"
    c2 = "OAuth2 token validation architecture specification"

    rel = extract_relations_heuristic(m1, c1, m2, c2)
    assert rel is not None
    assert rel["relation"] == "implements"
    assert rel["source_id"] == m1
    assert rel["target_id"] == m2


def test_heuristic_relations_contradicts():
    m1 = "db_choice_v2"
    c1 = "Postgres migration: no longer uses MongoDB for user profiles"
    m2 = "db_choice_v1"
    c2 = "MongoDB database used for user profiles storage"

    rel = extract_relations_heuristic(m1, c1, m2, c2)
    assert rel is not None
    assert rel["relation"] == "contradicts"


def test_heuristic_relations_part_of():
    m1 = "parser_mod"
    c1 = "JSON parser is a component of ingestion engine"
    m2 = "ingest_engine"
    c2 = "Ingestion engine processes all incoming document data"

    rel = extract_relations_heuristic(m1, c1, m2, c2)
    assert rel is not None
    assert rel["relation"] == "part_of"


def test_heuristic_relations_relates_to():
    m1 = "deploy_script"
    c1 = "Deploy script triggers Docker container build and push"
    m2 = "ci_pipeline"
    c2 = "CI pipeline runs Docker container tests before build"

    rel = extract_relations_heuristic(m1, c1, m2, c2)
    assert rel is not None
    assert rel["relation"] == "relates_to"


def test_heuristic_relations_self_filtering():
    assert extract_relations_heuristic("m1", "some content", "m1", "some content") is None


def test_relation_extractor_with_llm():
    fake_response = json.dumps([
        {
            "target_id": "cand_1",
            "relation": "depends_on",
            "confidence": 0.95,
            "reason": "Primary memory explicitly declares dependency on cand_1"
        }
    ])
    llm_mock = MagicMock(return_value=fake_response)
    extractor = RelationExtractor(llm_synthesize=llm_mock)

    candidates = [
        {"id": "cand_1", "content": "Database cluster connection string", "category": "fact"},
        {"id": "cand_2", "content": "Unrelated garden flowers", "category": "fact"},
    ]

    results = extractor.extract("source_1", "Backend app requires database cluster connection string", candidates)
    assert len(results) == 1
    assert results[0]["target_id"] == "cand_1"
    assert results[0]["relation"] == "depends_on"
    assert results[0]["confidence"] == 0.95
    assert "reason" in results[0]
    llm_mock.assert_called_once()
    prompt = llm_mock.call_args[0][0]
    assert "<<<UNTRUSTED_MEMORY_DATA>>>" in prompt


def test_relation_extractor_llm_failure_falls_back_to_heuristic():
    llm_mock = MagicMock(side_effect=RuntimeError("LLM API quota exceeded"))
    extractor = RelationExtractor(llm_synthesize=llm_mock)

    candidates = [
        {"id": "cand_1", "content": "PostgreSQL storage connection", "category": "fact"},
    ]
    results = extractor.extract(
        "source_1",
        "Backend server requires PostgreSQL storage connection pool",
        candidates
    )
    assert len(results) == 1
    assert results[0]["target_id"] == "cand_1"
    assert results[0]["relation"] == "depends_on"


def test_memory_store_mine_relations(tmp_path: Path):
    db_file = tmp_path / "test_relations.db"
    store = MemoryStore(db_file)
    store.ensure_profile("default")

    id1 = store.add_memory("default", "PostgreSQL database instance running on port 5432", category="fact")
    id2 = store.add_memory("default", "Worker service requires PostgreSQL database instance", category="fact")

    assert id1 and id2

    # Mine relations with explicit candidate
    mined = store.mine_relations(id2, candidate_ids=[id1], profile="default", auto_link=True)
    assert len(mined) >= 1
    assert mined[0]["source_id"] == id2
    assert mined[0]["target_id"] == id1
    assert mined[0]["relation"] == "depends_on"
    assert "edge_id" in mined[0]

    # Verify edge is stored in memory_edges
    edges = store.get_edges(id2)
    assert any(e["target_id"] == id1 and e["relation"] == "depends_on" for e in edges)

    # Calling mine_relations again is idempotent
    mined_again = store.mine_relations(id2, candidate_ids=[id1], profile="default", auto_link=True)
    assert len(mined_again) >= 1
    assert mined_again[0]["edge_id"] == mined[0]["edge_id"]


def test_consolidate_session_mines_relations(tmp_path: Path):
    import server

    db_file = tmp_path / "test_consolidate.db"
    store = MemoryStore(db_file)
    server._store = store

    notes = """
    - Database cluster initialized with Redis cache on localhost:6379
    - Web server requires Redis cache for session storage
    """

    res_raw = server.consolidate_session(notes, profile="default")
    res = json.loads(res_raw)
    assert res["status"] == "consolidated"
    assert res["added_count"] == 2
    assert res.get("edges_created", 0) >= 1
