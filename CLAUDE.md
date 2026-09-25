# memorybridge

Local-first MCP memory server (FastMCP 3.x). Cross-model memory portability via SQLite + FTS5.

## Quick commands

Use the repo's `.venv` (Python 3.12 + the pinned fastmcp). A bare `python` may
resolve to an interpreter with fastmcp 2.x, which requirements.txt rejects for
CVE reasons — a green run there proves nothing.

- `fastmcp run server.py` — start server
- `uv pip install --python .venv/bin/python -r requirements.txt -r requirements-dev.txt` — install deps
- `.venv/bin/python -m pytest tests/ -q` — run tests

## Architecture

- **server.py** — FastMCP MCP server entry point
- **db/** — SQLite persistence layer (WAL mode, FTS5 search)
- **ingestion/** — JSON conversation-export ingestion (Claude/ChatGPT/Gemini/Hermes) via DeepSeek extraction + Anthropic resolver; a watcher automatically polls the inbox for new export files via a launchd agent (`com.memorybridge.ingestion-watcher`)
- **scripts/** — admin/utility scripts
- **ui/** — optional web interface

## Rules

- Do not modify database schema without checking `db/` module
- Run tests after touching core storage logic
- Keep server backward-compatible (no breaking API changes across phases)
- Never add a tool to REMOTE_ALLOWED_TOOLS (server.py) without explicit review — remote/HTTP-bridge callers are restricted to read-only tools by design
