"""Domain routing: assign each extracted fact to a memory profile.

Routing is **config-driven** (see ``memorybridge.example.yaml``). A user defines
domains + keyword sets; each fact is scored against them and written to the
best-matching domain, defaulting to "default" (the cross-cutting bucket for
identity / voice / tooling that applies everywhere) whenever the signal is weak.
Misrouting to "default" is recoverable; misrouting into the wrong domain is
silent damage — so the bias is intentionally toward "default".

If no domains are configured, routing is OFF: every fact goes to the run's
profile. Power users who need bespoke logic can point ``custom_router`` at a
Python module exporting ``route_profile(fact, base_profile)``.

Signal priority (generic engine):
  1. strong domain signal (score >= 2, e.g. a project-name hit or two keywords)
  2. cross-cutting default_keywords -> "default" (or an anchor domain)
  3. weak domain signal (score >= 1)
  4. fall back to the run profile
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

try:
    from config import routing as _config_routing
except Exception:  # pragma: no cover - config always importable in practice
    def _config_routing() -> dict:
        return {"domains": {}, "default_keywords": [], "anchors": {}, "custom_router": None}


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").lower())


_CUSTOM_CACHE: dict[str, object] = {}


def _load_custom_router(path: str):
    """Import a user-provided router module exporting route_profile(fact, base)."""
    if path in _CUSTOM_CACHE:
        return _CUSTOM_CACHE[path]
    p = Path(path).expanduser()
    if not p.exists():
        raise FileNotFoundError(f"custom_router not found: {p}")
    spec = importlib.util.spec_from_file_location("_mb_custom_router", str(p))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_mb_custom_router"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    if not hasattr(mod, "route_profile"):
        raise AttributeError(f"custom_router {p} must define route_profile(fact, base_profile)")
    _CUSTOM_CACHE[path] = mod
    return mod


def route_profile(fact: dict, base_profile: str = "default", routing: dict | None = None) -> str:
    """Return the profile this fact should be written to.

    *base_profile* is the run-level profile (what --profile was set to); it is
    the fallback when no domain signal is found. *routing* overrides the loaded
    config (used by tests).
    """
    cfg = routing if routing is not None else _config_routing()

    custom = cfg.get("custom_router")
    if custom:
        return _load_custom_router(custom).route_profile(fact, base_profile)

    domains: dict = cfg.get("domains") or {}
    if not domains:
        # Routing is off — honor the run profile.
        return base_profile

    text = _norm(fact.get("fact", ""))
    project = _norm(fact.get("project", ""))
    haystack = f"{project} {text}"

    # 1. Score each domain by keyword hits (project text weighted 2x).
    best, best_score = None, 0
    for profile, kws in domains.items():
        score = 0
        for k in kws:
            k = _norm(k)
            if k and k in project:
                score += 2
            elif k and k in text:
                score += 1
        if score > best_score:
            best, best_score = profile, score

    default_keywords = [_norm(k) for k in (cfg.get("default_keywords") or [])]
    default_signal = any(k in haystack for k in default_keywords)

    anchors: dict = cfg.get("anchors") or {}
    anchor_domain = None
    for dom, kws in anchors.items():
        if dom in domains and any(_norm(k) in haystack for k in kws):
            anchor_domain = dom
            break

    # 2. Strong domain signal wins even if cross-cutting words are also present.
    if best and best_score >= 2:
        return best

    # 3. Cross-cutting identity/voice/tooling -> default, unless an anchor pulls
    #    it into a specific domain.
    if default_signal:
        return anchor_domain or "default"

    # 4. Weak-but-present domain signal routes to its domain.
    if best and best_score >= 1:
        return best

    # 5. No signal -> run profile (or default).
    return base_profile if base_profile in domains or base_profile == "default" else "default"
