"""Section-aware document parser for markdown/text files.

Splits documents by headings (H1-H3), detects document type (ADR, README,
guide, generic), and returns candidate memories with provenance metadata.
"""

import re
from pathlib import Path

# Document type detection patterns
_ADR_PATTERNS = [
    re.compile(r'\bADR[-_]?\d', re.IGNORECASE),
    re.compile(r'architecture.decision.record', re.IGNORECASE),
]
_README_PATTERN = re.compile(r'^README', re.IGNORECASE)
_GUIDE_PATTERNS = [
    re.compile(r'\bguide\b', re.IGNORECASE),
    re.compile(r'\bsetup\b', re.IGNORECASE),
    re.compile(r'\btutorial\b', re.IGNORECASE),
]

# ADR section headings (standard Nygard format)
_ADR_SECTIONS = {'status', 'context', 'decision', 'consequences', 'options'}

# Heading pattern: match Markdown H1-H3
_HEADING_RE = re.compile(r'^(#{1,3})\s+(.+)$', re.MULTILINE)

# Minimum section content length to keep (chars, not tokens)
MIN_SECTION_CHARS = 20


def detect_document_type(filepath: str, content: str = "") -> str:
    """Classify a document by its filename and first heading.

    Returns: "adr", "readme", "guide", or "generic".
    """
    name = Path(filepath).stem
    full_name = Path(filepath).name

    # ADR detection
    for pat in _ADR_PATTERNS:
        if pat.search(full_name):
            return "adr"

    # Also check first heading for ADR pattern
    first_heading = _HEADING_RE.search(content)
    if first_heading:
        heading_text = first_heading.group(2)
        for pat in _ADR_PATTERNS:
            if pat.search(heading_text):
                return "adr"

    # README detection
    if _README_PATTERN.match(full_name):
        return "readme"

    # Guide detection
    for pat in _GUIDE_PATTERNS:
        if pat.search(name):
            return "guide"

    return "generic"


def _split_by_headings(content: str) -> list[dict]:
    """Split markdown content by H1-H3 headings into sections.

    Returns list of {"heading": str, "level": int, "content": str,
    "line_start": int, "line_end": int}.
    """
    lines = content.split('\n')
    sections = []
    current_heading = ""
    current_level = 0
    current_lines = []
    current_start = 1

    for i, line in enumerate(lines, 1):
        match = _HEADING_RE.match(line)
        if match:
            # Save previous section
            if current_lines or current_heading:
                body = '\n'.join(current_lines).strip()
                if body or current_heading:
                    sections.append({
                        "heading": current_heading,
                        "level": current_level,
                        "content": body,
                        "line_start": current_start,
                        "line_end": i - 1,
                    })
            current_heading = match.group(2).strip()
            current_level = len(match.group(1))
            current_lines = []
            current_start = i
        else:
            current_lines.append(line)

    # Final section
    body = '\n'.join(current_lines).strip()
    if body or current_heading:
        sections.append({
            "heading": current_heading,
            "level": current_level,
            "content": body,
            "line_start": current_start,
            "line_end": len(lines),
        })

    return sections


def _merge_short_sections(sections: list[dict], min_chars: int = MIN_SECTION_CHARS) -> list[dict]:
    """Merge sections shorter than min_chars with their next sibling."""
    if not sections:
        return sections

    merged = []
    i = 0
    while i < len(sections):
        section = sections[i]
        # If content is too short and there's a next section, merge forward
        if len(section["content"]) < min_chars and i + 1 < len(sections):
            next_section = sections[i + 1]
            # Combine: use the shorter section's heading as a prefix
            prefix = section["heading"] + ": " if section["heading"] else ""
            combined_content = (prefix + section["content"]).strip()
            if combined_content:
                combined_content += "\n\n"
            combined_content += next_section["content"]

            merged.append({
                "heading": next_section["heading"] or section["heading"],
                "level": next_section["level"] or section["level"],
                "content": combined_content.strip(),
                "line_start": section["line_start"],
                "line_end": next_section["line_end"],
            })
            i += 2  # Skip both sections
        else:
            merged.append(section)
            i += 1

    return merged


def _adr_category(heading: str) -> str:
    """Map ADR section headings to memory categories."""
    lower = heading.lower().strip()
    if lower in ('decision', 'decisions'):
        return "decision"
    if lower in ('context', 'background', 'problem'):
        return "fact"
    if lower in ('consequences', 'implications'):
        return "insight"
    if lower in ('status',):
        return "project_status"
    return "fact"


def _adr_why_it_matters(heading: str, decision_content: str = "") -> str | None:
    """Auto-generate why_it_matters for ADR sections."""
    lower = heading.lower().strip()
    if lower in ('decision', 'decisions'):
        return "This is an architectural decision — reference it when making related design choices"
    if lower in ('context', 'background', 'problem'):
        if decision_content:
            return f"This context led to a decision: {decision_content[:120]}"
        return "Background context for an architectural decision"
    if lower in ('consequences', 'implications'):
        return "Consider these consequences when evaluating changes to the related decision"
    return None


MAX_DOCUMENT_BYTES = 10 * 1024 * 1024  # 10MB ceiling to prevent OOM


def parse(filepath: str, *, extract_facts: bool = False) -> list[dict]:
    """Parse a markdown/text document into candidate memories."""
    path = Path(filepath)
    if not path.exists():
        return []

    try:
        if path.stat().st_size > MAX_DOCUMENT_BYTES:
            return []
    except OSError:
        return []

    content = path.read_text(encoding="utf-8", errors="replace")
    if not content.strip():
        return []

    doc_type = detect_document_type(filepath, content)
    sections = _split_by_headings(content)
    sections = _merge_short_sections(sections)

    # For ADR docs, find the decision section content to use in why_it_matters
    decision_content = ""
    if doc_type == "adr":
        for s in sections:
            if s["heading"].lower().strip() in ('decision', 'decisions'):
                decision_content = s["content"][:200]
                break

    candidates = []
    for section in sections:
        body = section["content"]
        if not body.strip():
            continue

        # Compose the memory content: heading + body for context
        if section["heading"]:
            memory_content = f"{section['heading']}: {body}"
        else:
            memory_content = body

        # Infer category, importance, why_it_matters based on doc type
        if doc_type == "adr":
            category = _adr_category(section["heading"])
            importance = "high" if category == "decision" else "medium"
            why = _adr_why_it_matters(section["heading"], decision_content)
            confidence = 0.85  # ADRs are authoritative
        elif doc_type == "readme":
            category = "fact"
            importance = "medium"
            why = None
            confidence = 0.85  # READMEs are authoritative
        elif doc_type == "guide":
            category = "procedural"
            importance = "medium"
            why = "Follow this procedure when performing the described task"
            confidence = 0.80  # Guides are flagged for review
        else:
            category = "fact"
            importance = "medium"
            why = None
            confidence = 0.80  # Generic docs flagged for review

        candidates.append({
            "content": memory_content.strip(),
            "section": section["heading"],
            "source_file": str(filepath),
            "line_range": (section["line_start"], section["line_end"]),
            "doc_type": doc_type,
            "category": category,
            "importance": importance,
            "why_it_matters": why,
            "confidence": confidence,
        })

    return candidates
