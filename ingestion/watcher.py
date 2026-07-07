"""
MemoryBridge Inbox Watcher.

Scans ~/memorybridge/inbox/ for JSON export files, auto-detects source
format, runs ingestion via subprocess, and moves files to processed/ or failed/.

Designed to be called by launchd WatchPaths — runs once and exits.

Usage:
    python ingestion/watcher.py                      # process inbox once
    python ingestion/watcher.py --inbox ~/custom/    # override inbox path
    python ingestion/watcher.py --profile work       # target profile
    python ingestion/watcher.py --preview            # dry run, no memory writes
"""

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("watcher")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s watcher: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

INGESTION_SCRIPT = Path(__file__).parent / "run.py"
# Inbox lives in the data dir (default ~/memorybridge), not the code repo
DEFAULT_INBOX = Path(
    os.environ.get("MEMORYBRIDGE_DATA", Path.home() / "memorybridge")
) / "inbox"


# ---------------------------------------------------------------------------
# Format detection
# ---------------------------------------------------------------------------

def detect_source(file_path: Path) -> str | None:
    """
    Fingerprint a JSON export file to determine its AI source.

    Returns 'claude', 'chatgpt', 'gemini', or None if unrecognized.
    Returns None on any parse error — caller should move file to failed/.
    """
    try:
        with open(file_path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None

    # Gemini Format A: top-level dict with "conversations" key
    if isinstance(data, dict) and "conversations" in data:
        return "gemini"

    if not isinstance(data, list) or not data:
        return None

    first = data[0]
    if not isinstance(first, dict):
        return None

    # Gemini Format B: list of activity records with a "header" dict/string
    header = first.get("header")
    if header is not None:
        header_str = header.get("title", "") if isinstance(header, dict) else str(header)
        if "gemini" in header_str.lower() or "bard" in header_str.lower():
            return "gemini"

    # Claude: has "chat_messages" list
    if "chat_messages" in first:
        return "claude"

    # ChatGPT: has "mapping" tree
    if "mapping" in first:
        return "chatgpt"

    return None


# ---------------------------------------------------------------------------
# File movement helpers
# ---------------------------------------------------------------------------

def _is_stable(file_path: Path, wait: float = 1.5) -> bool:
    """Two-sample size check: True only if the file size is unchanged over a
    short interval. A file mid-copy (Finder drag, browser download) is still
    growing; processing it would parse truncated JSON and wrongly condemn the
    completed file to failed/. Unstable files are left for the next run."""
    try:
        s1 = file_path.stat().st_size
        time.sleep(wait)
        s2 = file_path.stat().st_size
    except OSError:
        return False
    return s1 == s2


def _unique_dest(dest_dir: Path, filename: str) -> Path:
    """Return a path in dest_dir that doesn't already exist."""
    candidate = dest_dir / filename
    if not candidate.exists():
        return candidate
    stem = Path(filename).stem
    suffix = Path(filename).suffix
    ts = datetime.now().strftime("%H%M%S%f")
    return dest_dir / f"{stem}_{ts}{suffix}"


def move_to_processed(file_path: Path, processed_dir: Path = None) -> Path:
    """Move a successfully processed file to the processed/ subdirectory."""
    processed_dir = processed_dir or (file_path.parent / "processed")
    processed_dir.mkdir(parents=True, exist_ok=True)
    dest = _unique_dest(processed_dir, file_path.name)
    file_path.rename(dest)
    return dest


def move_to_failed(file_path: Path, failed_dir: Path = None) -> Path:
    """Move an unprocessable file to the failed/ subdirectory."""
    failed_dir = failed_dir or (file_path.parent / "failed")
    failed_dir.mkdir(parents=True, exist_ok=True)
    dest = _unique_dest(failed_dir, file_path.name)
    file_path.rename(dest)
    return dest


# ---------------------------------------------------------------------------
# Ingestion runner
# ---------------------------------------------------------------------------

def run_ingestion(source: str, file_path: Path, profile: str,
                  preview: bool = False) -> bool:
    """
    Invoke ingestion/run.py as a subprocess.

    Returns True on success (exit code 0), False otherwise.
    """
    cmd = [
        sys.executable,
        str(INGESTION_SCRIPT),
        "--source", source,
        "--file", str(file_path),
        "--profile", profile,
    ]
    if preview:
        cmd.append("--preview")

    logger.info("Running: %s", " ".join(cmd))
    # A large history can legitimately run long, so the timeout is generous and
    # tunable. Critically, TimeoutExpired/OSError must be CAUGHT: if it escaped,
    # scan_inbox would crash mid-loop, the file would never leave the inbox, and
    # launchd's WatchPaths would re-fire it forever — re-paying the extraction
    # API on every cycle. On timeout we return False so the file goes to failed/.
    timeout = int(os.environ.get("MEMORYBRIDGE_INGEST_TIMEOUT", "1800"))
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        logger.error("Ingestion timed out after %ds for %s — marking failed",
                     timeout, file_path.name)
        return False
    except OSError as e:
        logger.error("Ingestion subprocess could not run for %s: %s", file_path.name, e)
        return False
    if result.stdout:
        for line in result.stdout.strip().splitlines():
            logger.info("[run.py] %s", line)
    if result.stderr:
        for line in result.stderr.strip().splitlines():
            logger.warning("[run.py stderr] %s", line)

    return result.returncode == 0


# ---------------------------------------------------------------------------
# Main scan
# ---------------------------------------------------------------------------

def scan_inbox(inbox: Path, profile: str = "default", preview: bool = False,
               _dry_run: bool = False) -> dict:
    """
    Scan inbox for .json files, ingest each, move to processed/ or failed/.

    Args:
        inbox:     Directory to scan.
        profile:   Memory profile to write to.
        preview:   Pass --preview to run.py (no memory writes).
        _dry_run:  Test-only flag — skip subprocess, treat detected files as
                   processed immediately.

    Returns:
        Summary dict with "processed", "failed", "skipped" counts.
    """
    inbox.mkdir(parents=True, exist_ok=True)
    files = sorted(f for f in inbox.iterdir()
                   if f.is_file() and f.suffix.lower() == ".json")

    if not files:
        logger.info("Inbox empty — nothing to process.")
        return {"processed": 0, "failed": 0, "skipped": 0}

    processed = 0
    failed = 0
    skipped = 0

    for f in files:
        # Skip files that are still being written (leave them for the next run)
        # rather than parsing a truncated copy and dumping it into failed/.
        if not _dry_run and not _is_stable(f):
            logger.info("Skipping %s — still being written (size changing)", f.name)
            skipped += 1
            continue

        source = detect_source(f)
        if source is None:
            logger.warning("Cannot detect source for %s — moving to failed/", f.name)
            move_to_failed(f)
            failed += 1
            continue

        logger.info("Detected %s → %s", f.name, source)

        if _dry_run:
            # Test mode: skip subprocess, treat detection success as processed
            dest = move_to_processed(f)
            logger.info("Dry-run processed → %s", dest)
            processed += 1
            continue

        success = run_ingestion(source, f, profile=profile, preview=preview)
        if success:
            dest = move_to_processed(f)
            logger.info("Processed → %s", dest)
            processed += 1
        else:
            dest = move_to_failed(f)
            logger.warning("Ingestion failed → %s", dest)
            failed += 1

    return {"processed": processed, "failed": failed, "skipped": skipped}


def main():
    parser = argparse.ArgumentParser(
        description="MemoryBridge inbox watcher (single-shot, for launchd)"
    )
    parser.add_argument(
        "--inbox", default=str(DEFAULT_INBOX),
        help=f"Inbox directory to scan (default: {DEFAULT_INBOX})"
    )
    parser.add_argument(
        "--profile", default="default",
        help="Target memory profile (default: default)"
    )
    parser.add_argument(
        "--preview", action="store_true",
        help="Dry run — detect and log, but do not write memories"
    )
    args = parser.parse_args()

    import fcntl

    inbox = Path(args.inbox).expanduser()
    inbox.mkdir(parents=True, exist_ok=True)

    # Single-instance lock: launchd WatchPaths fires on every inbox mutation
    # (including our own processed/ renames), so a second watcher can start
    # while one is mid-ingestion and process the same file twice (double API
    # cost + duplicate writes, then a FileNotFoundError when the loser's rename
    # finds the file gone). Hold an exclusive lock for the run; if another
    # instance holds it, exit cleanly.
    lock_file = open(inbox / ".watcher.lock", "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, BlockingIOError):
        logger.info("Another watcher instance is running — exiting.")
        print(json.dumps({"processed": 0, "failed": 0, "skipped": 0,
                          "status": "locked"}))
        lock_file.close()
        return

    try:
        start = time.time()
        summary = scan_inbox(inbox, profile=args.profile, preview=args.preview)
        elapsed = time.time() - start
    finally:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()

    print(json.dumps(summary))
    logger.info(
        "Done in %.1fs — processed: %d, failed: %d",
        elapsed, summary["processed"], summary["failed"],
    )


if __name__ == "__main__":
    main()
