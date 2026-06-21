"""Write-path guardrail tests.
Run: python -m pytest tests/unit/test_guardrail.py -v
"""
import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from db.constants import guardrail_check, MAX_MEMORY_TOKENS  # noqa: E402
from db.store import MemoryStore, GuardrailRejection  # noqa: E402


@pytest.fixture
def db(tmp_path):
    return MemoryStore(tmp_path / "g.db")


# --- pure function ---------------------------------------------------------

def test_short_fact_passes():
    ok, reason = guardrail_check("Cale prefers the Oxford comma and two spaces after periods.")
    assert ok, reason


def test_empty_rejected():
    ok, _ = guardrail_check("   ")
    assert not ok


def test_markdown_heading_rejected():
    ok, reason = guardrail_check("# LinkedIn Strategy\n\nSome long doc here.")
    assert not ok
    assert "heading" in reason


def test_long_content_rejected():
    big = "word " * (MAX_MEMORY_TOKENS + 200)
    ok, reason = guardrail_check(big)
    assert not ok
    assert "too long" in reason


def test_multisection_doc_rejected():
    doc = "Intro line.\n\n## Section One\nx\n\n## Section Two\ny"
    ok, reason = guardrail_check(doc)
    assert not ok


def test_many_newlines_rejected():
    ok, _ = guardrail_check("a\n" * 20)
    assert not ok


# --- store integration -----------------------------------------------------

def test_add_memory_passes_short(db):
    mid = db.add_memory("default", "Cale is PMP certified", category="fact")
    assert mid


def test_add_memory_rejects_document(db):
    with pytest.raises(GuardrailRejection):
        db.add_memory("default", "# A Document\n\nlots of content " + "x " * 600)


def test_batch_skips_rejected_keeps_good(db):
    facts = [
        "Cale prefers dark mode",                       # good
        "# Big Doc\n\n" + "y " * 700,                    # rejected: heading + long
        "Cale uses the PARA method",                    # good
    ]
    added = db.add_memories("default", facts, category="fact")
    assert added == 2
    assert len(db.last_rejected) == 1
    assert "heading" in db.last_rejected[0][1] or "too long" in db.last_rejected[0][1]
