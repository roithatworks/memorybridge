#!/usr/bin/env python3
"""Export MemoryBridge SQLite DB → Open Knowledge Format (.okf/) directory.

Usage:
    python3 scripts/export-okf.py [--db ~/memorybridge/memory.db] [--out ./export.okf] [--project "Hermes Agent"]

Design:
    - One .md file per memory (named by content slug + id suffix)
    - Grouped: <out>/<project>/<category>/<concept>.md
    - OKF-compliant frontmatter (type, title, description, tags, timestamp)
    - index.md at each level with progressive disclosure summaries
    - log.md at root with export timestamp
    - Cross-references between related memories (same project + overlapping tags)
"""

import argparse
import json
import os
import re
import sqlite3
import sys
import textwrap
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ── Mapping: MemoryBridge category → OKF type ──────────────────────────
CATEGORY_TO_OKF_TYPE = {
    "fact":           "Fact",
    "decision":       "Decision",
    "insight":        "Insight",
    "preference":     "Preference",
    "constraint":     "Constraint",
    "skill":          "Skill",
    "relationship":   "Relationship",
    "project_status": "Status",
}

IMPORTANCE_TO_PRIORITY = {
    "low":     "low",
    "medium":  "medium",
    "high":    "high",
    "critical":"critical",
}

# ── Helpers ─────────────────────────────────────────────────────────────

def slugify(text: str, max_len: int = 48) -> str:
    """Turn content into a filesystem-safe slug."""
    text = text.lower()
    text = re.sub(r"[^a-z0-9\s-]", "", text)
    text = re.sub(r"\s+", "-", text.strip())
    text = text[:max_len].rstrip("-")
    return text or "untitled"


def safe_project_dir(proj: str) -> str:
    """Filesystem-safe directory name for a project. project_id comes from
    LLM-extracted ingestion, so a value like '../../etc' must not escape the
    output directory (#105). slugify() strips '/', '.', etc."""
    if proj == "_unassigned":
        return "_unassigned"
    return slugify(proj) or "_unassigned"


def wrap_text(text: str, width: int = 80) -> str:
    """Wrap text at width, preserving existing paragraphs."""
    paragraphs = text.split("\n")
    wrapped = []
    for p in paragraphs:
        if p.strip():
            wrapped.extend(textwrap.wrap(p, width=width))
        else:
            wrapped.append("")
    return "\n".join(wrapped)


def parse_tags(tags_json: str) -> list[str]:
    """Parse JSON tags array, filtering out entity: prefix noise."""
    try:
        tags = json.loads(tags_json)
    except (json.JSONDecodeError, TypeError):
        return []
    # Exclude entity: references (those are internal)
    return [t for t in tags if not t.startswith("entity:")]


def pick_title(content: str, max_len: int = 72) -> str:
    """Derive a title from content: first sentence or meaningful prefix."""
    # Try first line
    first_line = content.split("\n")[0].strip()
    if first_line and len(first_line) <= max_len:
        return first_line.rstrip(".:")
    # Try first sentence
    match = re.match(r"^([^.!?\n]*[.!?])", content)
    if match:
        title = match.group(1).strip()
        if len(title) <= max_len:
            return title.rstrip(".")
    # Truncate
    return content[:max_len].rstrip() + "…"


def get_description(content: str, max_len: int = 160) -> str:
    """First ~160 chars of content as description."""
    clean = content.replace("\n", " ").strip()
    if len(clean) <= max_len:
        return clean
    return clean[:max_len].rsplit(" ", 1)[0] + "…"


# ── Core export logic ───────────────────────────────────────────────────

class OKFExporter:
    """Reads MemoryBridge SQLite, writes .okf/ directory tree."""

    def __init__(self, db_path: str, out_dir: str,
                 profile: str = "default",
                 project_filter: str | None = None,
                 min_importance: str = "low"):
        self.db_path = Path(db_path).expanduser()
        self.out_dir = Path(out_dir).expanduser()
        self.profile = profile
        self.project_filter = project_filter
        self.min_importance = min_importance
        self.conn: sqlite3.Connection | None = None
        self._stats: dict[str, int] = defaultdict(int)

    def connect(self):
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row

    def close(self):
        if self.conn:
            self.conn.close()

    def fetch_memories(self) -> list[sqlite3.Row]:
        """Fetch non-archived memories, optionally filtered by project."""
        importance_order = {"low": 0, "medium": 1, "high": 2, "critical": 3}
        min_val = importance_order.get(self.min_importance, 0)

        query = """
            SELECT id, content, category, importance, tags, project_id,
                   created_at, last_accessed, access_count
            FROM memories
            WHERE archived = 0
              AND profile = ?
        """
        params: list[Any] = [self.profile]

        if self.project_filter:
            query += " AND project_id = ?"
            params.append(self.project_filter)

        rows = self.conn.execute(query, params).fetchall()

        # Filter by importance
        filtered = []
        for r in rows:
            if importance_order.get(r["importance"], 0) >= min_val:
                filtered.append(r)
        return filtered

    def _build_entity_index(self, memories: list[sqlite3.Row]) -> dict[str, set[str]]:
        """Build a map: entity_name → set of memory ids that reference it."""
        index: dict[str, set[str]] = defaultdict(set)
        for m in memories:
            tags = parse_tags(m["tags"])
            for t in tags:
                index[t].add(m["id"])
        return index

    def write_concept(self, out_path: Path, memory: sqlite3.Row,
                      related_ids: list[str]):
        """Write a single OKF concept document."""
        content = memory["content"].strip()
        category = memory["category"]
        okf_type = CATEGORY_TO_OKF_TYPE.get(category, "Knowledge")
        title = pick_title(content)
        description = get_description(content)
        tags = parse_tags(memory["tags"])
        tags.append(category)  # Always include category as a tag

        frontmatter = {
            "type": okf_type,
            "title": title,
            "description": description,
            "tags": sorted(set(tags)),
            "importance": IMPORTANCE_TO_PRIORITY.get(memory["importance"], "medium"),
            "timestamp": memory["created_at"],
            "last_accessed": memory["last_accessed"],
            "access_count": memory["access_count"],
        }
        if memory["project_id"]:
            frontmatter["project"] = memory["project_id"]

        fm_lines = ["---"]
        for k, v in frontmatter.items():
            if v is None or v == "" or v == []:
                continue
            if k == "tags":
                fm_lines.append(f"{k}: [{', '.join(v)}]")
            elif isinstance(v, str):
                # Escape colons and quotes in values
                sv = str(v).replace('"', '\\"')
                if ":" in sv or "'" in sv:
                    fm_lines.append(f'{k}: "{sv}"')
                else:
                    fm_lines.append(f"{k}: {sv}")
            else:
                fm_lines.append(f"{k}: {v}")
        fm_lines.append("---")

        # Body
        body_parts = [f"# {title}", "", wrap_text(description), ""]
        if content != description:
            body_parts.extend([wrap_text(content), ""])

        # Related links
        if related_ids:
            body_parts.append("## Related")
            body_parts.append("")
            for rid in sorted(related_ids):
                # Link to sibling concept file (relative)
                body_parts.append(f"- [{rid}](./{rid}.md)")
            body_parts.append("")

        body_parts.append("---")
        body_parts.append(f"*MemoryBridge ID: `{memory['id']}`*")

        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text("\n".join(fm_lines + body_parts) + "\n")

    def write_index(self, dir_path: Path, title: str,
                    entries: list[tuple[str, str, str]],
                    description: str = ""):
        """Write an index.md for progressive disclosure."""
        lines = [
            "---",
            f'type: Index',
            f'title: "{title}"',
            f'description: "{description}"' if description else "",
            "---",
            "",
            f"# {title}",
            "",
        ]
        if description:
            lines.append(f"{description}")
            lines.append("")

        if entries:
            lines.append("## Contents")
            lines.append("")
            lines.append("| Name | Type | Summary |")
            lines.append("|------|------|---------|")
            for name, okf_type, summary in sorted(entries):
                # Strip .md suffix for linking
                display = name.replace(".md", "")
                slug_path = Path(name)
                link = str(slug_path)
                lines.append(f"| [{display}]({link}) | {okf_type} | {summary[:80]} |")
            lines.append("")

        dir_path.mkdir(parents=True, exist_ok=True)
        (dir_path / "index.md").write_text("\n".join(lines) + "\n")

    def write_log(self, out_dir: Path, stats: dict[str, int]):
        """Write log.md with export metadata."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        lines = [
            "---",
            f'type: Log',
            f'title: "Export Log"',
            "---",
            "",
            f"# Export Log — {now}",
            "",
            f"MemoryBridge export generated by `export-okf.py`.",
            "",
            "## Summary",
            "",
            f"| Metric | Value |",
            "|--------|-------|",
            f"| Total concepts exported | {stats.get('total', 0)} |",
            f"| Projects represented | {stats.get('projects', 0)} |",
            f"| Categories used | {stats.get('categories', 0)} |",
            f"| Source DB | `{self.db_path}` |",
            f"| Profile | `{self.profile}` |",
            "",
            "## Per-Category Breakdown",
            "",
            "| Category | Count |",
            "|----------|-------|",
        ]
        for cat, count in sorted(stats.get("by_category", {}).items()):
            lines.append(f"| {cat} | {count} |")
        lines.append("")

        (out_dir / "log.md").write_text("\n".join(lines) + "\n")

    def export(self):
        self.connect()
        memories = self.fetch_memories()
        if not memories:
            print("No memories found matching filters.")
            return

        # Group: project → category → list of memories
        grouped: dict[str, dict[str, list[sqlite3.Row]]] = defaultdict(
            lambda: defaultdict(list)
        )
        for m in memories:
            proj = m["project_id"] or "_unassigned"
            grouped[proj][m["category"]].append(m)

        # Entity index for related-links
        entity_index = self._build_entity_index(memories)

        out_root = self.out_dir

        # Write root index
        project_entries = []
        for proj in sorted(grouped.keys()):
            cat_count = len(grouped[proj])
            total = sum(len(v) for v in grouped[proj].values())
            display_name = proj.replace("_unassigned", "Uncategorized")
            project_entries.append(
                (f"{safe_project_dir(proj)}/index.md", "Index",
                 f"{total} concepts across {cat_count} categories")
            )
        self.write_index(out_root, "MemoryBridge Knowledge",
                         project_entries,
                         "OKF bundle exported from MemoryBridge. "
                         f"{len(memories)} concepts across {len(grouped)} projects.")

        # Write each project
        for proj, categories in sorted(grouped.items()):
            proj_dir = out_root / safe_project_dir(proj)
            # Defense in depth — should be impossible after slugify.
            if not proj_dir.resolve().is_relative_to(out_root.resolve()):
                print(f"  Skipping project with unsafe path: {proj!r}", file=sys.stderr)
                continue
            proj_total = sum(len(v) for v in categories.values())

            # Project index
            cat_entries = []
            for cat, mems in sorted(categories.items()):
                okf_type = CATEGORY_TO_OKF_TYPE.get(cat, "Knowledge")
                cat_entries.append(
                    (f"{cat}/index.md", okf_type,
                     f"{len(mems)} items")
                )
            self.write_index(proj_dir, proj or "Uncategorized",
                             cat_entries,
                             f"{proj_total} concepts")

            # Write each category
            for cat, mems in sorted(categories.items()):
                okf_type = CATEGORY_TO_OKF_TYPE.get(cat, "Knowledge")
                cat_dir = proj_dir / cat

                # Category index
                concept_entries = []
                for m in mems:
                    slug = slugify(m["content"])
                    fname = f"{slug}-{m['id'][-8:]}.md"
                    title = pick_title(m["content"])
                    concept_entries.append((fname, okf_type, title))
                self.write_index(cat_dir, f"{okf_type} — {proj or 'Uncategorized'}",
                                 concept_entries,
                                 f"{len(mems)} {cat} memories")

                # Write each concept
                all_mem_ids = {m["id"] for m in mems}
                for m in mems:
                    slug = slugify(m["content"])
                    fname = f"{slug}-{m['id'][-8:]}.md"
                    concept_path = cat_dir / fname

                    # Find related: same project, overlapping tags
                    mem_tags = set(parse_tags(m["tags"]))
                    related = []
                    for other_id in all_mem_ids:
                        if other_id == m["id"]:
                            continue
                        other_tags = set()
                        # Check entity index for simple tag overlap
                        for tag in mem_tags:
                            if tag in entity_index and other_id in entity_index[tag]:
                                other_tags.add(tag)
                        # Also match by same project+category (always include)
                        # We'll just flag same-category siblings if tags overlap
                        if other_tags:
                            related.append(other_id)

                    self.write_concept(concept_path, m, related[:5])  # cap at 5 links
                    self._stats["total"] += 1
                    self._stats[f"by_category:{cat}"] += 1

        self._stats["projects"] = len(grouped)

        # Write log
        by_cat = {}
        for k, v in self._stats.items():
            if k.startswith("by_category:"):
                by_cat[k.replace("by_category:", "")] = v
        self._stats["by_category"] = by_cat
        self._stats["categories"] = len(by_cat)
        self.write_log(out_root, self._stats)

        self.close()
        self._print_summary()

    def _print_summary(self):
        print(f"\n✅ OKF export complete")
        print(f"   Source: {self.db_path}")
        print(f"   Output: {self.out_dir}")
        print(f"   Concepts: {self._stats.get('total', 0)}")
        print(f"   Projects: {self._stats.get('projects', 0)}")
        print(f"   Categories: {self._stats.get('categories', 0)}")
        print(f"\n   Tree:")
        self._print_tree(self.out_dir)

    def _print_tree(self, path: Path, prefix: str = ""):
        if not path.is_dir():
            print(f"{prefix}📄 {path.name}")
            return
        items = sorted(path.iterdir())
        dirs = [p for p in items if p.is_dir() and p.name not in ("__pycache__",)]
        files = [p for p in items if p.is_file() and p.name not in ("log.md",)]
        log_file = path / "log.md"

        index_file = path / "index.md"
        if index_file.exists():
            print(f"{prefix}📂 {path.name}/")
        else:
            print(f"{prefix}📂 {path.name}/ (no index)")

        sub_prefix = prefix + "   "
        for d in sorted(dirs):
            self._print_tree(d, sub_prefix)
        for f in sorted(files):
            print(f"{sub_prefix}📄 {f.name}")
        if log_file.exists():
            print(f"{sub_prefix}📋 log.md")


def main():
    parser = argparse.ArgumentParser(
        description="Export MemoryBridge to Open Knowledge Format (.okf)"
    )
    parser.add_argument(
        "--db",
        default=os.path.join(
            os.environ.get("MEMORYBRIDGE_DATA", os.path.expanduser("~/memorybridge")),
            "memory.db"),
        help="Path to MemoryBridge SQLite DB (default honors MEMORYBRIDGE_DATA)")
    parser.add_argument("--out", default="./memorybridge.okf",
                        help="Output .okf/ directory")
    parser.add_argument("--profile", default="default",
                        help="Profile to export (default: 'default')")
    parser.add_argument("--project", default=None,
                        help="Filter to single project (export all if omitted)")
    parser.add_argument("--min-importance", default="low",
                        choices=["low", "medium", "high", "critical"],
                        help="Minimum importance level (default: low = all)")
    parser.add_argument("--preview", action="store_true",
                        help="Just print stats, don't write files")
    args = parser.parse_args()

    exporter = OKFExporter(
        db_path=args.db,
        out_dir=args.out,
        profile=args.profile,
        project_filter=args.project,
        min_importance=args.min_importance,
    )

    if args.preview:
        exporter.connect()
        memories = exporter.fetch_memories()
        exporter.close()
        by_proj = defaultdict(int)
        by_cat = defaultdict(int)
        for m in memories:
            proj = m["project_id"] or "_unassigned"
            by_proj[proj] += 1
            by_cat[m["category"]] += 1
        print(f"Preview: {len(memories)} memories match filters")
        print(f"\nProjects:")
        for p, c in sorted(by_proj.items(), key=lambda x: -x[1]):
            print(f"  {p or '(unassigned)'}: {c}")
        print(f"\nCategories:")
        for c, n in sorted(by_cat.items(), key=lambda x: -x[1]):
            print(f"  {c}: {n}")
        return

    exporter.export()


if __name__ == "__main__":
    main()
