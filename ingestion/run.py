"""MemoryBridge Phase 2 — Ingestion CLI.

Usage:
    python ingestion/run.py --source claude --file ~/Downloads/conversations.json
    python ingestion/run.py --source chatgpt --file ~/Downloads/conversations.json --days 30
    python ingestion/run.py --source gemini --file ~/Downloads/MyActivity.json --profile job_search
    python ingestion/run.py --source claude --file ~/Downloads/conversations.json --preview
"""

import argparse
import json
import logging
import os
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

# Allow importing local ingestion modules when run from other directories
sys.path.insert(0, str(Path(__file__).parent))

# Data dir: mutable state (db, logs, .env, inbox) — defaults to ~/memorybridge,
# override with MEMORYBRIDGE_DATA. Code stays in the repo.
import os
_DATA_DIR = Path(os.environ.get("MEMORYBRIDGE_DATA", Path.home() / "memorybridge"))

# Load .env before any module imports that need env vars.
# Data-dir .env is canonical; repo-local .env supported as fallback for dev.
from dotenv import load_dotenv
load_dotenv(_DATA_DIR / ".env")
load_dotenv(Path(__file__).parent.parent / ".env", override=False)

# Ingestion modules
from parse_claude import parse as parse_claude
from parse_chatgpt import parse as parse_chatgpt
from parse_gemini import parse as parse_gemini
from extractor import extract, ExtractionError
from router import route
from resolver import resolve
from merger import merge

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("run")

PARSERS = {
    "claude": parse_claude,
    "chatgpt": parse_chatgpt,
    "gemini": parse_gemini,
}

MEMORYBRIDGE_DIR = _DATA_DIR
LOGS_DIR = MEMORYBRIDGE_DIR / "logs"
FLAGGED_QUEUE = MEMORYBRIDGE_DIR / "flagged_queue.json"


def _write_flagged(flagged: list, source: str, profile: str) -> None:
    """Append flagged items to flagged_queue.json."""
    if not flagged:
        return

    existing = {"generated": datetime.now().isoformat(), "items": []}
    if FLAGGED_QUEUE.exists():
        try:
            existing = json.loads(FLAGGED_QUEUE.read_text())
        except Exception:
            pass

    for fact in flagged:
        existing["items"].append({
            "id": str(uuid.uuid4()),
            "fact": fact.get("fact", ""),
            "confidence": fact.get("confidence", 0.0),
            "reason": fact.get("reason", ""),
            "category": fact.get("category", "fact"),
            "importance": fact.get("importance", "medium"),
            "project": fact.get("project"),
            "source_conversation_id": fact.get("source_conversation_id", ""),
            "source": source,
            "profile": profile,
            "status": "pending",
        })

    existing["generated"] = datetime.now().isoformat()
    FLAGGED_QUEUE.parent.mkdir(parents=True, exist_ok=True)
    FLAGGED_QUEUE.write_text(json.dumps(existing, indent=2))


def _write_log(report: dict, flagged_count: int, escalated_count: int) -> Path:
    """Write diff report to logs/ingest_YYYY-MM-DD_HH-MM.json."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    filename = LOGS_DIR / f"ingest_{datetime.now().strftime('%Y-%m-%d_%H-%M')}.json"
    full_report = {
        **report,
        "flagged_count": flagged_count,
        "escalated_count": escalated_count,
    }
    filename.write_text(json.dumps(full_report, indent=2))
    return filename


def _print_summary(report: dict, conv_count: int, flagged_count: int, elapsed: float) -> None:
    print("\nMemoryBridge Ingestion Complete")
    print("─" * 35)
    print(f"Source:    {report['source']} ({conv_count} conversations)")
    print(f"Profile:   {report['profile']}")
    print(f"Added:     {report['added']} new facts")
    print(f"Skipped:   {report['skipped_duplicate']} duplicates")
    print(f"Merged:    {report['merged']}")
    if flagged_count:
        print(f"Flagged:   {flagged_count} (review at {FLAGGED_QUEUE})")
    print(f"Escalated: {report['escalated_count']}")
    print(f"Rejected:  {report['rejected']}")
    print(f"Time:      {elapsed:.1f}s")
    if report.get("preview"):
        print("\n[PREVIEW MODE — no writes performed]")


def main():
    parser = argparse.ArgumentParser(description="MemoryBridge ingestion pipeline")
    parser.add_argument("--source", required=True, choices=["claude", "chatgpt", "gemini"])
    parser.add_argument("--file", required=True, help="Path to export file")
    parser.add_argument("--days", type=int, default=None, help="Only process last N days")
    parser.add_argument("--profile", default="default", help="Memory profile to write to")
    parser.add_argument("--preview", action="store_true", help="Dry run — no writes")
    args = parser.parse_args()

    start = time.time()

    # 1. Parse
    parse_fn = PARSERS[args.source]
    print(f"Parsing {args.source} export...")
    normalized = parse_fn(args.file, days=args.days)
    conv_count = len(normalized.get("conversations", []))
    print(f"  Found {conv_count} conversations")

    if conv_count == 0:
        print("No conversations to process.")
        return

    # 2. Extract
    print("Extracting facts via DeepSeek R1...")
    try:
        facts = extract(normalized)
    except ExtractionError as e:
        print(f"Extraction failed: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"  Extracted {len(facts)} candidate facts")

    if not facts:
        print("No facts extracted.")
        return

    # 3. Route
    print("Routing by confidence...")
    buckets = route(facts, profile=args.profile)
    accepted = buckets["accepted"]
    flagged = buckets["flagged"]
    escalated = buckets["escalated"]
    print(f"  Accepted: {len(accepted)}, Flagged: {len(flagged)}, Escalated: {len(escalated)}")

    # 4. Resolve escalated (unless preview)
    resolved = []
    if escalated and not args.preview:
        print(f"Resolving {len(escalated)} escalated facts via Claude...")
        resolved = resolve(escalated)
    elif escalated and args.preview:
        print(f"  [Preview] Would resolve {len(escalated)} escalated facts")
        resolved = [{**f, "verdict": "reject", "merged_fact": None} for f in escalated]

    # 5. Merge
    print("Merging into memory..." if not args.preview else "Previewing merge (no writes)...")
    report = merge(
        accepted=accepted,
        resolved=resolved,
        source=args.source,
        profile=args.profile,
        preview=args.preview,
    )
    report["escalated_count"] = len(escalated)
    if args.preview:
        report["preview"] = True

    # 6. Write flagged queue and log (skip in preview)
    if not args.preview:
        _write_flagged(flagged, args.source, args.profile)
        log_path = _write_log(report, len(flagged), len(escalated))
        print(f"  Log written to {log_path}")

        # Write to Notion if configured (opt-in via env vars)
        notion_db_id = os.environ.get("NOTION_FLAGGED_DB_ID", "")
        if notion_db_id and flagged:
            try:
                from notion_queue import build_notion_client, write_flagged_to_notion
                notion_client = build_notion_client()
                if notion_client:
                    written = write_flagged_to_notion(
                        flagged=flagged,
                        source=args.source,
                        profile=args.profile,
                        client=notion_client,
                        database_id=notion_db_id,
                    )
                    print(f"  Notion: {written} flagged items added to database")
            except Exception as e:
                print(f"  Notion write skipped: {e}", file=sys.stderr)

    elapsed = time.time() - start
    _print_summary(report, conv_count, len(flagged), elapsed)


if __name__ == "__main__":
    # Allow running from repo root: python ingestion/run.py
    main()
