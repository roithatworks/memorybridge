"""Extract durable facts from normalized conversations via DeepSeek R1."""

import json
import logging
import os
from typing import Optional

from openai import OpenAI

logger = logging.getLogger(__name__)

# ~4 chars per token; 8000 tokens ≈ 32000 chars
_MAX_CONV_CHARS = 32_000
_BATCH_SIZE = 5

SYSTEM_PROMPT = """\
You are an expert at extracting durable, high-signal facts from AI conversation history.
Your job is to read the provided conversations and extract facts worth remembering long-term.

Rules:
- Extract only durable truths — not one-off comments, pleasantries, or task outputs
- Infer implicit traits only when evidence is strong; mark inferred=true
- Omit anything low-signal, outdated, or already obvious
- Every fact must map to one of these categories exactly:
  preference, fact, insight, decision, project_status, relationship, skill, constraint
- Every fact must have one of these importance levels exactly:
  low, medium, high, critical
- Return ONLY a JSON array. No preamble. No markdown. No explanation.\
"""

USER_PROMPT_TEMPLATE = """\
Extract durable facts from these conversations:

{conversations_json}

Return a JSON array where each object has:
{{
  "fact": "string — stated as a durable truth, present tense",
  "confidence": 0.0-1.0,
  "reason": "string — why this confidence level",
  "category": "one of the valid categories",
  "importance": "one of the valid importance levels",
  "project": "project_id string or null",
  "conflicts_with": "existing fact string or null — only if you know it conflicts",
  "inferred": true | false
}}\
"""


class ExtractionError(Exception):
    pass


def _get_client() -> OpenAI:
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        raise ExtractionError("DEEPSEEK_API_KEY not set")
    return OpenAI(api_key=api_key, base_url="https://api.deepseek.com")


def _truncate_conversation(conv: dict) -> dict:
    """Truncate long conversations to last ~8K tokens by character estimate."""
    messages = conv.get("messages", [])
    # Serialize to measure size; truncate from the front (keep recency)
    total = 0
    kept = []
    for msg in reversed(messages):
        chunk = json.dumps(msg)
        if total + len(chunk) > _MAX_CONV_CHARS:
            break
        kept.append(msg)
        total += len(chunk)
    result = conv.copy()
    result["messages"] = list(reversed(kept))
    return result


def _call_deepseek(client: OpenAI, conversations: list) -> list:
    """Call DeepSeek R1 and return parsed fact list. Retries once on JSON error."""
    batch = [_truncate_conversation(c) for c in conversations]
    user_prompt = USER_PROMPT_TEMPLATE.format(
        conversations_json=json.dumps(batch, indent=2)
    )

    def _attempt() -> Optional[list]:
        resp = client.chat.completions.create(
            model="deepseek-reasoner",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
        )
        raw = resp.choices[0].message.content.strip()
        # Strip accidental markdown fences
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw)

    try:
        return _attempt()
    except json.JSONDecodeError as e:
        logger.warning("DeepSeek returned malformed JSON, retrying once: %s", e)
        try:
            return _attempt()
        except json.JSONDecodeError as e2:
            logger.error("Second attempt also failed: %s — skipping batch", e2)
            return []


def extract(normalized: dict) -> list:
    """
    Extract facts from all conversations in a normalized export.

    Args:
        normalized: Output from any parse_*.py parser
    Returns:
        Flat list of extracted fact dicts, each tagged with source_conversation_id
    """
    conversations = normalized.get("conversations", [])
    if not conversations:
        return []

    try:
        client = _get_client()
    except ExtractionError:
        raise

    all_facts = []
    batches = [conversations[i:i + _BATCH_SIZE] for i in range(0, len(conversations), _BATCH_SIZE)]

    for batch_idx, batch in enumerate(batches):
        logger.info("Extracting batch %d/%d (%d conversations)", batch_idx + 1, len(batches), len(batch))
        try:
            facts = _call_deepseek(client, batch)
        except Exception as e:
            raise ExtractionError(f"DeepSeek API unavailable: {e}") from e

        # Tag each fact with the first conversation id in the batch as a rough source
        for fact in facts:
            if not isinstance(fact, dict):
                continue
            if "source_conversation_id" not in fact:
                fact["source_conversation_id"] = batch[0].get("id", "")
            all_facts.append(fact)

    return all_facts
