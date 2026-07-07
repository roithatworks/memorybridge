"""SQLite persistence layer for MemoryBridge. Zero MCP coupling.

All SQL lives here. server.py contains zero SQL.
Tool signatures are unchanged — this is a drop-in persistence swap.
"""

import json
import logging
import os
import sqlite3
import threading
import uuid
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

from db.constants import (  # noqa: E402
    VALID_CATEGORIES, IMPORTANCE_LEVELS, _content_hash, _count_tokens, effective_score,
    guardrail_check, _merge_tags, _max_importance, generate_tags
)
from db.entities import EntityExtractor


class GuardrailRejection(ValueError):
    """Raised when content is document-shaped and rejected by the write path.

    Carries the human-readable reason so callers (MCP tools, ingest pipeline)
    can report why and route the content to a file instead of a memory.
    """
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)

SCHEMA_SQL = Path(__file__).parent / "schema.sql"


class _LockedConnection:
    """Serialize all access to a shared sqlite3.Connection (issue #6).

    The store uses one connection with check_same_thread=False; FastMCP may
    dispatch tool calls on different threads. Without serialization,
    interleaved execute/commit across threads can commit another handler's
    half-finished transaction. Guarding at the connection level (rather than
    per store method) also covers direct ``store._conn`` access from
    server.py (issue #15) and pruner.py.

    An RLock (re-entrant) is used so a locked method may call another locked
    method on the same thread. ``transaction()`` lets multi-statement
    sequences hold the lock across statements.
    """

    _PASSTHROUGH = ("execute", "executemany", "executescript", "commit",
                    "rollback", "close")

    def __init__(self, conn: sqlite3.Connection):
        self._raw = conn
        self._lock = threading.RLock()

    def __getattr__(self, name):
        attr = getattr(self._raw, name)
        if name in self._PASSTHROUGH:
            def locked(*args, **kwargs):
                with self._lock:
                    return attr(*args, **kwargs)
            return locked
        return attr

    @property
    def row_factory(self):
        return self._raw.row_factory

    @row_factory.setter
    def row_factory(self, value):
        self._raw.row_factory = value

    def transaction(self):
        """Hold the lock across a multi-statement read-modify-write block."""
        return self._lock


def _jaccard_similarity(a: str, b: str) -> float:
    """Jaccard-style keyword overlap between two strings."""
    words_a = set(a.lower().split())
    words_b = set(b.lower().split())
    if not words_a or not words_b:
        return 0.0
    return len(words_a & words_b) / max(len(words_a), len(words_b))


def _mem_id() -> str:
    return f"mem_{uuid.uuid4().hex[:8]}"


class MemoryStore:
    """
    SQLite-backed memory store with WAL mode, FTS5 search, and content-hash dedup.

    Usage:
        store = MemoryStore(Path.home() / "memorybridge" / "memory.db")
        store.ensure_profile("default")
        mid = store.add_memory("default", "Cale prefers dark mode", category="preference")
    """

    def __init__(self, db_path: Path, entity_extractor: EntityExtractor | None = None,
                 merge_threshold: float = 0.35, merge_min_tags: int = 2,
                 recency_decay_days: int = 0,
                 llm_synthesize: Callable | None = None):
        """Initialize MemoryStore.

        *db_path* — path to the SQLite database file.
        *entity_extractor* — optional ``EntityExtractor`` instance. When set,
        ``add_memory`` automatically extracts entity tags from content and
        merges them with caller-supplied tags. Pass ``False`` to disable
        even if a default extractor is configured.
        *merge_threshold* — Jaccard similarity threshold for fuzzy dedup
        (0.0 = never merge, 1.0 = only merge identical content). Default 0.35.
        *merge_min_tags* — minimum shared entity tags required to consider
        two memories as related for fuzzy dedup. Default 2.
        *recency_decay_days* — half-life in days for recency weighting in
        search results. 0 (default) = no recency boost. Higher values = slower
        decay (older memories stay relevant longer).
        *llm_synthesize* — optional callable ``(question, memory_text) -> str``
        for the ``reflect()`` synthesis tool. If ``None``, uses keyword-based
        fallback.
        """
        self._path = db_path
        self._entity_extractor = entity_extractor
        self._merge_threshold = merge_threshold
        self._merge_min_tags = merge_min_tags
        self._recency_decay_days = recency_decay_days
        self._llm_synthesize = llm_synthesize
        self._reflector: Any = None
        # Pending embed threads (issue #5): daemon threads for embed-on-write
        # that must be drained before os._exit(0) so their SQLite writes
        # complete. Avoids backfill-on-next-startup for fast shutdowns.
        self._pending_embeds: set[threading.Thread] = set()
        self._pending_embed_lock = threading.Lock()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False is safe only because _LockedConnection
        # serializes every statement (issue #6).
        self._conn = _LockedConnection(
            sqlite3.connect(str(db_path), check_same_thread=False)
        )
        self._conn.row_factory = sqlite3.Row
        # WAL mode: lets a second connection (e.g. the ingestion subprocess)
        # READ while the live MCP server holds a write, and vice-versa, instead
        # of serializing all access. The class docstring already assumes WAL,
        # but the pragma was never actually set — this enables it. Persists on
        # the DB file once set, but assert it every open to be safe.
        self._conn.execute("PRAGMA journal_mode=WAL")
        # busy_timeout: when another connection still holds the write lock,
        # wait up to N ms then raise sqlite3.OperationalError("database is
        # locked") INSTEAD OF blocking forever. Without this the ingestion CLI
        # hangs indefinitely whenever the server is running.
        self._conn.execute("PRAGMA busy_timeout=5000")
        # FK enforcement is OFF by default in SQLite and is per-connection —
        # without this, ON DELETE CASCADE (memory_embeddings) never fires.
        # Issue #4: 103/128 embeddings were orphaned because of this.
        self._conn.execute("PRAGMA foreign_keys=ON")

        # Is the DB already initialized? If the schema exists, a second process
        # (e.g. the ingestion CLI opening this DB while the server holds it)
        # must NOT re-run the one-time schema/DELETE/rebuild writes — those grab
        # the write lock and needlessly contend, which is what made the backfill
        # hang. All of that work is idempotent hygiene guarded by meta flags, so
        # on an already-initialized DB it's a no-op anyway. Fresh DBs (tests,
        # first run) take the full path below unchanged.
        already_init = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='memories'"
        ).fetchone() is not None

        if not already_init:
            self._conn.executescript(SCHEMA_SQL.read_text())
            # executescript commits and can reset pragmas — re-assert.
            self._conn.execute("PRAGMA foreign_keys=ON")
            # Pruner tables (idempotent CREATE IF NOT EXISTS)
            from db.pruner import PRUNER_SCHEMA, bootstrap_rules
            self._conn.executescript(PRUNER_SCHEMA)
            bootstrap_rules(self._conn)
            self._conn.commit()
            # One-time hygiene (issue #4): purge embedding rows orphaned while
            # FK enforcement was off. Idempotent and cheap.
            self._conn.execute(
                "DELETE FROM memory_embeddings "
                "WHERE id NOT IN (SELECT id FROM memories)"
            )
            self._conn.commit()
            # One-time repair (issue #3): rebuild FTS once, marked in meta.
            rebuilt = self._conn.execute(
                "SELECT value FROM meta WHERE key='fts_rebuilt_v1'"
            ).fetchone()
            if not rebuilt:
                self._conn.execute(
                    "INSERT INTO memories_fts(memories_fts) VALUES('rebuild')"
                )
                self._conn.execute(
                    "INSERT OR REPLACE INTO meta VALUES('fts_rebuilt_v1', ?)",
                    (datetime.now().isoformat(),)
                )
                self._conn.commit()

        # Phase 4: lazy-loaded FastEmbed model (cached per-process)
        self._embed_model = None
        self._embed_lock = threading.Lock()
        # Issue #5: backfill embeddings for memories missing vectors, on a
        # background thread so startup isn't blocked by model load. New
        # memories are embedded on write (see add_memory). Skipped when
        # MEMORYBRIDGE_NO_EMBED is set — the ingestion subprocess writes via
        # add_memory (which embeds per-memory) and must not pay the one-time
        # model download just to start up.
        if not os.environ.get("MEMORYBRIDGE_NO_EMBED"):
            threading.Thread(
                target=self._backfill_missing_embeddings,
                daemon=True, name="embedding-backfill"
            ).start()

    # -------------------------------------------------------------------------
    # Profiles
    # -------------------------------------------------------------------------

    def ensure_profile(self, name: str) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO profiles(name) VALUES(?)", (name,)
        )
        self._conn.commit()

    def get_profile(self, name: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM profiles WHERE name=?", (name,)
        ).fetchone()
        if not row:
            return None
        return {
            "name": row["name"],
            "identity": json.loads(row["identity"]),
            "projects": json.loads(row["projects"]),
            "model_preferences": json.loads(row["model_prefs"]),
        }

    def update_profile(self, name: str, *,
                       identity: dict = None,
                       projects: list = None,
                       model_preferences: dict = None) -> None:
        self.ensure_profile(name)
        if identity is not None:
            self._conn.execute(
                "UPDATE profiles SET identity=? WHERE name=?",
                (json.dumps(identity), name)
            )
        if projects is not None:
            self._conn.execute(
                "UPDATE profiles SET projects=? WHERE name=?",
                (json.dumps(projects), name)
            )
        if model_preferences is not None:
            self._conn.execute(
                "UPDATE profiles SET model_prefs=? WHERE name=?",
                (json.dumps(model_preferences), name)
            )
        self._conn.commit()

    def list_profiles(self) -> list[str]:
        rows = self._conn.execute("SELECT name FROM profiles").fetchall()
        return [r["name"] for r in rows]

    # -------------------------------------------------------------------------
    # Memories — write
    # -------------------------------------------------------------------------

    def add_memory(self, profile: str, content: str, *,
                   category: str = "fact", importance: str = "medium",
                   tags: list = None, project_id: str = None,
                   enforce_guardrail: bool = True,
                   skip_enrichment: bool = False) -> str | None:
        """Returns memory ID on success, None if exact duplicate.

        Raises GuardrailRejection if content is document-shaped (too long, too
        many lines, or markdown-heading/multi-section). Pass
        enforce_guardrail=False only for trusted internal migrations.

        Pass skip_enrichment=True for internal auto-saves (conversation
        snippets, session summaries) that don't need full tag enrichment.
        """
        if enforce_guardrail:
            ok, reason = guardrail_check(content)
            if not ok:
                raise GuardrailRejection(reason)
        self.ensure_profile(profile)
        h = _content_hash(content)
        mid = _mem_id()
        now = datetime.now().isoformat()
        tc = _count_tokens(content)

        # Entity extraction: if an EntityExtractor is configured, extract
        # entity tags from content and merge with caller-supplied tags.
        enriched_tags: list[str] | None = tags
        if self._entity_extractor:
            entity_tags = self._entity_extractor.extract(content)
            if entity_tags:
                enriched_tags = _merge_tags(tags or [], entity_tags)

        # Topical tag enrichment: adds category, project, content-type
        # (question, decision, bug, etc.), and keyword tags.
        # Skip for internal auto-saves (conversation snippets, summaries).
        if not skip_enrichment:
            enriched_tags = generate_tags(
                content,
                category=category,
                project_id=project_id,
                existing_tags=enriched_tags or [],
            )

        # Fuzzy dedup/merge: check for near-duplicate by entity tag overlap
        # + content similarity. Runs after entity extraction (tags available)
        # but before INSERT so we can redirect to UPDATE.
        merged_id = self._maybe_merge(profile, content, enriched_tags or [])
        if merged_id:
            return merged_id

        try:
            # Hold the lock across INSERT + FTS sync + commit so another
            # thread's commit can't land mid-sequence (issue #6).
            with self._conn.transaction():
                self._conn.execute(
                    """INSERT INTO memories
                       (id,profile,content,content_hash,category,importance,
                        created_at,last_accessed,tags,project_id,token_count)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (mid, profile, content, h, category, importance,
                     now, now, json.dumps(enriched_tags or []), project_id, tc)
                )
                # FTS sync handled by memories_ai trigger (issue #3)
                self._conn.commit()
            # Embed-on-write (issue #5): background thread so model load
            # doesn't block the MCP response. Tracked in _pending_embeds
            # so shutdown drains it before os._exit(0).
            t = threading.Thread(
                target=self._embed_and_cleanup, args=(mid, profile, content),
                daemon=True, name=f"embed-{mid}"
            )
            with self._pending_embed_lock:
                self._pending_embeds.add(t)
            t.start()
            return mid
        except sqlite3.IntegrityError:
            self._conn.rollback()
            return None  # duplicate content_hash

    # -------------------------------------------------------------------------
    # Fuzzy dedup / merge
    # -------------------------------------------------------------------------

    def _maybe_merge(self, profile: str, content: str,
                     enriched_tags: list[str]) -> str | None:
        """Check for a near-duplicate memory and merge if found.

        Uses entity tags (from step 1) + Jaccard content similarity.
        Returns existing memory ID if merged, ``None`` if no match.

        Disabled when ``merge_threshold`` is 0.0 or content has no entity tags.
        """
        if self._merge_threshold <= 0.0:
            return None

        # Need entity tags to find candidates — pure keyword content can't
        # reliably disambiguate "is this the same topic?"
        entity_tags = [t for t in enriched_tags if t.startswith("entity:")]
        if len(entity_tags) < self._merge_min_tags:
            return None

        # Find candidate memories sharing ≥ merge_min_tags entity tags.
        # Bind tags as ? placeholders — never interpolate caller-supplied
        # tag values into SQL (they reach here from add_memory's `tags` arg).
        placeholders = ",".join("?" for _ in entity_tags)
        try:
            rows = self._conn.execute(
                f"""SELECT m.id, m.content, m.importance, m.tags,
                           COUNT(DISTINCT jt.value) AS shared
                    FROM memories m, json_each(m.tags) AS jt
                    WHERE m.profile = ?
                      AND m.archived = 0
                      AND jt.value IN ({placeholders})
                    GROUP BY m.id
                    HAVING shared >= ?
                    ORDER BY shared DESC
                    LIMIT 5""",
                (profile, *entity_tags, self._merge_min_tags),
            ).fetchall()
        except Exception:
            return None  # json_each may fail on old SQLite — fail-soft

        if not rows:
            return None

        # Score each candidate by Jaccard similarity
        best_id: str | None = None
        best_sim = 0.0
        best_row = None
        for row in rows:
            sim = _jaccard_similarity(content, row["content"])
            if sim > best_sim:
                best_sim = sim
                best_id = row["id"]
                best_row = row

        if best_sim < self._merge_threshold:
            return None

        # Merge: append new info annotation, bump importance and timestamp.
        # Recompute content_hash and token_count for the merged content — the
        # old code left both stale, which under-counted the token budget and
        # broke exact-dedup (content_hash no longer matched content).
        now = datetime.now().isoformat()
        date_str = datetime.now().strftime("%Y-%m-%d")
        merge_note = f"\n[merged: {date_str}] {content.strip()}"
        new_content = (best_row["content"] or "") + merge_note
        new_hash = _content_hash(new_content)
        new_tokens = _count_tokens(new_content)
        # Keep the higher importance level
        final_imp = _max_importance(best_row["importance"], "medium")

        try:
            with self._conn.transaction():
                self._conn.execute(
                    """UPDATE memories
                       SET content = ?,
                           content_hash = ?,
                           token_count = ?,
                           importance = ?,
                           last_accessed = ?,
                           access_count = access_count + 1
                       WHERE id = ? AND profile = ?""",
                    (new_content, new_hash, new_tokens, final_imp, now, best_id, profile),
                )
                self._conn.commit()
            logger.info("Merged new content into existing memory %s (sim=%.2f)",
                        best_id, best_sim)
            return best_id
        except Exception:
            logger.exception("Merge UPDATE failed for best_id=%s", best_id)
            return None

    def add_memories(self, profile: str, facts: list[str], *,
                     category: str = "fact", importance: str = "medium",
                     project_id: str = None) -> int:
        """Batch insert. Returns count of actually inserted rows.

        Guardrail-rejected (document-shaped) facts are skipped, not fatal, and
        recorded on self.last_rejected as [(fact, reason), ...] for the caller.
        """
        added = 0
        self.last_rejected = []
        for fact in facts:
            try:
                if self.add_memory(profile, fact, category=category,
                                   importance=importance, project_id=project_id):
                    added += 1
            except GuardrailRejection as e:
                self.last_rejected.append((fact, e.reason))
        return added

    def edit_memory(self, profile: str, memory_id: str, **kwargs) -> bool:
        """Edit an existing memory in place. Only provided keyword fields are updated.

        Supported kwargs: content, importance, category, project_id.
        If content is provided, content_hash and token_count are recomputed.
        Returns True if the row was found and updated, False if not found.
        """
        allowed = {"content", "importance", "category", "project_id"}
        fields = {k: v for k, v in kwargs.items() if k in allowed and v is not None}
        if not fields:
            # Nothing to update — check existence and return
            exists = self._conn.execute(
                "SELECT 1 FROM memories WHERE id=? AND profile=?",
                (memory_id, profile)
            ).fetchone()
            return exists is not None

        if "content" in fields:
            fields["content_hash"] = _content_hash(fields["content"])
            fields["token_count"] = _count_tokens(fields["content"])

        set_clause = ", ".join(f"{col}=?" for col in fields)
        values = list(fields.values()) + [memory_id, profile]

        try:
            with self._conn.transaction():
                cur = self._conn.execute(
                    f"UPDATE memories SET {set_clause} WHERE id=? AND profile=?",
                    values
                )
                self._conn.commit()
            updated = cur.rowcount > 0
        except sqlite3.IntegrityError:
            self._conn.rollback()
            updated = False

        # Re-embed if content changed (fire-and-forget, fail-soft)
        if updated and "content" in fields:
            threading.Thread(
                target=self._embed_one,
                args=(memory_id, profile, fields["content"]),
                daemon=True, name=f"embed-edit-{memory_id}"
            ).start()

        return updated

    def delete_memory(self, profile: str, memory_id: str) -> int:
        """Delete memory. Returns token_count freed, or 0 if not found."""
        # SELECT + deletes + commit must be atomic vs. other threads
        # (issue #6): a concurrent delete of the same id would otherwise
        # double-issue the FTS delete command against a stale rowid.
        with self._conn.transaction():
            row = self._conn.execute(
                "SELECT token_count FROM memories WHERE id=? AND profile=?",
                (memory_id, profile)
            ).fetchone()
            if not row:
                return 0
            tc = row["token_count"]
            self._conn.execute("DELETE FROM memories WHERE id=?", (memory_id,))
            # Belt-and-suspenders for issue #4: FK CASCADE handles this when
            # foreign_keys=ON, but delete explicitly so a future connection
            # missing the pragma can't re-orphan embeddings.
            self._conn.execute(
                "DELETE FROM memory_embeddings WHERE id=?", (memory_id,)
            )
            # FTS removal handled by memories_ad trigger with original
            # content (issue #3 — the old manual delete passed "" which
            # left ghost tokens in the index).
            self._conn.commit()
        return tc

    # -------------------------------------------------------------------------
    # Memories — read
    # -------------------------------------------------------------------------

    def get_memories(self, profile: str, category: str = None,
                     max_tokens: int = None) -> list[dict]:
        """Return active memories, ranked by relevance_score desc."""
        sql = "SELECT * FROM memories WHERE profile=? AND archived=0"
        params = [profile]
        if category:
            sql += " AND category=?"
            params.append(category)
        sql += " ORDER BY relevance_score DESC"
        rows = self._conn.execute(sql, params).fetchall()
        mems = [self._row_to_dict(r) for r in rows]

        if max_tokens:
            budget, result = 0, []
            for m in mems:
                tc = m["token_count"]
                if budget + tc <= max_tokens:
                    result.append(m)
                    budget += tc
            return result
        return mems

    def search(self, profile: str, query: str, category: str = None,
               limit: int = 5, max_tokens: int = 800) -> list[dict]:
        """FTS5 BM25 search with token budget."""
        # Sanitize each term for FTS5
        terms = [t for t in query.split() if len(t) > 1]
        if not terms:
            return []
        safe_query = " OR ".join(f'"{t}"' for t in terms)

        sql = """
            SELECT m.*, bm25(memories_fts) AS bm25_score
            FROM memories_fts
            JOIN memories m ON memories_fts.rowid = m.rowid
            WHERE memories_fts MATCH ?
              AND m.profile = ?
              AND m.archived = 0
        """
        params = [safe_query, profile]
        if category:
            sql += " AND m.category = ?"
            params.append(category)
        sql += " ORDER BY bm25_score LIMIT ?"
        params.append(limit * 3)  # over-fetch, then trim by token budget

        try:
            rows = self._conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError as fts_err:
            logging.warning("FTS5 parse error for query %r: %s — falling back to LIKE search", query, fts_err)
            like_pattern = f"%{query}%"
            like_sql = (
                "SELECT * FROM memories WHERE profile=? AND archived=0 AND content LIKE ?"
            )
            like_params = [profile, like_pattern]
            if category:
                like_sql += " AND category=?"
                like_params.append(category)
            like_sql += " LIMIT ?"
            like_params.append(limit * 3)
            rows = self._conn.execute(like_sql, like_params).fetchall()

        results, tokens_used = [], 0
        for row in rows:
            m = self._row_to_dict(row)
            m.pop("bm25_score", None)
            m["match_score"] = _jaccard_similarity(query, m["content"])
            if tokens_used + m["token_count"] <= max_tokens:
                results.append(m)
                tokens_used += m["token_count"]
            if len(results) >= limit:
                break
        return results

    def boost_on_access(self, profile: str, memory_id: str,
                        boost: float = 0.1) -> None:
        """Increment access_count and boost relevance_score (capped at 1.0)."""
        today = datetime.now().isoformat()
        self._conn.execute(
            """UPDATE memories
               SET access_count = access_count + 1,
                   last_accessed = ?,
                   relevance_score = MIN(relevance_score + ?, 1.0)
               WHERE id = ? AND profile = ?""",
            (today, boost, memory_id, profile)
        )
        self._conn.commit()

    def boost_batch(self, profile: str, ids: list,
                    boost: float = 0.1) -> None:
        """Boost relevance_score for multiple memories in a single commit.

        Replaces calling boost_on_access once per result (issue #12): uses
        executemany + a single conn.commit() instead of N commits.
        """
        if not ids:
            return
        today = datetime.now().isoformat()
        with self._conn.transaction():
            self._conn.executemany(
                """UPDATE memories
                   SET access_count = access_count + 1,
                       last_accessed = ?,
                       relevance_score = MIN(relevance_score + ?, 1.0)
                   WHERE id = ? AND profile = ?""",
                [(today, boost, mid, profile) for mid in ids]
            )
            self._conn.commit()

    # -------------------------------------------------------------------------
    # Pruning
    # -------------------------------------------------------------------------

    def auto_prune(self, profile: str, threshold: float = 0.15) -> list[str]:
        """Archive memories whose decay-adjusted score is below threshold."""
        now = datetime.now()
        rows = self._conn.execute(
            "SELECT id, relevance_score, importance, access_count, created_at "
            "FROM memories WHERE profile=? AND archived=0", (profile,)
        ).fetchall()

        to_archive = []
        for row in rows:
            score = effective_score(dict(row), now)
            if score < threshold:
                to_archive.append(row["id"])

        if to_archive:
            now_str = now.isoformat()
            with self._conn.transaction():
                self._conn.executemany(
                    "UPDATE memories SET archived=1, archived_at=?, "
                    "archive_reason='auto_prune_low_score' WHERE id=?",
                    [(now_str, mid) for mid in to_archive]
                )
                self._conn.commit()
        return to_archive

    # -------------------------------------------------------------------------
    # Access log
    # -------------------------------------------------------------------------

    def log_access(self, action: str, profile: str,
                   details: str = "", tokens_served: int = 0) -> None:
        self._conn.execute(
            "INSERT INTO access_log(ts,action,profile,details,tokens_served) "
            "VALUES(?,?,?,?,?)",
            (datetime.now().isoformat(), action, profile, details, tokens_served)
        )
        self._conn.commit()

    def get_access_log(self, limit: int = 50) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM access_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_access_log_count(self) -> int:
        """Return total number of rows in access_log."""
        return self._conn.execute(
            "SELECT COUNT(*) FROM access_log"
        ).fetchone()[0]

    def get_access_log_token_summary(self) -> dict:
        """Return total tokens served and a per-profile breakdown from access_log."""
        total_served = self._conn.execute(
            "SELECT COALESCE(SUM(tokens_served), 0) FROM access_log"
        ).fetchone()[0]
        served_by_profile = {}
        for row in self._conn.execute(
            "SELECT profile, SUM(tokens_served) as t FROM access_log GROUP BY profile"
        ).fetchall():
            served_by_profile[row["profile"]] = row["t"] or 0
        return {"total_served": total_served, "by_profile": served_by_profile}

    def get_archived_stats(self, profile: str) -> dict:
        """Return count and total tokens for archived memories in a profile."""
        row = self._conn.execute(
            "SELECT COUNT(*) as c, COALESCE(SUM(token_count), 0) as t "
            "FROM memories WHERE profile=? AND archived=1", (profile,)
        ).fetchone()
        return {"count": row["c"], "tokens": row["t"]}

    def get_tokens_served(self, profile: str) -> int:
        """Return total tokens_served from access_log for a profile."""
        return self._conn.execute(
            "SELECT COALESCE(SUM(tokens_served), 0) FROM access_log WHERE profile=?",
            (profile,)
        ).fetchone()[0]

    # -------------------------------------------------------------------------
    # Analytics events (issue #8: replaces analytics.json)
    # -------------------------------------------------------------------------

    def log_analytics_event(self, tokens_served: int, memories_returned: int,
                            model: str = "claude", profile: str = "default",
                            operation: str = "get_memory") -> None:
        """Insert one analytics event row. Errors go to stderr — never silently swallowed."""
        import uuid as _uuid
        now = datetime.now()
        try:
            self._conn.execute(
                "INSERT INTO analytics_events"
                "(id, session_date, tokens_served, memories_returned, model, operation, profile, created_at)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (
                    f"ae_{_uuid.uuid4().hex[:8]}",
                    now.strftime("%Y-%m-%d"),
                    tokens_served,
                    memories_returned,
                    model,
                    operation,
                    profile,
                    now.isoformat(),
                )
            )
            self._conn.commit()
        except Exception as exc:
            logging.warning("log_analytics_event failed: %s", exc)

    def get_analytics_summary(self, since_days: int = 30) -> dict:
        """Return daily_stats, by_model, by_operation aggregations via SQL GROUP BY.

        Mirrors the dict shape that analytics.py previously read from analytics.json
        so the UI can be updated with a minimal diff.
        """
        from datetime import timedelta
        cutoff = (datetime.now() - timedelta(days=since_days)).strftime("%Y-%m-%d")

        daily_rows = self._conn.execute(
            """SELECT session_date,
                      SUM(tokens_served)     AS tokens_served,
                      COUNT(*)               AS sessions,
                      SUM(memories_returned) AS memories_returned
               FROM analytics_events
               WHERE session_date >= ?
               GROUP BY session_date
               ORDER BY session_date""",
            (cutoff,)
        ).fetchall()
        daily_stats = {
            r["session_date"]: {
                "tokens_served": r["tokens_served"] or 0,
                "sessions": r["sessions"] or 0,
                "memories_returned": r["memories_returned"] or 0,
            }
            for r in daily_rows
        }

        model_rows = self._conn.execute(
            """SELECT model,
                      SUM(tokens_served) AS tokens,
                      COUNT(*)           AS sessions
               FROM analytics_events
               WHERE session_date >= ?
               GROUP BY model""",
            (cutoff,)
        ).fetchall()
        by_model = {
            r["model"]: {"tokens": r["tokens"] or 0, "sessions": r["sessions"] or 0}
            for r in model_rows
        }

        op_rows = self._conn.execute(
            """SELECT operation,
                      SUM(tokens_served) AS tokens,
                      COUNT(*)           AS count
               FROM analytics_events
               WHERE session_date >= ?
               GROUP BY operation""",
            (cutoff,)
        ).fetchall()
        by_operation = {
            r["operation"]: {"tokens": r["tokens"] or 0, "count": r["count"] or 0}
            for r in op_rows
        }

        return {
            "daily_stats": daily_stats,
            "by_model": by_model,
            "by_operation": by_operation,
        }

    def get_rule_confidence_after(self, queue_id: str) -> dict | None:
        """Return rule_name and confidence for the pruner_rule linked to a queue item."""
        row = self._conn.execute(
            """SELECT rule_name, confidence FROM pruner_rules
               JOIN prune_queue ON pruner_rules.rule_name = prune_queue.rule_name
               WHERE prune_queue.id = ?""",
            (queue_id,)
        ).fetchone()
        if row is None:
            return None
        return {"rule_name": row["rule_name"], "confidence": row["confidence"]}

    # -------------------------------------------------------------------------
    # Stats
    # -------------------------------------------------------------------------

    def token_stats(self, profile: str) -> dict:
        row = self._conn.execute(
            """SELECT COUNT(*) as count, SUM(token_count) as total,
                      AVG(token_count) as avg
               FROM memories WHERE profile=? AND archived=0""", (profile,)
        ).fetchone()
        by_cat = {}
        for r in self._conn.execute(
            "SELECT category, SUM(token_count) as t FROM memories "
            "WHERE profile=? AND archived=0 GROUP BY category", (profile,)
        ).fetchall():
            by_cat[r["category"]] = r["t"] or 0
        return {
            "memory_count": row["count"] or 0,
            "total_tokens": row["total"] or 0,
            "avg_tokens_per_memory": int(row["avg"] or 0),
            "by_category": by_cat,
        }

    # -------------------------------------------------------------------------
    # Phase 4: Embeddings + hybrid search
    # -------------------------------------------------------------------------

    def _get_embed_model(self):
        """Lazy-load FastEmbed model once per process (thread-safe)."""
        if self._embed_model is None:
            with self._embed_lock:
                if self._embed_model is None:
                    from fastembed import TextEmbedding
                    self._embed_model = TextEmbedding("BAAI/bge-small-en-v1.5")
        return self._embed_model

    def _embed_texts(self, texts: list[str]) -> list[list[float]]:
        """Return list of 384-dim float vectors."""
        model = self._get_embed_model()
        return [v.tolist() for v in model.embed(texts)]

    def _embed_one(self, memory_id: str, profile: str, content: str) -> bool:
        """Embed and persist a single memory's vector. Fail-soft (issue #5):
        if fastembed is unavailable or inference fails, the memory still
        exists and FTS search still works — semantic search just won't see
        it until the next backfill. Returns True on success."""
        try:
            vec = self._embed_texts([content])[0]
            # Guard against the memory having been deleted while embedding
            with self._conn.transaction():
                exists = self._conn.execute(
                    "SELECT 1 FROM memories WHERE id=?", (memory_id,)
                ).fetchone()
                if exists:
                    self._conn.execute(
                        "INSERT OR REPLACE INTO memory_embeddings(id, profile, vector) "
                        "VALUES(?,?,?)",
                        (memory_id, profile, json.dumps(vec))
                    )
                    self._conn.commit()
            return True
        except Exception as e:
            import sys
            print(f"[memorybridge] embed failed for {memory_id}: {e}",
                  file=sys.stderr)
            return False

    def _embed_and_cleanup(self, mid: str, profile: str, content: str) -> None:
        """Wrapper around _embed_one that removes thread from pending set."""
        try:
            self._embed_one(mid, profile, content)
        finally:
            with self._pending_embed_lock:
                self._pending_embeds.discard(threading.current_thread())

    def drain_embeds(self, timeout: float = 3.0) -> None:
        """Block until all pending embed threads finish, or timeout.

        Call before os._exit(0) in the SIGTERM handler to avoid leaving
        orphaned memory_embeddings rows that trigger backfill on restart.
        """
        with self._pending_embed_lock:
            threads = list(self._pending_embeds)
        if not threads:
            return
        per_thread = timeout / max(len(threads), 1)
        for t in threads:
            t.join(timeout=per_thread)

    def _backfill_missing_embeddings(self) -> int:
        """Embed all active memories that have no vector (issue #5).
        Runs on a background thread at startup; fail-soft."""
        try:
            rows = self._conn.execute(
                """SELECT m.id, m.profile, m.content FROM memories m
                   LEFT JOIN memory_embeddings e ON m.id = e.id
                   WHERE m.archived = 0 AND e.id IS NULL"""
            ).fetchall()
            if not rows:
                return 0
            texts = [r["content"] for r in rows]
            vectors = self._embed_texts(texts)
            with self._conn.transaction():
                self._conn.executemany(
                    "INSERT OR REPLACE INTO memory_embeddings(id, profile, vector) "
                    "VALUES(?,?,?)",
                    [(r["id"], r["profile"], json.dumps(v))
                     for r, v in zip(rows, vectors)]
                )
                self._conn.commit()
            import sys
            print(f"[memorybridge] backfilled {len(rows)} missing embeddings",
                  file=sys.stderr)
            return len(rows)
        except Exception as e:
            import sys
            print(f"[memorybridge] embedding backfill skipped: {e}",
                  file=sys.stderr)
            return 0

    def build_embeddings(self, profile: str) -> int:
        """
        Compute and persist embeddings for all active memories in a profile.
        Returns count of embeddings written.
        Idempotent — re-running updates existing rows.
        """
        rows = self._conn.execute(
            "SELECT id, content FROM memories WHERE profile=? AND archived=0",
            (profile,)
        ).fetchall()
        if not rows:
            return 0

        ids = [r["id"] for r in rows]
        texts = [r["content"] for r in rows]
        vectors = self._embed_texts(texts)

        self._conn.executemany(
            "INSERT OR REPLACE INTO memory_embeddings(id, profile, vector) VALUES(?,?,?)",
            [(mid, profile, json.dumps(vec)) for mid, vec in zip(ids, vectors)]
        )
        self._conn.commit()
        return len(ids)

    def _cosine_similarity(self, a: list[float], b: list[float]) -> float:
        """Fast cosine similarity using numpy. Returns 0.0 on a shape mismatch
        rather than raising, so a stale-dimension vector can't crash a search."""
        if len(a) != len(b):
            return 0.0
        import numpy as np
        va, vb = np.array(a, dtype=np.float32), np.array(b, dtype=np.float32)
        denom = np.linalg.norm(va) * np.linalg.norm(vb)
        return float(np.dot(va, vb) / denom) if denom > 0 else 0.0

    def search_semantic(self, profile: str, query: str,
                        limit: int = 5, max_tokens: int = 800) -> list[dict]:
        """
        Vector cosine-similarity search against stored embeddings.
        Falls back to FTS5 if no embeddings have been built for this profile.
        """
        # Check embeddings exist for this profile
        count = self._conn.execute(
            "SELECT COUNT(*) FROM memory_embeddings WHERE profile=?", (profile,)
        ).fetchone()[0]
        if count == 0:
            return self.search(profile, query, limit=limit, max_tokens=max_tokens)

        # Embed query
        q_vec = self._embed_texts([query])[0]
        q_len = len(q_vec)

        # Fetch all embedding rows for profile
        rows = self._conn.execute(
            "SELECT e.id, e.vector FROM memory_embeddings e WHERE e.profile=?",
            (profile,)
        ).fetchall()

        # Score by cosine similarity. Guard against stale/mismatched vectors:
        # if the embedding model ever changes dimension, old rows would make
        # numpy's dot product raise and take down every search. Skip any vector
        # whose length differs from the current query vector (or won't parse),
        # and fall back to keyword search if none are usable.
        scored = []
        mismatched = 0
        for row in rows:
            try:
                vec = json.loads(row["vector"])
            except (json.JSONDecodeError, TypeError):
                mismatched += 1
                continue
            if not isinstance(vec, list) or len(vec) != q_len:
                mismatched += 1
                continue
            scored.append((row["id"], self._cosine_similarity(q_vec, vec)))

        if mismatched:
            logger.warning(
                "search_semantic: %d/%d embeddings for profile '%s' have a stale "
                "dimension (current=%d) and were skipped — rebuild with "
                "build_embeddings(profile).", mismatched, len(rows), profile, q_len)
        if not scored:
            # Every stored vector is unusable (e.g. model changed) — don't return
            # an empty result set; degrade to keyword/FTS search.
            return self.search(profile, query, limit=limit, max_tokens=max_tokens)

        # Sort descending by similarity
        scored.sort(key=lambda x: x[1], reverse=True)
        top_ids = [mid for mid, _ in scored[:limit * 2]]

        if not top_ids:
            return []

        # Fetch full memory rows for top IDs (excluding archived)
        placeholders = ",".join("?" * len(top_ids))
        mem_rows = self._conn.execute(
            f"SELECT * FROM memories WHERE id IN ({placeholders}) AND archived=0",
            top_ids
        ).fetchall()

        # Re-rank in similarity order
        mem_by_id = {r["id"]: self._row_to_dict(r) for r in mem_rows}
        sim_by_id = {mid: sim for mid, sim in scored}
        results, tokens_used = [], 0
        for mid, _ in scored:
            if mid not in mem_by_id:
                continue
            m = mem_by_id[mid]
            m["match_score"] = sim_by_id[mid]
            if tokens_used + m["token_count"] <= max_tokens:
                results.append(m)
                tokens_used += m["token_count"]
            if len(results) >= limit:
                break
        return results

    def search_hybrid(self, profile: str, query: str,
                      category: str = None,
                      limit: int = 5, max_tokens: int = 800,
                      recency_boost: bool = True,
                      include_related: bool = True) -> list[dict]:
        """
        Reciprocal Rank Fusion of FTS5 BM25 + semantic cosine results.
        RRF score = sum(1 / (60 + rank)) across both lists.
        Falls back to FTS5-only if no embeddings built.

        *recency_boost* — when True (default), applies recency weighting.
        *include_related* — when True (default), expands results with
        entity-tag-related memories.
        """
        keyword_results = self.search(profile, query, category=category,
                                      limit=limit * 2, max_tokens=max_tokens * 2)
        semantic_results = self.search_semantic(profile, query, limit=limit * 2,
                                                max_tokens=max_tokens * 2)

        scores: dict[str, float] = {}
        all_mems: dict[str, dict] = {}
        sim_scores: dict[str, float] = {}

        for rank, mem in enumerate(keyword_results):
            mid = mem["id"]
            scores[mid] = scores.get(mid, 0.0) + 1.0 / (60 + rank)
            all_mems[mid] = mem

        for rank, mem in enumerate(semantic_results):
            mid = mem["id"]
            scores[mid] = scores.get(mid, 0.0) + 1.0 / (60 + rank)
            all_mems[mid] = mem
            sim_scores[mid] = mem.get("match_score", 0.0)

        ranked = sorted(scores, key=lambda k: scores[k], reverse=True)

        # Build result list with recency adjustment
        results, tokens_used = [], 0
        for mid in ranked:
            mem = all_mems[mid]
            mem["match_score"] = sim_scores.get(mid, mem.get("match_score", _jaccard_similarity(query, mem["content"])))
            if tokens_used + mem["token_count"] <= max_tokens:
                results.append(mem)
                tokens_used += mem["token_count"]
            if len(results) >= limit:
                break

        # Apply recency weighting if configured (re-ranks within search results)
        if recency_boost and self._recency_decay_days > 0 and results:
            results = self._re_rank_by_recency(results)

        # Expand with related memories by entity tag overlap
        if include_related and results:
            results = self._expand_related(results, profile)

        return results

    # -------------------------------------------------------------------------
    # Reflect — synthesis tool
    # -------------------------------------------------------------------------

    def reflect(self, profile: str, question: str,
                limit: int = 15, max_tokens: int = 3000) -> dict[str, Any]:
        """Synthesize a reasoned answer from memories.

        Retrieves relevant memories and produces a structured synthesis.
        Uses the configured ``llm_synthesize`` callable if provided, otherwise
        falls back to keyword-based summary.

        Returns a dict with keys: question, key_facts, dates, preferences,
        contradictions, confidence, raw_synthesis, memory_count, entity_groups.
        """
        from db.reflect import Reflector

        # Build the Reflector per call. Caching it on the instance froze the
        # first call's profile/limit/max_tokens into the search closure, so a
        # later reflect() for a different profile searched the first profile's
        # memories — a cross-profile data leak. The Reflector is cheap to build.
        reflector = Reflector(
            search_fn=lambda q: self.search_hybrid(
                profile, q, limit=limit, max_tokens=max_tokens
            ),
            llm_synthesize=self._llm_synthesize,
        )
        return reflector.reflect(question)

    # -------------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------------

    def _row_to_dict(self, row) -> dict:
        m = dict(row)
        if isinstance(m.get("tags"), str):
            m["tags"] = json.loads(m["tags"])
        return m

    def _re_rank_by_recency(self, results: list[dict]) -> list[dict]:
        """Re-rank search results by recency-adjusted score.

        Each result's relevance score is multiplied by an exponential decay
        factor: ``0.5 ** (days_old / recency_decay_days)`` (same formula as
        ``effective_score`` in constants.py). Results are re-sorted by the
        adjusted score.

        Only results with a ``created_at`` field are affected — memories
        without a valid timestamp keep their original rank.
        """
        if not results or self._recency_decay_days <= 0:
            return results

        now = datetime.now().replace(tzinfo=None)
        half_life = float(self._recency_decay_days)

        scored: list[tuple[float, int, dict]] = []
        for idx, mem in enumerate(results):
            raw_score = mem.get("match_score", 0.5)
            created = mem.get("created_at", "")
            if created:
                try:
                    dt = datetime.fromisoformat(created).replace(tzinfo=None)
                    days_old = (now - dt).total_seconds() / 86400.0
                    recency_factor = 0.5 ** (days_old / half_life)
                    adjusted = raw_score * recency_factor
                except (ValueError, TypeError):
                    adjusted = raw_score
            else:
                adjusted = raw_score
            scored.append((adjusted, idx, mem))

        # Sort by adjusted score descending, then original index as tiebreak
        scored.sort(key=lambda x: (-x[0], x[1]))
        return [mem for _, _, mem in scored]

    def _expand_related(self, results: list[dict], profile: str,
                        max_related: int = 3) -> list[dict]:
        """Expand search results with related memories by entity tag overlap.

        Collects all entity tags from the primary results, then finds
        additional memories that share ≥2 of those tags. Related results
        are appended after primary results with a ``related: true`` flag.

        Skips memories already in the primary result set. Caps at
        ``max_related`` additional results.
        """
        if not results or max_related <= 0:
            return results

        # Collect all entity tags from primary results
        all_entity_tags: set[str] = set()
        existing_ids: set[str] = set()
        for mem in results:
            existing_ids.add(mem["id"])
            tags = mem.get("tags", [])
            for tag in tags:
                if isinstance(tag, str) and tag.startswith("entity:"):
                    all_entity_tags.add(tag)

        if len(all_entity_tags) < 2:
            return results  # Not enough entity signal

        # Query for memories sharing ≥2 entity tags.
        # Bind tags as ? placeholders — these values originate from stored
        # `tags` (ultimately caller-supplied) and must never be interpolated.
        entity_tag_list = list(all_entity_tags)
        placeholders = ",".join("?" for _ in entity_tag_list)
        try:
            rows = self._conn.execute(
                f"""SELECT m.*, COUNT(DISTINCT jt.value) AS shared
                    FROM memories m, json_each(m.tags) AS jt
                    WHERE m.profile = ?
                      AND m.archived = 0
                      AND jt.value IN ({placeholders})
                    GROUP BY m.id
                    HAVING shared >= 2
                    ORDER BY shared DESC, m.relevance_score DESC
                    LIMIT ?""",
                (profile, *entity_tag_list, max_related * 2),
            ).fetchall()
        except Exception as e:
            logger.debug("Related expansion query failed: %s", e)
            return results

        related: list[dict] = []
        for row in rows:
            mem = self._row_to_dict(row)
            if mem["id"] in existing_ids:
                continue
            mem["related"] = True
            mem["match_score"] = row["shared"] / max(len(all_entity_tags), 1)
            related.append(mem)
            existing_ids.add(mem["id"])
            if len(related) >= max_related:
                break

        if not related:
            return results

        return results + related
