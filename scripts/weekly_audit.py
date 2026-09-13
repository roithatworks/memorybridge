#!/usr/bin/env python3
"""Weekly pruner audit — rejections, expansion proposals, regression check.

Run by launchd: com.memorybridge.weekly-audit (Sunday 03:15).

History (issue #194): the original regression check matched outcome values by
exact string, so when those values were annotated during the 2026-09-12 record
correction the query matched zero rows and the report printed "no logs to spot
check" -- absence of evidence rendered as a clean bill of health. Matching is now
prefix-based (LIKE 'user_approved%') so an annotation can no longer silently
disable the check.

It also flagged "not present in memories" as PHANTOM. That premise was wrong: a
candidate missing after a prune means the memory was HARD-DELETED (#195), not
that the ID never matched. Both look identical post-hoc. Now reported as three
distinct states -- live / archived / missing -- and only "missing" is a problem.
"""
import sys
import sqlite3
import os
import random
from datetime import datetime, timedelta


def main():
    db_path = os.path.expanduser("~/memorybridge/memory.db")
    if not os.path.exists(db_path):
        print(f"Error: Database not found at {db_path}")
        sys.exit(1)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    now = datetime.now()
    seven_days_ago = (now - timedelta(days=7)).isoformat()
    fourteen_days_ago = (now - timedelta(days=14)).isoformat()

    report_lines = [
        "# Weekly Pruner Audit Report",
        f"Generated: {now.isoformat()}",
        ""
    ]

    # 1. Pull past 7 days of pruner_log
    logs_7d = conn.execute(
        "SELECT * FROM pruner_log WHERE created_at >= ?", (seven_days_ago,)
    ).fetchall()

    rejections_by_rule = {}
    for log in logs_7d:
        if (log["outcome"] or "").startswith("user_rejected"):
            rule = log["rule_name"]
            rejections_by_rule[rule] = rejections_by_rule.get(rule, 0) + 1

    report_lines.append("## Rule Rejections (Last 7 Days)")
    if not rejections_by_rule:
        report_lines.append("No rules had rejections. Clean week!")
    else:
        for rule, count in rejections_by_rule.items():
            report_lines.append(f"- **{rule}**: {count} rejections (Scope expansion paused, confidence held/lowered)")
            # Lower confidence of rules with rejections
            conn.execute(
                "UPDATE pruner_rules SET confidence = MAX(0.2, confidence - 0.1), updated_at = ? WHERE rule_name = ?",
                (now.isoformat(), rule)
            )
        conn.commit()
    report_lines.append("")

    # 2. Check for 2+ clean weeks to propose widening
    rules = conn.execute("SELECT rule_name, confidence FROM pruner_rules").fetchall()
    report_lines.append("## Proposed Auto-Execute Expansions")
    proposals = []
    for rule_row in rules:
        rule = rule_row["rule_name"]

        # Check rejections in last 14 days
        rejections_14d = conn.execute(
            "SELECT COUNT(*) FROM pruner_log WHERE rule_name = ? AND outcome LIKE 'user_rejected%' AND created_at >= ?",
            (rule, fourteen_days_ago)
        ).fetchone()[0]

        if rejections_14d == 0 and rule not in rejections_by_rule:
            if 0.75 <= rule_row["confidence"] < 0.85:
                proposals.append(rule)
                report_lines.append(
                    f"- **{rule}**: 2 clean weeks. Proposed to widen auto-execute scope "
                    f"(confidence {rule_row['confidence']:.2f}, near the 0.85 bar)."
                )

    if not proposals:
        report_lines.append("No new rules proposed for expansion this week.")

    report_lines.append("")

    # 3. Regression check (issues #189 / #194): three-state, prefix-matched.
    report_lines.append("## Regression Check")
    candidates = conn.execute(
        """SELECT candidate_id FROM pruner_log
           WHERE created_at >= ?
             AND (outcome LIKE 'user_approved%'
                  OR outcome LIKE 'auto_deleted%'
                  OR outcome LIKE 'auto_archived%')""",
        (seven_days_ago,)
    ).fetchall()

    if not candidates:
        report_lines.append(
            f"- No pruner decisions in the last 7 days (checked {len(logs_7d)} log entries) -- "
            "nothing to verify. This is not a pass; it means the check had no input."
        )
    else:
        spot_checks = random.sample(candidates, min(3, len(candidates)))
        live, archived, missing = [], [], []
        for c in spot_checks:
            cand_id = c["candidate_id"]
            mem = conn.execute(
                "SELECT archived FROM memories WHERE id = ?", (cand_id,)
            ).fetchone()
            if mem is None:
                missing.append(cand_id)
            elif mem["archived"] == 1:
                archived.append(cand_id)
            else:
                live.append(cand_id)

        report_lines.append(f"- Spot-checked {len(spot_checks)} of {len(candidates)} recent decision(s):")
        if live:
            report_lines.append(f"    - live (unarchived, no prune took effect): {', '.join(live)}")
        if archived:
            report_lines.append(f"    - archived (prune applied, recoverable): {', '.join(archived)}")
        if missing:
            report_lines.append(f"    - MISSING (row absent -- hard delete?): {', '.join(missing)}")

        if missing:
            report_lines.append("")
            report_lines.append(
                "**REGRESSION: candidate row is absent from `memories`.** Post-#195 a prune "
                "archives, so the row should still exist. Absence means a hard delete occurred. "
                "Pausing auto-execute pending review."
            )
            conn.execute("UPDATE pruner_rules SET confidence = 0.5")
            conn.commit()
        else:
            report_lines.append("- No missing rows. Prunes are archiving as expected.")

    report_content = "\n".join(report_lines)
    report_path = os.path.expanduser("~/memorybridge/logs/weekly_audit_report.md")
    with open(report_path, "w") as f:
        f.write(report_content)

    print(f"Weekly audit complete. Report saved to {report_path}")


if __name__ == "__main__":
    main()
