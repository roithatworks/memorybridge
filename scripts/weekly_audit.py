#!/usr/bin/env python3
import sys
import sqlite3
import os
import random
from datetime import datetime, timedelta

def main():
    db_path = os.path.expanduser("~/GitHub/memorybridge/memory.db")
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
        if log["outcome"] == "user_rejected":
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
            "SELECT COUNT(*) FROM pruner_log WHERE rule_name = ? AND outcome = 'user_rejected' AND created_at >= ?",
            (rule, fourteen_days_ago)
        ).fetchone()[0]

        if rejections_14d == 0 and rule not in rejections_by_rule:
            if rule_row["confidence"] < 0.85 and rule_row["confidence"] >= 0.75:
                proposals.append(rule)
                # Technically it proposes, so we might just bump it to 0.85 after a week. 
                # We can just note the proposal here.
                report_lines.append(f"- **{rule}**: 2 clean weeks. Proposed to widen auto-execute scope (confidence near 0.85).")

    if not proposals:
        report_lines.append("No new rules proposed for expansion this week.")
    
    report_lines.append("")

    # 3. Regression check (#189): 3 random spot checks
    report_lines.append("## Regression Check (#189)")
    candidates = conn.execute(
        "SELECT candidate_id FROM pruner_log WHERE outcome IN ('user_approved', 'auto_deleted') AND created_at >= ?",
        (seven_days_ago,)
    ).fetchall()

    if candidates:
        spot_checks = random.sample(candidates, min(3, len(candidates)))
        phantom_found = False
        for c in spot_checks:
            cand_id = c["candidate_id"]
            mem = conn.execute("SELECT id FROM memories WHERE id = ?", (cand_id,)).fetchone()
            if not mem:
                phantom_found = True
                report_lines.append(f"- ❌ PHANTOM CANDIDATE DETECTED: {cand_id} does not exist in memories!")
                
        if phantom_found:
            report_lines.append("\n**CRITICAL REGRESSION:** Phantom entries found. Pausing all auto-execute immediately.")
            conn.execute("UPDATE pruner_rules SET confidence = 0.5") # drastic drop to pause
            conn.commit()
        else:
            report_lines.append("- ✅ Spot checks passed: No phantom entries detected.")
    else:
        report_lines.append("- No pruner logs this week to spot check.")

    report_content = "\n".join(report_lines)
    report_path = os.path.expanduser("~/GitHub/memorybridge/weekly_audit_report.md")
    with open(report_path, "w") as f:
        f.write(report_content)
        
    print(f"Weekly audit complete. Report saved to {report_path}")

if __name__ == "__main__":
    main()
