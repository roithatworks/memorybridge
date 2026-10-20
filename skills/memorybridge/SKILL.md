---
name: memorybridge
description: Model Context Protocol (MCP) persistent memory management for AI coding and chat agents using MemoryBridge.
---

# MemoryBridge Agent Skill

Use this skill when interacting with a workspace or project managed by MemoryBridge. MemoryBridge provides persistent local memory across agent sessions, preventing repeated errors and carrying forward user preferences, decisions, and procedural workflows.

---

## Agent Execution Loop (`RECALL → PLAN → ACT → OBSERVE → UPDATE`)

When working on complex tasks or multi-step requests:

### 1. Pre-Task Recall (RECALL)
Before starting substantial work or touching code:
- Search MemoryBridge using `search_memory` or `get_memory` with a `context_hint` related to the project, entities, or failure modes.
- Example: `search_memory(query="deployment instructions for billing app", category="procedural")`
- Load only memories that could directly change your next action.

### 2. Live Task State (WORKING MEMORY)
- Keep temporary scratch calculations, raw command outputs, and intermediate file edits in working context.
- **Do not** write raw tool outputs or full transcripts directly to MemoryBridge.

### 3. Session End / Promotion (UPDATE)
Before completing the session or after resolving an issue:
- Call `consolidate_session` or `add_memory` to save durable facts, decisions, and reusable procedures.
- Categorize correctly:
  - **procedural:** Step-by-step checklists, testing routines, or fixed CLI command sequences.
  - **decision:** Reached decisions with clear rationale.
  - **preference:** User formatting, communication, or architectural preferences.
  - **constraint:** Environment, library, or security boundaries.
- **Security Check:** Never save API keys (`sk-...`, `ghp_...`), DB credentials, or secrets to memory.

---

## MCP Tool Reference

- `get_memory`: Retrieve active memories within a token budget (`profile`, `context_hint`, `min_confidence`).
- `search_memory`: Hybrid BM25 + semantic search for specific keywords (`query`, `category`, `min_confidence`).
- `add_memory`: Store a single memory with category, importance, `confidence`, and optional `expires_at` TTL.
- `consolidate_session`: Extract and promote session notes into consolidated memories (`session_notes`).
- `add_memory_edge`: Link two memories in the Knowledge Graph (`source_id`, `target_id`, `relation`).
- `get_memory_edges`: Retrieve connected edges for a memory ID.
- `traverse_memory_graph`: Multi-hop BFS traversal from a memory (`memory_id`, `max_hops`, `max_nodes`).
- `reflect`: Produce a structured synthesis of facts, dates, preferences, and contradictions for a query.
