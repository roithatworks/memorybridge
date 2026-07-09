"""Model-export logic, decoupled from the MCP server (#91).

Both ``server.py`` (the MCP tools) and the Streamlit UI need to render a
profile's memories for another model / as a passport. Previously the UI did
``from server import export_for_model; export_for_model = _tool.fn`` — which
imported the whole server module (constructing a second ``MemoryStore`` +
``EntityExtractor`` and registering an ``atexit`` hook inside the Streamlit
process) just to unwrap a function via FastMCP's undocumented ``.fn`` attribute.

This module holds the actual logic as plain functions that take an explicit
``store`` and an optional analytics callback. Importing it has NO side effects
(no store construction, no atexit), so the UI can build its own store and call
these directly. ``server.py`` imports the same functions for its tool wrappers,
so there is a single implementation.
"""
import json
import re
from datetime import datetime
from typing import Callable, Optional

from db.constants import effective_score
from ingestion.passport import build_passport

# --------------------------------------------------------------------------- #
# Token counting (moved out of server.py so it carries no server dependency).
# --------------------------------------------------------------------------- #
try:
    import tiktoken
    _enc = tiktoken.get_encoding("cl100k_base")

    def _count_tokens_impl(text: str) -> int:
        return len(_enc.encode(text))
except ImportError:
    def _count_tokens_impl(text: str) -> int:
        words = len(re.findall(r'\b\w+\b', text))
        punctuation = len(re.findall(r'[^\w\s]', text))
        return int(words * 1.3 + punctuation * 0.5) or 1


def count_tokens(text: str) -> int:
    if not text:
        return 0
    return _count_tokens_impl(text) or 1


# --------------------------------------------------------------------------- #
# Recency decay scoring.
# --------------------------------------------------------------------------- #
DECAY_CONFIG = {
    "enabled": True,
    "half_life_days": 30,
    "min_score": 0.1,
    "boost_on_access": 0.1,
}


def apply_decay(memories: list, decay_config: dict) -> list:
    if not decay_config.get("enabled", True):
        return memories
    today = datetime.now()
    for mem in memories:
        mem["effective_score"] = effective_score(mem, today)
    return memories


# --------------------------------------------------------------------------- #
# Exports.
# --------------------------------------------------------------------------- #
def export_for_model(
    store,
    model: str,
    profile: str,
    depth: str = "full",
    max_tokens: int = 2000,
    log_analytics: Optional[Callable] = None,
) -> str:
    """Export a profile's memory formatted for a specific model, within budget.

    *store* is any MemoryStore-like object; *profile* must already be resolved.
    *log_analytics* is an optional callable matching server.log_to_analytics.
    """
    store.ensure_profile(profile)
    profile_data = store.get_profile(profile)
    if profile_data is None:
        return json.dumps({"error": f"Profile '{profile}' not found"})

    identity = profile_data["identity"]
    projects = profile_data["projects"]
    memories = store.get_memories(profile)

    memories = apply_decay([m.copy() for m in memories], DECAY_CONFIG)
    memories.sort(key=lambda m: m.get("effective_score", 0), reverse=True)

    budgets = {"full": max_tokens, "summary": max_tokens // 2, "minimal": max_tokens // 4}
    budget = budgets.get(depth, max_tokens)
    tokens_used = 0

    # Collect exported memories so we can count what was actually included.
    exported_memories: list = []

    def _collect_memories(lines_or_parts, append_fn, fmt_fn):
        nonlocal tokens_used
        for m in memories:
            if tokens_used >= budget - 50:
                break
            content = m.get("content", "")
            mem_tokens = count_tokens(content)
            if tokens_used + mem_tokens > budget - 50:
                remaining = budget - tokens_used - 50
                content = content[:remaining * 3] + "…"
            append_fn(fmt_fn(content))
            tokens_used += count_tokens(content) + 2
            exported_memories.append(m)

    if model == "chatgpt":
        lines = [
            "# Memory Chip",
            f"*Exported: {datetime.now().strftime('%Y-%m-%d %H:%M')}*",
            "",
            "## Identity",
            f"**Name:** {identity.get('name', 'Unknown')}",
            f"**Role:** {identity.get('role', 'Unknown')}",
            ""
        ]
        tokens_used = count_tokens("\n".join(lines))

        if identity.get("communication_style") and tokens_used < budget - 100:
            style = identity["communication_style"]
            style_lines = [
                "## Communication Style",
                f"**Tone:** {style.get('tone', '')}",
            ]
            if style.get("preferences"):
                for pref in style["preferences"][:3]:
                    style_lines.append(f"- {pref}")
            style_lines.append("")
            style_text = "\n".join(style_lines)
            if tokens_used + count_tokens(style_text) < budget:
                lines.extend(style_lines)
                tokens_used += count_tokens(style_text)

        if memories and depth in ("full", "summary") and tokens_used < budget - 100:
            lines.append("## Key Memories")
            _collect_memories(lines, lines.append, lambda c: f"- {c}")
            lines.append("")

        if projects and depth == "full" and tokens_used < budget - 100:
            lines.append("## Active Projects")
            for p in projects:
                if p.get("status") == "active" and tokens_used < budget - 50:
                    proj_line = f"- **{p.get('name', p.get('id'))}**: {p.get('description', '')[:50]}"
                    lines.append(proj_line)
                    tokens_used += count_tokens(proj_line)
            lines.append("")

        export_text = "\n".join(lines)

    elif model == "gemini":
        parts = [f"User: {identity.get('name', 'Unknown')} - {identity.get('role', 'Unknown')}"]
        tokens_used = count_tokens(parts[0])

        if identity.get("communication_style", {}).get("tone"):
            tone_part = f"Style: {identity['communication_style']['tone'][:50]}"
            if tokens_used + count_tokens(tone_part) < budget:
                parts.append(tone_part)
                tokens_used += count_tokens(tone_part)

        if memories and depth in ("full", "summary") and tokens_used < budget - 100:
            _collect_memories(parts, parts.append, lambda c: f"Mem: {c}")

        if projects and depth == "full" and tokens_used < budget - 100:
            active = [p.get("name", p["id"]) for p in projects if p.get("status") == "active"]
            if active:
                proj_part = f"Projects: {', '.join(active)}"
                if tokens_used + count_tokens(proj_part) < budget:
                    parts.append(proj_part)
                    tokens_used += count_tokens(proj_part)

        export_text = " | ".join(parts)

    elif model == "ollama":
        parts = [
            f"User={identity.get('name', 'Unknown')}",
            f"Role={identity.get('role', 'Unknown')[:30]}"
        ]
        tokens_used = sum(count_tokens(p) for p in parts)

        if memories and depth in ("full", "summary") and tokens_used < budget - 100:
            _collect_memories(parts, parts.append, lambda c: f"Mem={c[:80]}")

        if projects and depth in ("full", "summary") and tokens_used < budget - 100:
            active = [p["id"] for p in projects if p.get("status") == "active"]
            if active:
                proj_part = f"Projects={','.join(active)}"
                if tokens_used + count_tokens(proj_part) < budget:
                    parts.append(proj_part)
                    tokens_used += count_tokens(proj_part)

        export_text = ";".join(parts)

    else:
        return json.dumps({"error": f"Unknown model: {model}. Supported: chatgpt, gemini, ollama"})

    final_tokens = count_tokens(export_text)
    store.log_access("export_for_model", profile,
                     f"model={model}, tokens={final_tokens}", final_tokens)
    if log_analytics:
        log_analytics(
            tokens_served=final_tokens,
            memories_returned=len(exported_memories),
            model=model,
            profile=profile,
            operation="export_for_model",
        )
    return export_text


def export_passport(
    store,
    profile: str,
    max_tokens: int = 2000,
    log_analytics: Optional[Callable] = None,
) -> str:
    """Export a profile's memory as a model-agnostic plain-text passport."""
    store.ensure_profile(profile)
    profile_data = store.get_profile(profile)
    if profile_data is None:
        return (f"# Memory Passport\nProfile: {profile}\n"
                f"Generated: {datetime.now().strftime('%Y-%m-%d')}\n\n"
                f"Error: profile not found.")

    memories = store.get_memories(profile)
    identity = profile_data.get("identity", {})

    passport = build_passport(
        memories=memories,
        identity=identity,
        profile=profile,
        max_tokens=max_tokens,
        # Budget against the same real (tiktoken) counter used to measure the
        # result, so max_tokens is the ceiling the caller gets (#126).
        token_counter=count_tokens,
    )

    final_tokens = count_tokens(passport)
    store.log_access("export_passport", profile, f"tokens={final_tokens}", final_tokens)
    if log_analytics:
        log_analytics(
            tokens_served=final_tokens,
            memories_returned=len(memories),
            model="passport",
            profile=profile,
            operation="export_passport",
        )
    return passport
