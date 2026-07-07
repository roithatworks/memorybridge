# Security Policy

## Supported Versions

MemoryBridge has no versioned releases — the `main` branch is the supported version. Security fixes are applied there directly.

## Architecture & attack surfaces

MemoryBridge runs in one of two transports (set by `MEMORYBRIDGE_TRANSPORT`):

- **stdio (default)** — a local Python process launched by the Claude desktop app, speaking MCP over stdio. No network listener.
- **HTTP bridge (opt-in, `http`)** — binds `127.0.0.1:8484` and is intended to be exposed to the internet via a Cloudflare tunnel so remote MCP clients (ChatGPT, Perplexity) can reach it. This is a real remote attack surface; see below.

Storage is a **SQLite database** (WAL mode) at `~/memorybridge/memory.db` (override the data dir with `MEMORYBRIDGE_DATA`). Concurrency between multiple local clients is handled by SQLite's `busy_timeout` + WAL, not file locks.

**Third-party APIs:** the memory store is local, but the **optional ingestion pipeline** sends conversation-export content to the DeepSeek (extraction) and Anthropic (conflict resolution) APIs, and can sync flagged facts to Notion — only when you configure those keys.

## HTTP bridge security

Because the bridge is designed to sit behind a public tunnel, it is hardened:

- **Capability-URL auth.** The only credential is a secret embedded in the request path (`/{MEMORYBRIDGE_TOKEN}/mcp`). The server refuses to start HTTP without a token of at least 32 characters. Generate a strong one:
  ```bash
  python3 -c "import secrets; print(secrets.token_urlsafe(32))"
  ```
  The token is compared in constant time (`secrets.compare_digest`) and is kept out of the access log.
- **Per-IP rate limiting** (`MEMORYBRIDGE_RATE_LIMIT`/`MEMORYBRIDGE_RATE_WINDOW`, default 120/60s) blunts brute-force of the token; excess requests get `429`.
- **Tool gating.** Remote clients are restricted to a read + add allowlist (`get_memory`, `search_memory`, `reflect`, `add_memory`, `list_projects`, `export_passport`). Destructive and subprocess-spawning tools (`edit_memory`, `delete_memory`, `prune_memories`, `ingest_from_inbox`, `switch_profile`, …) are removed before serving, and gating **fails closed** — the server aborts rather than serve them if the tool set can't be enumerated.
- **No cross-session profile bleed.** Over the bridge, a profile-omitted call resolves to the default profile, not a shared mutable global.

## Other considerations

- **Prompt injection.** Memory content and ingested conversation text are untrusted (writable via `add_memory`, or supplied in third-party exports). They flow into LLM prompts (extraction, conflict resolution, `reflect`) wrapped in explicit untrusted-data delimiters with instructions to treat them as data, not instructions. Extracted facts are field-validated before storage.
- **Ingestion input.** The inbox watcher ignores symlinks (no arbitrary-file read into an API prompt) and skips files that are still being written. Embedding-model failure fails the ingest by default rather than silently degrading dedup (`MEMORYBRIDGE_ALLOW_DEGRADED=1` to override).
- **File permissions.** `memory.db` is created with your user's default permissions and is unencrypted. Protect your home directory; avoid storing secrets as memories. `chmod 600 ~/memorybridge/memory.db` if desired.
- **Streamlit UI.** The review UI (`ui/app.py`) binds to `localhost` and can be password-gated via `MEMORYBRIDGE_UI_PASSWORD`; it can delete memories, so don't expose it on a shared network.
- **Dependencies.** MemoryBridge depends on `fastmcp`, `tiktoken`, `python-dotenv`, `openai`, `anthropic`, `notion-client`, `fastembed`, `numpy`, `streamlit`, and `pandas` (see `requirements.txt`). Keep them current.

## Reporting a Vulnerability

Please report security issues privately rather than opening a public GitHub issue.

Email: **corvus00@gmail.com**

Include a description and impact, steps to reproduce, and a suggested fix if you have one. This is a personal open-source project with no SLA, but genuine security issues will be addressed promptly.
