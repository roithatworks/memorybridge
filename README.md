# MemoryBridge

Persistent, local memory for Claude — across every conversation.

Every Claude session starts from zero. MemoryBridge fixes that. It runs a lightweight local server that gives Claude a set of memory tools via the Model Context Protocol (MCP). Tell Claude something once, and it remembers — across sessions, profiles, and even other AI models.

Everything is stored in a plain JSON file on your machine. Nothing goes to the cloud.

---

## Features

- **Persistent memory** across conversations — Claude knows your name, preferences, and projects without you re-explaining
- **Local & private** — stored in `~/memorybridge/memory.json`, no telemetry, no cloud sync
- **Token-budget aware** — smart retrieval that fits memories into Claude's context window; older or low-relevance memories are compressed or archived automatically
- **Decay scoring** — memories fade naturally over time unless they're accessed frequently or marked important
- **Multiple profiles** — separate memory contexts for work, personal, research, etc.
- **Cross-model export** — export memories formatted for ChatGPT, Gemini, or Ollama
- **Concurrent-safe** — file locking ensures Claude Desktop and Claude Code can run simultaneously without dropping data

---

## Requirements

- Python 3.10+
- Claude desktop app
- macOS, Linux, or Windows (WSL)

---

## Installation

**1. Put the files in place**

```bash
mkdir ~/memorybridge
cp server.py ~/memorybridge/
cp requirements.txt ~/memorybridge/
```

**2. Install dependencies**

```bash
pip3 install -r ~/memorybridge/requirements.txt
```

Dependencies: `fastmcp` and `tiktoken`. If you get a permissions error, add `--user` or `--break-system-packages`.

**3. Register with Claude desktop**

Open `~/Library/Application Support/Claude/claude_desktop_config.json` (create it if it doesn't exist) and add:

```json
{
  "mcpServers": {
    "memorybridge": {
      "command": "python3",
      "args": ["/Users/YOUR_USERNAME/memorybridge/server.py"]
    }
  }
}
```

Replace `YOUR_USERNAME` with your actual username (`whoami` in Terminal). If you already have other MCP servers configured, add the `memorybridge` block alongside them.

**4. Restart Claude**

Fully quit the Claude desktop app (⌘Q) and relaunch it.

**5. Verify**

Start a new conversation and ask:

> "Can you check if MemoryBridge is connected?"

Or try adding your first memory:

> "Add a memory: I prefer bullet-point answers."

Full setup walkthrough in [SETUP_GUIDE.md](SETUP_GUIDE.md).

---

## Usage

Once connected, just talk to Claude naturally:

```
"Remember that my manager is Sarah and she values concise updates."
"What do you know about my projects?"
"Search my memories for anything about marketing."
"Add a memory: I'm working on a project called Phoenix."
"Export my memories for ChatGPT."
"Show me my memory stats."
"Delete the memory about X."
```

Claude calls the memory tools automatically — no special syntax required.

---

## MCP Tools

| Tool | Description |
|---|---|
| `get_memory` | Retrieve memories within a token budget |
| `add_memory` | Add a single memory with category and importance |
| `update_memory` | Batch-add multiple facts in one save operation |
| `search_memory` | Full-text search across memories |
| `delete_memory` | Remove a memory by ID |
| `get_token_stats` | Token usage breakdown by profile |
| `prune_memories` | Archive low-score memories to free token budget |
| `switch_profile` | Change active memory profile |
| `list_projects` | List projects in a profile |
| `get_access_log` | Recent read/write history with token stats |
| `export_for_model` | Export memories formatted for ChatGPT, Gemini, or Ollama |

---

## Data

All data is local:

- `~/memorybridge/memory.json` — your memories, profiles, and access log
- `~/memorybridge/analytics.json` — token usage stats (buffered writes, flushed every 10 operations)

Both files are plain JSON — human-readable and easy to back up.

---

## Profiles

Profiles let you maintain separate memory contexts. Examples: `work`, `personal`, `research`. Claude can switch between them mid-conversation. Memories, projects, identity, and model preferences are all scoped per profile.

---

## How Token Budgeting Works

Each memory has a `token_count` computed by `tiktoken` (the same BPE tokenizer used by Claude and GPT models). When `get_memory` is called with a `max_tokens` budget, it:

1. Measures the overhead of identity, projects, and model preferences
2. Fills the remaining budget with memories ranked by decay-adjusted relevance score
3. Compresses or truncates lower-priority memories to fit more in

Memories that fall below a relevance threshold (default `0.15`) are automatically archived rather than deleted, keeping your active memory lean.

---

## Troubleshooting

**Claude doesn't see memory tools**
- Fully quit and relaunch Claude (close the window isn't enough — use ⌘Q)
- Check that the path in `claude_desktop_config.json` is correct (`whoami` gives your username)
- Run `python3 ~/memorybridge/server.py` in Terminal — fix any errors that appear

**"Module not found" error**
- Run `pip3 install fastmcp tiktoken` and retry
- If you have multiple Python versions, use the full path to the right interpreter: `which python3`

**Config file not found**
- Default location: `~/Library/Application Support/Claude/claude_desktop_config.json`
- If missing, create it using the template in `claude_desktop_config_snippet.json`

---

*Local-first. No telemetry. No cloud. Shared freely.*
