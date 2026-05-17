"""
Phase 6 passport tests — export_passport MCP tool and ingestion/passport.py.

Run: python -m pytest tests/unit/test_passport.py -v
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import pytest
from db.store import MemoryStore


@pytest.fixture
def fresh_store(tmp_path, monkeypatch):
    import server
    s = MemoryStore(tmp_path / "test.db")
    monkeypatch.setattr(server, "_store", s)
    s.ensure_profile("default")

    # Seed memories across categories
    facts = [
        ("Prefers dark mode in all apps", "preference", "medium"),
        ("No em dashes in writing", "preference", "high"),
        ("Prefers bullet-point answers", "preference", "medium"),
        ("PMP certified", "skill", "high"),
        ("Has $126M in documented program impact", "skill", "high"),
        ("Eight years in the United States Air Force", "fact", "high"),
        ("Works as a program leader and consultant", "fact", "high"),
        ("Cannot scrape LinkedIn via automated tools", "constraint", "critical"),
        ("Volunteers for Hopeful Fridays", "fact", "medium"),
        ("Tone: direct, sardonic, Gen X peer-to-peer", "preference", "high"),
    ]
    for content, cat, imp in facts:
        s.add_memory("default", content, category=cat, importance=imp)
    return s


def test_passport_is_plain_text(fresh_store):
    """Passport must be plain text — no JSON, no markdown code fences."""
    import server
    passport = server.export_passport.fn(profile="default", max_tokens=2000)
    assert not passport.startswith("{"), "Passport should not be JSON"
    assert "```" not in passport, "Passport should not contain code fences"
    assert "# Memory Passport" in passport, "Missing passport header"


def test_passport_fits_token_budget(fresh_store):
    """export_passport should never exceed max_tokens (10% tolerance)."""
    import server
    passport = server.export_passport.fn(profile="default", max_tokens=500)
    tokens = server.count_tokens(passport)
    assert tokens <= 550, f"Passport exceeded budget: {tokens} tokens (limit 550)"


def test_passport_contains_category_sections(fresh_store):
    """Passport should include section headers for populated categories."""
    import server
    passport = server.export_passport.fn(profile="default", max_tokens=2000)
    assert "## Preferences" in passport
    assert "## Skills" in passport
    assert "## Constraints" in passport


def test_passport_includes_memory_content(fresh_store):
    """Passport should include the actual memory text."""
    import server
    passport = server.export_passport.fn(profile="default", max_tokens=2000)
    assert "PMP" in passport
    assert "dark mode" in passport


def test_passport_has_metadata_header(fresh_store):
    """Passport header should include generation date and profile name."""
    import server
    passport = server.export_passport.fn(profile="default", max_tokens=2000)
    assert "Profile: default" in passport
    assert "Generated:" in passport


def test_passport_respects_small_budget(fresh_store):
    """Very small budget should still produce valid passport, just truncated."""
    import server
    passport = server.export_passport.fn(profile="default", max_tokens=100)
    tokens = server.count_tokens(passport)
    assert tokens <= 115, f"Small budget not respected: {tokens} tokens"
    assert "# Memory Passport" in passport  # header always present


def test_passport_builder_standalone():
    """ingestion/passport.py build_passport() should work independently of server."""
    from ingestion.passport import build_passport

    memories = [
        {"content": "Prefers dark mode", "category": "preference",
         "importance": "medium", "token_count": 25},
        {"content": "PMP certified", "category": "skill",
         "importance": "high", "token_count": 20},
    ]
    identity = {"name": "Cale", "role": "Consultant"}
    text = build_passport(memories, identity, profile="default", max_tokens=500)

    assert "# Memory Passport" in text
    assert "Prefers dark mode" in text
    assert "PMP certified" in text
    assert "Cale" in text
