from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml
from agent.terminal_env_provider import TerminalEnvironmentProvider

_REQUIRED_ENV = {
    "CODER_URL": "https://coder.example",
    "CODER_API_KEY": "secret-token",
    "CODER_WORKSPACE": "shared-dev",
}
_OPTIONAL_ENV = (
    "TERMINAL_CODER_FORWARD_ENV",
    "TERMINAL_CODER_WORKSPACE_STARTUP_TIMEOUT",
)


def _set_required_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in _REQUIRED_ENV.items():
        monkeypatch.setenv(name, value)


def _clear_coder_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (*_REQUIRED_ENV, *_OPTIONAL_ENV):
        monkeypatch.delenv(name, raising=False)


def test_register_adds_coder_terminal_environment_provider():
    from hermes_plugin_coder.plugin import CoderTerminalEnvironmentProvider, register

    ctx = MagicMock()
    register(ctx)

    ctx.register_terminal_environment_provider.assert_called_once()
    provider = ctx.register_terminal_environment_provider.call_args.args[0]
    assert isinstance(provider, CoderTerminalEnvironmentProvider)
    assert isinstance(provider, TerminalEnvironmentProvider)
    assert provider.name == "coder"
    assert provider.display_name == "Coder"
    assert provider.is_remote is True
    assert provider.is_container is True
    assert provider.skip_container_guards is False
    assert provider.cache_path_base == "~/.hermes"
    assert provider.strip_env_keys == frozenset({"CODER_API_KEY"})


def test_manifest_does_not_gate_provider_discovery_before_setup():
    manifest = yaml.safe_load((Path(__file__).parents[1] / "plugin.yaml").read_text())

    assert manifest["name"] == "coder"
    assert "requires_env" not in manifest


def test_provider_does_not_define_feature_branch_config_hooks():
    from hermes_plugin_coder.plugin import CoderTerminalEnvironmentProvider

    provider_methods = CoderTerminalEnvironmentProvider.__dict__
    assert "get_config_schema" not in provider_methods
    assert "resolve_config" not in provider_methods
    assert "probe_with_config" not in provider_methods


def test_coder_availability_requires_all_connection_environment(monkeypatch):
    from hermes_plugin_coder.plugin import CoderTerminalEnvironmentProvider

    provider = CoderTerminalEnvironmentProvider()
    for name in _REQUIRED_ENV:
        _set_required_env(monkeypatch)
        monkeypatch.delenv(name)
        assert provider.is_available() is False

    _set_required_env(monkeypatch)
    assert provider.is_available() is True


def test_coder_probe_and_requirements_use_process_environment(monkeypatch, caplog):
    from hermes_plugin_coder.plugin import CoderTerminalEnvironmentProvider

    provider = CoderTerminalEnvironmentProvider()
    _clear_coder_env(monkeypatch)

    assert provider.probe() == (
        "needs_setup",
        "Configure Coder environment variables: base_url, api_key, workspace_name.",
    )
    assert provider.check_requirements({"env_type": "coder"}) is False
    assert "base_url, api_key, workspace_name" in caplog.text
    assert "secret-token" not in caplog.text

    _set_required_env(monkeypatch)
    assert provider.probe() == ("ready", "")
    assert provider.check_requirements({"env_type": "coder"}) is True


@pytest.mark.parametrize(
    ("name", "value", "invalid_field"),
    [
        ("CODER_URL", "http://coder.example", "base_url"),
        ("CODER_API_KEY", "token\nInjected: value", "api_key"),
        ("CODER_WORKSPACE", "   ", "workspace_name"),
        ("TERMINAL_CODER_FORWARD_ENV", "not-json", "forward_env"),
        (
            "TERMINAL_CODER_WORKSPACE_STARTUP_TIMEOUT",
            "0",
            "workspace_startup_timeout",
        ),
    ],
)
def test_probe_requirements_and_factory_share_environment_validation(
    monkeypatch, caplog, name, value, invalid_field
):
    from hermes_plugin_coder.plugin import CoderTerminalEnvironmentProvider

    _set_required_env(monkeypatch)
    monkeypatch.setenv(name, value)
    provider = CoderTerminalEnvironmentProvider()

    assert provider.probe() == (
        "needs_setup",
        f"Configure Coder environment variables: {invalid_field}.",
    )
    assert provider.check_requirements({"env_type": "coder"}) is False
    assert invalid_field in caplog.text
    if value.strip():
        assert value not in caplog.text

    with pytest.raises(ValueError, match=invalid_field):
        provider.create_environment(cwd="~", timeout=60)


def test_factory_builds_coder_environment_from_process_environment(monkeypatch):
    from hermes_plugin_coder import plugin

    _set_required_env(monkeypatch)
    monkeypatch.setenv("TERMINAL_CODER_FORWARD_ENV", '["GITHUB_TOKEN", "CUSTOM_VALUE"]')
    monkeypatch.setenv("TERMINAL_CODER_WORKSPACE_STARTUP_TIMEOUT", "240")
    constructor = MagicMock(return_value=MagicMock())
    monkeypatch.setattr(plugin, "CoderEnvironment", constructor)

    result = plugin.CoderTerminalEnvironmentProvider().create_environment(
        cwd="/worktree",
        timeout=45,
        task_id="task-coder",
        image="ignored",
        container_config={"container_cpu": 2},
        future_host_field="ignored",
    )

    assert result is constructor.return_value
    constructor.assert_called_once_with(
        base_url="https://coder.example",
        task_id="task-coder",
        api_key="secret-token",
        workspace_name="shared-dev",
        cwd="/worktree",
        timeout=45,
        forward_env=["GITHUB_TOKEN", "CUSTOM_VALUE"],
        workspace_startup_timeout=240,
    )


def test_factory_ignores_feature_branch_backend_config_payload(monkeypatch):
    from hermes_plugin_coder import plugin

    _set_required_env(monkeypatch)
    constructor = MagicMock(return_value=MagicMock())
    monkeypatch.setattr(plugin, "CoderEnvironment", constructor)

    plugin.CoderTerminalEnvironmentProvider().create_environment(
        cwd="~",
        timeout=60,
        backend_config={
            "base_url": "https://feature-branch.example",
            "api_key": "feature-branch-token",
            "workspace_name": "feature-branch-workspace",
        },
    )

    assert constructor.call_args.kwargs["base_url"] == "https://coder.example"
    assert constructor.call_args.kwargs["api_key"] == "secret-token"
    assert constructor.call_args.kwargs["workspace_name"] == "shared-dev"


def test_factory_defaults_generated_root_cwd_to_remote_home(monkeypatch):
    from hermes_plugin_coder import plugin

    _set_required_env(monkeypatch)
    constructor = MagicMock(return_value=MagicMock())
    monkeypatch.setattr(plugin, "CoderEnvironment", constructor)

    plugin.CoderTerminalEnvironmentProvider().create_environment(
        cwd="/root",
        timeout=60,
    )

    assert constructor.call_args.kwargs["cwd"] == "~"


def test_environment_loader_supplies_optional_defaults(monkeypatch):
    from hermes_plugin_coder.plugin import _load_coder_environment_config

    _set_required_env(monkeypatch)
    for name in _OPTIONAL_ENV:
        monkeypatch.delenv(name, raising=False)

    assert _load_coder_environment_config() == {
        "base_url": "https://coder.example",
        "api_key": "secret-token",
        "workspace_name": "shared-dev",
        "forward_env": [],
        "workspace_startup_timeout": 180,
    }


def test_upstream_host_runtime_calls_provider_factory_without_config_payload(
    monkeypatch,
):
    from agent.terminal_env_registry import (
        register_provider,
        restore_registration,
        snapshot_registration,
    )
    from tools import terminal_tool_backends

    from hermes_plugin_coder import plugin

    _set_required_env(monkeypatch)
    constructor = MagicMock(return_value=MagicMock())
    monkeypatch.setattr(plugin, "CoderEnvironment", constructor)
    provider = plugin.CoderTerminalEnvironmentProvider()
    previous = snapshot_registration(provider.name)
    register_provider(provider)
    try:
        result = terminal_tool_backends._create_environment(
            "coder", "", "/workspace", 45, task_id="task-coder"
        )
    finally:
        restore_registration(provider.name, provider, previous)

    assert result is constructor.return_value
    constructor.assert_called_once_with(
        base_url="https://coder.example",
        task_id="task-coder",
        api_key="secret-token",
        workspace_name="shared-dev",
        cwd="/workspace",
        timeout=45,
        forward_env=[],
        workspace_startup_timeout=180,
    )
