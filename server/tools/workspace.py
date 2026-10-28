"""Sandboxed workspace filesystem MCP tools."""

import json
import workspace
import server as _srv

mcp = _srv.mcp


@mcp.tool()
def ws_status() -> str:
    """
    Report the workspace root, file count, and current write allowlist.

    Returns:
        JSON with root path, file_count, and write_allowed prefixes.
    """
    return json.dumps(workspace.ws_status(), indent=2)


@mcp.tool()
def ws_list(path: str = "", recursive: bool = False) -> str:
    """
    List entries under the workspace (or a subdirectory of it).

    Args:
        path: Subdirectory relative to the workspace root (default: root)
        recursive: If True, list all nested entries, not just the top level
    Returns:
        JSON with the listed path and its entries.
    """
    return json.dumps(workspace.ws_list(path, recursive=recursive), indent=2)


@mcp.tool()
def ws_read(path: str) -> str:
    """
    Read a text file from the workspace.

    Args:
        path: File path relative to the workspace root
    Returns:
        JSON with the file content, or a structured error (not found, binary).
    """
    return json.dumps(workspace.ws_read(path), indent=2)


@mcp.tool()
def ws_write(path: str, content: str, overwrite: bool = False) -> str:
    """
    Write a text file to the workspace. Deny-by-default: only paths under a
    configured `workspace_write_allowed` prefix may be written.

    Over the HTTP bridge (remote mode), overwrite=True is required even for
    a brand-new file — see F4 in docs/MULTI_MODEL_FEATURES.md.

    Args:
        path: File path relative to the workspace root
        content: Text content to write
        overwrite: Required to replace an existing file (or, remotely, to write at all)
    Returns:
        JSON with bytes_written, or a structured error.
    """
    return json.dumps(workspace.ws_write(path, content, overwrite=overwrite), indent=2)


@mcp.tool()
def ws_search(query: str, path: str = "") -> str:
    """
    Search workspace text files for a substring match.

    Args:
        query: Plain substring to search for
        path: Subdirectory relative to the workspace root (default: whole root)
    Returns:
        JSON with the query and the list of matching file paths.
    """
    return json.dumps(workspace.ws_search(query, path), indent=2)
