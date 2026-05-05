"""Resolve escalated facts via Claude API."""

import json
import logging
import os

import anthropic

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are resolving conflicts in a personal AI memory system.
You will receive a new fact extracted from a conversation and potentially a conflicting existing memory.
Return ONLY a JSON object: {"verdict": "accept"|"reject"|"merge", "merged_fact": "string or null"}
Be conservative — when in doubt, reject rather than pollute memory with noise.\
"""


def _get_client() -> anthropic.Anthropic:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    return anthropic.Anthropic(api_key=api_key)


def _build_user_message(fact: dict) -> str:
    parts = [f"New fact: {fact.get('fact', '')}"]
    if fact.get("conflicts_with"):
        parts.append(f"Conflicting existing memory: {fact['conflicts_with']}")
    parts.append(f"Confidence: {fact.get('confidence', 0):.2f}")
    parts.append(f"Reason: {fact.get('reason', '')}")
    return "\n".join(parts)


def _resolve_one(client: anthropic.Anthropic, fact: dict) -> dict:
    """Call Claude and return the verdict dict."""
    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=256,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": _build_user_message(fact)}],
    )
    raw = msg.content[0].text.strip()
    try:
        verdict = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Claude resolver returned bad JSON for fact '%s...' — rejecting", fact.get("fact", "")[:40])
        verdict = {"verdict": "reject", "merged_fact": None}
    return verdict


def resolve(escalated: list) -> list:
    """
    Resolve each escalated fact via Claude API.

    Args:
        escalated: List of fact dicts from router's escalated bucket
    Returns:
        List of dicts: original fact + verdict fields merged in.
        Facts with verdict "reject" are included so merger can count them.
    """
    if not escalated:
        return []

    try:
        client = _get_client()
    except RuntimeError as e:
        logger.error("Cannot resolve escalated facts: %s", e)
        return [{**f, "verdict": "reject", "merged_fact": None} for f in escalated]

    resolved = []
    for fact in escalated:
        try:
            verdict = _resolve_one(client, fact)
            resolved.append({**fact, **verdict})
        except Exception as e:
            logger.error("Resolution failed for fact '%s...': %s — rejecting", fact.get("fact", "")[:40], e)
            resolved.append({**fact, "verdict": "reject", "merged_fact": None})

    return resolved
