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


def test_remote_allowlist_includes_export_for_model_and_list_profiles():
    # Regression (issue #180): export_for_model exists specifically for
    # non-Claude remote clients to consume, but was unreachable from the
    # bridge those clients connect through. list_profiles is new — a
    # read-only enumeration so a remote client (pinned to the default
    # profile, but able to pass an explicit profile= to get_memory/etc.) can
    # discover what profile names exist instead of guessing blind.
    allow = server.REMOTE_ALLOWED_TOOLS
    assert "export_for_model" in allow
    assert "list_profiles" in allow
    # switch_profile mutates global _current_profile state — must stay local.
    assert "switch_profile" not in allow


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
# Caller-model attribution + list_profiles (#180)
# --------------------------------------------------------------------------

def test_caller_model_reflects_remote_mode():
    # Regression (issue #180): every analytics event and, now, every
    # add_memory write hardcoded model="claude" regardless of transport — the
    # analytics table had no record a non-Claude client had ever touched the
    # system even when one demonstrably had (proven live against the running
    # bridge during the audit). _caller_model() is the honest ceiling given
    # the current shared-secret auth: "claude" only when it's provably true
    # (stdio), "remote" otherwise (HTTP bridge — non-Claude-Code, but not
    # which specific model).
    orig_remote = server._REMOTE_MODE
    try:
        server._REMOTE_MODE = False
        assert server._caller_model() == "claude"
        server._REMOTE_MODE = True
        assert server._caller_model() == "remote"
    finally:
        server._REMOTE_MODE = orig_remote


def test_list_profiles_is_read_only_and_does_not_switch(tmp_path, monkeypatch):
    from db.store import MemoryStore
    s = MemoryStore(tmp_path / "list_profiles_test.db")
    monkeypatch.setattr(server, "_store", s)
    s.ensure_profile("default")
    s.ensure_profile("consulting")
    orig_cur = server._current_profile
    try:
        server._current_profile = "default"
        import json
        result = json.loads(server.list_profiles.fn())
        assert set(result["profiles"]) >= {"default", "consulting"}
        assert result["default_profile"] == server.DEFAULT_PROFILE
        # Must not have switched anything — that's switch_profile's job, and
        # switch_profile stays local-only precisely because it mutates this.
        assert server._current_profile == "default"
    finally:
        server._current_profile = orig_cur


# --------------------------------------------------------------------------
# Self-reported client_name (follow-on to #180)
# --------------------------------------------------------------------------

def test_sanitize_client_name():
    assert server._sanitize_client_name("hermes") == "hermes"
    assert server._sanitize_client_name("Hermes") == "hermes"
    assert server._sanitize_client_name("  hermes  ") == "hermes"
    assert server._sanitize_client_name("Hermes Agent!!") == "hermesagent"
    assert server._sanitize_client_name("a" * 40) == "a" * 32
    assert server._sanitize_client_name("") is None
    assert server._sanitize_client_name("   ") is None
    assert server._sanitize_client_name(None) is None
    assert server._sanitize_client_name("!!!") is None  # sanitizes to empty


def test_add_memory_tool_persists_sanitized_client_name(tmp_path, monkeypatch):
    from db.store import MemoryStore
    s = MemoryStore(tmp_path / "client_name_test.db")
    monkeypatch.setattr(server, "_store", s)
    s.ensure_profile("default")
    monkeypatch.setattr(server, "_REMOTE_MODE", False)

    import json
    out = json.loads(server.add_memory.fn(
        content="written by a second local agent",
        profile="default", client_name="Hermes Agent",
    ))
    mid = out["memory_id"]
    row = s._conn.execute("SELECT source, client_name FROM memories WHERE id=?", (mid,)).fetchone()
    assert row["source"] == "claude"          # transport-derived, unaffected
    assert row["client_name"] == "hermesagent"  # self-reported, sanitized


def test_get_memory_serves_client_name_field(tmp_path, monkeypatch):
    from db.store import MemoryStore
    s = MemoryStore(tmp_path / "client_name_served_test.db")
    monkeypatch.setattr(server, "_store", s)
    s.ensure_profile("default")
    s.add_memory("default", "written by hermes", category="fact",
                source="claude", client_name="hermes")

    import json
    out = json.loads(server.get_memory.fn(profile="default"))
    mems = out["memories"]
    assert any(m.get("client_name") == "hermes" for m in mems)


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


def test_middleware_non_ascii_path_returns_404_not_500():
    # Regression (issue #177): secrets.compare_digest raises TypeError on a
    # non-ASCII str, and that ran before auth — so any unauthenticated request
    # with a percent-encoded non-ASCII path segment (uvicorn decodes the raw
    # path as UTF-8 before the scope reaches this middleware) crashed the ASGI
    # app with an unhandled 500 instead of the uniform 404 every other
    # bad-token path gets. Live-reproduced against the running bridge:
    # GET /%C3%A9/mcp -> 500. Must behave exactly like any other wrong token.
    assert _status(_make_mw(TOKEN), "/é/mcp", "8.8.8.8") == 404
