"""Test harness: stub the Hermes host modules so the plugin imports standalone.

At runtime the plugin loads inside the hermes-agent process, where
``agent.memory_provider``, ``tools.registry``, ``hermes_constants`` and
``utils`` all exist. For unit tests we provide lightweight fakes so the
provider can be imported and exercised without a full Hermes checkout.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _install_host_stubs(home: Path) -> None:
    # agent.memory_provider.MemoryProvider — minimal base class.
    agent_pkg = types.ModuleType("agent")
    mem_mod = types.ModuleType("agent.memory_provider")

    class MemoryProvider:  # noqa: D401 - minimal stand-in for the ABC
        """Stand-in base; the real one is an ABC with the same surface."""

    mem_mod.MemoryProvider = MemoryProvider
    agent_pkg.memory_provider = mem_mod
    sys.modules["agent"] = agent_pkg
    sys.modules["agent.memory_provider"] = mem_mod

    # tools.registry.tool_error
    tools_pkg = types.ModuleType("tools")
    registry_mod = types.ModuleType("tools.registry")
    registry_mod.tool_error = lambda msg: json.dumps({"error": msg})
    tools_pkg.registry = registry_mod
    sys.modules["tools"] = tools_pkg
    sys.modules["tools.registry"] = registry_mod

    # hermes_constants.get_hermes_home
    hc_mod = types.ModuleType("hermes_constants")
    hc_mod.get_hermes_home = lambda: home
    sys.modules["hermes_constants"] = hc_mod

    # utils.atomic_json_write
    utils_mod = types.ModuleType("utils")

    def _atomic_json_write(path, data, mode=0o600):
        Path(path).write_text(json.dumps(data))

    utils_mod.atomic_json_write = _atomic_json_write
    sys.modules["utils"] = utils_mod


@pytest.fixture
def vity_module(tmp_path, monkeypatch):
    """Import the provider module fresh with host stubs in place."""
    _install_host_stubs(tmp_path)
    # Re-point get_hermes_home at this test's tmp dir on every load.
    monkeypatch.setattr(sys.modules["hermes_constants"], "get_hermes_home", lambda: tmp_path)

    spec = importlib.util.spec_from_file_location(
        "vity_plugin_under_test",
        _REPO_ROOT / "src" / "hermes_maximem_vity" / "payload" / "provider.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod
