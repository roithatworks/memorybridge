"""Shared constants for MemoryBridge — single source of truth.

Imported by both db/store.py and server.py so definitions are never
duplicated.  Zero dependencies on FastMCP, SQLite, or any project module.
"""

import hashlib
import re
from datetime import datetime


MIN_SCORE = 0.1
HALF_LIFE_DAYS = 30

_IMPORTANCE_BOOSTS = {"low": 0.8, "medium": 1.0, "high": 1.2, "critical": 1.5}


def effective_score(row: dict, now: datetime) -> float:
    """Canonical decay-adjusted score for a memory row.

    Ordering: decay → importance_boost → access_boost → clamp.
    All callers (apply_decay, auto_prune, prune dry-run) must use this.
    """
    try:
        created = datetime.fromisoformat(row.get("created_at", ""))
        # Strip tzinfo so subtraction works regardless of whether the stored
        # timestamp has a UTC offset (Python 3.11+ fromisoformat parses "+00:00"
        # as timezone-aware, which can't be subtracted from a naive datetime.now()).
        created = created.replace(tzinfo=None)
    except Exception:
        created = now.replace(tzinfo=None)
    now_naive = now.replace(tzinfo=None)
    days_old = (now_naive - created).days
    decay = 0.5 ** (days_old / HALF_LIFE_DAYS)
    importance_boost = _IMPORTANCE_BOOSTS.get(row.get("importance", "medium"), 1.0)
    access_boost = 1 + min(row.get("access_count", 0) * 0.05, 0.5)
    result = row.get("relevance_score", 1.0) * decay * importance_boost * access_boost
    return max(result, MIN_SCORE)


VALID_CATEGORIES = [
    "preference", "fact", "insight", "decision",
    "project_status", "relationship", "skill", "constraint"
]

IMPORTANCE_LEVELS = ["low", "medium", "high", "critical"]


# --- Write-path guardrails -------------------------------------------------
# A memory is a COMPRESSED FACT, not a document. These thresholds reject
# document-shaped content at the write path so reference docs, prompt files,
# and pasted essays never leak into the context-loaded store. Tune here.
MAX_MEMORY_TOKENS = 500        # hard cap per memory; over this = "store as a file"
MAX_MEMORY_NEWLINES = 15       # multi-section docs blow past this
MAX_MEMORY_H2_SECTIONS = 2     # >=2 "## " headers means it's a document


def guardrail_check(content: str) -> tuple[bool, str]:
    """Deterministic gate for the memory write path.

    Returns (ok, reason). ok=False means the content is document-shaped and
    should be stored as a file (or compressed into a fact), NOT inserted as a
    memory. Pure / zero-dependency so it is trivially unit-testable.
    """
    text = (content or "").strip()
    if not text:
        return False, "empty content"

    tc = _count_tokens(text)
    if tc > MAX_MEMORY_TOKENS:
        return False, (
            f"too long ({tc} tokens > {MAX_MEMORY_TOKENS} cap) — looks like a "
            f"document; store as a file or compress into a fact"
        )

    if text.count("\n") >= MAX_MEMORY_NEWLINES:
        return False, (
            f"too many lines ({text.count(chr(10))} >= {MAX_MEMORY_NEWLINES}) — "
            f"document-shaped, not a memory"
        )

    # Leading markdown title is the strongest document fingerprint.
    stripped = text.lstrip()
    if stripped.startswith(("# ", "## ", "### ")):
        return False, "starts with a markdown heading — looks like a document, not a memory"

    h2 = sum(1 for ln in text.splitlines() if ln.lstrip().startswith("## "))
    if h2 >= MAX_MEMORY_H2_SECTIONS:
        return False, (
            f"has {h2} section headers (>= {MAX_MEMORY_H2_SECTIONS}) — "
            f"multi-section document, not a memory"
        )

    return True, ""


def _content_hash(content: str) -> str:
    """SHA256 of normalized content — same normalization across all callers."""
    return hashlib.sha256(content.strip().lower().encode()).hexdigest()


def _count_tokens(text: str) -> int:
    """Token-count with tiktoken; falls back to word-count heuristic."""
    try:
        import tiktoken
        enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text)) + 20
    except Exception:
        return len(text.split()) + 20


def _merge_tags(*tag_lists: list[str]) -> list[str]:
    """Merge and deduplicate tag lists.

    Each tag is stripped and lowercased. Returns a flat deduplicated list
    preserving the order of first appearance.
    Useful for merging caller-supplied tags with auto-extracted entity tags.
    """
    seen: set[str] = set()
    result: list[str] = []
    for tags in tag_lists:
        for tag in (tags or []):
            t = tag.strip().lower()
            if t and t not in seen:
                seen.add(t)
                result.append(t)
    return result


_IMPORTANCE_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}


def _max_importance(a: str, b: str) -> str:
    """Return the higher of two importance levels."""
    return a if _IMPORTANCE_RANK.get(a, 0) >= _IMPORTANCE_RANK.get(b, 0) else b


# --- Content-type pattern detection (lightweight, zero deps) ----------------
# Order matters: first match wins. Add patterns below to extend coverage.

_CONTENT_TYPE_PATTERNS: list[tuple[re.Pattern, str]] = [
    # Questions / inquiries
    (re.compile(r"\bshould\s+(I|we)\b", re.I), "question"),
    (re.compile(r"\bhow\s+(do|does|can|should|would|to)\b", re.I), "question"),
    (re.compile(r"\bwhat\s+(is|are|would|should|does|about)\b", re.I), "question"),
    (re.compile(r"\bwhy\s+(is|are|do|does|did|would|not)\b", re.I), "question"),
    (re.compile(r"\bcan\s+(I|we|you)\b", re.I), "question"),
    (re.compile(r"\bis\s+(it|there|this)\b", re.I), "question"),
    (re.compile(r"\bcompare\b", re.I), "comparison"),
    (re.compile(r"\bdifference\s+between\b", re.I), "comparison"),
    (re.compile(r"\bvs\.?\s", re.I), "comparison"),
    (re.compile(r"\bor\s+should\s+(I|we)\b", re.I), "comparison"),
    # Decisions
    (re.compile(r"\bdecided\b", re.I), "decision"),
    (re.compile(r"\belected\s+to\b", re.I), "decision"),
    (re.compile(r"\bchose\b", re.I), "decision"),
    (re.compile(r"\bgoing with\b", re.I), "decision"),
    (re.compile(r"\bwe'll use\b", re.I), "decision"),
    # Preferences
    (re.compile(r"\bprefer\b", re.I), "preference"),
    (re.compile(r"\blike\s+it\s+when\b", re.I), "preference"),
    (re.compile(r"\bwould rather\b", re.I), "preference"),
    # Bugs / issues
    (re.compile(r"\bbug\b", re.I), "bug"),
    (re.compile(r"\b(doesn.t|does.not|isn.t)\s+work\b", re.I), "bug"),
    (re.compile(r"\bfailed?\b", re.I), "bug"),
    (re.compile(r"\berror\b", re.I), "bug"),
    (re.compile(r"\bcrash(ed|es)?\b", re.I), "bug"),
    (re.compile(r"\b(404|500|timeout)\b", re.I), "bug"),
    # Workarounds / fixes
    (re.compile(r"\bfix(?:ed|es|ing)?\b", re.I), "fix"),
    (re.compile(r"\bworked around\b", re.I), "fix"),
    (re.compile(r"\bworkaround\b", re.I), "fix"),
    # Recommendations
    (re.compile(r"\brecommend\b", re.I), "recommendation"),
    (re.compile(r"\bsuggestion\b", re.I), "recommendation"),
    # Definitions / explanations
    (re.compile(r"\bis\s+(a|an|the)\s+.{3,30}\s+(that|which|where)\b", re.I), "definition"),
    (re.compile(r"\bstands for\b", re.I), "definition"),
    (re.compile(r"\bdefined as\b", re.I), "definition"),
    # Workflows / processes
    (re.compile(r"\bfirst\s+,?\s+(then|next)\b", re.I), "workflow"),
    (re.compile(r"\b(step\s+\d|steps? to)\b", re.I), "workflow"),
    (re.compile(r"\bwhen\s+.+\s+then\b", re.I), "workflow"),
    (re.compile(r"\bprocess\s+(is|looks|works)\b", re.I), "workflow"),
    # Session / conversation
    (re.compile(r"\bsession discussed\b", re.I), "conversation"),
    (re.compile(r"\bthis (week|month|quarter).{0,40}:\b", re.I), "plan"),
    (re.compile(r"\b(roadmap|plan|planned)\b", re.I), "plan"),
    # Status / updates (project_status category markers)
    (re.compile(r"\bcompleted\b", re.I), "completed"),
    (re.compile(r"\bin progress\b", re.I), "in-progress"),
    (re.compile(r"\bblocked on\b", re.I), "blocked"),
    (re.compile(r"\bwaiting on\b", re.I), "blocked"),
]


def _detect_content_type(content: str) -> list[str]:
    """Detect content-type tags from text signals.

    Uses pattern matching to classify what kind of knowledge this is
    (question, decision, bug, fix, comparison, recommendation, etc.).
    First match wins per pattern-group. Returns empty list if no signal.
    """
    if not content:
        return []
    seen: set[str] = set()
    types: list[str] = []
    for pattern, tag in _CONTENT_TYPE_PATTERNS:
        if tag in seen:
            continue
        if pattern.search(content):
            seen.add(tag)
            types.append(tag)
    return types


# --- Code detection: skip keyword extraction on structured content ----------
# These patterns indicate code, SQL, JSON, paths, or structured data rather
# than natural language. If any trigger, keyword extraction returns empty.

_CODE_SIGNALS: list[re.Pattern] = [
    # SQL — SELECT/FROM/WHERE/JOIN/INSERT with enough density
    re.compile(r"\b(SELECT|FROM|WHERE|JOIN|INSERT|UPDATE|DELETE|CREATE|ALTER|DROP)\b.*"
               r"\b(SELECT|FROM|WHERE|JOIN|INSERT|UPDATE|DELETE|CREATE|ALTER|DROP)\b", re.I),
    # Code fences or inline code markers
    re.compile(r"```|`[a-z]+\s{|`[a-z]+\.\w+`", re.I),
    # JSON-like structure: starts with { and has : inside
    re.compile(r"^\s*\{.*:.*\}"),
    # Shell commands: leading $, paths with /, flags like --flag
    re.compile(r"^(?:\$\s|sudo |npm |pip |git |brew |cd |ls |rm |cp |mv |chmod )", re.I),
    # File paths with / or \ (not URLs)
    re.compile(r"\s(/[a-zA-Z0-9_.-]+){2,}/?"),
    # High density of special characters (non-alpha) — suggests data dump
    re.compile(r"[^a-zA-Z0-9\s]{10,}"),
    # Markdown tables or headings that aren't natural language
    re.compile(r"^\|.*\|.*\|$", re.MULTILINE),
    # XML/HTML tags
    re.compile(r"<[a-zA-Z]+[^>]*>"),
]


def _looks_like_code(content: str) -> bool:
    """Return True if content looks like code or structured data.

    Runs a series of lightweight signal checks. Intended to catch
    git-activity dumps, SQL snippets, JSON, shell commands, and
    file paths before they produce garbage keywords.
    """
    if not content:
        return False
    # Quick check: ratio of alphabetic vs non-alphabetic characters
    clean = re.sub(r"\s+", "", content)
    if not clean:
        return False
    alpha = sum(1 for c in clean if c.isalpha())
    total = len(clean)
    if total > 20 and alpha / total < 0.5:
        return True  # less than 50% letters → likely structured data
    # Pattern-based signals
    for pattern in _CODE_SIGNALS:
        if pattern.search(content):
            return True
    return False


# --- Project name normalization ---------------------------------------------

_PROJECT_NORMALIZE: dict[str, str] = {
    # Hermes Agent variants
    "hermes agent": "hermes-agent",
    "hermes-agent": "hermes-agent",
    "hermes": "hermes-agent",
    # Ops Radar variants
    "ops_radar": "ops-radar",
    "ops-radar": "ops-radar",
    "ops radar": "ops-radar",
    # Control Alt Recover
    "controlaltrecover": "car",
    "car": "car",
    # ROI That Works
    "roithatworks": "roi",
    "roithatworks.com": "roi",
    "roi": "roi",
    # Strategic Alignment Playbook
    "strategic alignment playbook": "sap",
    "sap": "sap",
}


def _normalize_project(project_id: str | None) -> str | None:
    """Normalize a project_id to its canonical form.

    ``\"Hermes Agent\"`` → ``\"hermes-agent\"``.
    ``\"ops_radar\"`` → ``\"ops-radar\"``.
    Unknown project IDs pass through unchanged.
    """
    if not project_id:
        return project_id
    key = project_id.strip().lower()
    return _PROJECT_NORMALIZE.get(key, project_id)


# --- Tag cap to prevent unbounded compounding --------------------------------

_MAX_TAGS = 12


def _cap_tags(tags: list[str], max_tags: int = _MAX_TAGS) -> list[str]:
    """Truncate tag list if it exceeds max_tags.

    Priority order: existing/caller tags first, then category, then
    project, then content-type, then keywords. This ensures the most
    informative tags survive the truncation.
    """
    if len(tags) <= max_tags:
        return tags
    return tags[:max_tags]


# --- Keyword extraction (n-gram + stop-word heuristic) ----------------------

_STOP_WORDS: frozenset[str] = frozenset({
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "can", "shall", "to", "of", "in", "for",
    "on", "with", "at", "by", "from", "as", "into", "through", "during",
    "before", "after", "above", "below", "between", "out", "off", "over",
    "under", "again", "further", "then", "once", "here", "there", "when",
    "where", "why", "how", "all", "each", "every", "both", "few", "more",
    "most", "other", "some", "such", "no", "nor", "not", "only", "own",
    "same", "so", "than", "too", "very", "just", "because", "but", "and",
    "or", "if", "while", "about", "up", "down", "it", "its", "this",
    "that", "these", "those", "i", "me", "my", "we", "our", "you", "your",
    "he", "him", "his", "she", "her", "they", "them", "their", "what",
    "which", "who", "whom", "any", "one", "two", "three", "also", "well",
})


def _extract_keywords(content: str, max_keywords: int = 3) -> list[str]:
    """Extract topical keywords from content using simple n-gram scoring.

    Skips stop words and single characters. Picks the most frequent
    meaningful bigrams and trigrams, then falls back to single words.
    Returns up to ``max_keywords`` items. Returns empty list if content
    looks like code / structured data (SQL, JSON, markdown, file paths).
    """
    if not content:
        return []
    if _looks_like_code(content):
        return []
    text = re.sub(r"[^a-zA-Z0-9\s'-]", " ", content)
    tokens = [t.strip().lower() for t in text.split() if len(t.strip()) > 2]

    # Score bigrams
    bigrams: dict[str, int] = {}
    for i in range(len(tokens) - 1):
        w1, w2 = tokens[i], tokens[i + 1]
        if w1 in _STOP_WORDS and w2 in _STOP_WORDS:
            continue
        bg = f"{w1} {w2}"
        bigrams[bg] = bigrams.get(bg, 0) + 1

    # Score unigrams (non-stop-words)
    unigrams: dict[str, int] = {}
    for t in tokens:
        if t not in _STOP_WORDS and not t.startswith("'"):
            unigrams[t] = unigrams.get(t, 0) + 1

    # Pick best bigrams first (more specific)
    scored: list[tuple[int, str]] = [
        (count, phrase) for phrase, count in bigrams.items()
    ]
    scored.sort(key=lambda x: (-x[0], x[1]))
    result: list[str] = []
    for _, phrase in scored:
        # Skip if both words are already covered by a longer phrase
        if phrase not in result:
            result.append(phrase)
        if len(result) >= max_keywords:
            return result

    # Fall back to unigrams
    for _, word in sorted(
        ((c, w) for w, c in unigrams.items()),
        key=lambda x: (-x[0], x[1]),
    ):
        if word not in result:
            result.append(word)
        if len(result) >= max_keywords:
            break

    return result


def generate_tags(
    content: str,
    *,
    category: str = "fact",
    project_id: str | None = None,
    existing_tags: list[str] | None = None,
) -> list[str]:
    """Generate enriched tags for a memory at write time.

    Combines:
    - Existing / caller-supplied tags
    - Memory category as a tag
    - Project ID as a ``project:<id>`` tag
    - Content-type patterns (question, bug, decision, comparison, etc.)
    - Topical keyword extraction (n-gram frequency)

    This is deterministic, zero-dependency, and fast enough for the write
    path (~1ms on average content). Results are deduplicated and lowered.

    Usage::

        tags = generate_tags(content, category="decision", project_id="hermes")
    """
    result: list[str] = []
    if existing_tags:
        result.extend(existing_tags)

    # Category tag (always)
    if category:
        result.append(category)

    # Project tag (normalized)
    if project_id:
        canonical = _normalize_project(project_id) or project_id
        result.append(f"project:{canonical}")

    # Content-type patterns
    result.extend(_detect_content_type(content))

    # Topical keywords (max 3, skip if content is very short / boilerplate)
    if len(content) > 30:
        result.extend(_extract_keywords(content, max_keywords=3))

    merged = _merge_tags(result)
    return _cap_tags(merged)
