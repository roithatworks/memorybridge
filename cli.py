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
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
