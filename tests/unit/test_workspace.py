"""Tests for the ws_* workspace tools (F1) and the remote write guard (F4).

Covers path-traversal blocking, write-allowlist enforcement, remote-mode
overwrite requirements, and the workspace root's own error handling.
"""
import os

os.environ.setdefault("MEMORYBRIDGE_NO_EMBED", "1")
os.environ.setdefault("MEMORYBRIDGE_DATA", "/tmp/mb_workspacetest_data")

import pytest  # noqa: E402

import config  # noqa: E402
import workspace  # noqa: E402


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolated_workspace_root(tmp_path, monkeypatch):
    """Point WORKSPACE_ROOT at a fresh tmp_path dir for every test."""
    root = tmp_path / "workspace"
    root.mkdir()
    monkeypatch.setattr(config, "workspace_root", lambda: root)
    monkeypatch.setattr(workspace, "_REMOTE_MODE", False)
    return root


@pytest.fixture
def allow_notes(monkeypatch):
    """Allow writes under notes/ — mirrors WORKSPACE_WRITE_ALLOWED config."""
    cfg = dict(config.DEFAULT_CONFIG)
    cfg["workspace_write_allowed"] = ["notes/"]
    monkeypatch.setattr(config, "load_config", lambda: cfg)
    return cfg


# --------------------------------------------------------------------------
# Happy path — status / list / read / write / search
# --------------------------------------------------------------------------

def test_ws_status_reports_root_and_file_count(_isolated_workspace_root):
    (_isolated_workspace_root / "a.txt").write_text("hello")
    result = workspace.ws_status()
    assert result["file_count"] == 1
    assert result["root"] == str(_isolated_workspace_root)


def test_ws_list_happy_path_returns_entries(_isolated_workspace_root):
    (_isolated_workspace_root / "a.txt").write_text("hello")
    (_isolated_workspace_root / "b.txt").write_text("world")
    result = workspace.ws_list()
    assert set(result["entries"]) == {"a.txt", "b.txt"}


def test_ws_list_recursive(_isolated_workspace_root):
    sub = _isolated_workspace_root / "notes"
    sub.mkdir()
    (sub / "c.txt").write_text("nested")
    result = workspace.ws_list(recursive=True)
    assert any("c.txt" in entry for entry in result["entries"])


def test_ws_read_happy_path_returns_content(_isolated_workspace_root):
    (_isolated_workspace_root / "a.txt").write_text("hello world")
    result = workspace.ws_read("a.txt")
    assert result["content"] == "hello world"


def test_ws_write_happy_path_creates_file(_isolated_workspace_root, allow_notes):
    result = workspace.ws_write("notes/new.txt", "fresh content")
    assert result["bytes_written"] == len("fresh content".encode("utf-8"))
    assert (_isolated_workspace_root / "notes" / "new.txt").read_text() == "fresh content"


def test_ws_write_overwrite_existing_file(_isolated_workspace_root, allow_notes):
    target = _isolated_workspace_root / "notes" / "existing.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("old")
    result = workspace.ws_write("notes/existing.txt", "new", overwrite=True)
    assert "error" not in result
    assert target.read_text() == "new"


def test_ws_search_finds_matching_content(_isolated_workspace_root):
    (_isolated_workspace_root / "a.txt").write_text("the quick brown fox")
    (_isolated_workspace_root / "b.txt").write_text("nothing relevant here")
    result = workspace.ws_search("quick brown")
    assert result["matches"] == ["a.txt"]


def test_ws_search_no_matches_returns_empty(_isolated_workspace_root):
    (_isolated_workspace_root / "a.txt").write_text("irrelevant")
    result = workspace.ws_search("nonexistent phrase")
    assert result["matches"] == []


# --------------------------------------------------------------------------
# Path traversal guard
# --------------------------------------------------------------------------

def test_path_traversal_blocked_on_read(_isolated_workspace_root):
    with pytest.raises(workspace.WorkspaceError):
        workspace.ws_read("../../etc/passwd")


def test_path_traversal_blocked_on_write(_isolated_workspace_root, allow_notes):
    with pytest.raises(workspace.WorkspaceError):
        workspace.ws_write("notes/../../escaped.txt", "pwned")


# --------------------------------------------------------------------------
# Write allowlist / remote guard (F4)
# --------------------------------------------------------------------------

def test_write_to_disallowed_folder_blocked(_isolated_workspace_root, allow_notes):
    result = workspace.ws_write("secrets/creds.txt", "nope")
    assert "error" in result
    assert not (_isolated_workspace_root / "secrets" / "creds.txt").exists()


def test_remote_write_without_overwrite_blocked(_isolated_workspace_root, allow_notes, monkeypatch):
    monkeypatch.setattr(workspace, "_REMOTE_MODE", True)
    result = workspace.ws_write("notes/new.txt", "content")
    assert "error" in result
    assert not (_isolated_workspace_root / "notes" / "new.txt").exists()


def test_remote_write_with_overwrite_true_allowed(_isolated_workspace_root, allow_notes, monkeypatch):
    monkeypatch.setattr(workspace, "_REMOTE_MODE", True)
    result = workspace.ws_write("notes/new.txt", "content", overwrite=True)
    assert "error" not in result
    assert (_isolated_workspace_root / "notes" / "new.txt").read_text() == "content"


# --------------------------------------------------------------------------
# Misconfiguration / non-text input
# --------------------------------------------------------------------------

def test_workspace_root_not_found(tmp_path, monkeypatch):
    missing = tmp_path / "does_not_exist"
    monkeypatch.setattr(config, "workspace_root", lambda: missing)
    result = workspace.ws_status()
    assert "error" in result


def test_ws_read_binary_file_rejected(_isolated_workspace_root):
    binary_path = _isolated_workspace_root / "image.png"
    binary_path.write_bytes(bytes([0x89, 0x50, 0x4E, 0x47, 0x00, 0xFF, 0xD8]))
    result = workspace.ws_read("image.png")
    assert "error" in result
