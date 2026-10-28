"""Model export, Memory Passport, and inbox ingestion MCP tools."""

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from exports import (
    export_for_model as _export_for_model_impl,
    export_passport as _export_passport_impl,
)
import server as _srv

mcp = _srv.mcp


@mcp.tool()
def export_for_model(
    model: str,
    profile: str = None,
    depth: str = "full",
    max_tokens: int = 2000
) -> str:
    """
    Export memory formatted for specific model, within token budget.

    Args:
        model: Target model (chatgpt, gemini, ollama)
        profile: Memory profile to export
        depth: Export depth (full, summary, minimal)
        max_tokens: Token budget for export (default 2000)
    """
    profile = profile or _srv._active_profile()
    return _export_for_model_impl(
        _srv._store, model, profile, depth=depth, max_tokens=max_tokens,
        log_analytics=_srv.log_to_analytics,
    )


@mcp.tool()
def export_passport(
    profile: str = None,
    max_tokens: int = 2000,
) -> str:
    """
    Export memory as a portable plain-text Memory Passport.

    The passport is model-agnostic — suitable for pasting into any AI's system
    prompt or context window.  Format: plain text, category sections, no JSON,
    no code fences.

    Args:
        profile:    Memory profile to export (default: "default")
        max_tokens: Token ceiling for the passport (default: 2000)
    Returns:
        Plain-text Memory Passport string.
    """
    profile = profile or _srv._active_profile()
    return _export_passport_impl(
        _srv._store, profile, max_tokens=max_tokens, log_analytics=_srv.log_to_analytics,
    )


@mcp.tool()
def ingest_from_inbox(
    profile: str = None,
    preview: bool = False
) -> str:
    """
    Process any export files sitting in ~/memorybridge/inbox/.

    Drop a ChatGPT conversations.json, Gemini MyActivity.json, or Claude
    export into that folder, then call this tool to ingest it.  Files are
    auto-detected by format, ingested via the standard pipeline, and moved
    to inbox/processed/ on success or inbox/failed/ on error.

    Args:
        profile: Memory profile to write to (default: "default")
        preview: If True, detect and report files without writing memories
    Returns:
        JSON with counts of processed/failed files and per-file results
    """
    profile = profile or _srv._active_profile()

    inbox = _srv.DATA_DIR / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)

    files = sorted(f for f in inbox.iterdir()
                   if f.is_file() and f.suffix.lower() == ".json"
                   and f.parent == inbox)

    if not files:
        return json.dumps({
            "status": "empty",
            "message": f"No files in inbox. Drop a ChatGPT, Gemini, or Claude export into {inbox}/ and call this again.",
            "inbox": str(inbox)
        }, indent=2)

    watcher_script = _srv.CODE_DIR / "ingestion" / "watcher.py"
    cmd = [
        sys.executable,
        str(watcher_script),
        "--inbox", str(inbox),
        "--profile", profile,
    ]
    if preview:
        cmd.append("--preview")

    start = datetime.now()
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=600,
            cwd=str(_srv.CODE_DIR),
            env={
                **os.environ,
                "HOME": str(Path.home()),
                "MEMORYBRIDGE_DATA": str(_srv.DATA_DIR),
                "PATH": os.environ.get(
                    "PATH",
                    "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
                ),
            }
        )
        elapsed = (datetime.now() - start).total_seconds()

        stdout_lines = result.stdout.strip().splitlines() if result.stdout else []
        stderr_lines = result.stderr.strip().splitlines() if result.stderr else []

        processed = failed = skipped = 0
        for line in stdout_lines:
            try:
                summary = json.loads(line)
                if isinstance(summary, dict) and "processed" in summary:
                    processed = summary.get("processed", 0)
                    failed = summary.get("failed", 0)
                    skipped = summary.get("skipped", 0)
                    break
            except (json.JSONDecodeError, ValueError):
                continue

        _srv._store.log_access("ingest_from_inbox", profile,
                               f"files={len(files)}, processed={processed}, failed={failed}")

        return json.dumps({
            "status": "ok" if result.returncode == 0 else "error",
            "files_found": len(files),
            "files_processed": processed,
            "files_failed": failed,
            "elapsed_seconds": round(elapsed, 1),
            "preview": preview,
            "profile": profile,
            "log": stdout_lines[-20:] if stdout_lines else [],
            "errors": stderr_lines[-10:] if stderr_lines else [],
            "exit_code": result.returncode,
        }, indent=2)

    except subprocess.TimeoutExpired:
        return json.dumps({
            "status": "timeout",
            "message": "Ingestion timed out after 600s — large export? Try running manually: python ingestion/watcher.py",
            "files_found": len(files),
        }, indent=2)
    except Exception as e:
        return json.dumps({
            "status": "error",
            "message": str(e),
            "files_found": len(files),
        }, indent=2)
