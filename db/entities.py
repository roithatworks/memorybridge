"""Entity extraction for MemoryBridge — lightweight, regex-based, zero deps.

Extracts known entities from memory content and attaches them as
``entity:<canonical_name>`` tags. Seed list covers Cale's known
projects, tools, people, and concepts. Overridable via custom JSON file.

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

_SEED_ENTITIES: list[tuple[str, list[str], str]] = [
    # -- Projects --
    ("roi", ["ROI That Works", "ROI That Works - Autopilot", "ROI", "roithatworks.com"], "project"),
    ("car", ["Control Alt Recover", "controlaltrecover.com",  "CAR"], "project"),
    ("sap", ["Strategic Alignment Playbook", "SAP"], "project"),
    ("canvas-grader", ["Canvas Grader", "Canvas Grader Web"], "tool"),
    ("mentiondesk", ["MentionDesk", "MentionDesk Clone", "mds"], "project"),
    ("crypto-momentum", ["Crypto Momentum Bot", "Crypto Momentum"], "project"),
    ("customer-journey", ["Customer Journey Simulator"], "project"),
    ("audio-trial", ["Audio Trial Jam"], "project"),
    ("ai-daily-brief", ["AI Daily Briefing", "Daily Briefing", "ai-daily-briefing"], "project"),
    ("expensive-mistake", ["Expensive Mistake Framework"], "project"),

    # -- People --
    ("chris", ["Chris"], "person"),
    ("rebecca", ["Rebecca"], "person"),
    ("mark-graban", ["Mark Graban"], "person"),

    # -- Tools & Platforms --
    ("notion", ["Notion"], "tool"),
    ("memorybridge", ["MemoryBridge", "memorybridge"], "tool"),
    ("hermes", ["Hermes Agent", "Hermes"], "tool"),
    ("monday", ["Monday.com", "Monday"], "tool"),
    ("telegram", ["Telegram"], "tool"),
    ("github", ["GitHub", "GitHub CLI"], "tool"),
    ("gojiberry", ["Gojiberry"], "tool"),
    ("linear", ["Linear"], "tool"),
    ("spotify", ["Spotify"], "tool"),
    ("deepseek", ["DeepSeek"], "tool"),
    ("openrouter", ["OpenRouter"], "tool"),
    ("cloudflare", ["Cloudflare", "Cloudflare Workers", "Cloudflare Pages", "Cloudflare Turnstile"], "tool"),
    ("streamlit", ["Streamlit"], "tool"),
    ("fastmcp", ["FastMCP", "MCP Server", "fastmcp"], "tool"),
    ("qdrant", ["Qdrant"], "tool"),
    ("hindsight", ["Hindsight"], "tool"),
    ("mem0", ["Mem0"], "tool"),

    # -- Teaching --
    ("uno", ["UNO", "University of Nebraska Omaha", "UNO CIST", "UNO ISQA"], "concept"),
    ("cist3110", ["CIST3110", "CIST 3110"], "concept"),
    ("isqa3420", ["ISQA3420", "ISQA 3420"], "concept"),
    ("isqa3910", ["ISQA3910", "ISQA 3910"], "concept"),
    ("ai-discussion", ["AI Discussion", "AI discussion grading"], "concept"),

    # -- Concepts --
    ("revops", ["RevOps", "Revenue Operations"], "concept"),
    ("lean", ["Lean", "Toyota Production System", "TPS"], "concept"),
    ("rcm", ["RCM", "Revenue Cycle Management"], "concept"),
    ("healthcare-rcm", ["Healthcare RCM", "Healthcare Revenue Cycle"], "concept"),
    ("volume-to-value", ["Volume to Value"], "concept"),
    ("podcast", ["podcast", "AHF", "Accountability Hopeful Fridays"], "concept"),

    # -- Career --
    ("ims", ["IMS", "McKesson"], "concept"),
    ("paladina", ["Paladina", "Paladina Health"], "concept"),
    ("ctp", ["CTP", "Google Cloud CTP", "Google Pinpoint", "Cloud Technology Partnerships"], "concept"),

    # -- Infrastructure --
    ("launchd", ["launchd", "LaunchDaemon"], "concept"),
    ("s6-overlay", ["s6-overlay", "s6"], "concept"),
    ("docker", ["Docker", "Docker Compose"], "concept"),
    ("homebrew", ["Homebrew", "brew"], "concept"),
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
        tags = extractor.extract("Cale teaches UNO courses")  # ["entity:uno"]
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
