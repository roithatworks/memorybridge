"""Central configuration for MemoryBridge.

Everything a self-hosting user might tune lives here, loaded from (in order of
precedence): environment variables → an optional per-install config file in the
data dir → built-in generic defaults. A fresh install works with ZERO config;
the config file only exists to override defaults (e.g. define routing domains).

Config file: ``$MEMORYBRIDGE_DATA/memorybridge.yaml`` (or ``.yml`` / ``.json``).
YAML needs PyYAML; JSON always works. See ``memorybridge.example.yaml``.

This module is import-side-effect-free (no store, no network) so any component
— server, ingestion, UI, CLI — can import it cheaply.
"""
from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------- #
# Built-in defaults — a generic, single-profile setup with routing OFF.
# --------------------------------------------------------------------------- #
DEFAULT_CONFIG: dict[str, Any] = {
    # Hard ceiling on total tokens the store will serve/hold before pruning
    # pressure. Override with MEMORYBRIDGE_MAX_TOKENS or config `max_total_tokens`.
    "max_total_tokens": 50000,

    # Domain auto-routing. Empty `domains` => routing is OFF and every fact goes
    # to the run's profile ("default" unless --profile says otherwise). Users who
    # want their memory split by area define domains + keywords here.
    "routing": {
        "domains": {},           # {"work": ["deadline", "sprint", ...], ...}
        "default_keywords": [],  # facts matching these are forced to "default"
        "anchors": {},           # {"work": ["acme"]} — pull a default-signal fact into a domain
        "custom_router": None,   # optional path to a .py exporting route_profile(fact, base_profile)
    },

    # Ingestion noise filter: extra regex patterns (case-insensitive) whose
    # matching lines are dropped at extraction as ephemeral ops telemetry. The
    # built-in generic set is always applied; this only ADDS to it.
    "noise_patterns": [],

    # Project-name aliases: collapse variant spellings to one canonical id in
    # `project:` tags. Shape: {"variant spelling": "canonical"} (keys are matched
    # case-insensitively). Empty by default (unknown ids pass through unchanged).
    "project_aliases": {},
}


def data_dir() -> Path:
    """The mutable data directory (db, logs, inbox, config). Never the code dir."""
    return Path(os.environ.get("MEMORYBRIDGE_DATA", Path.home() / "memorybridge")).expanduser()


def _find_config_file() -> Path | None:
    d = data_dir()
    for name in ("memorybridge.yaml", "memorybridge.yml", "memorybridge.json"):
        p = d / name
        if p.exists():
            return p
    return None


def _read_config_file(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".json":
        return json.loads(text) or {}
    try:
        import yaml  # PyYAML
    except ImportError as e:  # pragma: no cover - depends on install
        raise RuntimeError(
            f"{path.name} is a YAML file but PyYAML is not installed. "
            f"Install it (`pip install pyyaml`) or use memorybridge.json instead."
        ) from e
    return yaml.safe_load(text) or {}


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


@lru_cache(maxsize=1)
def load() -> dict:
    """Return the merged config (defaults <- file <- env). Cached per process."""
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy of defaults
    path = _find_config_file()
    if path is not None:
        cfg = _deep_merge(cfg, _read_config_file(path))

    # Environment overrides for the handful of scalar knobs.
    env_tokens = os.environ.get("MEMORYBRIDGE_MAX_TOKENS")
    if env_tokens:
        try:
            cfg["max_total_tokens"] = int(env_tokens)
        except ValueError:
            pass
    return cfg


def reset_cache() -> None:
    """Clear the cached config — used by tests and after `mb init`."""
    load.cache_clear()


def max_total_tokens() -> int:
    return int(load().get("max_total_tokens", DEFAULT_CONFIG["max_total_tokens"]))


def routing() -> dict:
    return load().get("routing", DEFAULT_CONFIG["routing"])


def noise_patterns() -> list[str]:
    return list(load().get("noise_patterns", []))


def project_aliases() -> dict:
    """{variant_lower: canonical} project-name aliases from config."""
    raw = load().get("project_aliases", {}) or {}
    return {str(k).strip().lower(): v for k, v in raw.items()}
