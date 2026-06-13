# memorybridge

Local-first MCP memory server (FastMCP v2). Cross-model memory portability via SQLite + FTS5.

## Quick commands

- `fastmcp run server.py` — start server
- `pip install -r requirements.txt` — install deps
- `python -m pytest tests/` — run tests

## Architecture

- **server.py** — FastMCP MCP server entry point
- **db/** — SQLite persistence layer (WAL mode, FTS5 search)
- **ingestion/** — markdown file watcher pipeline
- **scripts/** — admin/utility scripts
- **ui/** — optional web interface

## Rules

- Do not modify database schema without checking `db/` module
- Run tests after touching core storage logic
- Keep server backward-compatible (no breaking API changes across phases)
