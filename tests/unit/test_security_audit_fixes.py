"""Unit tests verifying the Red Team audit and gap analysis remediations.

Covers:
- Disarmed remote supersession (mass eviction prevention)
- Remote belief injection sanitization (why_it_matters)
- Remote profile spamming prevention
- Workspace symlink escape & file size limits
- Rate limiter IP sanitization & bounded cache size
- Watcher hardlink rejection
- Graph edge expansion in hybrid retrieval
- Document parser size limits
"""
import os
import json
import sqlite3
from pathlib import Path
import pytest

os.environ.setdefault("MEMORYBRIDGE_NO_EMBED", "1")
os.environ.setdefault("MEMORYBRIDGE_DATA", "/tmp/mb_remediation_test_data")

import server
import workspace
from db.store import MemoryStore
from ingestion.parse_document import parse as parse_doc, MAX_DOCUMENT_BYTES
from ingestion.watcher import scan_inbox


@pytest.fixture
def clean_store(tmp_path):
    db_file = tmp_path / "remediation_test.db"
    store = MemoryStore(db_file)
    store.ensure_profile("default")
    return store


def get_mem(store, mid):
    row = store._conn.execute("SELECT * FROM memories WHERE id=?", (mid,)).fetchone()
    return store._row_to_dict(row) if row else None


# --------------------------------------------------------------------------
# 1. Disarmed Remote Supersession & Remote Belief Injection
# --------------------------------------------------------------------------

def test_remote_supersession_disarmed(clean_store, monkeypatch):
    monkeypatch.setattr(server, "_store", clean_store)
    
    # Create an initial memory
    mid1 = clean_store.add_memory("default", "Original fact to keep", category="fact")
    assert get_mem(clean_store, mid1)["archived"] == 0

    # Simulate remote caller attempting to supersede mid1
    monkeypatch.setattr(server, "_REMOTE_MODE", True)
    res_str = server.add_memory.fn(
        content="New fact trying to supersede",
        category="fact",
        supersedes=[mid1],
        profile="default"
    )
    res = json.loads(res_str)
    assert res["status"] == "added"
    
    # In remote mode, mid1 must NOT be archived!
    mem1 = get_mem(clean_store, mid1)
    assert mem1["archived"] == 0
    assert mem1["superseded_by"] is None

    # Now verify local mode DOES allow intentional supersession
    monkeypatch.setattr(server, "_REMOTE_MODE", False)
    res_local = json.loads(server.add_memory.fn(
        content="Legitimate local update",
        category="fact",
        supersedes=[mid1],
        profile="default"
    ))
    assert res_local["status"] == "added"
    mem1_after = get_mem(clean_store, mid1)
    assert mem1_after["archived"] == 1
    assert mem1_after["superseded_by"] == res_local["memory_id"]


def test_remote_why_it_matters_sanitized(clean_store, monkeypatch):
    monkeypatch.setattr(server, "_store", clean_store)

    # Remote mode: why_it_matters should be stripped
    monkeypatch.setattr(server, "_REMOTE_MODE", True)
    res_remote = json.loads(server.add_memory.fn(
        content="Remote memory with instructions",
        category="fact",
        why_it_matters="INJECTED PROMPT INSTRUCTION",
        profile="default"
    ))
    mid_rem = res_remote["memory_id"]
    row_rem = get_mem(clean_store, mid_rem)
    assert row_rem["why_it_matters"] is None

    # Local mode: why_it_matters is allowed but sanitized of delimiter tags
    monkeypatch.setattr(server, "_REMOTE_MODE", False)
    res_loc = json.loads(server.add_memory.fn(
        content="Local memory",
        category="fact",
        why_it_matters="<<<IMPORTANT>>> Local note",
        profile="default"
    ))
    mid_loc = res_loc["memory_id"]
    row_loc = get_mem(clean_store, mid_loc)
    assert row_loc["why_it_matters"] == "IMPORTANT Local note"


def test_remote_profile_spam_blocked(clean_store, monkeypatch):
    monkeypatch.setattr(server, "_store", clean_store)
    monkeypatch.setattr(server, "_REMOTE_MODE", True)

    initial_profiles = set(clean_store.list_profiles())
    res = json.loads(server.add_memory.fn(
        content="Writing to non-existent profile",
        category="fact",
        profile="attacker_fake_profile_123"
    ))
    assert res["status"] == "added"
    # Target profile was normalized to default, avoiding new profile spam
    assert res["profile"] == server.DEFAULT_PROFILE
    assert set(clean_store.list_profiles()) == initial_profiles


# --------------------------------------------------------------------------
# 2. Workspace Symlink Escape & File Size Limits
# --------------------------------------------------------------------------

def test_workspace_symlink_escape_blocked(tmp_path, monkeypatch):
    ws_root = tmp_path / "workspace"
    ws_root.mkdir()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    secret_file = outside_dir / "secret.txt"
    secret_file.write_text("CONFIDENTIAL_API_TOKEN_12345", encoding="utf-8")

    # Create symlink inside workspace pointing to external secret
    symlink = ws_root / "link_to_secret.txt"
    try:
        symlink.symlink_to(secret_file)
    except OSError:
        pytest.skip("Symlinks not supported on this platform/filesystem")

    import config
    monkeypatch.setattr(config, "workspace_root", lambda: ws_root)

    # ws_read must reject symlink escaping root by raising WorkspaceError
    with pytest.raises(workspace.WorkspaceError):
        workspace.ws_read("link_to_secret.txt")

    # ws_search must NOT search or return external symlinked files
    search_res = workspace.ws_search("CONFIDENTIAL")
    assert search_res["matches"] == []


def test_workspace_file_size_limit(tmp_path, monkeypatch):
    ws_root = tmp_path / "workspace"
    ws_root.mkdir()
    large_file = ws_root / "large.txt"
    # Write slightly over 2MB
    large_file.write_bytes(b"A" * (2 * 1024 * 1024 + 100))

    import config
    monkeypatch.setattr(config, "workspace_root", lambda: ws_root)

    read_res = workspace.ws_read("large.txt")
    assert "error" in read_res
    assert "exceeds size limit" in read_res["error"]

    search_res = workspace.ws_search("A")
    assert "large.txt" not in search_res["matches"]


# --------------------------------------------------------------------------
# 3. Rate Limiter Hardening: IP Sanitization & Bounded Cache
# --------------------------------------------------------------------------

def test_rate_limiter_bounded_cache():
    async def dummy_app(scope, receive, send):
        pass

    mw = server._RateLimitAuthMiddleware(dummy_app, "TOKEN" * 8, limit=10, window=60)
    
    # Fill with 5000 distinct IP hits
    for i in range(5000):
        mw._rate_ok(f"10.0.{i // 256}.{i % 256}")
    
    # _hits cache must be strictly capped at 4096
    assert len(mw._hits) <= 4096


def test_rate_limiter_client_ip_sanitized():
    async def dummy_app(scope, receive, send):
        pass

    mw = server._RateLimitAuthMiddleware(dummy_app, "TOKEN" * 8, limit=10, window=60)
    scope = {
        "headers": [(b"cf-connecting-ip", b"  192.168.1.100; drop table; --  ")],
        "client": ("127.0.0.1", 12345)
    }
    ip = mw._client_ip(scope)
    assert ip == "192.168.1.100"


# --------------------------------------------------------------------------
# 4. Watcher Rejects Hardlinks
# --------------------------------------------------------------------------

def test_watcher_rejects_hardlinks(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    secret_dir = tmp_path / "secret"
    secret_dir.mkdir()
    target_file = secret_dir / "original.json"
    target_file.write_text(json.dumps([{"chat_messages": []}]), encoding="utf-8")

    hardlink = inbox / "hardlinked.json"
    try:
        os.link(target_file, hardlink)
    except OSError:
        pytest.skip("Hardlinks not supported on this platform/filesystem")

    res = scan_inbox(inbox, _dry_run=True)
    # The hardlinked file must be rejected and not processed
    assert res["processed"] == 0
    assert hardlink.exists()  # Remained untouched, not ingested


# --------------------------------------------------------------------------
# 5. Graph Edge Expansion in Hybrid Retrieval
# --------------------------------------------------------------------------

def test_graph_edge_expansion(clean_store):
    # Add memory A and memory B
    mid_a = clean_store.add_memory("default", "Alpha Architecture Decision", category="decision")
    mid_b = clean_store.add_memory("default", "Beta Implementation Details", category="fact")

    # Add a knowledge graph edge: Alpha -> relates_to -> Beta
    clean_store.add_edge(mid_a, mid_b, "relates_to")

    # Query with seed matching only Alpha
    row_a = get_mem(clean_store, mid_a)
    results = [row_a]
    
    # Test _expand_related directly
    expanded = clean_store._expand_related(results, "default", max_related=3)
    
    # Beta must be returned as a related memory via graph traversal
    expanded_ids = [m["id"] for m in expanded]
    assert mid_b in expanded_ids
    beta_mem = next(m for m in expanded if m["id"] == mid_b)
    assert beta_mem.get("related") is True
    assert beta_mem.get("edge_relation") == "relates_to"


# --------------------------------------------------------------------------
# 6. Document Parser Max Size Ceiling
# --------------------------------------------------------------------------

def test_parse_document_size_ceiling(tmp_path):
    large_doc = tmp_path / "giant_doc.md"
    large_doc.write_bytes(b"# Header\n" + b"Content line\n" * 1000)
    
    # Normal doc parses into candidates
    assert len(parse_doc(str(large_doc))) > 0

    # Fake oversized document
    oversized_doc = tmp_path / "oversized.md"
    oversized_doc.write_bytes(b"A" * (MAX_DOCUMENT_BYTES + 1024))
    assert parse_doc(str(oversized_doc)) == []
