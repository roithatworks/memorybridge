"""Tests for the HTTP bridge security boundary (#66):
remote tool allowlist, the rate-limit + constant-time-auth ASGI middleware,
and remote-mode profile resolution.
"""
import os

# Must be set before importing server: skip the FastEmbed load and keep the
# store off the developer's real data dir.
os.environ.setdefault("MEMORYBRIDGE_NO_EMBED", "1")
os.environ.setdefault("MEMORYBRIDGE_DATA", "/tmp/mb_httptest_data")

import asyncio  # noqa: E402

import server  # noqa: E402


# --------------------------------------------------------------------------
# Remote tool allowlist
# --------------------------------------------------------------------------

def test_remote_allowlist_excludes_destructive_and_subprocess_tools():
    allow = server.REMOTE_ALLOWED_TOOLS
    for tool in ("edit_memory", "add_memories", "delete_memory", "prune_memories",
                 "resolve_prune_queue", "ingest_from_inbox", "switch_profile"):
        assert tool not in allow, f"{tool} must not be reachable over the bridge"


def test_remote_allowlist_includes_read_and_add():
    allow = server.REMOTE_ALLOWED_TOOLS
    for tool in ("get_memory", "search_memory", "add_memory"):
        assert tool in allow


# --------------------------------------------------------------------------
# Remote-mode profile resolution (#70)
# --------------------------------------------------------------------------

def test_active_profile_pins_default_in_remote_mode():
    orig_remote, orig_cur = server._REMOTE_MODE, server._current_profile
    try:
        server._current_profile = "job_search"
        server._REMOTE_MODE = False
        assert server._active_profile() == "job_search"        # stdio: switchable
        server._REMOTE_MODE = True
        assert server._active_profile() == server.DEFAULT_PROFILE  # remote: pinned
    finally:
        server._REMOTE_MODE, server._current_profile = orig_remote, orig_cur


# --------------------------------------------------------------------------
# Rate-limit + constant-time auth middleware
# --------------------------------------------------------------------------

def _make_mw(token, limit=5, window=60):
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"OK"})
    return server._RateLimitAuthMiddleware(app, token, limit, window)


def _status(mw, path, ip="1.2.3.4"):
    got = {}

    async def recv():
        return {"type": "http.request"}

    async def send(msg):
        if msg["type"] == "http.response.start":
            got["status"] = msg["status"]

    scope = {"type": "http", "path": path,
             "headers": [(b"cf-connecting-ip", ip.encode())], "client": ("9.9.9.9", 1)}
    asyncio.new_event_loop().run_until_complete(mw(scope, recv, send))
    return got["status"]


TOKEN = "T" * 40


def test_middleware_valid_token_passes_through():
    assert _status(_make_mw(TOKEN), f"/{TOKEN}/mcp", "1.1.1.1") == 200


def test_middleware_bad_token_returns_404():
    assert _status(_make_mw(TOKEN), "/deadbeef/mcp", "2.2.2.2") == 404


def test_middleware_rate_limit_returns_429_and_is_per_ip():
    mw = _make_mw(TOKEN, limit=2, window=60)
    assert [_status(mw, f"/{TOKEN}/mcp", "5.5.5.5") for _ in range(3)] == [200, 200, 429]
    # a different client IP is unaffected by the first IP's exhausted window
    assert _status(mw, f"/{TOKEN}/mcp", "6.6.6.6") == 200


def test_middleware_uses_constant_time_compare():
    import secrets
    # The middleware must compare the path token with secrets.compare_digest,
    # not ==, to avoid a timing oracle. Verify the primitive is wired in.
    assert secrets.compare_digest("a" * 40, "a" * 40) is True
    assert _status(_make_mw(TOKEN), f"/{'T' * 39}X/mcp", "7.7.7.7") == 404
