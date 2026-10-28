"""Tests for canonical entity normalization and alias dictionary (Issue #212)."""
import json
import pytest
from pathlib import Path

import config
from db.constants import _normalize_tag, _normalize_entity, _merge_tags
from db.store import MemoryStore


def test_entity_aliases_config_resolution(tmp_path, monkeypatch):
    """config.entity_aliases correctly resolves dict and list formats."""
    cfg_data = {
        "entity_aliases": {
            "cale": ["cale_corbett", "author", "me"],
            "fastmcp_server": "fastmcp",
        }
    }
    (tmp_path / "memorybridge.json").write_text(json.dumps(cfg_data))
    monkeypatch.setenv("MEMORYBRIDGE_DATA", str(tmp_path))
    config.reset_cache()

    aliases = config.entity_aliases()
    assert aliases["cale_corbett"] == "cale"
    assert aliases["author"] == "cale"
    assert aliases["me"] == "cale"
    assert aliases["fastmcp_server"] == "fastmcp"

    group = config.get_entity_alias_group("cale")
    assert "cale" in group
    assert "cale_corbett" in group
    assert "entity:cale_corbett" in group
    assert "entity:cale" in group


def test_normalize_entity_tag(tmp_path, monkeypatch):
    """_normalize_tag maps variant entity tags to canonical forms."""
    cfg_data = {
        "entity_aliases": {
            "cale": ["cale_corbett", "author"],
        }
    }
    (tmp_path / "memorybridge.json").write_text(json.dumps(cfg_data))
    monkeypatch.setenv("MEMORYBRIDGE_DATA", str(tmp_path))
    config.reset_cache()

    assert _normalize_entity("cale_corbett") == "cale"
    assert _normalize_tag("entity:cale_corbett") == "entity:cale"
    assert _normalize_tag("entity:author") == "entity:cale"
    assert _normalize_tag("entity:cale") == "entity:cale"
    assert _normalize_tag("entity:unknown_entity") == "entity:unknown_entity"

    # _merge_tags deduplicates across aliases
    tags = _merge_tags(["entity:cale_corbett", "tech:python"], ["entity:author", "entity:cale"])
    assert tags == ["entity:cale", "tech:python"]


def test_auto_link_entities_bridges_synonymous_aliases(tmp_path, monkeypatch):
    """Memories sharing an entity via alias variants link together via shares_entities edge."""
    cfg_data = {
        "entity_aliases": {
            "cale": ["cale_corbett"],
        }
    }
    (tmp_path / "memorybridge.json").write_text(json.dumps(cfg_data))
    monkeypatch.setenv("MEMORYBRIDGE_DATA", str(tmp_path))
    config.reset_cache()

    st = MemoryStore(tmp_path / "test_alias.db")
    st.ensure_profile("default")

    # Memory 1 tagged with entity:cale and entity:python
    m1 = st.add_memory("default", "Cale prefers writing async networking code in Python",
                       category="fact", tags=["entity:cale", "entity:python"])

    # Memory 2 tagged with entity:cale_corbett (alias) and entity:python
    m2 = st.add_memory("default", "Cale Corbett deployed the high throughput telemetry collector",
                       category="fact", tags=["entity:cale_corbett", "entity:python"])

    # Since entity:cale_corbett is normalized and alias-expanded, m1 and m2 share 2 entities
    edges = st.get_edges(m1)
    shared_edges = [e for e in edges if e["relation"] == "shares_entities"]
    assert len(shared_edges) >= 1
    assert any(e["target_id"] == m2 or e["source_id"] == m2 for e in shared_edges)
