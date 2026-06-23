#!/usr/bin/env python3
"""Backfill entity tags for all active memories in MemoryBridge.

Extracts entity tags using the EntityExtractor and updates any memory
that's missing entity tags. Idempotent — safe to re-run.

Usage:
    python3 scripts/backfill-entities.py
    python3 scripts/backfill-entities.py --dry-run    # preview only
"""

import argparse
import sys
from pathlib import Path

# Ensure we can import from the repo root
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from db.store import MemoryStore, EntityExtractor  # noqa: E402
from db.constants import _merge_tags  # noqa: E402

DATA_DIR = Path.home() / "memorybridge"
MEMORY_DB = DATA_DIR / "memory.db"
ENTITY_CONFIG = DATA_DIR / "entities.json"


def main():
    parser = argparse.ArgumentParser(description="Backfill entity tags for existing memories")
    parser.add_argument("--dry-run", action="store_true", help="Preview only — no writes")
    parser.add_argument("--profile", default="default", help="Memory profile (default: default)")
    args = parser.parse_args()

    if not MEMORY_DB.exists():
        print(f"Error: MemoryBridge DB not found at {MEMORY_DB}")
        sys.exit(1)

    # Initialize extractor and store
    config_path = ENTITY_CONFIG if ENTITY_CONFIG.exists() else None
    extractor = EntityExtractor(config_path=config_path)
    store = MemoryStore(MEMORY_DB, entity_extractor=extractor, merge_threshold=0.0)
    store.ensure_profile(args.profile)

    print(f"Entity config: {ENTITY_CONFIG if config_path else '(seed list)'}")
    print(f"Patterns: {extractor.pattern_count}")
    print(f"\nFetching active memories for profile '{args.profile}'...")

    all_mems = store.get_memories(args.profile)
    print(f"Total active memories: {len(all_mems)}")

    scanned = 0
    updated = 0
    skipped_no_entities = 0  # content has no entities to extract
    skipped_has_entities = 0  # already has entity tags
    tag_stats: dict[str, int] = {}

    for mem in all_mems:
        mid = mem["id"]
        content = mem.get("content", "")
        existing_tags = mem.get("tags", []) or []
        existing_entity_tags = {t for t in existing_tags if t.startswith("entity:")}

        scanned += 1

        # Skip if already has entity tags
        if existing_entity_tags:
            skipped_has_entities += 1
            continue

        # Extract entities
        new_entity_tags = extractor.extract(content)
        if not new_entity_tags:
            skipped_no_entities += 1
            continue

        # Merge with existing tags
        merged_tags = _merge_tags(existing_tags, new_entity_tags)

        # Skip if nothing changed
        if merged_tags == existing_tags:
            skipped_no_entities += 1
            continue

        # Update tag stats
        for tag in new_entity_tags:
            tag_stats[tag] = tag_stats.get(tag, 0) + 1

        # Preview
        tag_diff = set(merged_tags) - set(existing_tags)
        if args.dry_run:
            print(f"  [{mid[:8]}] +{sorted(tag_diff)}")
            continue

        # Write update
        try:
            store._conn.execute(
                "UPDATE memories SET tags=? WHERE id=? AND profile=?",
                (str(merged_tags).replace("'", '"'), mid, args.profile),  # simple JSON
            )
            store._conn.commit()
            updated += 1
            if updated <= 5 or updated % 20 == 0:
                print(f"  [{mid[:8]}] +{sorted(tag_diff)}")
        except Exception as e:
            print(f"  [{mid[:8]}] ERROR: {e}", file=sys.stderr)

    # Summary
    print(f"\n{'─' * 50}")
    print(f"Scanned:           {scanned}")
    print(f"Already tagged:    {skipped_has_entities}")
    print(f"No entities found: {skipped_no_entities}")
    print(f"Updated:           {updated if not args.dry_run else '(dry run)'}")
    print(f"Skipped (no tags): {scanned - updated - skipped_has_entities - skipped_no_entities}")

    if tag_stats:
        print(f"\nTop entities found:")
        for tag, count in sorted(tag_stats.items(), key=lambda x: -x[1])[:15]:
            label = tag.replace("entity:", "")
            print(f"  {label:20s}  {count:3d} memories")


if __name__ == "__main__":
    main()
