"""MemoryBridge Phase 2 — Ingestion CLI.

Usage:
    python ingestion/run.py --source claude --file ~/Downloads/conversations.json
    python ingestion/run.py --source chatgpt --file ~/Downloads/conversations.json --days 30
    python ingestion/run.py --source gemini --file ~/Downloads/MyActivity.json --profile job_search
    python ingestion/run.py --source claude --file ~/Downloads/conversations.json --preview
"""

import argparse
import hashlib
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

# Skip the FastEmbed model load/backfill at store construction. This subprocess
# writes via add_memory (which embeds each new memory on its own thread); paying
# the one-time model download just to START UP would stall the run with no
# output. Must be set BEFORE importing router/merger (which import server ->
# constructs _store).
os.environ.setdefault("MEMORYBRIDGE_NO_EMBED", "1")

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
from parse_hermes import parse as parse_hermes
from extractor import extract, ExtractionError
from router import route
from resolver import resolve
from merger import merge

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("run")

PARSERS = {
    "hermes": parse_hermes,
    "claude": parse_claude,
    "chatgpt": parse_chatgpt,
    "gemini": parse_gemini,
}

MEMORYBRIDGE_DIR = _DATA_DIR
LOGS_DIR = MEMORYBRIDGE_DIR / "logs"
FLAGGED_QUEUE = MEMORYBRIDGE_DIR / "flagged_queue.json"
# Idempotency ledger (#45): sha256 of every conversation already ingested, so
# re-dropping the same export doesn't re-pay extraction or double-write.
INGESTED_LEDGER = MEMORYBRIDGE_DIR / "ingested_conversations.json"


def _conv_hash(source: str, conv: dict) -> str:
    """Stable content hash of a conversation for the idempotency ledger."""
    payload = json.dumps(
        {"source": source, "id": conv.get("id", ""),
         "messages": conv.get("messages", [])},
        sort_keys=True, ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _load_ledger() -> dict:
    """Load the ingested-conversation ledger ({hash: iso_ts}). Tolerant of a
    missing or corrupt file (treated as empty)."""
    if not INGESTED_LEDGER.exists():
        return {}
    try:
        data = json.loads(INGESTED_LEDGER.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        logger.warning("ingested_conversations.json unreadable — treating as empty")
        return {}


def _record_ingested(source: str, convs: list) -> None:
    """Atomically add the given conversations' hashes to the ledger."""
    if not convs:
        return
    ledger = _load_ledger()
    now = datetime.now().isoformat()
    for c in convs:
        ledger[_conv_hash(source, c)] = now
    INGESTED_LEDGER.parent.mkdir(parents=True, exist_ok=True)
    tmp = INGESTED_LEDGER.with_name(f"{INGESTED_LEDGER.name}.tmp")
    tmp.write_text(json.dumps(ledger, indent=0))
    os.replace(tmp, INGESTED_LEDGER)


def _write_flagged(flagged: list, source: str, profile: str) -> None:
    """Append flagged items to flagged_queue.json (atomically, without ever
    silently discarding existing pending items)."""
    if not flagged:
        return

    FLAGGED_QUEUE.parent.mkdir(parents=True, exist_ok=True)
    existing = {"generated": datetime.now().isoformat(), "items": []}
    if FLAGGED_QUEUE.exists():
        try:
            loaded = json.loads(FLAGGED_QUEUE.read_text())
            if isinstance(loaded, dict) and isinstance(loaded.get("items"), list):
                existing = loaded
            else:
                raise ValueError("unexpected queue shape")
        except Exception as e:
            # Do NOT silently overwrite an unreadable queue — that would drop
            # every pending review item. Preserve it as a .corrupt backup first.
            ts = datetime.now().strftime("%Y%m%d-%H%M%S")
            bak = FLAGGED_QUEUE.with_name(f"{FLAGGED_QUEUE.name}.corrupt-{ts}")
            try:
                FLAGGED_QUEUE.replace(bak)
                logger.error("flagged_queue.json unreadable (%s) — backed up to %s",
                             e, bak)
            except Exception:
                logger.error("flagged_queue.json unreadable (%s) and backup failed", e)

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
    # Atomic write: a crash mid-write must not corrupt the queue.
    tmp = FLAGGED_QUEUE.with_name(f"{FLAGGED_QUEUE.name}.tmp")
    tmp.write_text(json.dumps(existing, indent=2))
    os.replace(tmp, FLAGGED_QUEUE)


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
    if report.get("guardrail_rejected"):
        print(f"Guardrail: {report['guardrail_rejected']} doc-shaped facts dropped")
    if flagged_count:
        print(f"Flagged:   {flagged_count} (review at {FLAGGED_QUEUE})")
    print(f"Escalated: {report['escalated_count']}")
    print(f"Rejected:  {report['rejected']}")
    routed = report.get("routed_by_profile") or {}
    if routed:
        split = ", ".join(f"{p}: {n}" for p, n in sorted(routed.items(), key=lambda x: -x[1]))
        print(f"Routed:    {split}")
    print(f"Time:      {elapsed:.1f}s")
    if report.get("preview"):
        print("\n[PREVIEW MODE — no writes performed]")


def main():
    parser = argparse.ArgumentParser(description="MemoryBridge ingestion pipeline")
    parser.add_argument("--source", required=True,
                        choices=["hermes", "claude", "chatgpt", "gemini"])
    parser.add_argument("--file", default=None,
                        help="Path to export file (optional for hermes; "
                             "defaults to ~/.hermes/state.db)")
    parser.add_argument("--days", type=int, default=None, help="Only process last N days")
    parser.add_argument("--profile", default="default", help="Memory profile to write to")
    parser.add_argument("--preview", action="store_true", help="Dry run — no writes")
    args = parser.parse_args()

    # All sources except hermes read from an export file; hermes defaults to
    # the local Hermes state.db when --file is omitted.
    if args.file is None and args.source != "hermes":
        parser.error(f"--file is required for source '{args.source}'")

    start = time.time()

    # 1. Parse
    parse_fn = PARSERS[args.source]
    print(f"Parsing {args.source} export...")
    normalized = parse_fn(args.file, days=args.days)
    conv_count = len(normalized.get("conversations", []))
    print(f"  Found {conv_count} conversations")

    # 1b. Idempotency (#45): skip conversations already ingested in a prior run
    # so a re-dropped export doesn't re-pay extraction or double-write. Record
    # (below) only after a successful run. MEMORYBRIDGE_FORCE_REINGEST=1 bypasses.
    force_reingest = os.environ.get("MEMORYBRIDGE_FORCE_REINGEST", "").lower() \
        in ("1", "true", "yes")
    _max_conv = int(os.environ.get("MEMORYBRIDGE_MAX_CONVERSATIONS", "500"))

    def _cap(seq):
        return seq[:_max_conv] if (_max_conv > 0 and len(seq) > _max_conv) else seq

    convs_to_record = []
    if not args.preview:
        all_convs = normalized.get("conversations", [])
        if force_reingest:
            convs_to_record = _cap(all_convs)
        else:
            ledger = _load_ledger()
            unseen = [c for c in all_convs
                      if _conv_hash(args.source, c) not in ledger]
            already = len(all_convs) - len(unseen)
            if already:
                print(f"  Skipping {already} already-ingested conversations "
                      f"(idempotency; set MEMORYBRIDGE_FORCE_REINGEST=1 to reprocess)")
            normalized["conversations"] = unseen
            conv_count = len(unseen)
            # Record only what extraction will actually process (mirrors the
            # #44 cost cap, which truncates to the first _max_conv).
            convs_to_record = _cap(unseen)

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

    # Honest exit status for the watcher (#60). The watcher uses our exit code to
    # decide processed/ vs failed/. If facts were meant to be written but the
    # merge accounted for NONE of them (not added, not duplicate, not merged, not
    # guardrail-rejected, not conflict-rejected), the writes silently vanished —
    # exit non-zero so the file lands in failed/ instead of being archived as a
    # success. Legitimate all-duplicate / all-flagged runs still exit 0.
    if not args.preview:
        intended = len(accepted) + sum(
            1 for r in resolved if r.get("verdict") in ("accept", "merge"))
        accounted = (report.get("added", 0) + report.get("skipped_duplicate", 0)
                     + report.get("merged", 0) + report.get("guardrail_rejected", 0)
                     + report.get("rejected", 0))
        if intended > 0 and accounted == 0:
            print(f"Ingestion accounted for none of {intended} intended facts — "
                  f"treating as failure.", file=sys.stderr)
            sys.exit(1)

        # Success — record processed conversations in the idempotency ledger.
        # Placed AFTER the failure exit above so a failed run can be retried.
        _record_ingested(args.source, convs_to_record)


if __name__ == "__main__":
    # Allow running from repo root: python ingestion/run.py
    main()
