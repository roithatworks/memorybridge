# MemoryBridge
### Persistent, local memory for Claude — across every conversation

---

## The Problem

Every Claude conversation starts from zero. Claude doesn't know your name, your preferences, your projects, or anything you've told it before. You re-explain yourself constantly.

## What MemoryBridge Does

MemoryBridge gives Claude a persistent memory that lives on your computer. Once set up, Claude can remember facts about you, your work, your preferences, and your projects — across every conversation, forever.

It works in Claude desktop, Cowork mode, and can export memories to ChatGPT, Gemini, and local models too.

---

## Key Features

**Persistent memory** — Remembers what you tell it across sessions. Start a new conversation and Claude already knows your context.

**Local & private** — Everything is stored in a plain JSON file on your machine. Nothing goes to a server or the cloud.

**Token-budget aware** — Smart retrieval that fits relevant memories into Claude's context window without wasting space. Older, less-accessed memories are compressed or archived automatically.

**Multiple profiles** — Organize memory by context (e.g., "work", "personal", "research").

**Cross-model export** — Export your memories in a format that works with ChatGPT, Gemini, or Ollama.

**Decay + scoring** — Memories that haven't been accessed in a while decay naturally. Important or frequently accessed ones stick around longer.

---

## What You Can Ask Claude (Once Set Up)

- "Remember that I prefer bullet-point answers"
- "What do you know about my projects?"
- "Search my memories for anything about marketing"
- "Add a memory: my manager is Sarah and she values concise updates"
- "Export my memories for ChatGPT"
- "Show me my memory stats"

---

## How It Works

MemoryBridge is a local Python server that runs alongside Claude desktop. It uses the Model Context Protocol (MCP) — the same standard that connects Claude to tools like Google Drive, Notion, and Slack. When you start a Claude conversation, MemoryBridge is available as a set of tools Claude can call to read and write your memory.

Your memories are stored in `~/memorybridge/memory.json` — a plain text file you can open, read, and back up any time.

---

## Setup Time

~10 minutes. You need Python installed (most Macs already have it). Full instructions in `SETUP_GUIDE.md`.

---

## Requirements

- Mac, Windows, or Linux
- Python 3.10+
- Claude desktop app
- Two Python packages: `fastmcp` and `tiktoken`

---

*Shared freely. No warranty, no telemetry, no cloud.*
