"""Tests for the central config loader."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest

import config


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORYBRIDGE_DATA", str(tmp_path))
    monkeypatch.delenv("MEMORYBRIDGE_MAX_TOKENS", raising=False)
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
