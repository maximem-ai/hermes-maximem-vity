"""Unit tests for the standalone Vity memory provider."""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock


def test_name_is_vity(vity_module):
    assert vity_module.VityMemoryProvider().name == "vity"


def test_is_available_requires_api_key(vity_module, monkeypatch):
    monkeypatch.delenv("MAXIMEM_API_KEY", raising=False)
    monkeypatch.delenv("VITY_API_KEY", raising=False)
    assert vity_module.VityMemoryProvider().is_available() is False

    monkeypatch.setenv("MAXIMEM_API_KEY", "mx_test_key")
    assert vity_module.VityMemoryProvider().is_available() is True


def test_config_loads_key_and_tunable_defaults(vity_module, monkeypatch):
    monkeypatch.setenv("MAXIMEM_API_KEY", "mx_test_key")
    cfg = vity_module._load_config()

    assert cfg["api_key"] == "mx_test_key"
    assert cfg["auto_recall"] is True
    assert cfg["auto_capture"] is True
    assert cfg["max_recall_tokens"] == 1000
    assert cfg["min_prompt_length"] == 5


def test_config_schema_is_api_key_only(vity_module):
    schema = vity_module.VityMemoryProvider().get_config_schema()
    assert [item["key"] for item in schema] == ["api_key"]
    assert schema[0]["secret"] is True
    assert schema[0]["env_var"] == "MAXIMEM_API_KEY"


def test_tool_schemas_expose_four_tools(vity_module):
    names = {t["name"] for t in vity_module.VityMemoryProvider().get_tool_schemas()}
    assert names == {"vity_recall", "vity_profile", "vity_store", "vity_forget"}


def test_client_built_with_api_key_only(vity_module, monkeypatch):
    constructor = MagicMock(return_value=MagicMock())
    monkeypatch.setitem(sys.modules, "maximem_vity", SimpleNamespace(VityClient=constructor))

    provider = vity_module.VityMemoryProvider()
    provider._api_key = "mx_test_key"
    provider._get_client()

    constructor.assert_called_once_with(api_key="mx_test_key")


def test_store_tool_dispatch(vity_module, monkeypatch):
    fake_client = MagicMock()
    fake_client.store.return_value = {"id": "mem_123", "stored": True}
    monkeypatch.setitem(
        sys.modules, "maximem_vity", SimpleNamespace(VityClient=lambda **_: fake_client)
    )

    provider = vity_module.VityMemoryProvider()
    provider._api_key = "mx_test_key"
    out = json.loads(provider.handle_tool_call("vity_store", {"content": "I like apples"}))

    assert out["id"] == "mem_123"
    fake_client.store.assert_called_once()


def test_forget_dry_run_does_not_confirm(vity_module, monkeypatch):
    fake_client = MagicMock()
    fake_client.forget.return_value = {"count": 3, "ids": ["a", "b", "c"]}
    monkeypatch.setitem(
        sys.modules, "maximem_vity", SimpleNamespace(VityClient=lambda **_: fake_client)
    )

    provider = vity_module.VityMemoryProvider()
    provider._api_key = "mx_test_key"
    out = json.loads(provider.handle_tool_call("vity_forget", {"query": "old"}))

    assert "Would delete 3" in out["result"]
    fake_client.forget.assert_called_once_with(query="old", dry_run=True)


def test_auto_capture_disabled_skips_sync(vity_module, monkeypatch):
    fake_client = MagicMock()
    monkeypatch.setitem(
        sys.modules, "maximem_vity", SimpleNamespace(VityClient=lambda **_: fake_client)
    )

    provider = vity_module.VityMemoryProvider()
    provider._api_key = "mx_test_key"
    provider._auto_capture = False
    provider.sync_turn("hi", "hello", session_id="s1")

    fake_client.capture.assert_not_called()


def test_register_entry_point(vity_module):
    captured = {}

    class Ctx:
        def register_memory_provider(self, provider):
            captured["provider"] = provider

    vity_module.register(Ctx())
    assert captured["provider"].name == "vity"


def test_system_prompt_mentions_api_key_scope(vity_module):
    block = vity_module.VityMemoryProvider().system_prompt_block()
    assert "configured Maximem API key" in block
