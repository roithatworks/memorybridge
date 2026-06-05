"""
MemoryBridge Adaptive Auto-Pruner
==================================
Called after every add_memory call. Detects redundant/stale memories,
auto-executes high-confidence cases, queues uncertain ones for human review.
The expensive subset scan runs immediately for small profiles and on a cadence
for large profiles so add_memory does not degrade to O(n^2) work per write.

Learns over time: every decision (auto or human) is logged to pruner_log.
Rule confidence thresholds auto-adjust based on approval/rejection history.
The weekly health report surfaces threshold changes and net token savings.

Rules implemented:
  verbatim_subset     -- A's content is fully contained in B's content
  stale_project_status -- project_status entry >30 days old, same project has newer entry

Confidence lifecycle:
  - New rule starts at INITIAL_CONFIDENCE (0.70)
  - Crosses AUTO_EXECUTE_THRESHOLD (0.85) -> auto-executes instead of queuing
  - Each approval nudges +CONFIDENCE_NUDGE (0.03)
  - Each rejection nudges -CONFIDENCE_NUDGE_DOWN (0.05)  [asymmetric: penalize harder]
  - Clamped to [MIN_CONFIDENCE (0.20), MAX_CONFIDENCE (0.99)]
"""

import json
import uuid
from datetime import datetime, timedelta
from typing import Optional

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------
INITIAL_CONFIDENCE       = 0.70
AUTO_EXECUTE_THRESHOLD   = 0.85
CONFIDENCE_NUDGE         = 0.03
CONFIDENCE_NUDGE_DOWN    = 0.05
MIN_CONFIDENCE           = 0.20
MAX_CONFIDENCE           = 0.99
STALE_DAYS               = 30
NEVER_AUTO_DELETE_IMPORTANCE = {"critical"}  # always queue, never auto-execute
SUBSET_SWEEP_ALWAYS_BELOW_COUNT = 100
SUBSET_SWEEP_ADD_INTERVAL = 50
SUBSET_SWEEP_GROWTH_RATIO = 0.10

RULE_NAMES = ["verbatim_subset", "stale_project_status"]


# ---------------------------------------------------------------------------
# Schema bootstrap (called by MemoryStore.__init__ via store.py)
# ---------------------------------------------------------------------------

PRUNER_SCHEMA = """
CREATE TABLE IF NOT EXISTS pruner_rules (
    rule_name   TEXT PRIMARY KEY,
    confidence  REAL NOT NULL DEFAULT 0.70,
    auto_count  INTEGER NOT NULL DEFAULT 0,
    queue_count INTEGER NOT NULL DEFAULT 0,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS prune_queue (
    id          TEXT PRIMARY KEY,
    profile     TEXT NOT NULL,
    rule_name   TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    superseded_by TEXT,
    reason      TEXT NOT NULL,
    confidence  REAL NOT NULL,
    suggested_action TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    resolved    INTEGER NOT NULL DEFAULT 0,
    resolution  TEXT,
    resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS pruner_log (
    id          TEXT PRIMARY KEY,
    profile     TEXT NOT NULL,
    rule_name   TEXT NOT NULL,
    action      TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    superseded_by TEXT,
    outcome     TEXT NOT NULL,
    tokens_freed INTEGER NOT NULL DEFAULT 0,
    triggered_by TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pruner_sweeps (
    profile      TEXT NOT NULL,
    sweep_name   TEXT NOT NULL,
    last_memory_count INTEGER NOT NULL DEFAULT 0,
    adds_since_sweep INTEGER NOT NULL DEFAULT 0,
    last_run_at  TEXT NOT NULL,
    PRIMARY KEY(profile, sweep_name)
);
"""


def bootstrap_rules(conn) -> None:
    """Ensure pruner_rules rows exist for all known rules."""
    now = datetime.now().isoformat()
    for rule in RULE_NAMES:
        conn.execute(
            "INSERT OR IGNORE INTO pruner_rules(rule_name, confidence, updated_at) VALUES(?,?,?)",
            (rule, INITIAL_CONFIDENCE, now)
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Rule: verbatim subset
# ---------------------------------------------------------------------------

def _normalize(text: str) -> str:
    return " ".join(text.lower().split())


def find_subset_candidates(conn, profile: str) -> list[dict]:
    """
    Return pairs where candidate's content is fully contained in superseder's content.
    Only flags the SMALLER entry (the one that adds nothing new).
    Skips entries with importance in NEVER_AUTO_DELETE_IMPORTANCE.
    """
    rows = conn.execute(
        "SELECT id, content, importance, category, project_id, token_count "
        "FROM memories WHERE profile=? AND archived=0",
        (profile,)
    ).fetchall()

    memories = []
    for row in rows:
        mem = dict(row)
        mem["_norm_content"] = _normalize(mem["content"])
        mem["_norm_len"] = len(mem["_norm_content"])
        memories.append(mem)
    memories.sort(key=lambda m: m["_norm_len"])
    candidates = []

    for i, mem in enumerate(memories):
        if mem["importance"] in NEVER_AUTO_DELETE_IMPORTANCE:
            continue
        norm_content = mem["_norm_content"]
        if not norm_content:
            continue
        for other in memories[i + 1:]:
            if mem["_norm_len"] >= other["_norm_len"]:
                continue
            # mem is a subset of other (other contains everything mem says)
            if norm_content in other["_norm_content"]:
                candidates.append({
                    "rule_name": "verbatim_subset",
                    "candidate_id": mem["id"],
                    "superseded_by": other["id"],
                    "reason": (
                        f"Content of {mem['id']} is fully contained within {other['id']}. "
                        f"Candidate: {mem['content'][:80]}..."
                    ),
                    "tokens_freed": mem["token_count"],
                    "candidate_importance": mem["importance"],
                })
                break  # one superseder is enough

    # Deduplicate: a candidate_id might match multiple superseders, keep first hit
    seen = set()
    unique = []
    for c in candidates:
        if c["candidate_id"] not in seen:
            seen.add(c["candidate_id"])
            unique.append(c)
    return unique


# ---------------------------------------------------------------------------
# Sweep cadence helpers
# ---------------------------------------------------------------------------

def _active_memory_count(conn, profile: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS count FROM memories WHERE profile=? AND archived=0",
        (profile,)
    ).fetchone()
    return int(row["count"] if row else 0)


def _load_sweep_state(conn, profile: str, sweep_name: str):
    return conn.execute(
        """SELECT * FROM pruner_sweeps
           WHERE profile=? AND sweep_name=?""",
        (profile, sweep_name)
    ).fetchone()


def _mark_subset_sweep_skipped(conn, profile: str, memory_count: int, now: str) -> None:
    row = _load_sweep_state(conn, profile, "verbatim_subset")
    if row:
        conn.execute(
            """UPDATE pruner_sweeps
               SET adds_since_sweep=adds_since_sweep+1
               WHERE profile=? AND sweep_name='verbatim_subset'""",
            (profile,)
        )
    else:
        conn.execute(
            """INSERT INTO pruner_sweeps
               (profile,sweep_name,last_memory_count,adds_since_sweep,last_run_at)
               VALUES(?,?,?,?,?)""",
            (profile, "verbatim_subset", memory_count, 1, now)
        )
    conn.commit()


def _mark_subset_sweep_ran(conn, profile: str, memory_count: int, now: str) -> None:
    conn.execute(
        """INSERT INTO pruner_sweeps
           (profile,sweep_name,last_memory_count,adds_since_sweep,last_run_at)
           VALUES(?,?,?,?,?)
           ON CONFLICT(profile,sweep_name) DO UPDATE SET
             last_memory_count=excluded.last_memory_count,
             adds_since_sweep=0,
             last_run_at=excluded.last_run_at""",
        (profile, "verbatim_subset", memory_count, 0, now)
    )
    conn.commit()


def should_run_subset_sweep(conn, profile: str, memory_count: int, force: bool = False) -> tuple[bool, str]:
    """
    The substring subset rule is O(n^2), so large profiles run it on a cadence.
    Small profiles still run every time because the scan is cheap and keeps the
    original immediate-dedup behavior.
    """
    if force:
        return True, "forced"
    if memory_count < SUBSET_SWEEP_ALWAYS_BELOW_COUNT:
        return True, "small_profile"

    row = _load_sweep_state(conn, profile, "verbatim_subset")
    if not row:
        return True, "first_large_profile_sweep"

    adds_since = int(row["adds_since_sweep"]) + 1
    last_count = int(row["last_memory_count"])
    growth_threshold = max(1, int(last_count * SUBSET_SWEEP_GROWTH_RATIO))

    if adds_since >= SUBSET_SWEEP_ADD_INTERVAL:
        return True, "add_interval"
    if memory_count >= last_count + growth_threshold:
        return True, "memory_count_growth"
    if memory_count < last_count:
        return True, "memory_count_shrank"
    return False, "cadence_not_due"


# ---------------------------------------------------------------------------
# Rule: stale project_status
# ---------------------------------------------------------------------------

def find_stale_project_status(conn, profile: str) -> list[dict]:
    """
    Find project_status entries older than STALE_DAYS where a newer entry
    exists for the same project_id.
    """
    cutoff = (datetime.now() - timedelta(days=STALE_DAYS)).strftime("%Y-%m-%d")

    rows = conn.execute(
        """SELECT id, content, importance, project_id, created_at, token_count
           FROM memories
           WHERE profile=? AND archived=0 AND category='project_status'
             AND project_id IS NOT NULL AND created_at <= ?""",
        (profile, cutoff)
    ).fetchall()

    candidates = []
    for row in rows:
        if row["importance"] in NEVER_AUTO_DELETE_IMPORTANCE:
            continue
        # Check for a newer entry on same project
        newer = conn.execute(
            """SELECT id FROM memories
               WHERE profile=? AND archived=0 AND category='project_status'
                 AND project_id=? AND created_at > ? AND id != ?""",
            (profile, row["project_id"], row["created_at"], row["id"])
        ).fetchone()
        if newer:
            candidates.append({
                "rule_name": "stale_project_status",
                "candidate_id": row["id"],
                "superseded_by": newer["id"],
                "reason": (
                    f"project_status entry {row['id']} is {STALE_DAYS}+ days old "
                    f"and project '{row['project_id']}' has a newer entry ({newer['id']}). "
                    f"Content: {row['content'][:80]}..."
                ),
                "tokens_freed": row["token_count"],
                "candidate_importance": row["importance"],
            })
    return candidates


# ---------------------------------------------------------------------------
# Core runner
# ---------------------------------------------------------------------------

def run_auto_prune(conn, profile: str, delete_fn, force_subset_sweep: bool = False) -> dict:
    """
    Main entry point. Called after every add_memory.

    delete_fn: callable(profile, memory_id) -> int (tokens_freed)
                This is MemoryStore.delete_memory — passed in to avoid circular import.

    Returns summary dict with auto_executed and queued counts.
    """
    bootstrap_rules(conn)
    now = datetime.now().isoformat()
    memory_count = _active_memory_count(conn, profile)
    run_subset_sweep, subset_reason = should_run_subset_sweep(
        conn, profile, memory_count, force=force_subset_sweep
    )

    all_candidates = []
    if run_subset_sweep:
        all_candidates.extend(find_subset_candidates(conn, profile))
        _mark_subset_sweep_ran(conn, profile, memory_count, now)
    else:
        _mark_subset_sweep_skipped(conn, profile, memory_count, now)

    all_candidates.extend(find_stale_project_status(conn, profile))

    sweep_status = {
        "verbatim_subset": {
            "ran": run_subset_sweep,
            "reason": subset_reason,
            "memory_count": memory_count,
        }
    }

    if not all_candidates:
        return {"auto_executed": [], "queued": [], "sweeps": sweep_status}

    auto_executed = []
    queued = []

    for cand in all_candidates:
        rule_name = cand["rule_name"]
        rule_row = conn.execute(
            "SELECT confidence FROM pruner_rules WHERE rule_name=?", (rule_name,)
        ).fetchone()
        confidence = rule_row["confidence"] if rule_row else INITIAL_CONFIDENCE

        # Skip if already queued and unresolved
        already_queued = conn.execute(
            "SELECT id FROM prune_queue WHERE candidate_id=? AND resolved=0",
            (cand["candidate_id"],)
        ).fetchone()
        if already_queued:
            continue

        # Skip if already logged (already acted on)
        already_logged = conn.execute(
            "SELECT id FROM pruner_log WHERE candidate_id=? AND outcome IN ('auto_deleted','user_approved')",
            (cand["candidate_id"],)
        ).fetchone()
        if already_logged:
            continue

        if confidence >= AUTO_EXECUTE_THRESHOLD:
            # Auto-execute
            tokens_freed = delete_fn(profile, cand["candidate_id"])
            _log_decision(conn, profile, rule_name, "delete", cand,
                          "auto_deleted", tokens_freed, "auto_pruner", now)
            conn.execute(
                "UPDATE pruner_rules SET auto_count=auto_count+1, updated_at=? WHERE rule_name=?",
                (now, rule_name)
            )
            conn.commit()
            auto_executed.append({
                "memory_id": cand["candidate_id"],
                "rule": rule_name,
                "tokens_freed": tokens_freed,
                "reason": cand["reason"],
            })
        else:
            # Queue for human review
            qid = f"pq_{uuid.uuid4().hex[:8]}"
            conn.execute(
                """INSERT INTO prune_queue
                   (id,profile,rule_name,candidate_id,superseded_by,reason,
                    confidence,suggested_action,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (qid, profile, rule_name, cand["candidate_id"],
                 cand.get("superseded_by"), cand["reason"],
                 confidence, "delete", now)
            )
            _log_decision(conn, profile, rule_name, "delete", cand,
                          "queued", 0, "auto_pruner", now)
            conn.execute(
                "UPDATE pruner_rules SET queue_count=queue_count+1, updated_at=? WHERE rule_name=?",
                (now, rule_name)
            )
            conn.commit()
            queued.append({
                "queue_id": qid,
                "memory_id": cand["candidate_id"],
                "rule": rule_name,
                "confidence": round(confidence, 3),
                "reason": cand["reason"],
            })

    return {"auto_executed": auto_executed, "queued": queued, "sweeps": sweep_status}


# ---------------------------------------------------------------------------
# Human feedback — called by resolve_prune_queue MCP tool
# ---------------------------------------------------------------------------

def record_outcome(conn, queue_id: str, approved: bool, delete_fn) -> dict:
    """
    Record a human approve/reject on a queued item.
    Adjusts rule confidence. Executes delete if approved.
    """
    row = conn.execute(
        "SELECT * FROM prune_queue WHERE id=? AND resolved=0", (queue_id,)
    ).fetchone()
    if not row:
        return {"error": f"Queue item '{queue_id}' not found or already resolved"}

    now = datetime.now().isoformat()
    outcome = "user_approved" if approved else "user_rejected"
    tokens_freed = 0

    if approved:
        tokens_freed = delete_fn(row["profile"], row["candidate_id"])

    # Mark queue item resolved
    conn.execute(
        "UPDATE prune_queue SET resolved=1, resolution=?, resolved_at=? WHERE id=?",
        (outcome, now, queue_id)
    )

    # Log it
    _log_decision(
        conn, row["profile"], row["rule_name"], "delete",
        {"candidate_id": row["candidate_id"], "superseded_by": row["superseded_by"],
         "tokens_freed": tokens_freed},
        outcome, tokens_freed, "human_review", now
    )

    # Recalibrate confidence
    recalibrate_thresholds(conn)
    conn.commit()

    return {
        "status": outcome,
        "queue_id": queue_id,
        "memory_id": row["candidate_id"],
        "tokens_freed": tokens_freed,
    }


# ---------------------------------------------------------------------------
# Auto-calibration
# ---------------------------------------------------------------------------

def recalibrate_thresholds(conn) -> list[dict]:
    """
    For each rule, look at the last 20 human decisions.
    Adjust confidence up/down based on approval rate.
    Returns list of changes made (for the summary report).
    """
    changes = []
    now = datetime.now().isoformat()

    for rule_name in RULE_NAMES:
        recent = conn.execute(
            """SELECT outcome FROM pruner_log
               WHERE rule_name=? AND outcome IN ('user_approved','user_rejected')
               ORDER BY created_at DESC LIMIT 20""",
            (rule_name,)
        ).fetchall()

        if not recent:
            continue

        approvals = sum(1 for r in recent if r["outcome"] == "user_approved")
        rejections = len(recent) - approvals

        rule_row = conn.execute(
            "SELECT confidence FROM pruner_rules WHERE rule_name=?", (rule_name,)
        ).fetchone()
        if not rule_row:
            continue

        old_confidence = rule_row["confidence"]
        new_confidence = old_confidence

        # Net adjustment: each approval nudges up, each rejection nudges down harder
        net = (approvals * CONFIDENCE_NUDGE) - (rejections * CONFIDENCE_NUDGE_DOWN)
        new_confidence = max(MIN_CONFIDENCE, min(MAX_CONFIDENCE, old_confidence + net))

        if abs(new_confidence - old_confidence) > 0.001:
            conn.execute(
                "UPDATE pruner_rules SET confidence=?, updated_at=? WHERE rule_name=?",
                (new_confidence, now, rule_name)
            )
            crossed_threshold = (
                (old_confidence < AUTO_EXECUTE_THRESHOLD <= new_confidence) or
                (new_confidence < AUTO_EXECUTE_THRESHOLD <= old_confidence)
            )
            changes.append({
                "rule": rule_name,
                "old_confidence": round(old_confidence, 3),
                "new_confidence": round(new_confidence, 3),
                "approvals_sampled": approvals,
                "rejections_sampled": rejections,
                "crossed_auto_execute_threshold": crossed_threshold,
                "now_auto_executes": new_confidence >= AUTO_EXECUTE_THRESHOLD,
            })

    if changes:
        conn.commit()
    return changes


# ---------------------------------------------------------------------------
# Report data — consumed by health report scheduled task
# ---------------------------------------------------------------------------

def get_pruner_report(conn, since_days: int = 7) -> dict:
    """
    Generate a human-readable summary of pruner activity for the health report.
    """
    cutoff = (datetime.now() - timedelta(days=since_days)).isoformat()

    # Activity since cutoff
    log_rows = conn.execute(
        "SELECT * FROM pruner_log WHERE created_at >= ? ORDER BY created_at DESC",
        (cutoff,)
    ).fetchall()

    auto_deleted = [r for r in log_rows if r["outcome"] == "auto_deleted"]
    user_approved = [r for r in log_rows if r["outcome"] == "user_approved"]
    user_rejected = [r for r in log_rows if r["outcome"] == "user_rejected"]
    queued_pending = conn.execute(
        "SELECT * FROM prune_queue WHERE resolved=0 ORDER BY created_at DESC"
    ).fetchall()

    tokens_freed = sum(r["tokens_freed"] for r in auto_deleted + user_approved)

    # Rule states
    rules = conn.execute("SELECT * FROM pruner_rules").fetchall()
    rule_summaries = []
    for r in rules:
        rule_summaries.append({
            "rule": r["rule_name"],
            "confidence": round(r["confidence"], 3),
            "auto_executes": r["confidence"] >= AUTO_EXECUTE_THRESHOLD,
            "auto_count": r["auto_count"],
            "queue_count": r["queue_count"],
        })

    # Recalibration events (threshold crossings) in the period
    recal_changes = recalibrate_thresholds(conn)

    return {
        "period_days": since_days,
        "auto_deleted_count": len(auto_deleted),
        "user_approved_count": len(user_approved),
        "user_rejected_count": len(user_rejected),
        "tokens_freed_this_period": tokens_freed,
        "pending_queue_count": len(queued_pending),
        "pending_queue": [
            {
                "queue_id": q["id"],
                "rule": q["rule_name"],
                "candidate_id": q["candidate_id"],
                "confidence": round(q["confidence"], 3),
                "reason": q["reason"],
                "created_at": q["created_at"],
            }
            for q in queued_pending
        ],
        "rule_states": rule_summaries,
        "recalibration_changes": recal_changes,
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _log_decision(conn, profile, rule_name, action, cand,
                  outcome, tokens_freed, triggered_by, now):
    lid = f"pl_{uuid.uuid4().hex[:8]}"
    conn.execute(
        """INSERT INTO pruner_log
           (id,profile,rule_name,action,candidate_id,superseded_by,
            outcome,tokens_freed,triggered_by,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (lid, profile, rule_name, action,
         cand["candidate_id"], cand.get("superseded_by"),
         outcome, tokens_freed, triggered_by, now)
    )
