"""
TDD tests for db/pruner.py

Tests cover:
  - Verbatim subset detection
  - Stale project_status detection
  - Confidence auto-calibration (approval nudges up, rejection nudges down)
  - Auto-execute vs queue routing based on confidence threshold
  - Human feedback loop via record_outcome
  - NEVER_AUTO_DELETE_IMPORTANCE guard (critical entries never auto-deleted)
"""

import sqlite3
import pytest
from datetime import datetime, timedelta
from pathlib import Path

from db.pruner import (
    PRUNER_SCHEMA,
    AUTO_EXECUTE_THRESHOLD,
    INITIAL_CONFIDENCE,
    CONFIDENCE_NUDGE,
    CONFIDENCE_NUDGE_DOWN,
    NEVER_AUTO_DELETE_IMPORTANCE,
    bootstrap_rules,
    find_subset_candidates,
    find_stale_project_status,
    run_auto_prune,
    record_outcome,
    recalibrate_thresholds,
    get_pruner_report,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

MEMORIES_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,
    profile TEXT NOT NULL,
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL UNIQUE,
    category TEXT NOT NULL DEFAULT 'fact',
    importance TEXT NOT NULL DEFAULT 'medium',
    created_at TEXT NOT NULL,
    last_accessed TEXT,
    tags TEXT NOT NULL DEFAULT '[]',
    project_id TEXT,
    token_count INTEGER NOT NULL DEFAULT 20,
    archived INTEGER NOT NULL DEFAULT 0,
    relevance_score REAL NOT NULL DEFAULT 1.0
);
"""


@pytest.fixture
def conn():
    """In-memory SQLite with both memories and pruner tables."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(MEMORIES_SCHEMA)
    c.executescript(PRUNER_SCHEMA)
    bootstrap_rules(c)
    c.commit()
    return c


def _add(conn, mid, content, profile="default", category="fact",
         importance="medium", project_id=None, days_ago=0):
    created = (datetime.now() - timedelta(days=days_ago)).strftime("%Y-%m-%d")
    import hashlib
    h = hashlib.sha256(content.strip().lower().encode()).hexdigest() + mid  # unique per id
    conn.execute(
        """INSERT INTO memories
           (id,profile,content,content_hash,category,importance,
            created_at,last_accessed,project_id,token_count)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (mid, profile, content, h, category, importance,
         created, created, project_id, len(content.split()) + 20)
    )
    conn.commit()


def make_delete_fn(deleted_tracker: dict):
    def delete_fn(profile, memory_id):
        row = None  # token_count not critical for tests
        deleted_tracker[memory_id] = True
        return 30  # fake tokens freed
    return delete_fn


# ---------------------------------------------------------------------------
# Verbatim subset detection
# ---------------------------------------------------------------------------

class TestSubsetDetection:

    def test_detects_clear_subset(self, conn):
        _add(conn, "mem_a", "Cale targets Director PMO roles")
        _add(conn, "mem_b",
             "Cale targets Director PMO roles, remote/hybrid, $180K+, OKC metro")
        candidates = find_subset_candidates(conn, "default")
        ids = [c["candidate_id"] for c in candidates]
        assert "mem_a" in ids
        assert "mem_b" not in ids

    def test_no_false_positive_on_equal_content(self, conn):
        _add(conn, "mem_a", "Cale targets Director PMO roles")
        _add(conn, "mem_b", "Cale targets Director PMO roles")
        # Equal length — neither is a strict subset
        candidates = find_subset_candidates(conn, "default")
        assert candidates == []

    def test_skips_critical_importance(self, conn):
        _add(conn, "mem_a", "Cale targets Director PMO roles", importance="critical")
        _add(conn, "mem_b",
             "Cale targets Director PMO roles, remote/hybrid, $180K+", importance="high")
        candidates = find_subset_candidates(conn, "default")
        ids = [c["candidate_id"] for c in candidates]
        assert "mem_a" not in ids  # critical — never flag

    def test_scoped_to_profile(self, conn):
        _add(conn, "mem_a", "short fact", profile="default")
        _add(conn, "mem_b", "short fact plus more context", profile="default")
        _add(conn, "mem_c", "short fact", profile="other")
        _add(conn, "mem_d", "short fact plus more context", profile="other")
        default_candidates = find_subset_candidates(conn, "default")
        other_candidates = find_subset_candidates(conn, "other")
        default_ids = {c["candidate_id"] for c in default_candidates}
        other_ids = {c["candidate_id"] for c in other_candidates}
        assert "mem_c" not in default_ids
        assert "mem_a" not in other_ids


# ---------------------------------------------------------------------------
# Stale project_status detection
# ---------------------------------------------------------------------------

class TestStaleProjectStatus:

    def test_detects_stale_entry(self, conn):
        _add(conn, "mem_old", "CAR site pre-launch",
             category="project_status", project_id="car", days_ago=40)
        _add(conn, "mem_new", "CAR site live",
             category="project_status", project_id="car", days_ago=2)
        candidates = find_stale_project_status(conn, "default")
        ids = [c["candidate_id"] for c in candidates]
        assert "mem_old" in ids
        assert "mem_new" not in ids

    def test_no_flag_if_no_newer_entry(self, conn):
        _add(conn, "mem_old", "CAR site pre-launch",
             category="project_status", project_id="car", days_ago=40)
        candidates = find_stale_project_status(conn, "default")
        assert candidates == []

    def test_no_flag_if_not_old_enough(self, conn):
        _add(conn, "mem_old", "CAR site pre-launch",
             category="project_status", project_id="car", days_ago=10)
        _add(conn, "mem_new", "CAR site live",
             category="project_status", project_id="car", days_ago=1)
        candidates = find_stale_project_status(conn, "default")
        assert candidates == []

    def test_skips_critical(self, conn):
        _add(conn, "mem_old", "CAR site pre-launch",
             category="project_status", project_id="car",
             importance="critical", days_ago=40)
        _add(conn, "mem_new", "CAR site live",
             category="project_status", project_id="car", days_ago=2)
        candidates = find_stale_project_status(conn, "default")
        assert candidates == []


# ---------------------------------------------------------------------------
# Auto-execute vs queue routing
# ---------------------------------------------------------------------------

class TestAutoExecuteRouting:

    def test_queues_when_below_threshold(self, conn):
        # Initial confidence is 0.70 < 0.85 threshold — should queue
        _add(conn, "mem_a", "short")
        _add(conn, "mem_b", "short plus more")
        deleted = {}
        result = run_auto_prune(conn, "default", make_delete_fn(deleted))
        assert len(result["queued"]) >= 1
        assert len(result["auto_executed"]) == 0
        assert "mem_a" not in deleted

    def test_auto_executes_when_above_threshold(self, conn):
        # Force confidence above threshold
        conn.execute(
            "UPDATE pruner_rules SET confidence=0.90 WHERE rule_name='verbatim_subset'"
        )
        conn.commit()
        _add(conn, "mem_a", "short fact here")
        _add(conn, "mem_b", "short fact here extended with more content")
        deleted = {}
        result = run_auto_prune(conn, "default", make_delete_fn(deleted))
        assert len(result["auto_executed"]) >= 1
        assert "mem_a" in deleted

    def test_no_double_queue(self, conn):
        _add(conn, "mem_a", "short")
        _add(conn, "mem_b", "short plus more")
        deleted = {}
        run_auto_prune(conn, "default", make_delete_fn(deleted))
        run_auto_prune(conn, "default", make_delete_fn(deleted))
        count = conn.execute(
            "SELECT COUNT(*) FROM prune_queue WHERE candidate_id='mem_a' AND resolved=0"
        ).fetchone()[0]
        assert count <= 1


# ---------------------------------------------------------------------------
# Confidence recalibration
# ---------------------------------------------------------------------------

class TestRecalibration:

    def _inject_outcomes(self, conn, rule, approvals, rejections):
        import uuid
        now = datetime.now().isoformat()
        for _ in range(approvals):
            lid = f"pl_{uuid.uuid4().hex[:8]}"
            conn.execute(
                """INSERT INTO pruner_log
                   (id,profile,rule_name,action,candidate_id,outcome,tokens_freed,triggered_by,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (lid, "default", rule, "delete", "mem_x", "user_approved", 30, "human_review", now)
            )
        for _ in range(rejections):
            lid = f"pl_{uuid.uuid4().hex[:8]}"
            conn.execute(
                """INSERT INTO pruner_log
                   (id,profile,rule_name,action,candidate_id,outcome,tokens_freed,triggered_by,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (lid, "default", rule, "delete", "mem_y", "user_rejected", 0, "human_review", now)
            )
        conn.commit()

    def test_approval_increases_confidence(self, conn):
        self._inject_outcomes(conn, "verbatim_subset", approvals=5, rejections=0)
        changes = recalibrate_thresholds(conn)
        rule_change = next((c for c in changes if c["rule"] == "verbatim_subset"), None)
        assert rule_change is not None
        assert rule_change["new_confidence"] > rule_change["old_confidence"]

    def test_rejection_decreases_confidence(self, conn):
        self._inject_outcomes(conn, "verbatim_subset", approvals=0, rejections=5)
        changes = recalibrate_thresholds(conn)
        rule_change = next((c for c in changes if c["rule"] == "verbatim_subset"), None)
        assert rule_change is not None
        assert rule_change["new_confidence"] < rule_change["old_confidence"]

    def test_confidence_clamped_at_max(self, conn):
        conn.execute(
            "UPDATE pruner_rules SET confidence=0.98 WHERE rule_name='verbatim_subset'"
        )
        conn.commit()
        self._inject_outcomes(conn, "verbatim_subset", approvals=10, rejections=0)
        recalibrate_thresholds(conn)
        row = conn.execute(
            "SELECT confidence FROM pruner_rules WHERE rule_name='verbatim_subset'"
        ).fetchone()
        assert row["confidence"] <= 0.99

    def test_confidence_clamped_at_min(self, conn):
        conn.execute(
            "UPDATE pruner_rules SET confidence=0.22 WHERE rule_name='verbatim_subset'"
        )
        conn.commit()
        self._inject_outcomes(conn, "verbatim_subset", approvals=0, rejections=10)
        recalibrate_thresholds(conn)
        row = conn.execute(
            "SELECT confidence FROM pruner_rules WHERE rule_name='verbatim_subset'"
        ).fetchone()
        assert row["confidence"] >= 0.20


# ---------------------------------------------------------------------------
# Human feedback loop
# ---------------------------------------------------------------------------

class TestHumanFeedback:

    def _queue_item(self, conn, queue_id, candidate_id, rule="verbatim_subset"):
        now = datetime.now().isoformat()
        conn.execute(
            """INSERT INTO prune_queue
               (id,profile,rule_name,candidate_id,reason,confidence,suggested_action,created_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (queue_id, "default", rule, candidate_id,
             "Test reason", INITIAL_CONFIDENCE, "delete", now)
        )
        conn.commit()

    def test_approval_deletes_memory(self, conn):
        _add(conn, "mem_target", "short fact")
        self._queue_item(conn, "pq_test1", "mem_target")
        deleted = {}
        result = record_outcome(conn, "pq_test1", approved=True, delete_fn=make_delete_fn(deleted))
        assert result["status"] == "user_approved"
        assert "mem_target" in deleted

    def test_rejection_keeps_memory(self, conn):
        _add(conn, "mem_target", "short fact")
        self._queue_item(conn, "pq_test2", "mem_target")
        deleted = {}
        result = record_outcome(conn, "pq_test2", approved=False, delete_fn=make_delete_fn(deleted))
        assert result["status"] == "user_rejected"
        assert "mem_target" not in deleted

    def test_double_resolve_errors(self, conn):
        _add(conn, "mem_target", "short fact")
        self._queue_item(conn, "pq_test3", "mem_target")
        deleted = {}
        record_outcome(conn, "pq_test3", approved=True, delete_fn=make_delete_fn(deleted))
        result2 = record_outcome(conn, "pq_test3", approved=True, delete_fn=make_delete_fn(deleted))
        assert "error" in result2


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------

class TestPrunerReport:

    def test_report_structure(self, conn):
        report = get_pruner_report(conn, since_days=7)
        assert "auto_deleted_count" in report
        assert "pending_queue" in report
        assert "rule_states" in report
        assert "tokens_freed_this_period" in report
        assert "recalibration_changes" in report

    def test_report_counts_auto_deleted(self, conn):
        conn.execute(
            "UPDATE pruner_rules SET confidence=0.90 WHERE rule_name='verbatim_subset'"
        )
        conn.commit()
        _add(conn, "mem_a", "short content here")
        _add(conn, "mem_b", "short content here with more details")
        deleted = {}
        run_auto_prune(conn, "default", make_delete_fn(deleted))
        report = get_pruner_report(conn, since_days=7)
        assert report["auto_deleted_count"] >= 1
