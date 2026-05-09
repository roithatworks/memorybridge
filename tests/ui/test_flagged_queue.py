"""
Phase 5 UI tests — flagged queue business logic.
Streamlit rendering is not tested here; only the accept/reject functions
that mutate the queue and call add_memory.

Run: python -m pytest tests/ui/test_flagged_queue.py -v
"""
import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent.parent))


def _make_queue(tmp_path, items=None):
    queue = {
        "generated": "2026-05-05T00:00:00",
        "items": items or [
            {"id": "item-1", "fact": "Cale volunteers for Hopeful Fridays",
             "confidence": 0.72, "category": "fact", "importance": "medium",
             "project": None, "source": "claude", "status": "pending"},
            {"id": "item-2", "fact": "Low confidence guess",
             "confidence": 0.61, "category": "insight", "importance": "low",
             "project": None, "source": "claude", "status": "pending"},
        ]
    }
    p = tmp_path / "flagged_queue.json"
    p.write_text(json.dumps(queue))
    return p


def test_accept_moves_fact_to_memory(tmp_path):
    """Accepting a flagged fact should call add_memory and mark status=accepted."""
    queue_path = _make_queue(tmp_path)

    from ui.pages.flagged_queue import accept_item

    mock_add = MagicMock(return_value='{"status":"added","memory_id":"mem_abc"}')
    with patch("ui.pages.flagged_queue.add_memory", mock_add):
        accept_item("item-1", queue_path)

    mock_add.assert_called_once()
    updated = json.loads(queue_path.read_text())
    item = next(i for i in updated["items"] if i["id"] == "item-1")
    assert item["status"] == "accepted"


def test_reject_marks_status_only(tmp_path):
    """Rejecting a flagged fact should NOT call add_memory."""
    queue_path = _make_queue(tmp_path)

    from ui.pages.flagged_queue import reject_item

    mock_add = MagicMock()
    with patch("ui.pages.flagged_queue.add_memory", mock_add):
        reject_item("item-2", queue_path)

    mock_add.assert_not_called()
    updated = json.loads(queue_path.read_text())
    item = next(i for i in updated["items"] if i["id"] == "item-2")
    assert item["status"] == "rejected"


def test_accept_passes_correct_fields(tmp_path):
    """accept_item should call add_memory with fact content, category, importance."""
    queue_path = _make_queue(tmp_path)

    from ui.pages.flagged_queue import accept_item

    mock_add = MagicMock(return_value='{"status":"added","memory_id":"mem_xyz"}')
    with patch("ui.pages.flagged_queue.add_memory", mock_add):
        accept_item("item-1", queue_path)

    call_kwargs = mock_add.call_args[1]
    assert call_kwargs["content"] == "Cale volunteers for Hopeful Fridays"
    assert call_kwargs["category"] == "fact"
    assert call_kwargs["importance"] == "medium"


def test_accept_nonexistent_item_is_noop(tmp_path):
    """Accepting an ID that doesn't exist should not raise or modify the queue."""
    queue_path = _make_queue(tmp_path)
    original = queue_path.read_text()

    from ui.pages.flagged_queue import accept_item

    mock_add = MagicMock()
    with patch("ui.pages.flagged_queue.add_memory", mock_add):
        accept_item("item-999", queue_path)

    mock_add.assert_not_called()
    assert queue_path.read_text() == original


def test_batch_accept_accepts_all_pending(tmp_path):
    """batch_accept should accept every item with status=pending."""
    queue_path = _make_queue(tmp_path)

    from ui.pages.flagged_queue import batch_accept

    mock_add = MagicMock(return_value='{"status":"added","memory_id":"mem_x"}')
    with patch("ui.pages.flagged_queue.add_memory", mock_add):
        count = batch_accept(queue_path)

    assert count == 2  # both items were pending
    assert mock_add.call_count == 2
    updated = json.loads(queue_path.read_text())
    for item in updated["items"]:
        assert item["status"] == "accepted"


def test_queue_counts(tmp_path):
    """get_counts should return pending/accepted/rejected tallies."""
    items = [
        {"id": "a", "fact": "f1", "confidence": 0.7, "category": "fact",
         "importance": "medium", "project": None, "source": "claude", "status": "pending"},
        {"id": "b", "fact": "f2", "confidence": 0.7, "category": "fact",
         "importance": "medium", "project": None, "source": "claude", "status": "accepted"},
        {"id": "c", "fact": "f3", "confidence": 0.7, "category": "fact",
         "importance": "medium", "project": None, "source": "claude", "status": "rejected"},
    ]
    queue_path = _make_queue(tmp_path, items=items)

    from ui.pages.flagged_queue import get_counts
    counts = get_counts(queue_path)

    assert counts["pending"] == 1
    assert counts["accepted"] == 1
    assert counts["rejected"] == 1
