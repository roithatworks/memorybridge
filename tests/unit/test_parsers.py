"""
Unit tests for ChatGPT and Gemini export parsers.

Uses synthetic fixtures derived from real export schemas — no actual export
files needed.

Run: python -m pytest tests/unit/test_parsers.py -v
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "ingestion"))



# ---------------------------------------------------------------------------
# ChatGPT fixtures
# ---------------------------------------------------------------------------

CHATGPT_MINIMAL = [
    {
        "id": "conv-001",
        "title": "Test Conversation",
        "create_time": 1700000000.0,
        "mapping": {
            "root": {
                "id": "root",
                "parent": None,
                "message": None,
                "children": ["msg-user-1"],
            },
            "msg-user-1": {
                "id": "msg-user-1",
                "parent": "root",
                "message": {
                    "author": {"role": "user"},
                    "content": {"parts": ["Hello, can you help me?"]},
                },
                "children": ["msg-asst-1"],
            },
            "msg-asst-1": {
                "id": "msg-asst-1",
                "parent": "msg-user-1",
                "message": {
                    "author": {"role": "assistant"},
                    "content": {"parts": ["Of course! What do you need?"]},
                },
                "children": [],
            },
        },
    }
]

CHATGPT_MULTI_CONV = CHATGPT_MINIMAL + [
    {
        "id": "conv-002",
        "title": "Another Chat",
        "create_time": 1700086400.0,
        "mapping": {
            "root2": {
                "id": "root2",
                "parent": None,
                "message": None,
                "children": ["msg-u2"],
            },
            "msg-u2": {
                "id": "msg-u2",
                "parent": "root2",
                "message": {
                    "author": {"role": "user"},
                    "content": {"parts": ["What is Python?"]},
                },
                "children": [],
            },
        },
    }
]


# ---------------------------------------------------------------------------
# Gemini fixtures
# ---------------------------------------------------------------------------

GEMINI_FORMAT_A = {
    "conversations": [
        {
            "conversation_id": "gemini-001",
            "conversation": [
                {"role": "user", "text": "Explain machine learning briefly."},
                {"role": "model", "text": "Machine learning is a subset of AI..."},
            ],
        },
        {
            "conversation_id": "gemini-002",
            "conversation": [
                {"role": "user", "text": "What is a neural network?"},
                {"role": "model", "text": "A neural network is..."},
            ],
        },
    ]
}

GEMINI_FORMAT_B = [
    {
        "header": {"title": "Gemini Apps Activity"},
        "title": "What is machine learning?",
        "time": "2024-01-15T10:00:00Z",
        "details": [{"name": "What is machine learning?"}],
    },
    {
        "header": {"title": "Gemini Apps Activity"},
        "title": "Explain neural networks",
        "time": "2024-01-16T09:00:00Z",
        "details": [{"name": "Explain neural networks"}],
    },
]

GEMINI_FORMAT_B_EMPTY_DETAILS = [
    {
        "header": {"title": "Gemini Apps Activity"},
        "title": "What is deep learning?",
        "time": "2024-01-17T08:00:00Z",
        "details": [],
    },
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_tmp(data) -> Path:
    """Write data to a temp JSON file and return its path."""
    tf = tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w",
                                     encoding="utf-8")
    json.dump(data, tf)
    tf.close()
    return Path(tf.name)


# ---------------------------------------------------------------------------
# ChatGPT parser tests
# ---------------------------------------------------------------------------

def test_chatgpt_parse_returns_normalized_structure():
    from parse_chatgpt import parse
    path = _write_tmp(CHATGPT_MINIMAL)
    result = parse(str(path))
    assert result["source"] == "chatgpt"
    assert "conversations" in result
    assert "exported_at" in result


def test_chatgpt_parse_extracts_messages():
    from parse_chatgpt import parse
    path = _write_tmp(CHATGPT_MINIMAL)
    result = parse(str(path))
    assert len(result["conversations"]) == 1
    conv = result["conversations"][0]
    assert conv["id"] == "conv-001"
    msgs = conv["messages"]
    assert len(msgs) == 2
    assert msgs[0]["role"] == "user"
    assert "Hello" in msgs[0]["content"]
    assert msgs[1]["role"] == "assistant"
    assert "Of course" in msgs[1]["content"]


def test_chatgpt_parse_multiple_conversations():
    from parse_chatgpt import parse
    path = _write_tmp(CHATGPT_MULTI_CONV)
    result = parse(str(path))
    assert len(result["conversations"]) == 2


def test_chatgpt_parse_days_filter():
    """Conversations outside the date window should be excluded."""
    from parse_chatgpt import parse
    path = _write_tmp(CHATGPT_MINIMAL)
    # create_time = 1700000000 ≈ Nov 2023 — filter to last 1 day excludes it
    result = parse(str(path), days=1)
    assert len(result["conversations"]) == 0


def test_chatgpt_parse_empty_mapping_skipped():
    """Conversations with no extractable messages are dropped."""
    from parse_chatgpt import parse
    data = [{"id": "empty", "title": "Empty", "create_time": 1700000000.0, "mapping": {}}]
    path = _write_tmp(data)
    result = parse(str(path))
    assert len(result["conversations"]) == 0


# ---------------------------------------------------------------------------
# Gemini parser tests
# ---------------------------------------------------------------------------

def test_gemini_format_a_returns_normalized_structure():
    from parse_gemini import parse
    path = _write_tmp(GEMINI_FORMAT_A)
    result = parse(str(path))
    assert result["source"] == "gemini"
    assert "conversations" in result
    assert "exported_at" in result


def test_gemini_format_a_extracts_conversations():
    from parse_gemini import parse
    path = _write_tmp(GEMINI_FORMAT_A)
    result = parse(str(path))
    assert len(result["conversations"]) == 2


def test_gemini_format_a_extracts_messages():
    from parse_gemini import parse
    path = _write_tmp(GEMINI_FORMAT_A)
    result = parse(str(path))
    conv = result["conversations"][0]
    msgs = conv["messages"]
    assert len(msgs) == 2
    assert msgs[0]["role"] == "user"
    assert "machine learning" in msgs[0]["content"].lower()


def test_gemini_format_b_activity_produces_conversations():
    """Activity-only format should produce one conversation per activity entry."""
    from parse_gemini import parse
    path = _write_tmp(GEMINI_FORMAT_B)
    result = parse(str(path))
    assert result["source"] == "gemini"
    assert len(result["conversations"]) == 2


def test_gemini_format_b_activity_has_user_message():
    """Each activity entry must surface at least one user message."""
    from parse_gemini import parse
    path = _write_tmp(GEMINI_FORMAT_B)
    result = parse(str(path))
    for conv in result["conversations"]:
        roles = [m["role"] for m in conv["messages"]]
        assert "user" in roles, f"No user message in conv: {conv}"


def test_gemini_format_b_empty_details_falls_back_to_title():
    """When details is empty, the activity title becomes the user message."""
    from parse_gemini import parse
    path = _write_tmp(GEMINI_FORMAT_B_EMPTY_DETAILS)
    result = parse(str(path))
    assert len(result["conversations"]) == 1
    msgs = result["conversations"][0]["messages"]
    user_msgs = [m for m in msgs if m["role"] == "user"]
    assert user_msgs, "Expected at least one user message from title fallback"
    assert "deep learning" in user_msgs[0]["content"].lower()


def test_gemini_days_filter():
    """Activities outside the date window should be excluded."""
    from parse_gemini import parse
    path = _write_tmp(GEMINI_FORMAT_B)
    # time = 2024-01-15/16 — filter to last 1 day excludes both
    result = parse(str(path), days=1)
    assert len(result["conversations"]) == 0
