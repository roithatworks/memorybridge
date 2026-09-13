"""MemoryBridge command-line interface.

Installed as the `mb` console script (see pyproject.toml). Cross-platform —
no macOS/launchd assumptions.

    mb init              # one-time setup: data dir, config, empty store, snippet
    mb serve             # run the MCP server over stdio (what Claude Desktop launches)
    mb serve --http      # run the HTTP bridge (for remote MCP clients)
    mb ingest --source claude --file conversations.json
    mb ui                # launch the Streamlit review UI
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import config


# --------------------------------------------------------------------------- #
def _module_path(name: str) -> str:
    """Absolute path to an installed top-level module, without importing it."""
    spec = importlib.util.find_spec(name)
    if spec is None or not spec.origin:
        raise RuntimeError(f"cannot locate module {name!r} — is MemoryBridge installed correctly?")
    return spec.origin


_ENV_TEMPLATE = """\
# MemoryBridge environment. Fill in the keys you use; leave the rest blank.
# Extraction (ingestion of conversation exports) needs a DeepSeek key; the
# escalation resolver needs an Anthropic key. The core memory server needs
# neither — you can run it and add memories without any API keys.
DEEPSEEK_API_KEY=
ANTHROPIC_API_KEY=

# Optional: Notion sync for the flagged-review queue.
# NOTION_API_TOKEN=
# NOTION_FLAGGED_DB_ID=

# Optional: shared secret for the HTTP bridge (`mb serve --http`).
# MEMORYBRIDGE_TOKEN=
"""

_CONFIG_TEMPLATE = """\
# MemoryBridge config (optional — delete to fall back to defaults).
# See memorybridge.example.yaml in the repo for the full documented schema.
max_total_tokens: 50000

# Domain auto-routing is OFF until you add domains here. Example:
# routing:
#   domains:
#     work: [deadline, sprint, roadmap]
#     personal: [family, home, health]
routing:
  domains: {}
"""


def cmd_init(args: argparse.Namespace) -> int:
    data = config.data_dir()
    data.mkdir(parents=True, exist_ok=True)
    (data / "inbox").mkdir(exist_ok=True)
    (data / "logs").mkdir(exist_ok=True)

    env_path = data / ".env"
    if not env_path.exists():
        env_path.write_text(_ENV_TEMPLATE, encoding="utf-8")
        print(f"  wrote {env_path}")
    else:
        print(f"  kept  {env_path} (already exists)")

    # Write a minimal starter config if the user has none yet.
    target = data / "memorybridge.yaml"
    if not target.exists():
        target.write_text(_CONFIG_TEMPLATE, encoding="utf-8")
        print(f"  wrote {target}")
    else:
        print(f"  kept  {target} (already exists)")

    # Create the store + a default profile (skip embedding model download).
    os.environ.setdefault("MEMORYBRIDGE_NO_EMBED", "1")
    from db.store import MemoryStore
    store = MemoryStore(data / "memory.db")
    store.ensure_profile("default")
    print(f"  ready {data / 'memory.db'} (profile 'default')")

    snippet = {
        "mcpServers": {
            "memorybridge": {
                "command": "mb",
                "args": ["serve"],
                "env": {"MEMORYBRIDGE_DATA": str(data)},
            }
        }
    }
    print("\nMemoryBridge is initialized. Add this to your Claude Desktop config")
    print("(Settings -> Developer -> Edit Config):\n")
    print(json.dumps(snippet, indent=2))
    print("\nThen restart Claude Desktop. Run `mb serve --http` to expose the "
          "HTTP bridge for other MCP clients.")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    # Replace this process with the server so stdio passes through cleanly.
    env = dict(os.environ)
    env["MEMORYBRIDGE_TRANSPORT"] = "http" if args.http else "stdio"
    os.execve(sys.executable, [sys.executable, _module_path("server")], env)
    return 0  # unreachable


def cmd_ingest(args: argparse.Namespace) -> int:
    run_py = _module_path("ingestion.run")
    argv = [sys.executable, run_py]
    for flag in ("source", "file", "days", "profile"):
        val = getattr(args, flag, None)
        if val is not None:
            argv += [f"--{flag}", str(val)]
    if args.preview:
        argv.append("--preview")
    return subprocess.call(argv, env=dict(os.environ))


def cmd_ui(args: argparse.Namespace) -> int:
    app = _module_path("ui.app")
    return subprocess.call(["streamlit", "run", app], env=dict(os.environ))


def cmd_backup(args: argparse.Namespace) -> int:
    """Create, list, or verify VACUUM INTO backups."""
    from db.backup import create_backup, verify_backup, list_backups
    data = config.data_dir()
    db_path = data / "memory.db"
    backup_dir = data / "backups"

    if args.list:
        results = list_backups(backup_dir)
        if not results:
            print("No backups found.")
            return 0
        print(f"{'Timestamp':<22} {'Size':>10}  {'Memories':>8}  {'Integrity'}")
        print("-" * 60)
        for r in results:
            size = f"{r.size_bytes / 1024 / 1024:.1f} MB"
            ok = "✓" if r.integrity_ok else "✗ FAILED"
            print(f"{r.created_at:<22} {size:>10}  {r.memory_count:>8}  {ok}")
        return 0

    if args.verify:
        results = list_backups(backup_dir)
        if not results:
            print("No backups found to verify.")
            return 0
        all_ok = True
        for r in results:
            status = "OK" if r.integrity_ok else "FAILED"
            print(f"{r.path.name}: {status} ({r.memory_count} memories)")
            if not r.integrity_ok:
                all_ok = False
        return 0 if all_ok else 1

    # Default: create a new backup.
    if not db_path.exists():
        print(f"Database not found: {db_path}")
        return 1
    result = create_backup(db_path, backup_dir)
    size_mb = result.size_bytes / 1024 / 1024
    print(f"Backup created: {result.path.name}")
    print(f"  Size: {size_mb:.1f} MB")
    print(f"  Memories: {result.memory_count}")
    print(f"  Integrity: {'OK' if result.integrity_ok else 'FAILED'}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    """Per-client activity summary: volume bar, count, 7-day delta, trust label.

    Trust labels are honest about provenance (#180): `source` is
    transport-verified ("claude" = stdio, provably Claude Code/Desktop;
    "remote" = HTTP bridge, provably non-Claude but unknown which model),
    while `client_name` is self-reported and unverified — any caller can
    claim any name. Rows written before provenance existed have NULL source
    and are labeled `untracked` rather than guessed at.
    """
    from datetime import datetime, timedelta
    data = config.data_dir()
    db_path = data / "memory.db"
    if not db_path.exists():
        print(f"Database not found: {db_path} (run `mb init` first)")
        return 1
    os.environ.setdefault("MEMORYBRIDGE_NO_EMBED", "1")
    from db.store import MemoryStore
    store = MemoryStore(db_path)
    profile = args.profile or "default"

    week_ago = (datetime.now() - timedelta(days=7)).isoformat()
    rows = store._conn.execute(
        """SELECT source, client_name,
                  COUNT(*)                                        AS mem,
                  SUM(CASE WHEN created_at >= ? THEN 1 ELSE 0 END) AS recent,
                  MAX(created_at)                                 AS last_write
           FROM memories
           WHERE profile = ? AND archived = 0
           GROUP BY source, client_name
           ORDER BY mem DESC""",
        (week_ago, profile),
    ).fetchall()

    if not rows:
        print(f"No active memories in profile '{profile}'.")
        return 0

    def identity(r) -> tuple[str, str]:
        """(display name, trust label) for a (source, client_name) group."""
        if r["client_name"]:
            return r["client_name"], "self-reported"
        if r["source"] == "claude":
            return "claude", "verified"
        if r["source"] == "remote":
            return "remote", "unattributed"
        return "unknown", "untracked"

    BAR_WIDTH = 22
    max_mem = max(r["mem"] for r in rows)
    clients = []
    for r in rows:
        name, trust = identity(r)
        filled = max(1, round(r["mem"] / max_mem * BAR_WIDTH)) if r["mem"] else 0
        bar = "●" * filled + "○" * (BAR_WIDTH - filled)
        delta = f"▲{r['recent']}" if r["recent"] else "—"
        clients.append((name, bar, r["mem"], delta, trust, r["last_write"]))

    name_w = max(8, max(len(c[0]) for c in clients) + 2)
    print(f"{'MODEL':<{name_w}} {'ACTIVITY':<{BAR_WIDTH + 2}} {'MEM':>5}  {'Δ7D':<5} TRUST")
    for name, bar, mem, delta, trust, _ in clients:
        print(f"{name:<{name_w}} {bar:<{BAR_WIDTH + 2}} {mem:>5}  {delta:<5} {trust}")

    # Totals line: count, % of token budget, most recent write across clients.
    stats = store.token_stats(profile)
    budget = config.max_total_tokens()
    pct = f"{stats['total_tokens'] / budget * 100:.1f}%" if budget else "n/a"
    last_name, last_ts = max(
        ((c[0], c[5]) for c in clients if c[5]), key=lambda t: t[1], default=(None, None)
    )
    if last_ts:
        try:
            age = datetime.now() - datetime.fromisoformat(last_ts)
            secs = int(age.total_seconds())
            human = (f"{secs // 86400}d" if secs >= 86400 else
                     f"{secs // 3600}h" if secs >= 3600 else
                     f"{max(secs // 60, 1)}m") + " ago"
        except ValueError:
            human = last_ts
        last_part = f" · last write {human} ({last_name})"
    else:
        last_part = ""
    print(f"\n{stats['memory_count']} total · {pct} of token budget{last_part}")
    return 0


def cmd_maintain(args: argparse.Namespace) -> int:
    from datetime import datetime
    data = config.data_dir()
    os.environ.setdefault("MEMORYBRIDGE_NO_EMBED", "1")
    from db.store import MemoryStore
    from db.pruner import run_auto_prune
    store = MemoryStore(data / "memory.db")
    profile = args.profile or "default"
    store.ensure_profile(profile)

    mode = "weekly" if args.weekly else "nightly"
    print(f"Running MemoryBridge {mode} maintenance for profile '{profile}'...")

    # 0. Pre-maintenance backup (before any destructive operation)
    db_path = data / "memory.db"
    if db_path.exists():
        try:
            from db.backup import create_backup
            result = create_backup(db_path, data / "backups")
            size_mb = result.size_bytes / 1024 / 1024
            print(f"  Backup: {result.path.name} ({size_mb:.1f} MB, {result.memory_count} memories)")
        except Exception as exc:
            print(f"  Backup failed (non-fatal): {exc}")

    # 1. Purge expired TTL memories
    now_iso = datetime.now().isoformat()
    with store._conn.transaction():
        cur = store._conn.execute(
            "UPDATE memories SET archived=1, archived_at=?, archive_reason='TTL expired' "
            "WHERE profile=? AND archived=0 AND expires_at IS NOT NULL AND expires_at < ?",
            (now_iso, profile, now_iso)
        )
        expired_count = cur.rowcount
        store._conn.commit()
    print(f"  Expired TTL memories purged: {expired_count}")

    # 2. Dedup / Auto-prune (archives, does not delete — issue #195)
    prune_res = run_auto_prune(
        store._conn, profile,
        lambda p, mid: store.archive_memory(p, mid, reason="pruned: cli maintenance"),
        allow_auto_delete=True)
    print(f"  Duplicates / stale auto-pruned: {len(prune_res.get('auto_executed', []))}")
    # 3. Purge archived embeddings (issue #192). Guarded to memories archived
    #    longer than 30 days, so a recent reversal keeps its vector.
    purged = store.purge_archived_embeddings(older_than_days=30)
    print(f"  Archived embeddings purged (archived >30d): {purged}")
    if args.weekly:
        # 3. Low-score pruning
        budget_pruned = store.auto_prune(profile, threshold=0.15)
        print(f"  Low-score memories archived: {len(budget_pruned)}")

        stats = store.token_stats(profile)
        edges = store._conn.execute("SELECT COUNT(*) FROM memory_edges").fetchone()[0]
        print(f"\nWeekly Health Check:")
        print(f"  Active memories: {stats.get('memory_count', 0)}")
        print(f"  Total tokens: {stats.get('total_tokens', 0)}")
        print(f"  Knowledge graph edges: {edges}")

    print("Maintenance complete.")
    return 0


def cmd_edges(args: argparse.Namespace) -> int:
    """Manage graph memory edges: list, add, rm."""
    data = config.data_dir()
    db_path = data / "memory.db"
    if not db_path.exists():
        print(f"Database not found: {db_path} (run `mb init` first)")
        return 1
        
    os.environ.setdefault("MEMORYBRIDGE_NO_EMBED", "1")
    from db.store import MemoryStore
    store = MemoryStore(db_path)
    
    if args.action == "list":
        if args.memory_id:
            rows = store.get_edges(args.memory_id)
            if not rows:
                print(f"No edges found for memory {args.memory_id}.")
                return 0
            for r in rows:
                print(f"{r['id']}: {r['source_id']} --[{r['relation']}]--> {r['target_id']}")
        else:
            rows = store._conn.execute("SELECT * FROM memory_edges ORDER BY created_at DESC LIMIT 50").fetchall()
            if not rows:
                print("No edges found in the database.")
                return 0
            for r in rows:
                print(f"{r['id']}: {r['source_id']} --[{r['relation']}]--> {r['target_id']}")
            if len(rows) == 50:
                print("(Showing 50 most recent edges. Pass --memory-id to filter.)")
                
    elif args.action == "add":
        try:
            edge_id = store.add_edge(args.source_id, args.target_id, args.relation)
            print(f"Created edge {edge_id}: {args.source_id} --[{args.relation}]--> {args.target_id}")
        except Exception as e:
            print(f"Failed to add edge: {e}")
            return 1
            
    elif args.action == "rm":
        deleted = store.delete_edge(args.edge_id)
        if deleted:
            print(f"Deleted edge {args.edge_id}")
        else:
            print(f"Edge {args.edge_id} not found")
            return 1
            
    return 0


def cmd_reverse_prune(args: argparse.Namespace) -> int:
    """Un-archive a pruned memory — the caller for autonomous_reverse_prune (#194).

    db/pruner.py owns the policy (when/what to penalize); this is the entry point
    that exercises it. Logged as 'auto_reversed' so the weekly audit can see it.
    """
    data = config.data_dir()
    os.environ.setdefault("MEMORYBRIDGE_NO_EMBED", "1")
    from db.store import MemoryStore
    from db.pruner import autonomous_reverse_prune

    store = MemoryStore(data / "memory.db")
    profile = args.profile or "default"
    store.ensure_profile(profile)

    result = autonomous_reverse_prune(store._conn, profile, args.memory_id, args.reason)

    if "error" in result:
        print(f"  Reverse failed: {result['error']}")
        return 1

    print(f"  Reversed {args.memory_id} (rule: {result['rule_name']})")
    print(f"  Reason: {result['reason']}")
    if result["penalty_applied"]:
        print(
            f"  Rule '{result['rule_name']}' penalized: >1 reversal in 30d, "
            f"confidence held below auto-execute until it re-earns it"
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mb", description="MemoryBridge — cross-model memory server")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="one-time setup: data dir, config, empty store").set_defaults(func=cmd_init)

    sp = sub.add_parser("serve", help="run the MCP server (stdio by default)")
    sp.add_argument("--http", action="store_true", help="run the HTTP bridge instead of stdio")
    sp.set_defaults(func=cmd_serve)

    ig = sub.add_parser("ingest", help="ingest a conversation export")
    ig.add_argument("--source", required=True, choices=["claude", "chatgpt", "gemini", "hermes"])
    ig.add_argument("--file")
    ig.add_argument("--days", type=int)
    ig.add_argument("--profile", default="default")
    ig.add_argument("--preview", action="store_true")
    ig.set_defaults(func=cmd_ingest)

    sub.add_parser("ui", help="launch the Streamlit review UI").set_defaults(func=cmd_ui)

    stp = sub.add_parser("status", help="per-client activity: volume, 7-day delta, trust")
    stp.add_argument("--profile", default="default", help="target profile")
    stp.set_defaults(func=cmd_status)

    bk = sub.add_parser("backup", help="create, list, or verify VACUUM INTO backups")
    bk.add_argument("--list", action="store_true", help="list existing backups")
    bk.add_argument("--verify", action="store_true", help="verify all existing backups")
    bk.set_defaults(func=cmd_backup)

    mt = sub.add_parser("maintain", help="run background maintenance (TTL cleanup, dedup, pruning)")
    mt.add_argument("--nightly", action="store_true", help="run nightly maintenance (default)")
    mt.add_argument("--weekly", action="store_true", help="run weekly maintenance & health report")
    mt.add_argument("--profile", default="default", help="target profile")
    mt.set_defaults(func=cmd_maintain)

    rv = sub.add_parser("reverse-prune", help="un-archive a pruned memory (autonomous reversal)")
    rv.add_argument("memory_id", help="memory ID to un-archive")
    rv.add_argument("--reason", required=True, help="why this prune is being reversed")
    rv.add_argument("--profile", default="default", help="target profile")
    rv.set_defaults(func=cmd_reverse_prune)

    edg = sub.add_parser("edges", help="manage knowledge graph edges")
    edg_sub = edg.add_subparsers(dest="action", required=True)
    el = edg_sub.add_parser("list", help="list graph edges")
    el.add_argument("--memory-id", help="filter edges by a specific memory ID")
    ea = edg_sub.add_parser("add", help="create a directed edge between memories")
    ea.add_argument("source_id", help="ID of the source memory")
    ea.add_argument("target_id", help="ID of the target memory")
    ea.add_argument("--relation", default="relates_to", help="relation type (default: relates_to)")
    er = edg_sub.add_parser("rm", help="delete a graph edge")
    er.add_argument("edge_id", help="ID of the edge to delete")
    edg.set_defaults(func=cmd_edges)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
