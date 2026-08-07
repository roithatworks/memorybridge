"""Regression tests for issue #179 — get_memory/search_memory under-counted
the actual served payload by up to ~1.5x at the default budget. The DB's
content-only token_count was summed to decide what to include and to report
"served", but the real response carries id/category/importance/project_id/
tags/token_count/created_at fields plus indent=2 whitespace (and, since #178,
a _security_notice field) — none of which were counted.

The old test_store.py::test_search_respects_token_budget is NOT a fix for
this: it sums the same token_count field db.search()'s own selection loop
used, which only proves db.search()'s internal bookkeeping is self-consistent
with itself. It says nothing about the real serialized get_memory/
search_memory payload a caller actually receives — that's what's tested here,
by measuring the real JSON string, not summing any per-item field.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import pytest

from db.store import MemoryStore
from exports import count_tokens


@pytest.fixture
def loaded_store(tmp_path, monkeypatch):
    """A store with enough varied memories (multiple tags each) AND a
    realistic identity/projects payload to actually stress both halves of the
    budget — a handful of short, tag-free memories on an empty-identity
    profile would trivially pass regardless of whether the accounting is
    correct, since a synthetic empty-identity profile understates the fixed
    (identity + projects) overhead real profiles carry."""
    import server
    s = MemoryStore(tmp_path / "test.db")
    monkeypatch.setattr(server, "_store", s)
    s.ensure_profile("default")
    s.update_profile(
        "default",
        identity={
            "name": "Cale", "role": "PMO/RevOps operator",
            "communication_style": {"tone": "direct, BLUF",
                                    "preferences": ["short replies", "bullet points"]},
        },
        projects=[{"id": "car", "name": "Control Alt Recover", "status": "active",
                  "description": "PMO transformation consultancy"}],
    )
    for i in range(60):
        s.add_memory(
            "default",
            f"Memory number {i} about the CAR project covering a decision made "
            f"during week {i % 12} regarding the ops-transformation rollout",
            category="fact", importance="medium",
            tags=["car", "ops-transformation", f"week-{i % 12}", "decision"],
            project_id="car",
        )
    return s


@pytest.mark.parametrize("budget", [1000, 2000, 4000, 8000])
def test_get_memory_actual_payload_within_budget(loaded_store, budget):
    import server
    out = server.get_memory.fn(profile="default", max_tokens=budget)
    actual = count_tokens(out)
    assert actual <= budget, (
        f"get_memory(max_tokens={budget}) served {actual} real tokens — "
        f"over budget by {actual - budget}"
    )


def test_get_memory_tiny_budget_below_fixed_overhead_degrades_honestly(loaded_store):
    # Known, separate limitation (not this fix's scope): identity/projects are
    # unconditionally included, so if their real serialized cost alone
    # exceeds max_tokens, even zero memories can't fit under budget. This is
    # NOT the ~1.5x per-memory undercount #179 fixes — it's a pre-existing
    # design question (should identity ever be droppable?) that's out of
    # scope here. What #179 guarantees even in this case: the report is
    # honest (served/reported match the real payload exactly, per
    # test_get_memory_reported_served_matches_actual_payload) and the
    # response doesn't crash — it just can't honor an impossibly tiny budget.
    import server
    out = server.get_memory.fn(profile="default", max_tokens=50)
    result = json.loads(out)
    assert result["token_stats"]["memories_returned"] == 0
    assert result["token_stats"]["served"] == count_tokens(out)


def test_get_memory_reported_served_matches_actual_payload(loaded_store):
    import server
    out = server.get_memory.fn(profile="default", max_tokens=4000)
    reported = json.loads(out)["token_stats"]["served"]
    actual = count_tokens(out)
    assert reported == actual, (
        f"token_stats.served ({reported}) must equal the real serialized "
        f"size ({actual}), not an estimate of it"
    )


def test_get_memory_zero_memories_no_overhead_surprise(tmp_path, monkeypatch):
    # Empty profile: overhead_tokens reserves room for the security notice
    # unconditionally (#179), but the notice itself is only added when
    # memories are non-empty (#178) — served/reported must still match
    # reality exactly in that case too.
    import server
    s = MemoryStore(tmp_path / "empty.db")
    monkeypatch.setattr(server, "_store", s)
    s.ensure_profile("empty_profile")
    out = server.get_memory.fn(profile="empty_profile", max_tokens=500)
    reported = json.loads(out)["token_stats"]["served"]
    assert reported == count_tokens(out)


def test_search_memory_reported_matches_actual_payload(loaded_store):
    import server
    out = server.search_memory.fn(query="ops-transformation", profile="default", max_tokens=800)
    reported = json.loads(out)["tokens_served"]
    actual = count_tokens(out)
    assert reported == actual
