# Security Policy

## Supported Versions

MemoryBridge is a single-file MCP server. There are no versioned releases — the `main` branch is the current supported version. Security fixes are applied there directly.

## Threat Model

MemoryBridge is a **local-only** tool. It has no network listener, no authentication layer, and no remote API. Understanding what it does and doesn't do:

**What MemoryBridge does:**
- Runs as a local Python process on your machine, launched by the Claude desktop app
- Reads and writes a JSON file at `~/memorybridge/memory.json`
- Communicates only with the Claude desktop app over stdio (standard MCP transport)

**What MemoryBridge does not do:**
- Open any network ports
- Send data to any remote server
- Require or store credentials
- Execute arbitrary code from memory content

## Security Considerations

**File permissions** — `memory.json` and `analytics.json` are created with your user's default permissions. If you store sensitive information in memory, ensure your home directory is appropriately protected. Consider setting restrictive permissions:

```bash
chmod 600 ~/memorybridge/memory.json
```

**Memory content** — MemoryBridge stores whatever you (or Claude) write to it. Avoid storing secrets, passwords, or credentials as memories. The file is plaintext JSON — anyone with read access to your home directory can read it.

**MCP trust boundary** — MemoryBridge is registered as a trusted MCP server in `claude_desktop_config.json`. Claude can call its tools automatically. Review what Claude is writing to memory if you share your machine or Claude account with others.

**Concurrent access** — The server uses `fcntl.flock` file locking to prevent data corruption when multiple Claude instances run simultaneously (e.g., Claude Desktop and Claude Code). This is a POSIX advisory lock — it protects against accidental concurrent writes from cooperating processes, not against malicious access.

**Dependencies** — MemoryBridge depends on `fastmcp` and `tiktoken`. Keep these up to date:

```bash
pip3 install --upgrade fastmcp tiktoken
```

## Reporting a Vulnerability

If you find a security issue — particularly one that could allow data exfiltration, privilege escalation, or arbitrary code execution — please report it privately rather than opening a public GitHub issue.

Email: **corvus00@gmail.com**

Please include:
- A description of the vulnerability and its potential impact
- Steps to reproduce
- Any suggested fix if you have one

You can expect an acknowledgement within a few days. This is a personal open-source project with no SLA, but genuine security issues will be taken seriously and addressed promptly.
