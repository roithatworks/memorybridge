"""
MemoryBridge Adaptive Auto-Pruner
==================================
Runs after every add_memory call. Detects redundant/stale memories,
auto-executes high-confidence cases, queues uncertain ones for human review.

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
import re
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

# Rules whose deletions must ALWAYS be human-reviewed, no matter how confident
# the rule has become. verbatim_subset infers "the shorter fact is redundant"
# from containment, which is often a refinement/exception rather than a true
# duplicate — too risky to ever auto-delete unattended.
REVIEW_ONLY_RULES = {"verbatim_subset"}

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

-- Indexes for the per-candidate lookups run on the add_memory write hot-path (#125).
CREATE INDEX IF NOT EXISTS idx_prune_queue_candidate ON prune_queue(candidate_id, resolved);
CREATE INDEX IF NOT EXISTS idx_pruner_log_candidate ON pruner_log(candidate_id);
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

    Optimization: memories are sorted by content length ASC in SQL. The inner loop
    skips any other entry whose normalized length is <= the candidate's — a subset
    must be strictly shorter, so we only check genuine potential superseders.
    This roughly halves comparisons vs. the naive O(n²) scan.
    """
    rows = conn.execute(
        "SELECT id, content, importance, category, project_id, token_count "
        "FROM memories WHERE profile=? AND archived=0 "
        "ORDER BY LENGTH(content) ASC",
        (profile,)
    ).fetchall()

    memories = [(dict(r), _normalize(r["content"])) for r in rows]
    candidates = []
    seen_candidates = set()

    for mem, norm_content in memories:
        if mem["importance"] in NEVER_AUTO_DELETE_IMPORTANCE:
            continue
        if mem["id"] in seen_candidates:
            continue
        for other, norm_other in memories:
            if other["id"] == mem["id"]:
                continue
            # subset must be strictly shorter — skip equal-or-shorter entries
            if len(norm_other) <= len(norm_content):
                continue
            # Only supersede within the same category AND project. A longer fact
            # in a different topic/scope is usually a refinement or exception
            # ("uses Notion" vs "uses Notion only for teaching"), not a true
            # duplicate — deleting the shorter one would lose the general fact.
            if mem.get("category") != other.get("category"):
                continue
            if mem.get("project_id") != other.get("project_id"):
                continue
            # Require WHOLE-WORD contiguous containment, not a raw substring, so
            # "SAP" isn't treated as contained in "mishap". Word-boundary
            # lookarounds also tolerate adjacent punctuation ("roles" matches
            # "roles, remote…") which a naive space-pad would miss.
            if not re.search(r"(?<!\w)" + re.escape(norm_content) + r"(?!\w)",
                             norm_other):
                continue
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
            seen_candidates.add(mem["id"])
            break  # one superseder is enough

    return candidates


# ---------------------------------------------------------------------------
# Rule: stale project_status
# ---------------------------------------------------------------------------

def find_stale_project_status(conn, profile: str) -> list[dict]:
    """
    Find project_status entries older than STALE_DAYS where a newer entry
    exists for the same project_id.
    """
    cutoff = (datetime.now() - timedelta(days=STALE_DAYS)).isoformat()

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

def run_auto_prune(conn, profile: str, delete_fn, allow_auto_delete: bool = True) -> dict:
    """
    Main entry point. Called after every add_memory.

    delete_fn: callable(profile, memory_id) -> int (tokens_freed)
                This is MemoryStore.delete_memory — passed in to avoid circular import.

    allow_auto_delete: when False (e.g. a remote-origin write over the HTTP
                bridge), candidates that meet the auto-execute confidence bar are
                routed to the human-review queue instead of being deleted. This
                keeps a prompt-injected/confused remote model from destroying
                memories while still surfacing the candidate for local approval.

    Returns summary dict with auto_executed and queued counts.
    """
    bootstrap_rules(conn)

    # stale_project_status is pure SQL — always run it (effectively O(n) with index).
    # Subset scan is O(n²) even with the length-guard optimization, so gate it:
    # run on small profiles (<=500) or every 50th add on larger ones.
    count = conn.execute(
        "SELECT COUNT(*) FROM memories WHERE profile=? AND archived=0", (profile,)
    ).fetchone()[0]

    all_candidates = find_stale_project_status(conn, profile)
    if count <= 500 or count % 50 == 0:
        all_candidates += find_subset_candidates(conn, profile)

    if not all_candidates:
        return {"auto_executed": [], "queued": []}

    auto_executed = []
    queued = []
    now = datetime.now().isoformat()

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

        review_only = rule_name in REVIEW_ONLY_RULES
        if confidence >= AUTO_EXECUTE_THRESHOLD and allow_auto_delete and not review_only:
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

    return {"auto_executed": auto_executed, "queued": queued}


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

    # NOTE: get_pruner_report is READ-ONLY. It must not call
    # recalibrate_thresholds() — that writes pruner_rules and commits, so every
    # report/UI-poll nudged rule confidence and could silently escalate rules
    # into auto-delete mode. Recalibration happens where it belongs, in
    # record_outcome() on an actual user decision.
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
        # Recalibration is applied by record_outcome(), not by this read-only
        # report; kept for response-shape compatibility.
        "recalibration_changes": [],
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
