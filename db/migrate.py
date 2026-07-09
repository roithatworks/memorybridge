"""One-time migration: memory.json → SQLite.

Usage:
    python db/migrate.py

Backs up memory.json to memory.json.pre-sqlite-backup before migrating.
Safe to re-run — duplicates are skipped via content_hash uniqueness.
"""
import hashlib
import json
import sqlite3
from pathlib import Path

MEMORY_JSON = Path.home() / "memorybridge" / "memory.json"
MEMORY_DB   = Path.home() / "memorybridge" / "memory.db"
SCHEMA_SQL  = Path(__file__).parent / "schema.sql"


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.strip().lower().encode()).hexdigest()


def migrate():
    if not MEMORY_JSON.exists():
        print("No memory.json found — nothing to migrate.")
        return

    print(f"Migrating {MEMORY_JSON} → {MEMORY_DB}")
    data = json.loads(MEMORY_JSON.read_text())

    conn = sqlite3.connect(str(MEMORY_DB))
    # Match the store's connection settings so this can't deadlock/error if the
    # server is running, and so the migration is durable (#92).
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(SCHEMA_SQL.read_text())  # DDL auto-commits

    # Migrate all rows in ONE transaction — a mid-loop failure rolls back the
    # whole thing instead of leaving a half-populated DB (#92).
    try:
        conn.execute("BEGIN")
        total_written, total_skipped = _migrate_rows(conn, data)
        conn.execute("INSERT INTO memories_fts(memories_fts) VALUES('rebuild')")
        conn.commit()
    except Exception:
        conn.rollback()
        conn.close()
        raise

    active_count = conn.execute(
        "SELECT COUNT(*) FROM memories WHERE archived=0").fetchone()[0]
    conn.close()

    # Back up the original JSON only AFTER a successful, committed migration.
    backup = MEMORY_JSON.with_suffix(".json.pre-sqlite-backup")
    if not backup.exists():
        MEMORY_JSON.rename(backup)
        print(f"Original backed up to {backup}")
    else:
        print(f"Backup already exists at {backup} — keeping memory.json in place")

    print("Migration complete.")
    print(f"  Active memories in DB : {active_count}")
    print(f"  Written: {total_written}, Skipped (dup): {total_skipped}")
    return


def _migrate_rows(conn, data):
    total_written = 0
    total_skipped = 0
    for profile_name, profile_data in data.get("profiles", {}).items():
        conn.execute(
            "INSERT OR REPLACE INTO profiles VALUES (?,?,?,?)",
            (profile_name,
             json.dumps(profile_data.get("identity", {})),
             json.dumps(profile_data.get("projects", [])),
             json.dumps(profile_data.get("model_preferences", {})))
        )

        for mem in profile_data.get("memories", []):
            mem_id = mem.get("id")
            content = mem.get("content")
            if not mem_id or content is None:
                total_skipped += 1
                continue
            h = _content_hash(content)
            try:
                conn.execute(
                    """INSERT INTO memories
                       (id,profile,content,content_hash,category,importance,
                        relevance_score,created_at,last_accessed,access_count,
                        tags,project_id,token_count)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (mem_id, profile_name, content, h,
                     mem.get("category", "general"),
                     mem.get("importance", "medium"),
                     mem.get("relevance_score", 1.0),
                     mem.get("created_at", "2026-01-01"),
                     mem.get("last_accessed", "2026-01-01"),
                     mem.get("access_count", 0),
                     json.dumps(mem.get("tags", [])),
                     mem.get("project_id"),
                     mem.get("token_count", 20))
                )
                total_written += 1
            except sqlite3.IntegrityError:
                total_skipped += 1

        for mem in profile_data.get("archived", []):
            mem_id = mem.get("id")
            content = mem.get("content")
            if not mem_id or content is None:
                continue
            h = _content_hash(content)
            try:
                conn.execute(
                    """INSERT INTO memories
                       (id,profile,content,content_hash,category,importance,
                        relevance_score,created_at,last_accessed,access_count,
                        tags,project_id,token_count,archived,archived_at,archive_reason)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)""",
                    (mem_id, profile_name, content, h,
                     mem.get("category", "general"),
                     mem.get("importance", "medium"),
                     mem.get("relevance_score", 1.0),
                     mem.get("created_at", "2026-01-01"),
                     mem.get("last_accessed", "2026-01-01"),
                     mem.get("access_count", 0),
                     json.dumps(mem.get("tags", [])),
                     mem.get("project_id"),
                     mem.get("token_count", 20),
                     mem.get("archived_at"),
                     mem.get("archive_reason"))
                )
            except sqlite3.IntegrityError:
                pass

    return total_written, total_skipped


if __name__ == "__main__":
    migrate()
