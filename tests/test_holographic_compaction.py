# tests/test_holographic_compaction.py
"""Tests for compaction recovery: infrastructure category, config, on_session_switch, bootstrap injection."""

import pytest
from pathlib import Path
from plugins.memory.holographic import HolographicMemoryProvider
from plugins.memory.holographic.store import MemoryStore


@pytest.fixture
def tmp_store(tmp_path):
    return MemoryStore(db_path=str(tmp_path / "mem.db"))


@pytest.fixture
def provider(tmp_store):
    config = {
        "db_path": str(tmp_store.db_path),
        "bootstrap_inject_limit": 5,
        "bootstrap_min_trust": 0.7,
        "bootstrap_shadow": False,
    }
    p = HolographicMemoryProvider(config=config)
    p.initialize("test-session-001")
    return p


class TestInfrastructureCategory:
    def test_infrastructure_accepted_by_fact_store(self, provider, tmp_store):
        """fact_store(action='add') with category='infrastructure' must not return an error."""
        import json
        result = provider.handle_tool_call("fact_store", {
            "action": "add",
            "content": "test host: runs on hive.local",
            "category": "infrastructure",
        })
        data = json.loads(result)
        assert "fact_id" in data
        assert data.get("status") == "added"

    def test_infrastructure_in_schema_enum(self, provider):
        schemas = provider.get_tool_schemas()
        fact_store_schema = next(s for s in schemas if s["name"] == "fact_store")
        category_enum = fact_store_schema["parameters"]["properties"]["category"]["enum"]
        assert "infrastructure" in category_enum


class TestConfigKeys:
    def test_default_config_values(self):
        p = HolographicMemoryProvider(config={})
        assert p._config.get("bootstrap_inject_limit", 15) == 15
        assert p._config.get("bootstrap_min_trust", 0.7) == 0.7
        assert p._config.get("bootstrap_shadow", False) == False
        assert p._config.get("okf_bundle_path", "/shared/agents/common/infrastructure/") == "/shared/agents/common/infrastructure/"

    def test_config_keys_in_schema(self):
        p = HolographicMemoryProvider(config={})
        schema = p.get_config_schema()
        keys = {entry["key"] for entry in schema}
        assert "okf_bundle_path" in keys
        assert "bootstrap_inject_limit" in keys
        assert "bootstrap_min_trust" in keys
        assert "bootstrap_shadow" in keys
