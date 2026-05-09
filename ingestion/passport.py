"""
Memory Passport builder — model-agnostic, plain-text export format.

build_passport() is intentionally decoupled from the MCP server so it can be
tested independently and reused by CLI tools or ingestion pipelines.
"""
from datetime import datetime
from typing import Optional


# Category display order and human-readable labels
_CATEGORY_ORDER = [
    ("constraint", "Constraints"),
    ("preference", "Preferences"),
    ("skill", "Skills"),
    ("fact", "Facts"),
    ("insight", "Insights"),
    ("decision", "Decisions"),
    ("project_status", "Project Status"),
    ("relationship", "Relationships"),
]

# Importance sort weight (higher = listed first within a section)
_IMPORTANCE_WEIGHT = {"critical": 4, "high": 3, "medium": 2, "low": 1}


def _approx_tokens(text: str) -> int:
    """Rough token estimate — 1 token ≈ 4 chars. Fast, no tiktoken dependency."""
    return max(1, len(text) // 4)


def build_passport(
    memories: list,
    identity: Optional[dict] = None,
    profile: str = "default",
    max_tokens: int = 2000,
) -> str:
    """
    Render a Memory Passport as plain text within the given token budget.

    Args:
        memories:   List of memory dicts (content, category, importance, token_count).
        identity:   Optional identity dict (name, role, etc.) from the profile.
        profile:    Profile name — included in the header.
        max_tokens: Token ceiling for the entire output.

    Returns:
        Plain-text passport string.  Never JSON, never markdown code fences.
    """
    identity = identity or {}
    today = datetime.now().strftime("%Y-%m-%d")

    # -----------------------------------------------------------------
    # Build the fixed header (always present regardless of budget)
    # -----------------------------------------------------------------
    header_lines = [
        "# Memory Passport",
        f"Profile: {profile}",
        f"Generated: {today}",
    ]
    if identity.get("name"):
        header_lines.append(f"Name: {identity['name']}")
    if identity.get("role"):
        header_lines.append(f"Role: {identity['role']}")
    header_lines.append("")

    header_text = "\n".join(header_lines)
    budget_remaining = max_tokens - _approx_tokens(header_text)

    # -----------------------------------------------------------------
    # Group memories by category, sorted by importance within each group
    # -----------------------------------------------------------------
    by_category: dict[str, list] = {}
    for mem in memories:
        cat = mem.get("category", "fact")
        by_category.setdefault(cat, []).append(mem)

    for cat in by_category:
        by_category[cat].sort(
            key=lambda m: _IMPORTANCE_WEIGHT.get(m.get("importance", "medium"), 2),
            reverse=True,
        )

    # Build sections in canonical order, then catch any unlisted categories
    ordered_cats = [c for c, _ in _CATEGORY_ORDER if c in by_category]
    extra_cats = [c for c in by_category if c not in ordered_cats]
    section_order = ordered_cats + extra_cats

    label_map = dict(_CATEGORY_ORDER)

    # -----------------------------------------------------------------
    # Render sections within budget
    # -----------------------------------------------------------------
    sections: list[str] = []

    for cat in section_order:
        mems = by_category[cat]
        label = label_map.get(cat, cat.replace("_", " ").title())
        section_header = f"\n## {label}\n"
        section_cost = _approx_tokens(section_header)

        if budget_remaining - section_cost < 10:
            break  # no room for even this section header

        lines = [section_header]
        budget_remaining -= section_cost

        for mem in mems:
            content = mem.get("content", "").strip()
            if not content:
                continue
            imp = mem.get("importance", "medium")
            # Mark critical/high importance inline
            prefix = "! " if imp in ("critical", "high") else "- "
            line = f"{prefix}{content}"
            cost = _approx_tokens(line) + 1  # +1 for newline
            if budget_remaining - cost < 5:
                break
            lines.append(line)
            budget_remaining -= cost

        if len(lines) > 1:  # at least one memory made it in
            sections.append("\n".join(lines))

    return header_text + "\n".join(sections)
