# MemoryBridge Audit Findings & Resolution Backlog (2026-10-28)

Findings from the October 2026 Security, Quality, and Performance audit, capturing architectural enhancements to achieve full functional parity with top GitHub AI memory engines (Mem0, Zep/Graphiti, Letta) via high-leverage 80/20 implementations.

All items below are tracked as GitHub issues and will be resolved via individual pull requests with full test coverage and passing CI workflows.

---

## Tracked Issues & Resolution Roadmap

### 1. Issue [#210](https://github.com/roithatworks/memorybridge/issues/210): Fix `scripts/weekly_audit.py` Crash on Missing Log Directory
* **Labels**: `bug`, `area:cleanup`, `severity:low`
* **Finding**: `scripts/weekly_audit.py` fails with `FileNotFoundError` if `~/memorybridge/logs` does not exist and hardcodes `~/memorybridge` instead of respecting `config.data_dir()`.
* **Resolution Plan**:
  - Dynamically resolve report destination to `config.data_dir() / "logs" / "weekly_audit_report.md"`.
  - Use `os.makedirs(report_path.parent, exist_ok=True)`.
  - Add unit test coverage in `tests/unit/test_db_maintenance.py`.
* **Branch**: `fix/weekly-audit-log-dir`

---

### 2. Issue [#211](https://github.com/roithatworks/memorybridge/issues/211): Two-Stage SQL Candidate Pre-Filtering for Semantic Search
* **Labels**: `enhancement`, `area:performance`, `area:db`, `severity:medium`
* **Finding**: Semantic search in `db/store.py` performs an $O(N)$ linear scan over all profile vectors in RAM. At >50,000 memories, this creates memory pressure and scales linearly.
* **Resolution Plan**:
  - Implement two-stage retrieval: use indexed SQLite metadata (active status, temporal validity, category, or top-K BM25/FTS candidate IDs) to narrow candidate IDs to top 500–1,000 in SQL (<1ms).
  - Compute NumPy cosine similarity strictly over the candidate slice, reducing vector calculation overhead to constant $O(K)$.
  - Add performance and accuracy unit tests verifying identical top-k retrieval quality.
* **Branch**: `perf/semantic-candidate-prefilter`

---

### 3. Issue [#212](https://github.com/roithatworks/memorybridge/issues/212): Canonical Entity Normalization & Alias Dictionary
* **Labels**: `enhancement`, `area:db`, `area:correctness`, `severity:medium`
* **Finding**: Entity tags (`entity:<name>`) lack alias normalization. Synonyms (`"cale"`, `"cale_corbett"`, `"author"`, `"me"`) create fragmented, disconnected nodes in `memory_edges` and graph visualizers.
* **Resolution Plan**:
  - Add an `aliases` dictionary mapping in `config.py` and `memorybridge.yaml`.
  - Normalize entity tags to their canonical root on write and search.
  - Expand multi-hop traversal in `traverse_memory_graph` across alias sets so graph exploration bridges synonyms.
  - Add unit tests for tag normalization and multi-hop alias resolution.
* **Branch**: `feat/entity-alias-dictionary`

---

### 4. Issue [#213](https://github.com/roithatworks/memorybridge/issues/213): Hierarchical Memory Synthesis Tool (`consolidate_memories`)
* **Labels**: `enhancement`, `area:db`, `severity:medium`
* **Finding**: Hundreds of individual episodic micro-memories accumulate over time, diluting context token budgets without being consolidated into high-density reference cards (parity with Letta Core Memory and Graphiti summaries).
* **Resolution Plan**:
  - Add a `consolidate_memories(topic_or_entity)` MCP tool and `mb consolidate` CLI command.
  - Retrieve candidate active memories using `search_hybrid()`.
  - Synthesize facts into a concise reference card and commit via `add_memory(content=summary, supersedes=[...])`.
  - Automatically archive constituent memories using the existing audited supersession engine.
  - Add unit tests verifying synthesis, supersession archiving, and retrieval ranking.
* **Branch**: `feat/hierarchical-memory-consolidation`

---

### 5. Issue [#214](https://github.com/roithatworks/memorybridge/issues/214): Profile-to-Bearer-Token Binding in HTTP Bridge (Scoped RBAC)
* **Labels**: `enhancement`, `audit`, `severity:medium`
* **Finding**: The HTTP bridge authenticates all callers with a single token, mapping to the default profile without profile isolation between different external models or agents.
* **Resolution Plan**:
  - Allow `memorybridge.yaml` (or env) to specify token-to-profile bindings (`token_agent_a -> agent_a_profile`).
  - In `_RateLimitAuthMiddleware`, lock authenticated sessions to their designated profile, preventing unauthorized profile access.
  - Maintain backward compatibility for single-user local setups.
  - Add unit tests for scoped multi-token authentication in `tests/unit/test_http_bridge.py`.
* **Branch**: `feat/http-token-profile-binding`

---

### 6. Issue [#215](https://github.com/roithatworks/memorybridge/issues/215): Modularize `server.py` Monolith into Submodules
* **Labels**: `refactor`, `area:cleanup`, `severity:low`
* **Finding**: `server.py` is a 2,050-line monolith containing FastMCP setup, 29 MCP tools, HTTP ASGI bridge lifecycle, and rate-limiting/auth middleware.
* **Resolution Plan**:
  - Retain `server.py` as backward-compatible entry point re-exporting all public symbols.
  - Extract ASGI components into `server/middleware.py`.
  - Group MCP tools into domain modules:
    - `server/tools/memory.py`
    - `server/tools/graph.py`
    - `server/tools/workspace.py`
    - `server/tools/ingestion.py`
  - Verify all 451 tests pass with zero regressions.
* **Branch**: `refactor/modularize-server`

---

## PR Resolution Protocol

Every issue above must follow this lifecycle:
1. **Branch Creation**: Branch from latest `main` (`git checkout -b <branch_name>`).
2. **Implementation & Tests**: Implement the fix or feature with full unit/integration test coverage.
3. **Local Test Gate**: Verify all tests pass locally (`pytest tests/`).
4. **Pull Request**: Open PR linking the issue (e.g. `Resolves #210`) with clear summary and verification evidence.
5. **CI Gate**: Wait for all 7 GitHub Actions workflows to pass.
6. **Squash Merge**: Squash and merge into `main`, delete branch, and sync local repository.
