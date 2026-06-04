"""SQLite persistence layer for MemoryBridge. Zero MCP coupling.

All SQL lives here. server.py contains zero SQL.
Tool signatures are unchanged — this is a drop-in persistence swap.
"""

import hashlib
import json
import sqlite3
import threading
import uuid
from datetime import datetime
from pathlib import Path

SCHEMA_SQL = Path(__file__).parent / "schema.sql"

VALID_CATEGORIES = [
    "preference", "fact", "insight", "decision",
    "project_status", "relationship", "skill", "constraint"
]
IMPORTANCE_LEVELS = ["low", "medium", "high", "critical"]


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


def _mem_id() -> str:
    return f"mem_{uuid.uuid4().hex[:8]}"


def _content_hash(content: str) -> str:
    """SHA256 of normalized content — same normalization as Phase 2.5 server.py."""
    return hashlib.sha256(content.strip().lower().encode()).hexdigest()


def _count_tokens(text: str) -> int:
    try:
        import tiktoken
        enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text)) + 20
    except Exception:
        return len(text.split()) + 20


class MemoryStore:
    """
    SQLite-backed memory store with WAL mode, FTS5 search, and content-hash dedup.

    Usage:
        store = MemoryStore(Path.home() / "memorybridge" / "memory.db")
        store.ensure_profile("default")
        mid = store.add_memory("default", "Cale prefers dark mode", category="preference")
    """

    def __init__(self, db_path: Path):
        self._path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False is safe only because _LockedConnection
        # serializes every statement (issue #6).
        self._conn = _LockedConnection(
            sqlite3.connect(str(db_path), check_same_thread=False)
        )
        self._conn.row_factory = sqlite3.Row
        # FK enforcement is OFF by default in SQLite and is per-connection —
        # without this, ON DELETE CASCADE (memory_embeddings) never fires.
        # Issue #4: 103/128 embeddings were orphaned because of this.
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA_SQL.read_text())
        # executescript commits and can reset pragmas — re-assert.
        self._conn.execute("PRAGMA foreign_keys=ON")
        # Pruner tables (idempotent CREATE IF NOT EXISTS)
        from db.pruner import PRUNER_SCHEMA, bootstrap_rules
        self._conn.executescript(PRUNER_SCHEMA)
        bootstrap_rules(self._conn)
        self._conn.commit()
        # One-time hygiene (issue #4): purge embedding rows orphaned while FK
        # enforcement was off. Idempotent and cheap on every startup.
        self._conn.execute(
            "DELETE FROM memory_embeddings "
            "WHERE id NOT IN (SELECT id FROM memories)"
        )
        self._conn.commit()
        # Phase 4: lazy-loaded FastEmbed model (cached per-process)
        self._embed_model = None
        self._embed_lock = None

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
                   tags: list = None, project_id: str = None) -> str | None:
        """Returns memory ID on success, None if exact duplicate."""
        self.ensure_profile(profile)
        h = _content_hash(content)
        mid = _mem_id()
        now = datetime.now().strftime("%Y-%m-%d")
        tc = _count_tokens(content)
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
                     now, now, json.dumps(tags or []), project_id, tc)
                )
                # Update FTS index
                self._conn.execute(
                    "INSERT INTO memories_fts(rowid,content) "
                    "SELECT rowid,content FROM memories WHERE id=?", (mid,)
                )
                self._conn.commit()
            return mid
        except sqlite3.IntegrityError:
            self._conn.rollback()
            return None  # duplicate content_hash

    def add_memories(self, profile: str, facts: list[str], *,
                     category: str = "fact", importance: str = "medium",
                     project_id: str = None) -> int:
        """Batch insert. Returns count of actually inserted rows."""
        added = 0
        for fact in facts:
            if self.add_memory(profile, fact, category=category,
                               importance=importance, project_id=project_id):
                added += 1
        return added

    def delete_memory(self, profile: str, memory_id: str) -> int:
        """Delete memory. Returns token_count freed, or 0 if not found."""
        # SELECT + deletes + commit must be atomic vs. other threads
        # (issue #6): a concurrent delete of the same id would otherwise
        # double-issue the FTS delete command against a stale rowid.
        with self._conn.transaction():
            row = self._conn.execute(
                "SELECT rowid, token_count FROM memories WHERE id=? AND profile=?",
                (memory_id, profile)
            ).fetchone()
            if not row:
                return 0
            rowid = row["rowid"]
            tc = row["token_count"]
            self._conn.execute("DELETE FROM memories WHERE id=?", (memory_id,))
            # Belt-and-suspenders for issue #4: FK CASCADE handles this when
            # foreign_keys=ON, but delete explicitly so a future connection
            # missing the pragma can't re-orphan embeddings.
            self._conn.execute(
                "DELETE FROM memory_embeddings WHERE id=?", (memory_id,)
            )
            # Remove from FTS (content-less table delete)
            self._conn.execute(
                "INSERT INTO memories_fts(memories_fts,rowid,content) VALUES('delete',?,?)",
                (rowid, "")
            )
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
        except sqlite3.OperationalError:
            # FTS query parse error — fall back to empty
            return []

        results, tokens_used = [], 0
        for row in rows:
            m = self._row_to_dict(row)
            m.pop("bm25_score", None)
            if tokens_used + m["token_count"] <= max_tokens:
                results.append(m)
                tokens_used += m["token_count"]
            if len(results) >= limit:
                break
        return results

    def boost_on_access(self, profile: str, memory_id: str,
                        boost: float = 0.1) -> None:
        """Increment access_count and boost relevance_score (capped at 1.0)."""
        today = datetime.now().strftime("%Y-%m-%d")
        self._conn.execute(
            """UPDATE memories
               SET access_count = access_count + 1,
                   last_accessed = ?,
                   relevance_score = MIN(relevance_score + ?, 1.0)
               WHERE id = ? AND profile = ?""",
            (today, boost, memory_id, profile)
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
            try:
                created = datetime.fromisoformat(row["created_at"])
            except Exception:
                created = now
            days = (now - created).days
            decay = 0.5 ** (days / 30)
            importance_boost = {
                "low": 0.8, "medium": 1.0, "high": 1.2, "critical": 1.5
            }.get(row["importance"], 1.0)
            access_boost = 1 + min(row["access_count"] * 0.05, 0.5)
            effective = max(
                row["relevance_score"] * decay * importance_boost * access_boost,
                0.1
            )
            if effective < threshold:
                to_archive.append(row["id"])

        if to_archive:
            now_str = now.strftime("%Y-%m-%d")
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
            import threading
            if self._embed_lock is None:
                self._embed_lock = threading.Lock()
            with self._embed_lock:
                if self._embed_model is None:
                    from fastembed import TextEmbedding
                    self._embed_model = TextEmbedding("BAAI/bge-small-en-v1.5")
        return self._embed_model

    def _embed_texts(self, texts: list[str]) -> list[list[float]]:
        """Return list of 384-dim float vectors."""
        model = self._get_embed_model()
        return [v.tolist() for v in model.embed(texts)]

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
        """Fast cosine similarity using numpy."""
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

        # Fetch all embedding rows for profile
        rows = self._conn.execute(
            "SELECT e.id, e.vector FROM memory_embeddings e WHERE e.profile=?",
            (profile,)
        ).fetchall()

        # Score by cosine similarity
        scored = []
        for row in rows:
            vec = json.loads(row["vector"])
            sim = self._cosine_similarity(q_vec, vec)
            scored.append((row["id"], sim))

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
        results, tokens_used = [], 0
        for mid, _ in scored:
            if mid not in mem_by_id:
                continue
            m = mem_by_id[mid]
            if tokens_used + m["token_count"] <= max_tokens:
                results.append(m)
                tokens_used += m["token_count"]
            if len(results) >= limit:
                break
        return results

    def search_hybrid(self, profile: str, query: str,
                      category: str = None,
                      limit: int = 5, max_tokens: int = 800) -> list[dict]:
        """
        Reciprocal Rank Fusion of FTS5 BM25 + semantic cosine results.
        RRF score = sum(1 / (60 + rank)) across both lists.
        Falls back to FTS5-only if no embeddings built.
        """
        keyword_results = self.search(profile, query, category=category,
                                      limit=limit * 2, max_tokens=max_tokens * 2)
        semantic_results = self.search_semantic(profile, query, limit=limit * 2,
                                                max_tokens=max_tokens * 2)

        scores: dict[str, float] = {}
        all_mems: dict[str, dict] = {}

        for rank, mem in enumerate(keyword_results):
            mid = mem["id"]
            scores[mid] = scores.get(mid, 0.0) + 1.0 / (60 + rank)
            all_mems[mid] = mem

        for rank, mem in enumerate(semantic_results):
            mid = mem["id"]
            scores[mid] = scores.get(mid, 0.0) + 1.0 / (60 + rank)
            all_mems[mid] = mem

        ranked = sorted(scores, key=lambda k: scores[k], reverse=True)

        results, tokens_used = [], 0
        for mid in ranked:
            mem = all_mems[mid]
            if tokens_used + mem["token_count"] <= max_tokens:
                results.append(mem)
                tokens_used += mem["token_count"]
            if len(results) >= limit:
                break
        return results

    # -------------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------------

    def _row_to_dict(self, row) -> dict:
        m = dict(row)
        if isinstance(m.get("tags"), str):
            m["tags"] = json.loads(m["tags"])
        return m
