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
- **Inbox watcher** — drop an export file into `~/memorybridge/inbox/` and launchd ingests it automatically; zero CLI required
- **Cross-model export** — Memory Passport (plain text, any AI) or model-specific formats (ChatGPT, Gemini, Ollama)
- **Streamlit UI** — review flagged memories, browse/delete, see analytics, run imports and exports

---

## Requirements

- Python 3.10+
- Claude desktop app
- macOS (launchd inbox watcher is macOS-only; everything else works on Linux/WSL)
- API keys: `DEEPSEEK_API_KEY` (extraction) and `ANTHROPIC_API_KEY` (conflict resolution)

---

## Installation

### 1. Clone and install dependencies

```bash
git clone https://github.com/your-username/memorybridge.git
cd memorybridge
pip3 install -r requirements.txt
```

### 2. Add API keys

Create `~/memorybridge/.env`:

```properties
DEEPSEEK_API_KEY=your_key_here
ANTHROPIC_API_KEY=your_key_here
```

### 3. Register with Claude desktop

Open `~/Library/Application Support/Claude/claude_desktop_config.json` (create it if missing) and add:

```json
{
  "mcpServers": {
    "memorybridge": {
      "command": "python3",
      "args": ["/path/to/memorybridge/server.py"]
    }
  }
}
```

Use the absolute path to `server.py` in your clone. Run `pwd` inside the repo to get it.

### 4. Restart Claude

Fully quit Claude (⌘Q) and relaunch. Ask it: *"Can you check if MemoryBridge is connected?"*

Full walkthrough in [SETUP_GUIDE.md](SETUP_GUIDE.md).

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
python ingestion/run.py --source claude --file ~/Downloads/conversations.json --days 30

# ChatGPT — import all history into the "work" profile
python ingestion/run.py --source chatgpt --file ~/Downloads/conversations.json --profile work

# Gemini — dry run first to see what would be extracted
python ingestion/run.py --source gemini --file ~/Downloads/MyActivity.json --preview
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

Drop any export file into `~/memorybridge/inbox/` and launchd detects it, auto-identifies the source (Claude/ChatGPT/Gemini), and runs ingestion automatically. Processed files move to `inbox/processed/`, failed ones to `inbox/failed/`.

Install the launchd agent once:

```bash
cp launchd/com.memorybridge.inbox.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.memorybridge.inbox.plist
```

---

## Streamlit UI

```bash
streamlit run ui/app.py
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
Generated: 2026-05-10

## Constraints
! Cannot scrape LinkedIn via automated tools

## Preferences
! No em dashes in writing
! Tone: direct, sardonic, Gen X peer-to-peer
- Prefers dark mode in all apps
- Prefers bullet-point answers

## Skills
! PMP certified
! Has $126M in documented program impact
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
  pages/               — flagged_queue, memory_browser, analytics, portability
launchd/
  com.memorybridge.inbox.plist  — WatchPaths agent
tests/
  integration/         — MCP tool integration tests
  unit/                — Store, search quality, parsers, passport, watcher
  ui/                  — Flagged queue business logic
```

**Search stack:** FTS5 BM25 + FastEmbed `BAAI/bge-small-en-v1.5` semantic embeddings (numpy cosine similarity) + Reciprocal Rank Fusion. Embeddings stored as JSON in SQLite — no native extension required.

---

## Tests

```bash
python -m pytest tests/ -v
```

86 tests, 0 failures.

---

## Troubleshooting

### Claude doesn't see memory tools

- Fully quit and relaunch Claude (⌘Q — closing the window isn't enough)
- Verify the path in `claude_desktop_config.json` is the absolute path to `server.py`
- Run `python3 server.py` in Terminal directly — fix any errors before restarting Claude

### "Module not found" error

- Run `pip3 install -r requirements.txt` from the repo root
- If you have multiple Python versions, confirm which one Claude's config points to: `which python3`

### Inbox watcher isn't firing

- Check `launchctl list | grep memorybridge` — `com.memorybridge.inbox` should appear
- Inspect `~/memorybridge/logs/watcher_err.log` for errors
- Verify the plist paths match your actual username: `plutil -lint ~/Library/LaunchAgents/com.memorybridge.inbox.plist`

---

*Local-first storage. No telemetry. Third-party APIs (DeepSeek/Anthropic/Notion) are used only by the optional ingestion pipeline when you configure them.*
