"""Tests for the central config loader."""
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest

import config


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORYBRIDGE_DATA", str(tmp_path))
    monkeypatch.delenv("MEMORYBRIDGE_MAX_TOKENS", raising=False)
    monkeypatch.delenv("MEMORYBRIDGE_PROFILE", raising=False)
    config.reset_cache()
    yield
    config.reset_cache()


def test_defaults_when_no_file(tmp_path):
    assert config.max_total_tokens() == 50000
    assert config.routing()["domains"] == {}          # routing off by default
    assert config.noise_patterns() == []


def test_json_config_overrides(tmp_path):
    (tmp_path / "memorybridge.json").write_text(json.dumps({
        "max_total_tokens": 12345,
        "routing": {"domains": {"work": ["sprint"]}},
    }))
    config.reset_cache()
    assert config.max_total_tokens() == 12345
    # deep-merged: domains overridden, other routing keys keep defaults
    rt = config.routing()
    assert rt["domains"] == {"work": ["sprint"]}
    assert rt["custom_router"] is None


def test_env_wins_over_file(tmp_path, monkeypatch):
    (tmp_path / "memorybridge.json").write_text(json.dumps({"max_total_tokens": 111}))
    monkeypatch.setenv("MEMORYBRIDGE_MAX_TOKENS", "999")
    config.reset_cache()
    assert config.max_total_tokens() == 999


def test_project_aliases_normalize(tmp_path, monkeypatch):
    (tmp_path / "memorybridge.json").write_text(json.dumps({
        "project_aliases": {"ROI That Works": "roi", "roithatworks": "roi"}}))
    config.reset_cache()
    assert config.project_aliases()["roi that works"] == "roi"
    from db.constants import _normalize_project
    assert _normalize_project("ROI That Works") == "roi"
    assert _normalize_project("roithatworks") == "roi"
    assert _normalize_project("Something Else") == "Something Else"  # pass-through


def test_yaml_config_if_pyyaml_available(tmp_path):
    pytest.importorskip("yaml")
    (tmp_path / "memorybridge.yaml").write_text(
        "max_total_tokens: 4242\nrouting:\n  domains:\n    home: [dishes]\n")
    config.reset_cache()
    assert config.max_total_tokens() == 4242
    assert config.routing()["domains"] == {"home": ["dishes"]}


# --------------------------------------------------------------------------- #
# Profile resolution (#197).
#
# The active profile is a config contract, not a literal: a consumer that pins
# the name silently hides every other profile from itself. These tests pin the
# resolution order AND that the two real consumers (CLI, MCP server) read it.
# --------------------------------------------------------------------------- #

def test_profile_defaults_to_default():
    assert config.DEFAULT_PROFILE == "default"
    assert config.profile() == "default"


def test_profile_from_json_config(tmp_path):
    (tmp_path / "memorybridge.json").write_text(json.dumps({"profile": "teaching"}))
    config.reset_cache()
    assert config.profile() == "teaching"


def test_profile_from_yaml_config(tmp_path):
    pytest.importorskip("yaml")
    (tmp_path / "memorybridge.yaml").write_text("profile: consulting\n")
    config.reset_cache()
    assert config.profile() == "consulting"


def test_profile_env_wins_over_file(tmp_path, monkeypatch):
    (tmp_path / "memorybridge.json").write_text(json.dumps({"profile": "teaching"}))
    monkeypatch.setenv("MEMORYBRIDGE_PROFILE", "consulting")
    config.reset_cache()
    assert config.profile() == "consulting"


def test_profile_env_is_stripped(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORYBRIDGE_PROFILE", "  teaching  ")
    config.reset_cache()
    assert config.profile() == "teaching"


def test_profile_blank_env_ignored(tmp_path, monkeypatch):
    """A blank env var must not blank the profile — it falls through to the file."""
    (tmp_path / "memorybridge.json").write_text(json.dumps({"profile": "teaching"}))
    monkeypatch.setenv("MEMORYBRIDGE_PROFILE", "   ")
    config.reset_cache()
    assert config.profile() == "teaching"


def test_profile_blank_file_value_falls_back(tmp_path):
    (tmp_path / "memorybridge.json").write_text(json.dumps({"profile": "  "}))
    config.reset_cache()
    assert config.profile() == config.DEFAULT_PROFILE


def test_cli_profile_default_follows_config(monkeypatch):
    """A contract nobody reads isn't one: the CLI's `--profile` defaults must
    track the configured profile, not a hardcoded literal."""
    monkeypatch.setenv("MEMORYBRIDGE_PROFILE", "teaching")
    config.reset_cache()
    import cli

    parser = cli.build_parser()
    for argv in (["status"], ["maintain"], ["ingest", "--source", "claude"],
                 ["reverse-prune", "mem_x", "--reason", "why"]):
        assert parser.parse_args(argv).profile == "teaching", argv


def test_server_default_profile_follows_config(tmp_path):
    """The MCP server resolves its fallback profile at import, so this runs in a
    fresh interpreter — an in-session `import server` (test_http_bridge does one)
    would hand back the already-cached module instead of exercising the change."""
    pytest.importorskip("fastmcp")
    repo_root = Path(__file__).resolve().parents[2]
    env = dict(os.environ)
    env.update({
        "MEMORYBRIDGE_DATA": str(tmp_path),
        "MEMORYBRIDGE_PROFILE": "teaching",
        "MEMORYBRIDGE_NO_EMBED": "1",
    })
    proc = subprocess.run(
        [sys.executable, "-c", "import server; print(server.DEFAULT_PROFILE)"],
        cwd=repo_root, env=env, capture_output=True, text=True, timeout=300,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert proc.stdout.strip().splitlines()[-1] == "teaching"
