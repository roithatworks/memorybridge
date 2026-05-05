"""
Phase 3 migration tests — verify memory.json → SQLite migration correctness.
Run: python -m pytest tests/unit/test_db.py -v
"""
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))


def make_memory_json(tmp_path):
    data = {
        "version": "1.3",
        "profiles": {
            "default": {
                "identity": {"name": "Cale"},
                "memories": [
                    {"id": "mem_001", "content": "Prefers dark mode",
                     "category": "preference", "importance": "medium",
                     "relevance_score": 1.0, "created_at": "2026-01-01",
                     "last_accessed": "2026-01-01", "access_count": 0,
                     "tags": [], "project_id": None, "token_count": 20},
                    {"id": "mem_002", "content": "PMP certified",
                     "category": "skill", "importance": "high",
                     "relevance_score": 0.9, "created_at": "2026-01-01",
                     "last_accessed": "2026-01-01", "access_count": 2,
                     "tags": [], "project_id": None, "token_count": 15}
                ],
                "archived": [],
                "projects": [],
                "model_preferences": {}
            }
        }
    }
    p = tmp_path / "memory.json"
    p.write_text(json.dumps(data))
    return p


def test_migration_preserves_memory_count(tmp_path, monkeypatch):
    import db.migrate as migrate_mod
    monkeypatch.setattr(migrate_mod, "MEMORY_JSON", make_memory_json(tmp_path))
    db_path = tmp_path / "memory.db"
    monkeypatch.setattr(migrate_mod, "MEMORY_DB", db_path)

    migrate_mod.migrate()

    conn = sqlite3.connect(str(db_path))
    count = conn.execute("SELECT COUNT(*) FROM memories WHERE archived=0").fetchone()[0]
    conn.close()
    assert count == 2, f"Expected 2 memories, got {count}"


def test_migration_deduplicates_identical_content(tmp_path, monkeypatch):
    import db.migrate as migrate_mod
    data = json.loads(make_memory_json(tmp_path).read_text())
    # Add exact duplicate content with different ID
    data["profiles"]["default"]["memories"].append(
        {**data["profiles"]["default"]["memories"][0], "id": "mem_003"}
    )
    p = tmp_path / "memory.json"
    p.write_text(json.dumps(data))
    monkeypatch.setattr(migrate_mod, "MEMORY_JSON", p)
    db_path = tmp_path / "memory.db"
    monkeypatch.setattr(migrate_mod, "MEMORY_DB", db_path)

    migrate_mod.migrate()

    conn = sqlite3.connect(str(db_path))
    count = conn.execute("SELECT COUNT(*) FROM memories WHERE archived=0").fetchone()[0]
    conn.close()
    assert count == 2, f"Duplicate not deduplicated — got {count} memories"


def test_fts_index_is_populated(tmp_path, monkeypatch):
    import db.migrate as migrate_mod
    monkeypatch.setattr(migrate_mod, "MEMORY_JSON", make_memory_json(tmp_path))
    db_path = tmp_path / "memory.db"
    monkeypatch.setattr(migrate_mod, "MEMORY_DB", db_path)
    migrate_mod.migrate()

    conn = sqlite3.connect(str(db_path))
    results = conn.execute(
        "SELECT rowid FROM memories_fts WHERE memories_fts MATCH 'dark'"
    ).fetchall()
    conn.close()
    assert len(results) > 0, "FTS index not populated — 'dark' not findable"


def test_migration_preserves_profile_identity(tmp_path, monkeypatch):
    import db.migrate as migrate_mod
    monkeypatch.setattr(migrate_mod, "MEMORY_JSON", make_memory_json(tmp_path))
    db_path = tmp_path / "memory.db"
    monkeypatch.setattr(migrate_mod, "MEMORY_DB", db_path)
    migrate_mod.migrate()

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT identity FROM profiles WHERE name='default'").fetchone()
    conn.close()
    identity = json.loads(row["identity"])
    assert identity.get("name") == "Cale", f"Identity not preserved: {identity}"


def test_migration_idempotent(tmp_path, monkeypatch):
    """Running migrate twice should not duplicate memories."""
    import db.migrate as migrate_mod
    json_path = make_memory_json(tmp_path)
    db_path = tmp_path / "memory.db"
    monkeypatch.setattr(migrate_mod, "MEMORY_JSON", json_path)
    monkeypatch.setattr(migrate_mod, "MEMORY_DB", db_path)

    migrate_mod.migrate()
    # Restore json for second run (migrate renames it)
    backup = json_path.with_suffix(".json.pre-sqlite-backup")
    if backup.exists():
        import shutil
        shutil.copy(backup, json_path)
    monkeypatch.setattr(migrate_mod, "MEMORY_JSON", json_path)
    migrate_mod.migrate()

    conn = sqlite3.connect(str(db_path))
    count = conn.execute("SELECT COUNT(*) FROM memories WHERE archived=0").fetchone()[0]
    conn.close()
    assert count == 2, f"Idempotency failed — got {count} after second run"
