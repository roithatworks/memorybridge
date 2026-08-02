"""Sandboxed workspace-file tools (F1) for non-Claude-Code MCP clients.

ChatGPT, Gemini, and Perplexity have no filesystem of their own — they only
see what an MCP tool hands them. These five tools give any MCP client a
scratch/reference filesystem rooted at ``config.workspace_root()``, with a
path-traversal guard shared by all of them and a deny-by-default write
allowlist (``workspace_write_allowed``).

F4 (remote write guard) lives here too rather than as a follow-on: a write
tool with no remote guard is a standing vulnerability the moment the HTTP
bridge (F2/F3) exposes it to the internet. This module mirrors server.py's
``_REMOTE_MODE`` global-flag pattern instead of importing ``server`` (which
would create a circular import, since these tools are registered *from*
server.py).

See docs/MULTI_MODEL_FEATURES.md (F1, F4) for the full design writeup.
"""
from __future__ import annotations

from pathlib import Path

import config

# --------------------------------------------------------------------------- #
# Remote-mode flag — set by server.py's _run_http(), mirrors _REMOTE_MODE
# there. Kept as a plain module global (not imported from server) to avoid
# a circular import.
# --------------------------------------------------------------------------- #
_REMOTE_MODE = False


def set_remote_mode(value: bool) -> None:
    """Called by server.py's _run_http() when serving over the HTTP bridge."""
    global _REMOTE_MODE
    _REMOTE_MODE = value


class WorkspaceError(Exception):
    """Raised when a requested path resolves outside the workspace root."""


# --------------------------------------------------------------------------- #
# Shared guards
# --------------------------------------------------------------------------- #

def _resolve(rel_path: str) -> Path:
    """Resolve rel_path against the workspace root, blocking any escape.

    Handles both literal `..` traversal and symlink escapes by comparing
    against the *resolved* root.
    """
    root = config.workspace_root().resolve()
    candidate = (root / rel_path).resolve()
    if not candidate.is_relative_to(root):
        raise WorkspaceError(f"path escapes workspace root: {rel_path!r}")
    return candidate


def _is_write_allowed(rel_path: str) -> bool:
    """Deny-by-default: only paths matching a `workspace_write_allowed` prefix."""
    allowed = config.load_config().get("workspace_write_allowed", [])
    return any(rel_path.startswith(prefix) for prefix in allowed)


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #

def ws_status() -> dict:
    """Report the workspace root, file count, and current write allowlist."""
    root = config.workspace_root()
    if not root.is_dir():
        return {"error": f"WORKSPACE_ROOT not found: {root}"}
    files = [p for p in root.rglob("*") if p.is_file()]
    return {
        "root": str(root),
        "file_count": len(files),
        "write_allowed": config.load_config().get("workspace_write_allowed", []),
    }


def ws_list(path: str = "", recursive: bool = False) -> dict:
    """List entries under `path` (relative to the workspace root)."""
    target = _resolve(path)
    if not target.is_dir():
        return {"error": f"not a directory: {path}"}
    pattern = "**/*" if recursive else "*"
    root = config.workspace_root()
    entries = sorted(str(p.relative_to(root)) for p in target.glob(pattern))
    return {"path": path, "entries": entries}


def ws_read(path: str) -> dict:
    """Read a text file at `path`. Binary files return a structured error."""
    target = _resolve(path)
    if not target.is_file():
        return {"error": f"not a file: {path}"}
    try:
        return {"path": path, "content": target.read_text(encoding="utf-8")}
    except UnicodeDecodeError:
        return {"error": f"binary file, cannot read as text: {path}"}


def ws_write(path: str, content: str, overwrite: bool = False) -> dict:
    """Write `content` to `path`, subject to the write allowlist and remote guard.

    F4: in remote mode, `overwrite=True` is required even for a brand-new
    file — the remote guard is about the caller being unable to prove intent,
    not about whether the target already exists.
    """
    if _REMOTE_MODE and not overwrite:
        return {"error": "remote writes require overwrite=True"}
    if not _is_write_allowed(path):
        return {"error": f"write not allowed under path: {path}"}
    target = _resolve(path)
    if target.exists() and not overwrite:
        return {"error": f"exists, pass overwrite=True to replace: {path}"}
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return {"path": path, "bytes_written": len(content.encode("utf-8"))}


def ws_search(query: str, path: str = "") -> dict:
    """Search for `query` (plain substring) in text files under `path`."""
    target = _resolve(path)
    matches = []
    root = config.workspace_root()
    candidates = target.rglob("*") if target.is_dir() else [target]
    for p in candidates:
        if not p.is_file():
            continue
        try:
            if query in p.read_text(encoding="utf-8"):
                matches.append(str(p.relative_to(root)))
        except UnicodeDecodeError:
            continue
    return {"query": query, "matches": matches}
