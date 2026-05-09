"""Parse Gemini MyActivity.json (Google Takeout) export into normalized format."""

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path


def _parse_time(time_str: str) -> tuple:
    """Return (timestamp_float, date_str) from an ISO or RFC3339 string."""
    try:
        dt = datetime.fromisoformat(time_str.replace("Z", "+00:00"))
        return dt.timestamp(), dt.strftime("%Y-%m-%d")
    except Exception:
        return 0.0, "unknown"


def _extract_text(detail) -> str:
    """Pull readable text out of a detail entry (varies by Takeout version)."""
    if isinstance(detail, str):
        return detail.strip()
    if isinstance(detail, dict):
        for key in ("name", "url", "value", "text"):
            if key in detail and isinstance(detail[key], str):
                return detail[key].strip()
    return ""


def parse(file_path: str, days: int = None) -> dict:
    """
    Parse a Gemini MyActivity.json Google Takeout export.

    Gemini's Takeout format varies significantly by export version:
    - Activity-only format: list of activity records with title + time, no message bodies
    - Conversation format: dict with a "conversations" key containing turn pairs

    Both are handled. Activity-only records produce a single synthesized user
    message from the activity title so the extractor has something to work with.

    Args:
        file_path: Path to MyActivity.json
        days: If set, only include items from the last N days
    Returns:
        Normalized conversation dict
    """
    path = Path(file_path).expanduser()
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    cutoff = None
    if days:
        cutoff = datetime.now(timezone.utc).timestamp() - (days * 86400)

    conversations = []

    # --- Format A: dict with "conversations" key ---
    if isinstance(raw, dict) and "conversations" in raw:
        for item in raw["conversations"]:
            conv_id = item.get("conversation_id", str(uuid.uuid4()))
            turns = item.get("conversation", [])
            messages = []
            date_str = "unknown"
            for turn in turns:
                human_text = ""
                model_text = ""
                # Schema variant A: {"human_turn": ..., "model_turn": ...}
                if isinstance(turn.get("human_turn"), dict):
                    human_text = turn["human_turn"].get("text", "").strip()
                elif isinstance(turn.get("human_turn"), str):
                    human_text = turn["human_turn"].strip()
                if isinstance(turn.get("model_turn"), dict):
                    model_text = turn["model_turn"].get("text", "").strip()
                elif isinstance(turn.get("model_turn"), str):
                    model_text = turn["model_turn"].strip()
                # Schema variant B: {"role": "user"/"model"/"assistant", "text": "..."}
                if not human_text and not model_text:
                    role = turn.get("role", "")
                    text = turn.get("text", "").strip()
                    if role == "user" and text:
                        human_text = text
                    elif role in ("model", "assistant") and text:
                        model_text = text
                if human_text:
                    messages.append({"role": "user", "content": human_text})
                if model_text:
                    messages.append({"role": "assistant", "content": model_text})

            ts_str = item.get("time", item.get("created_at", ""))
            ts, date_str = _parse_time(ts_str) if ts_str else (0.0, "unknown")
            if cutoff and ts < cutoff:
                continue
            if messages:
                conversations.append({
                    "id": conv_id,
                    "title": item.get("title", "Gemini conversation"),
                    "date": date_str,
                    "messages": messages,
                })
        return {
            "source": "gemini",
            "exported_at": datetime.now().strftime("%Y-%m-%d"),
            "conversations": conversations,
        }

    # --- Format B: list of activity records ---
    if not isinstance(raw, list):
        raw = []

    for item in raw:
        # Only include Gemini App entries
        products = item.get("products", [])
        header_raw = item.get("header", "")
        # header may be a plain string or a dict {"title": "Gemini Apps Activity"}
        if isinstance(header_raw, dict):
            header = header_raw.get("title", "")
        else:
            header = str(header_raw)
        if products and not any("Gemini" in p or "Bard" in p for p in products):
            continue
        if header and "Gemini" not in header and "Bard" not in header:
            continue

        ts_str = item.get("time", "")
        ts, date_str = _parse_time(ts_str) if ts_str else (0.0, "unknown")
        if cutoff and ts < cutoff:
            continue

        title = item.get("title", "Untitled activity")
        # Build a minimal message from title + any detail text
        detail_texts = []
        for detail in item.get("details", []):
            t = _extract_text(detail)
            if t:
                detail_texts.append(t)

        user_content = title
        if detail_texts:
            user_content += " | " + " | ".join(detail_texts)

        messages = [{"role": "user", "content": user_content}]
        conv_id = str(uuid.uuid4())

        conversations.append({
            "id": conv_id,
            "title": title,
            "date": date_str,
            "messages": messages,
        })

    return {
        "source": "gemini",
        "exported_at": datetime.now().strftime("%Y-%m-%d"),
        "conversations": conversations,
    }
