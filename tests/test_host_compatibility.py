from __future__ import annotations

import importlib
import importlib.util
import sys
import types
import uuid
from pathlib import Path

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def _load_installer():
    module_name = f"record_install_mac_test_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(
        module_name,
        PLUGIN_ROOT / "scripts" / "install-mac.py",
    )
    if spec is None or spec.loader is None:
        raise ImportError("Could not load Record macOS installer")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _install_fake_host_modules(monkeypatch, plugin_agent) -> None:
    agent_package = types.ModuleType("agent")
    agent_package.__path__ = []
    agent_module = types.ModuleType("agent.plugin_agent")
    agent_module.PluginAgent = plugin_agent
    hermes_package = types.ModuleType("hermes_cli")
    hermes_package.__path__ = []
    plugins_module = types.ModuleType("hermes_cli.plugins")
    webhook_module = types.ModuleType("hermes_cli.webhook")

    class PluginContext:
        agent = property(lambda self: None)

    plugins_module.PluginContext = PluginContext
    webhook_module.mutate_subscriptions = lambda mutator: mutator

    monkeypatch.setitem(sys.modules, "agent", agent_package)
    monkeypatch.setitem(sys.modules, "agent.plugin_agent", agent_module)
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_package)
    monkeypatch.setitem(sys.modules, "hermes_cli.plugins", plugins_module)
    monkeypatch.setitem(sys.modules, "hermes_cli.webhook", webhook_module)


class CompatiblePluginAgent:
    def run(
        self,
        *,
        user_message,
        system_prompt,
        task_id=None,
        max_iterations=24,
        tool_progress_callback=None,
        status_callback=None,
        required_tools=None,
        allowed_tools=None,
        full_capabilities=False,
        purpose="plugin.agent",
    ):
        raise AssertionError("signature probe must not execute the host agent")


class PreAllowlistPluginAgent:
    def run(
        self,
        *,
        user_message,
        system_prompt,
        task_id=None,
        max_iterations=24,
        tool_progress_callback=None,
        status_callback=None,
        required_tools=None,
        full_capabilities=False,
        purpose="plugin.agent",
    ):
        raise AssertionError("signature probe must not execute the host agent")


@pytest.mark.parametrize(
    ("agent_type", "compatible"),
    [
        (CompatiblePluginAgent, True),
        (PreAllowlistPluginAgent, False),
    ],
)
def test_plugin_registration_binds_exact_agent_run_contract(
    monkeypatch,
    agent_type,
    compatible,
):
    plugin = importlib.import_module("hermes_plugins.record")
    _install_fake_host_modules(monkeypatch, agent_type)
    context = types.SimpleNamespace(agent=agent_type())

    if compatible:
        plugin._require_host_features(context)
    else:
        with pytest.raises(RuntimeError, match="Update Wexpro Agent"):
            plugin._require_host_features(context)


@pytest.mark.parametrize(
    ("agent_type", "compatible"),
    [
        (CompatiblePluginAgent, True),
        (PreAllowlistPluginAgent, False),
    ],
)
def test_installer_probe_binds_exact_agent_run_contract(
    monkeypatch,
    agent_type,
    compatible,
):
    installer = _load_installer()
    _install_fake_host_modules(monkeypatch, agent_type)

    if compatible:
        exec(installer.MANAGED_RUNTIME_PROBE, {})
    else:
        with pytest.raises(TypeError, match="allowed_tools"):
            exec(installer.MANAGED_RUNTIME_PROBE, {})
