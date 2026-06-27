"""Resolve escalated facts via Claude API."""

import json
import logging
import os

import anthropic

logger = logging.getLogger(__name__)

# Model for conflict resolution. The old "claude-3-5-sonnet-latest" alias was
# retired by Anthropic and 404s — every escalated fact was being rejected.
# Override via RESOLVER_MODEL env var when model names change again.
RESOLVER_MODEL = os.environ.get("RESOLVER_MODEL", "claude-sonnet-4-5")

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


# Model auto-resolution: model aliases get retired (claude-3-5-sonnet-latest
# 404'd and silently killed every resolution). Rather than hard-pin and rot,
# we resolve the model once per run: try RESOLVER_MODEL; if it 404s, query the
# Models API for the newest available Sonnet and use that. Cached for the run.
_RESOLVED_MODEL = None


def _pick_model(client: anthropic.Anthropic) -> str:
    """Return a working model id. Prefers RESOLVER_MODEL; auto-falls-back to the
    newest Sonnet from the Models API if the configured one is unavailable."""
    global _RESOLVED_MODEL
    if _RESOLVED_MODEL:
        return _RESOLVED_MODEL

    # 1. Try the configured model with a 1-token ping.
    try:
        client.messages.create(model=RESOLVER_MODEL, max_tokens=1,
                               messages=[{"role": "user", "content": "hi"}])
        _RESOLVED_MODEL = RESOLVER_MODEL
        return _RESOLVED_MODEL
    except Exception as e:
        logger.warning("Configured RESOLVER_MODEL '%s' unavailable (%s) — "
                       "auto-selecting newest Sonnet", RESOLVER_MODEL, str(e)[:60])

    # 2. Ask the Models API for the newest Sonnet (models list is newest-first).
    try:
        models = client.models.list(limit=50)
        sonnets = [m.id for m in models.data if "sonnet" in m.id.lower()]
        if sonnets:
            _RESOLVED_MODEL = sonnets[0]
            logger.warning("Resolver now using auto-selected model: %s", _RESOLVED_MODEL)
            return _RESOLVED_MODEL
    except Exception as e:
        logger.error("Model auto-selection failed: %s", e)

    # 3. Last resort: use the configured name and let calls error visibly.
    _RESOLVED_MODEL = RESOLVER_MODEL
    return _RESOLVED_MODEL


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
        model=_pick_model(client),
        max_tokens=256,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": _build_user_message(fact)}],
    )
    raw = msg.content[0].text.strip()
    verdict = _parse_verdict(raw)
    if verdict is None:
        logger.warning("Claude resolver returned unparseable JSON for fact '%s...' — rejecting",
                       fact.get("fact", "")[:40])
        verdict = {"verdict": "reject", "merged_fact": None}
    return verdict


def _parse_verdict(raw: str):
    """Parse the resolver verdict, tolerating markdown fences and preamble.

    Newer Claude models wrap JSON in ```json ... ``` fences (and sometimes add
    a sentence), even when asked not to. The old 3.5 model returned bare JSON,
    so the original json.loads(raw) rejected every modern response. Strip the
    fence, then fall back to extracting the first {...} object.
    """
    if not raw:
        return None
    text = raw.strip()
    # Strip a leading ```json / ``` fence and trailing ```.
    if text.startswith("```"):
        text = text.split("```", 2)[1] if text.count("```") >= 2 else text.lstrip("`")
        if text.lstrip().lower().startswith("json"):
            text = text.lstrip()[4:]
        text = text.strip().rstrip("`").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Last resort: grab the first {...} block anywhere in the response.
    import re
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    return None


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
