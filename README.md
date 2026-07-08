# MemoryBridge

Persistent, local-first memory for Claude — and every other AI you use.

Every Claude session starts from zero. MemoryBridge fixes that. It runs a lightweight local MCP server that gives Claude a set of memory tools via the Model Context Protocol. Tell it something once, and it remembers — across sessions, profiles, and AI models.

Your memory database lives in SQLite on your machine — the store and stdio serving are fully local. The **optional** ingestion pipeline is the one exception: extracting durable facts from ChatGPT/Gemini/Claude exports sends that conversation content to the DeepSeek and Anthropic APIs, and can sync flagged facts to Notion, only when you configure those keys.

---

## What it does

- **Persistent memory** across conversations — Claude knows your name, preferences, and projects without re-explaining
- **Hybrid search** — BM25 full-text + semantic embeddings + Reciprocal Rank Fusion find the right memories even on paraphrased queries
- **Token-budget aware** — smart retrieval fills Claude's context window without going over; older memories are compressed or archived automatically
- **Decay scoring** — memories fade naturally over time unless accessed frequently or marked important
- **Multiple profiles** — separate memory contexts for work, personal, research, etc.
- **Ingestion pipeline** — import conversation history from Claude, ChatGPT, or Gemini; DeepSeek R1 extracts facts, Claude resolves conflicts
- **Inbox watcher** — drop an export file into `<data dir>/inbox/` and it's ingested automatically on the next scan; zero CLI required
- **Cross-model export** — Memory Passport (plain text, any AI) or model-specific formats (ChatGPT, Gemini, Ollama)
- **Streamlit UI** — review flagged memories, browse/delete, see analytics, run imports and exports

---

## Requirements

- Python 3.11+
- An MCP client (Claude Desktop, or any MCP-compatible tool)
- Works on macOS, Linux, and Windows. The core memory server needs **no API keys**.
- API keys are only needed for the **optional** ingestion pipeline: `DEEPSEEK_API_KEY` (fact extraction) and `ANTHROPIC_API_KEY` (conflict resolution).

---

## Install

```bash
pip install memorybridge          # or: git clone … && pip install -e .
mb init                           # creates the data dir, config, empty store
```

`mb init` prints a ready-to-paste Claude Desktop snippet. Open **Settings →
Developer → Edit Config** and add it:

```json
{
  "mcpServers": {
    "memorybridge": {
      "command": "mb",
      "args": ["serve"],
      "env": { "MEMORYBRIDGE_DATA": "/Users/you/memorybridge" }
    }
  }
}
```

Restart your MCP client and ask: *"Can you check if MemoryBridge is connected?"*

To expose the server to other MCP clients over HTTP, run `mb serve --http` (see
[docs/DEPLOY.md](docs/DEPLOY.md) for running it as a background service on
macOS/Linux/Windows/Docker).

### API keys (optional)

Only needed for ingestion. `mb init` writes a `.env` template in your data dir —
fill in the keys you use:

```properties
DEEPSEEK_API_KEY=your_key_here     # fact extraction
ANTHROPIC_API_KEY=your_key_here    # conflict resolution
```

---

## Configuration

Everything works with zero config. To customize, edit `memorybridge.yaml` in
your data dir (`mb init` writes a starter; full documented schema in
[memorybridge.example.yaml](memorybridge.example.yaml)).

| Setting | What it does |
| --- | --- |
| `max_total_tokens` | Token ceiling the store serves/holds (default 50000). Also `MEMORYBRIDGE_MAX_TOKENS`. |
| `routing.domains` | Split memory by area. Each fact is scored against your keyword sets; best match wins, else `default`. **Off by default** — everything goes to `default` until you add domains. |
| `routing.default_keywords` | Facts matching these are forced to `default` (cross-cutting identity/voice). |
| `routing.anchors` | Pull a `default`-signal fact into a specific domain when it also mentions an anchor term. |
| `routing.custom_router` | Escape hatch: path to a Python file exporting `route_profile(fact, base_profile)` for logic that isn't keyword-expressible. |
| `noise_patterns` | Extra regex whose matching lines are dropped at ingestion as ephemeral noise (added to the built-in set). |

**Entities.** Drop an `entities.json` in your data dir to tag your own projects,
people, and concepts (`{"entities": [{"tag": "acme", "names": ["Acme Corp"], "type": "project"}]}`).
It merges with a small generic built-in seed.

**Key environment variables:** `MEMORYBRIDGE_DATA` (data dir, default `~/memorybridge`),
`MEMORYBRIDGE_MAX_TOKENS`, `MEMORYBRIDGE_PORT` (HTTP bridge), `MEMORYBRIDGE_TOKEN`
(HTTP bridge auth), `MEMORYBRIDGE_NO_EMBED` (skip the embedding model).

---

## MCP Tools

Claude calls these automatically — no special syntax required.

| Tool | Description |
| --- | --- |
| `get_memory` | Retrieve memories within a token budget, ranked by decay-adjusted relevance |
| `add_memory` | Add a single memory with category and importance |
| `add_memories` | Batch-add multiple facts in one call (does not mutate existing rows) |
| `edit_memory` | Edit content, importance, category, or project association of an existing memory in place |
| `search_memory` | Hybrid BM25 + semantic search (default: limit=5, max_tokens=800) |
| `delete_memory` | Remove a memory by ID |
| `get_token_stats` | Token usage breakdown by profile |
| `prune_memories` | Archive low-score memories to free budget |
| `switch_profile` | Change active memory profile |
| `list_projects` | List projects in a profile |
| `get_access_log` | Recent read/write history with token stats |
| `export_for_model` | Export formatted for ChatGPT, Gemini, or Ollama |
| `export_passport` | Export a plain-text Memory Passport (works with any AI) |

---

## Ingestion — import your conversation history

Export your history from any AI provider, then run:

```bash
# Claude — import last 30 days
mb ingest --source claude --file ~/Downloads/conversations.json --days 30

# ChatGPT — import all history into the "work" profile
mb ingest --source chatgpt --file ~/Downloads/conversations.json --profile work

# Gemini — dry run first to see what would be extracted
mb ingest --source gemini --file ~/Downloads/MyActivity.json --preview
```

The pipeline:

```text
Export file → parse → DeepSeek R1 extraction → confidence routing → conflict resolution → memory write
```

- **≥ 0.85 confidence** — written automatically
- **0.60-0.84 confidence** — queued in `~/memorybridge/flagged_queue.json` for review in the UI
- **Conflicts / relationship facts / project status** — escalated to Claude for a verdict
- **Duplicates** — caught by content hash before writing

### Inbox watcher (zero-CLI ingestion)

Drop any export file into `<data dir>/inbox/` and the watcher auto-identifies
the source (Claude/ChatGPT/Gemini) and runs ingestion. Processed files move to
`inbox/processed/`, failed ones to `inbox/failed/`; unrecognized files are left
in place and logged.

Run the watcher on a schedule with your OS's scheduler — launchd on macOS
(templates in `launchd/`), a systemd timer or cron on Linux. See
[docs/DEPLOY.md](docs/DEPLOY.md).

---

## Streamlit UI

```bash
mb ui
```

Four pages:

| Page | Purpose |
| --- | --- |
| Flagged Queue | Review extractions with 60-84% confidence — accept or reject |
| Memory Browser | Filter, search, sort, and delete memories |
| Analytics | Token usage trends, operation breakdown, baseline comparison |
| Portability | Import exports, generate model-specific exports, download Memory Passport |

---

## Memory Passport

A portable plain-text snapshot of your memory — paste it into any AI's system prompt.

```markdown
# Memory Passport
Profile: default
Generated: 2026-07-08

## Preferences
! Tone: direct and concise
- Prefers dark mode in all apps
- Prefers bullet-point answers

## Skills
! Ships production Python daily
- Comfortable with SQL and data modeling

## Projects
- Building an internal analytics dashboard
...
```

Generate via Claude: *"Export my memory passport"* — or use the Portability tab in the UI.

---

## Data

All data is local:

| Path | Contents |
| --- | --- |
| `~/memorybridge/memory.db` | SQLite database — memories, profiles, FTS index, embeddings |
| `~/memorybridge/analytics.json` | Token usage stats |
| `~/memorybridge/flagged_queue.json` | Pending manual review queue |
| `~/memorybridge/logs/` | Per-run ingestion reports, watcher log |
| `~/memorybridge/inbox/` | Drop exports here for auto-ingestion |

---

## Code / Data Split

MemoryBridge keeps code and runtime data in separate locations so the repo stays clean and data survives a re-clone.

**Code** — lives in the GitHub repo (wherever you cloned it). Contains `server.py`, `db/`, `ingestion/`, `ui/`, `launchd/`, `tests/`, and config templates.

**Data** — lives in `~/memorybridge/` by default:

| Path | Contents |
| --- | --- |
| `~/memorybridge/memory.db` | SQLite database — memories, profiles, FTS index, embeddings |
| `~/memorybridge/analytics.json` | Token usage stats |
| `~/memorybridge/inbox/` | Drop export files here for auto-ingestion |
| `~/memorybridge/logs/` | Per-run ingestion reports, watcher log |
| `~/memorybridge/.env` | Your API keys and config — never committed |
| `~/memorybridge/instance.pid` | PID of the running server process (auto-managed) |

To relocate the data directory, set `MEMORYBRIDGE_DATA` in your environment before launching:

```bash
export MEMORYBRIDGE_DATA=/Volumes/external/memorybridge
```

Or add it to your shell profile (`~/.zprofile`, `~/.bashrc`, etc.).

---

## Server Lifecycle

`server.py` is spawned by the Claude desktop app once per session via the MCP `mcpServers` config. It exits automatically when the session ends — via SIGTERM, stdin EOF, or parent-death watchdog (detects PPID = 1).

**Never run `server.py` under a launchd `KeepAlive` job.** The watchdog kills the process when its parent (Claude) exits; launchd would immediately restart it, creating a respawn loop. The server is session-scoped by design.

Retired launchd job plists are archived in `~/memorybridge/launchd-retired/` for reference. Only the inbox watcher plist (`launchd/com.memorybridge.inbox.plist`) should be active.

---

## Architecture

```text
server.py              — FastMCP server, 12 MCP tools
db/store.py            — SQLite persistence, FTS5, embeddings, WAL mode
db/schema.sql          — Schema with content-hash dedup index
ingestion/
  run.py               — CLI ingestion pipeline
  watcher.py           — Inbox scanner (called by launchd)
  parse_claude.py      — Claude export parser
  parse_chatgpt.py     — ChatGPT export parser
  parse_gemini.py      — Gemini export parser (Format A + B)
  extractor.py         — DeepSeek R1 fact extraction
  router.py            — Confidence routing (accept/flag/escalate)
  resolver.py          — Claude conflict resolution
  merger.py            — Writes accepted facts via MCP tools
  passport.py          — Memory Passport builder
ui/
  app.py               — Streamlit entry point
  views/               — flagged_queue, memory_browser, analytics, portability
cli.py                 — the `mb` command (init / serve / ingest / ui)
config.py              — central config loader (memorybridge.yaml)
launchd/               — optional macOS service templates
tests/
  integration/         — MCP tool integration tests
  unit/                — Store, search quality, parsers, passport, watcher
  ui/                  — Flagged queue business logic
```

**Search stack:** FTS5 BM25 + FastEmbed `BAAI/bge-small-en-v1.5` semantic embeddings (numpy cosine similarity) + Reciprocal Rank Fusion. Embeddings stored as JSON in SQLite — no native extension required.

---

## Tests

```bash
pip install -e ".[dev]"
python -m pytest tests/unit -q
```

The gating unit suite runs in CI on Python 3.11 and 3.12.

---

## Troubleshooting

### Claude doesn't see memory tools

- Fully quit and relaunch your MCP client (on Claude Desktop, ⌘Q — closing the window isn't enough)
- Verify the config snippet uses `"command": "mb"` and that `mb` is on your PATH (`which mb`)
- Run `mb serve` in a terminal directly — fix any errors before restarting the client

### "Command not found: mb" or "Module not found"

- Reinstall: `pip install -e .` from the repo root (or `pip install memorybridge`)
- If you use multiple Python versions, confirm `mb` resolves to the same interpreter you installed into

### Inbox watcher isn't firing

- Confirm your scheduler (launchd/systemd/cron) is actually invoking the watcher — see [docs/DEPLOY.md](docs/DEPLOY.md)
- Inspect `<data dir>/logs/` for errors
- Unrecognized (non-`.json`) files are skipped and logged, not processed

---

*Local-first storage. No telemetry. Third-party APIs (DeepSeek/Anthropic/Notion) are used only by the optional ingestion pipeline when you configure them.*
