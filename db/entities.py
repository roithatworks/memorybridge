"""Entity extraction for MemoryBridge — lightweight, regex-based, zero deps.

Extracts known entities from memory content and attaches them as
``entity:<canonical_name>`` tags. The built-in seed is a small, generic set of
widely-used tools/platforms; add your own projects, people, and concepts via an
``entities.json`` file in your data dir (see the config format below).

Heuristic for acronym false-positive avoidance: any alias that is all-uppercase
and ≤4 characters (e.g. "CAR", "ROI", "IMS") is matched case-sensitively.
Multi-word and longer aliases are matched case-insensitively.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Seed entity list
# Entry format: (canonical_tag, [aliases], type)
# ---------------------------------------------------------------------------

# A small, generic seed of widely-used tools/platforms. This is intentionally
# NOT personal — add your own projects/people/concepts via entities.json.
_SEED_ENTITIES: list[tuple[str, list[str], str]] = [
    # -- AI / LLM tools --
    ("chatgpt", ["ChatGPT", "OpenAI"], "tool"),
    ("claude", ["Claude", "Anthropic"], "tool"),
    ("gemini", ["Gemini", "Google Gemini"], "tool"),
    ("memorybridge", ["MemoryBridge", "memorybridge"], "tool"),
    ("mcp", ["Model Context Protocol", "FastMCP", "MCP Server"], "tool"),

    # -- Dev / collaboration tools --
    ("github", ["GitHub", "GitHub CLI"], "tool"),
    ("gitlab", ["GitLab"], "tool"),
    ("notion", ["Notion"], "tool"),
    ("slack", ["Slack"], "tool"),
    ("jira", ["Jira"], "tool"),
    ("linear", ["Linear"], "tool"),
    ("docker", ["Docker", "Docker Compose"], "concept"),

    # -- Cloud / infra --
    ("aws", ["AWS", "Amazon Web Services"], "tool"),
    ("cloudflare", ["Cloudflare", "Cloudflare Workers", "Cloudflare Pages"], "tool"),
    ("postgres", ["Postgres", "PostgreSQL"], "tool"),
    ("sqlite", ["SQLite"], "tool"),
]

# ---------------------------------------------------------------------------
# Heuristic: short all-uppercase aliases are probably acronyms —
# match case-sensitively to avoid false positives (e.g. "car" → "carpenter").
# ---------------------------------------------------------------------------

_ACRONYM_MAX_LEN = 4


def _is_acronym(name: str) -> bool:
    return len(name) <= _ACRONYM_MAX_LEN and name.isupper()


def _build_patterns(
    entities: list[tuple[str, list[str], str]],
) -> list[tuple[re.Pattern, str, str]]:
    """Build compiled regex patterns from entity definitions.

    Returns list of (compiled_regex, canonical_tag, type).
    Aliases sorted by length descending so multi-word names match before
    short acronyms when both are present for the same entity.
    """
    patterns: list[tuple[re.Pattern, str, str]] = []
    for tag, aliases, etype in entities:
        for alias in sorted(aliases, key=len, reverse=True):
            if not alias.strip():
                continue
            escaped = re.escape(alias.strip())
            flags = 0 if _is_acronym(alias) else re.IGNORECASE
            try:
                pattern = re.compile(r"\b" + escaped + r"\b", flags)
                patterns.append((pattern, tag, etype))
            except re.error:
                logger.warning("Skipping invalid entity alias %r for %s", alias, tag)
    return patterns


# ---------------------------------------------------------------------------
# Entity config file format (JSON)
#
# {
#   "entities": [
#     {"names": ["ROI That Works", "ROI"], "tag": "roi", "type": "project"},
#     {"names": ["Canvas Grader"], "tag": "canvas-grader", "type": "tool"}
#   ]
# }
# ---------------------------------------------------------------------------

_DEFAULT_CONFIG_PATH = "entities.json"


def _load_config(path: Path | None) -> list[tuple[str, list[str], str]]:
    """Load entity definitions from a JSON config file, or return seed list."""
    if path is None:
        return _SEED_ENTITIES
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        raw = data.get("entities", [])
        if not raw:
            return _SEED_ENTITIES
        custom: list[tuple[str, list[str], str]] = []
        for entry in raw:
            tag = str(entry.get("tag", "") or "").strip()
            names = entry.get("names", [])
            etype = str(entry.get("type", "custom")).strip()
            if tag and names:
                custom.append((tag, list(names), etype))
        if not custom:
            return _SEED_ENTITIES
        logger.info("Loaded %d entity definitions from %s", len(custom), path)
        # Merge: custom entries override seed entries with the same tag
        overridden = {tag for tag, _, _ in custom}
        merged = [e for e in _SEED_ENTITIES if e[0] not in overridden]
        merged.extend(custom)
        return merged
    except Exception as exc:
        logger.warning("Failed to load entity config %s: %s — using seed list", path, exc)
        return _SEED_ENTITIES


# ---------------------------------------------------------------------------
# EntityExtractor
# ---------------------------------------------------------------------------


class EntityExtractor:
    """Lightweight entity extractor for memory content.

    Usage::

        extractor = EntityExtractor()
        tags = extractor.extract("We deploy on Cloudflare")  # ["entity:cloudflare"]
    """

    def __init__(self, config_path: Path | None = None):
        """Initialize with optional custom config file.

        *config_path* — path to a JSON entity config file. If ``None``
        (default), uses the seed list built into this module.
        """
        entities = _load_config(config_path)
        self._patterns = _build_patterns(entities)
        logger.debug(
            "EntityExtractor initialized with %d patterns",
            len(self._patterns),
        )

    @property
    def pattern_count(self) -> int:
        return len(self._patterns)

    def extract(self, content: str) -> list[str]:
        """Extract entity tags from *content*.

        Returns deduplicated list of entity tag strings
        (e.g. ``["entity:roi", "entity:canvas-grader"]``).
        Empty list if no entities matched or content is blank.
        """
        if not content or not content.strip():
            return []

        seen: set[str] = set()
        tags: list[str] = []
        for pattern, tag, _etype in self._patterns:
            if pattern.search(content) and tag not in seen:
                seen.add(tag)
                tags.append(f"entity:{tag}")
        return tags


# ---------------------------------------------------------------------------
# Convenience function — single-call extraction
# ---------------------------------------------------------------------------


_DEFAULT_EXTRACTOR: EntityExtractor | None = None


def extract_entities(content: str) -> list[str]:
    """One-shot entity extraction using the default extractor.

    Lazy-creates a singleton ``EntityExtractor`` (seed list only, no
    custom config). Use ``EntityExtractor(config_path=...)`` for
    custom configs.
    """
    global _DEFAULT_EXTRACTOR
    if _DEFAULT_EXTRACTOR is None:
        _DEFAULT_EXTRACTOR = EntityExtractor()
    return _DEFAULT_EXTRACTOR.extract(content)
