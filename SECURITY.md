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

- **Dual-mode authentication (Bearer header & Capability-URL).** Remote requests authenticate via a shared secret (minimum 32 characters) using either:
  1. Standard `Authorization: Bearer {MEMORYBRIDGE_TOKEN}` header (recommended; keeps secrets out of URLs, browser history, and proxy logs).
  2. Capability-URL path (`/{MEMORYBRIDGE_TOKEN}/mcp`) for connector clients that cannot send custom headers (e.g. ChatGPT / Perplexity).
  The server refuses to start HTTP without a token of at least 32 characters. Generate a strong one:
  ```bash
  python3 -c "import secrets; print(secrets.token_urlsafe(32))"
  ```
  Credentials are compared in constant time (`secrets.compare_digest`) and kept out of access logs.
- **Per-IP rate limiting** (`MEMORYBRIDGE_RATE_LIMIT`/`MEMORYBRIDGE_RATE_WINDOW`, default 120/60s) blunts brute-force attempts; excess requests receive `429`.
- **Tool gating & write guardrails.** Remote clients are restricted to a safe allowlist (`get_memory`, `search_memory`, `reflect`, `add_memory`, `list_projects`, `export_passport`, `export_for_model`, `list_profiles`, `traverse_memory_graph`, `get_memory_edges`, `ws_status`, `ws_list`, `ws_read`, `ws_search`, `ws_write`). Destructive and subprocess-spawning tools (`edit_memory`, `delete_memory`, `prune_memories`, `ingest_from_inbox`, `switch_profile`, …) are removed before serving, and gating **fails closed** — the server aborts rather than serve them if the tool set can't be enumerated. Furthermore, remote `add_memory` calls **disarm supersession** (ignoring `supersedes` to prevent remote eviction of legitimate memories) and **strip `why_it_matters`** (preventing stored prompt injection into reasoning contexts). Remote `ws_write` calls require `overwrite=True` and must match configured allowlist paths. Profile creation is also restricted to known profiles.
- **No cross-session profile bleed.** Over the bridge, a profile-omitted call resolves to the default profile, not a shared mutable global.

## Other considerations

- **Prompt injection.** Memory content and ingested conversation text are untrusted (writable via `add_memory`, or supplied in third-party exports). They flow into LLM prompts on both the ingestion side (extraction, conflict resolution, `reflect`) and the retrieval side (`get_memory`, `search_memory`, `export_for_model`, `export_passport` — the whole point of which is to feed this content into another model's context) wrapped in explicit untrusted-data delimiters with instructions to treat them as data, not instructions. The plain-text exports use literal `<<<UNTRUSTED_MEMORY_DATA>>>` markers around the memory section; the JSON tools (`get_memory`/`search_memory`) instead add a `_security_notice` field rather than mutating each memory's `content` value, since the Streamlit UI displays that value verbatim. Extracted facts are field-validated before storage.
- **Workspace sandbox.** Workspace exploration tools (`ws_read`, `ws_search`, `ws_list`, `ws_write`) enforce strict directory resolution against `workspace_root()`. Symlinks escaping the root are rejected on read and search, write operations require explicit allowlisting (`workspace_write_allowed`), and file reads are bounded by a 2 MB ceiling to prevent out-of-memory exhaustion.
- **Ingestion input.** The inbox watcher ignores both symlinks and hardlinks (preventing arbitrary-file read into an API prompt) and skips files that are still being written. Documents are constrained to a 10 MB ceiling. Embedding-model failure fails the ingest by default rather than silently degrading dedup (`MEMORYBRIDGE_ALLOW_DEGRADED=1` to override).
- **File permissions.** `memory.db` is created with your user's default permissions and is unencrypted. Protect your home directory; avoid storing secrets as memories. `chmod 600 ~/memorybridge/memory.db` if desired.
- **Streamlit UI.** The review UI (`ui/app.py`) binds to `localhost` and can be password-gated via `MEMORYBRIDGE_UI_PASSWORD`; it can delete memories, so don't expose it on a shared network.
- **Dependencies.** MemoryBridge depends on `fastmcp`, `tiktoken`, `python-dotenv`, `openai`, `anthropic`, `notion-client`, `fastembed`, `numpy`, `streamlit`, and `pandas` (see `requirements.txt`). Keep them current.

## Reporting a Vulnerability

Please report security issues privately rather than opening a public GitHub issue.

Email: **corvus00@gmail.com**

Include a description and impact, steps to reproduce, and a suggested fix if you have one. This is a personal open-source project with no SLA, but genuine security issues will be addressed promptly.
