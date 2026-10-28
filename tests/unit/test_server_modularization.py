"""Unit tests verifying the modularized server architecture (#215)."""

import os
from pathlib import Path

os.environ.setdefault("MEMORYBRIDGE_NO_EMBED", "1")

import cli
import server
import server.middleware
import server.tools.memory
import server.tools.graph
import server.tools.workspace
import server.tools.ingestion


def test_server_entry_point_path_resolution():
    """cli._module_path('server') must resolve to the top-level server.py file."""
    path = Path(cli._module_path("server"))
    assert path.name == "server.py"
    assert path.is_file()


def test_server_middleware_reexports():
    """ASGI middleware symbols live in server.middleware and are re-exported on server."""
    for sym in (
        "REMOTE_ALLOWED_TOOLS",
        "_gate_tools_for_remote",
        "_send_plain",
        "_RateLimitAuthMiddleware",
        "_run_http",
    ):
        assert hasattr(server.middleware, sym)
        assert getattr(server, sym) is getattr(server.middleware, sym)


def test_server_tool_domain_modules_and_reexports():
    """All 25 MCP tools are grouped into domain submodules and re-exported on server."""
    domain_map = {
        server.tools.memory: [
            "get_memory",
            "add_memory",
            "add_memories",
            "edit_memory",
            "search_memory",
            "reflect",
            "consolidate_session",
            "consolidate_memories",
            "delete_memory",
            "bulk_delete",
            "get_token_stats",
            "prune_memories",
            "list_profiles",
            "switch_profile",
            "list_projects",
            "get_access_log",
            "get_prune_queue",
            "resolve_prune_queue",
        ],
        server.tools.graph: [
            "add_memory_edge",
            "get_memory_edges",
            "traverse_memory_graph",
            "mine_memory_relations",
            "get_memory_provenance",
        ],
        server.tools.workspace: [
            "ws_status",
            "ws_list",
            "ws_read",
            "ws_write",
            "ws_search",
        ],
        server.tools.ingestion: [
            "export_for_model",
            "export_passport",
            "ingest_from_inbox",
        ],
    }

    for mod, tools in domain_map.items():
        for name in tools:
            assert hasattr(mod, name), f"{mod.__name__} missing {name}"
            tool_obj = getattr(mod, name)
            assert getattr(server, name) is tool_obj
            assert hasattr(tool_obj, "fn")
