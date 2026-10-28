"""ASGI middleware and HTTP transport lifecycle for MemoryBridge remote bridge."""

import os
import re
import sys
import time
import secrets
import threading
from typing import Optional

import config as _config
import workspace
import server as _srv

# =============================================================================
# REMOTE BRIDGE (HTTP transport) — ChatGPT / Perplexity / Gemini CLI
# =============================================================================
# Remote clients get read + add only. A prompt-injected or confused remote
# model must not be able to destroy or overwrite memories; destructive and
# subprocess-spawning tools stay stdio/Claude-local.
#   - edit_memory: removed — it overwrites arbitrary memory content by id
#     (destruction-equivalent for a confused/hostile remote model).
#   - add_memories: removed — it is a batch wrapper over add_memory with no
#     added remote value and the same side effects.
#   - add_memory: kept (remote clients need to write), but its auto-prune
#     delete path is disabled for remote writes via _REMOTE_MODE — candidates
#     are routed to the review queue instead of deleted (issue #37).
REMOTE_ALLOWED_TOOLS = {
    "get_memory", "search_memory", "reflect", "add_memory",
    "list_projects", "export_passport",
    # export_for_model (#180): the ChatGPT/Gemini/Ollama-formatted exports
    # exist specifically for non-Claude remote clients to consume, but were
    # never reachable from the bridge those clients connect through — only
    # Claude stdio and the Streamlit UI could call it, so using it remotely
    # meant generating a blob locally and pasting it in by hand, the exact
    # manual workflow the bridge exists to replace. Read-only, no new risk.
    "export_for_model",
    # list_profiles (#180): switch_profile stays excluded — it mutates
    # _current_profile, global state a remote model shouldn't control. But
    # get_memory/search_memory/etc. all accept an explicit profile= argument
    # that IS honored remotely (remote mode only pins the *default* when one
    # isn't given), so a remote client was access-unblocked but
    # discovery-blind: it could reach a non-default profile if it happened to
    # guess the name, with no way to learn what names exist. This is a
    # read-only enumeration of names only — no identity/content/memory data.
    "list_profiles",
    # F1/F4 workspace tools — ws_write is guarded by _REMOTE_MODE (requires overwrite=True
    # and enforces workspace_write_allowed allowlist prefixes).
    "ws_status", "ws_list", "ws_read", "ws_search", "ws_write",
    # Knowledge Graph read-only exploration tools (v5.2):
    "traverse_memory_graph", "get_memory_edges",
}


def _gate_tools_for_remote() -> list[str]:
    """Remove non-allowlisted tools from the MCP server. Returns removed names.

    Fails CLOSED: if the tool set cannot be enumerated (or comes back empty),
    the process exits rather than risk serving destructive tools remotely.
    """
    mcp = _srv.mcp
    try:
        import asyncio
        if hasattr(mcp, "list_tools"):
            tools = asyncio.run(mcp.list_tools())
            tool_names = [t.name if hasattr(t, "name") else str(t) for t in tools]
        elif hasattr(mcp, "get_tools"):
            tool_names = list(asyncio.run(mcp.get_tools()).keys())
        else:
            raise RuntimeError("Unknown FastMCP tool enumeration method")
    except Exception as e:
        # Do NOT fall back to a private FastMCP attribute — a rename there
        # would silently yield an empty set and serve every tool (fail-open).
        print(f"[memorybridge] FATAL: could not enumerate tools for remote "
              f"gating ({e}). Refusing to serve HTTP.", file=sys.stderr)
        os._exit(1)

    if not tool_names:
        print("[memorybridge] FATAL: tool enumeration returned empty; refusing "
              "to serve HTTP (fail-closed).", file=sys.stderr)
        os._exit(1)

    removed = []
    for name in tool_names:
        if name not in REMOTE_ALLOWED_TOOLS:
            try:
                if hasattr(mcp, "local_provider") and hasattr(mcp.local_provider, "remove_tool"):
                    mcp.local_provider.remove_tool(name)
                else:
                    mcp.remove_tool(name)
                removed.append(name)
            except Exception as e:
                print(f"[memorybridge] FATAL: could not remove tool {name}: {e}",
                      file=sys.stderr)
                os._exit(1)  # never serve destructive tools remotely by accident
    return removed


async def _send_plain(send, status: int, text: str) -> None:
    """Emit a minimal ASGI plain-text response."""
    body = text.encode()
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [
            (b"content-type", b"text/plain; charset=utf-8"),
            (b"content-length", str(len(body)).encode()),
        ],
    })
    await send({"type": "http.response.body", "body": body})


class _RateLimitAuthMiddleware:
    """ASGI middleware guarding the HTTP bridge (issues #69, #177, and Milestone F2).

    - Per-client-IP fixed-window rate limiting -> 429 when exceeded, so tokens
      cannot be brute-forced without backoff.
    - Constant-time comparison of credentials (`secrets.compare_digest`); a mismatch
      returns a uniform 404 with no timing signal and never reaches the MCP app.
    - Dual auth support (Milestone F2):
      1. Standard `Authorization: Bearer <token>` header (preferred, keeps secrets out
        of URLs, browser history, and proxy logs). Transparently rewrites `/mcp` to
        the registered FastMCP route.
      2. Capability-URL path (`/{token}/mcp`) for clients that cannot send custom
         headers (e.g. ChatGPT / Perplexity no-auth modes).
    - Behind the Cloudflare tunnel the real client IP arrives in CF-Connecting-IP /
      X-Forwarded-For, so those are honored before the transport peer address.
    """

    def __init__(self, app, expected_token: str, limit: int, window: int, token_profiles: dict[str, str] | None = None):
        self.app = app
        self.expected_token = expected_token
        self.limit = limit
        self.window = window
        self.token_profiles = token_profiles or {}
        self._hits: dict[str, list] = {}
        self._lock = threading.Lock()

    def _client_ip(self, scope) -> str:
        headers = {k.decode("latin1").lower(): v.decode("latin1")
                   for k, v in scope.get("headers", [])}
        for h in ("cf-connecting-ip", "x-forwarded-for"):
            val = headers.get(h, "").strip()
            if val:
                first = val.split(",")[0].strip()
                m = re.match(r"^([0-9a-fA-F:.]+)", first)
                if m:
                    candidate = m.group(1)[:45]
                    try:
                        import ipaddress
                        return str(ipaddress.ip_address(candidate))
                    except ValueError:
                        return candidate
        client = scope.get("client")
        return client[0] if client else "unknown"

    def _rate_ok(self, ip: str) -> bool:
        now = time.monotonic()
        with self._lock:
            window_start, count = self._hits.get(ip, (now, 0))
            if now - window_start >= self.window:
                window_start, count = now, 0
            count += 1
            self._hits[ip] = [window_start, count]
            if len(self._hits) > 4096:  # bound memory; drop stale windows
                stale = [k for k, (s, _) in self._hits.items()
                         if now - s >= self.window]
                for k in stale:
                    self._hits.pop(k, None)
                # Hard cap fallback if attacker floods non-stale windows
                while len(self._hits) > 4096:
                    self._hits.pop(next(iter(self._hits)), None)
            return count <= self.limit

    def _authenticate(self, scope) -> tuple[bool, bool, Optional[str], Optional[str]]:
        """Authenticate request via Bearer header (F2) or capability URL path.

        Returns (is_authenticated, was_bearer_token, matched_token, bound_profile).
        Uses constant-time secrets.compare_digest for all token checks.
        """
        # 1. Bearer header authentication (F2)
        headers = {k.decode("latin1").lower(): v.decode("latin1", errors="replace")
                   for k, v in scope.get("headers", [])}
        auth_val = headers.get("authorization", "").strip()
        is_bearer = False
        cand_token = ""

        if auth_val.lower().startswith("bearer "):
            is_bearer = True
            cand_token = auth_val[7:].strip()
        else:
            # 2. Capability URL path fallback: /{token}/mcp
            segment = scope.get("path", "").lstrip("/").split("/", 1)[0]
            cand_token = segment

        if not cand_token or not cand_token.isascii():
            return False, False, None, None

        # Check primary expected token
        if self.expected_token and secrets.compare_digest(cand_token, self.expected_token):
            bound = self.token_profiles.get(self.expected_token)
            return True, is_bearer, self.expected_token, bound

        # Check scoped token_profiles mapping
        for tok, prof in self.token_profiles.items():
            if tok.isascii() and secrets.compare_digest(cand_token, tok):
                return True, is_bearer, tok, prof

        return False, False, None, None

    def _token_ok(self, scope) -> bool:
        return self._authenticate(scope)[0]

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        # This is a pre-auth boundary parsing attacker-controlled input (path,
        # headers) from the open internet. Any unexpected exception here must
        # not become an unauthenticated 500 (issue #177) — that both discloses
        # more than a uniform 404 and, since it's logged, is a free remote log
        # amplification vector. Fail closed to 404, log the exception type only.
        try:
            rate_ok = self._rate_ok(self._client_ip(scope))
            auth_ok, is_bearer, matched_tok, bound_prof = (
                self._authenticate(scope) if rate_ok else (True, False, None, None)
            )
        except Exception as e:
            print(f"[memorybridge] auth middleware error (denying request): "
                  f"{type(e).__name__}", file=sys.stderr)
            await _send_plain(send, 404, "not found")
            return
        if not rate_ok:
            await _send_plain(send, 429, "rate limit exceeded")
            return
        if not auth_ok:
            await _send_plain(send, 404, "not found")
            return

        # If authenticated via Bearer token at /mcp root, rewrite path to FastMCP's
        # internal registered capability route so routing succeeds transparently.
        if is_bearer:
            path = scope.get("path", "")
            if path == "/mcp" or path.startswith("/mcp/"):
                new_path = f"/{self.expected_token}{path}"
                scope = dict(scope)
                scope["path"] = new_path
                if "raw_path" in scope:
                    scope["raw_path"] = new_path.encode("utf-8")
        elif matched_tok and matched_tok != self.expected_token:
            path = scope.get("path", "")
            prefix = f"/{matched_tok}"
            if path.startswith(prefix):
                new_path = f"/{self.expected_token}" + path[len(prefix):]
                scope = dict(scope)
                scope["path"] = new_path
                if "raw_path" in scope:
                    scope["raw_path"] = new_path.encode("utf-8")

        token_reset = _srv._BOUND_PROFILE.set(bound_prof)
        try:
            await self.app(scope, receive, send)
        finally:
            _srv._BOUND_PROFILE.reset(token_reset)


def _run_http() -> None:
    """Serve over streamable HTTP for remote MCP clients.

    - Bound to 127.0.0.1; exposure to the internet happens only via the
      Cloudflare tunnel in front of it.
    - The MCP path embeds a secret (capability URL): ChatGPT's no-auth
      connector mode and Perplexity's open mode can both use it, and the
      token never appears in server logs ChatGPT/Perplexity side.
    - Parent watchdog is NOT started: under launchd our PPID is legitimately
      1, and the watchdog would kill the server 5s after boot.
    """
    # Mark remote mode so the auto-pruner's delete path is disabled for
    # writes that arrive over this bridge (see add_memory / issue #37).
    _srv._REMOTE_MODE = True
    workspace.set_remote_mode(True)  # F4: ws_write now requires overwrite=True

    token = os.environ.get("MEMORYBRIDGE_TOKEN", "").strip()
    if len(token) < 32:
        print("[memorybridge] FATAL: MEMORYBRIDGE_TOKEN missing or under 32 chars "
              "(set it in DATA_DIR/.env). Refusing to serve HTTP without a secret.",
              file=sys.stderr)
        os._exit(1)

    removed = _gate_tools_for_remote()
    port = int(os.environ.get("MEMORYBRIDGE_PORT", "8484"))

    # The secret token is embedded in the request path (capability URL). The
    # uvicorn access logger records the full path on every request, which would
    # write the secret to stdout/stderr and any tunnel/proxy log. Disable it so
    # the token never lands in a log. Never print even a prefix of the token.
    import logging
    logging.getLogger("uvicorn.access").disabled = True

    rate_limit = int(os.environ.get("MEMORYBRIDGE_RATE_LIMIT", "120"))
    rate_window = int(os.environ.get("MEMORYBRIDGE_RATE_WINDOW", "60"))
    token_profiles = _config.token_profiles()

    # Wrap the FastMCP ASGI app with rate-limiting + constant-time token auth
    # (issue #69). Only take this path if we can build the app at the SAME
    # capability path clients expect; otherwise fall back to mcp.run so a
    # FastMCP API change can never leave the bridge serving on the wrong path.
    mcp_path = f"/{token}/mcp"
    mcp = _srv.mcp
    # FastMCP 2.x exposes http_app(path=...); older builds used streamable_http_app.
    app_factory = getattr(mcp, "http_app", None) or getattr(mcp, "streamable_http_app", None)
    app = None
    if callable(app_factory):
        try:
            app = app_factory(path=mcp_path)
        except TypeError:
            app = None  # can't pin the path safely -> fall back

    if app is not None:
        import uvicorn
        wrapped = _RateLimitAuthMiddleware(app, token, rate_limit, rate_window, token_profiles=token_profiles)
        print(f"[memorybridge] HTTP bridge on 127.0.0.1:{port} "
              f"path=/<redacted>/mcp | tools gated: removed {len(removed)} "
              f"({', '.join(sorted(removed))}) | rate limit {rate_limit}/{rate_window}s per IP",
              file=sys.stderr)
        # lifespan="on": the streamable-HTTP session manager is started by the
        # Starlette lifespan; the ASGI wrapper forwards lifespan scopes, and
        # forcing it on (vs "auto") makes a lifespan failure loud instead of
        # degrading every request to a 500.
        uvicorn.run(wrapped, host="127.0.0.1", port=port,
                    access_log=False, log_level="warning", lifespan="on")
    else:
        print(f"[memorybridge] WARNING: could not attach rate-limiting middleware "
              f"(FastMCP app factory unavailable); serving without per-IP rate "
              f"limiting. HTTP bridge on 127.0.0.1:{port} path=/<redacted>/mcp | "
              f"tools gated: removed {len(removed)} ({', '.join(sorted(removed))})",
              file=sys.stderr)
        mcp.run(transport="http", host="127.0.0.1", port=port, path=mcp_path)
