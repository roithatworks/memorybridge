#!/usr/bin/env python3
"""Backfill tags on existing MemoryBridge memories via the tag enrichment pipeline.

Slow by design: processes in batches with a configurable delay between writes
to avoid saturating CPU or hammering the commit log on WAL-mode SQLite.

Usage:
    python3 scripts/backfill-tags.py [--delay 0.5] [--batch 50]
    python3 scripts/backfill-tags.py --dry-run      # preview only
    python3 scripts/backfill-tags.py --min-tags 0    # tag everything (default)
    python3 scripts/backfill-tags.py --min-tags 1    # skip memories with ≥1 tag

Logic:
    1. Query all non-archived memories from the default profile
    2. Re-run the tag enrichment pipeline on each (same logic as add_memory)
    3. If generated tags differ from stored tags, update in place
    4. Report summary at end

Exit codes:
    0 — completed (possibly with skips)
    1 — critical error (DB not found, no profile)
"""

import argparse
import json
import os
import shutil
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

# ── Add repo root to path so db/ imports work ──────────────────────────
_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
sys.path.insert(0, str(_REPO))

from db.constants import generate_tags  # noqa: E402

_DATA_DIR = Path(os.environ.get("MEMORYBRIDGE_DATA", Path.home() / "memorybridge"))
_DB_PATH = _DATA_DIR / "memory.db"
_PROFILE = "default"


def _backup_db(db_path: Path) -> Path:
    """Timestamped backup of the DB (+ WAL/SHM) before any bulk write."""
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = db_path.with_name(f"{db_path.name}.bak-backfill-tags-{ts}")
    shutil.copy2(db_path, backup)
    for ext in ("-wal", "-shm"):
        side = db_path.with_name(db_path.name + ext)
        if side.exists():
            shutil.copy2(side, backup.with_name(backup.name + ext))
    return backup


def main():
    parser = argparse.ArgumentParser(
        description="Backfill tags on existing MemoryBridge memories."
    )
    parser.add_argument("--db", default=str(_DB_PATH), help="MemoryBridge SQLite path")
    parser.add_argument("--profile", default=_PROFILE, help="Profile name")
    parser.add_argument("--delay", type=float, default=0.2,
                        help="Seconds between writes (default: 0.2)")
    parser.add_argument("--batch", type=int, default=50,
                        help="Rows to fetch per query (default: 50)")
    parser.add_argument("--min-tags", type=int, default=0,
                        help="Skip memories with >= this many tags (default: 0 = process all)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview what would change without writing")
    args = parser.parse_args()

    db_path = Path(args.db).expanduser()
    if not db_path.exists():
        print(f"✗ DB not found: {db_path}", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    # Wait for the write lock instead of erroring instantly if the live server
    # is mid-write (the store uses WAL + busy_timeout; match it here).
    conn.execute("PRAGMA busy_timeout=5000")

    if not args.dry_run:
        backup = _backup_db(db_path)
        print(f"ℹ Backup written: {backup}")

    # Fetch memories
    # Using offset pagination for simplicity — backfill is a one-shot script
    row_count = conn.execute(
        "SELECT COUNT(*) AS c FROM memories WHERE profile=? AND archived=0",
        (args.profile,)
    ).fetchone()["c"]

    print(f"ℹ MemoryBridge DB: {db_path}")
    print(f"ℹ Profile: {args.profile}")
    print(f"ℹ Total memories: {row_count}")
    print(f"ℹ Min tags threshold: {args.min_tags} (skip if tags > {args.min_tags})")
    print(f"ℹ Dry run: {args.dry_run}")
    print()

    offset = 0
    updated = 0
    skipped = 0
    no_change = 0
    errors = 0

    while offset < row_count:
        rows = conn.execute(
            "SELECT id, content, category, importance, tags, project_id "
            "FROM memories WHERE profile=? AND archived=0 "
            "ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (args.profile, args.batch, offset),
        ).fetchall()

        for row in rows:
            mem_id = row["id"]
            content = row["content"]
            stored_tags_raw = row["tags"]
            category = row["category"]
            project_id = row["project_id"]

            # Parse stored tags
            try:
                stored_tags = json.loads(stored_tags_raw) if stored_tags_raw else []
            except (json.JSONDecodeError, TypeError):
                stored_tags = []

            # Skip if already well-tagged
            if len(stored_tags) > args.min_tags:
                skipped += 1
                continue

            # Generate enriched tags using same pipeline as add_memory
            new_tags = generate_tags(
                content,
                category=category,
                project_id=project_id,
                existing_tags=stored_tags,
            )

            if new_tags == stored_tags:
                no_change += 1
                continue

            if args.dry_run:
                added = set(new_tags) - set(stored_tags)
                print(f"  ~ {mem_id[-8:]}: +{', '.join(sorted(added))}")
                continue

            # Update in place
            try:
                conn.execute(
                    "UPDATE memories SET tags=? WHERE id=?",
                    (json.dumps(new_tags), mem_id),
                )
                conn.commit()
                updated += 1
                added = set(new_tags) - set(stored_tags)
                print(f"  ✓ {mem_id[-8:]}: +{', '.join(sorted(added))}")
            except Exception as e:
                print(f"  ✗ {mem_id[-8:]}: {e}")
                errors += 1

            if args.delay > 0:
                time.sleep(args.delay)

        offset += args.batch
        remaining = row_count - offset
        print(f"  … {remaining} remaining (offset={offset})", end="\r")
        sys.stdout.flush()

    print()
    print(f"\n─ Done ──────────────────────────────")
    print(f"  Updated: {updated}")
    print(f"  No change: {no_change}")
    print(f"  Skipped (already tagged): {skipped}")
    if errors:
        print(f"  Errors: {errors}")
    print(f"  Total processed: {updated + no_change + skipped} / {row_count}")

    conn.close()
    sys.exit(0 if errors == 0 else 1)


if __name__ == "__main__":
    main()
