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


def _set_required_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in _REQUIRED_ENV.items():
        monkeypatch.setenv(name, value)


def _resolved_config(**overrides):
    config = {
        "base_url": "https://coder.example",
        "api_key": "secret-token",
        "workspace_name": "shared-dev",
        "forward_env": [],
        "workspace_startup_timeout": 180,
    }
    config.update(overrides)
    return config


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


def test_manifest_does_not_gate_profile_configured_provider_on_environment():
    manifest = yaml.safe_load((Path(__file__).parents[1] / "plugin.yaml").read_text())

    assert manifest["name"] == "coder"
    assert "requires_env" not in manifest


def test_coder_config_schema_uses_dashboard_contract_types():
    from hermes_plugin_coder.plugin import CoderTerminalEnvironmentProvider

    schema = CoderTerminalEnvironmentProvider().validated_config_schema()

    assert schema == {
        "base_url": {
            "type": "string",
            "description": "Coder deployment URL",
            "env": "CODER_URL",
            "required": True,
        },
        "api_key": {
            "type": "secret",
            "description": "Coder API key",
            "env": "CODER_API_KEY",
            "required": True,
        },
        "workspace_name": {
            "type": "string",
            "description": "Coder workspace name",
            "env": "CODER_WORKSPACE",
            "required": True,
        },
        "forward_env": {
            "type": "list",
            "description": "Environment variables forwarded to the workspace",
            "env": "TERMINAL_CODER_FORWARD_ENV",
            "default": [],
        },
        "workspace_startup_timeout": {
            "type": "number",
            "description": "Workspace startup timeout in seconds",
            "env": "TERMINAL_CODER_WORKSPACE_STARTUP_TIMEOUT",
            "default": 180,
        },
    }


def test_coder_availability_requires_all_connection_environment(monkeypatch):
    from hermes_plugin_coder.plugin import CoderTerminalEnvironmentProvider

    provider = CoderTerminalEnvironmentProvider()
    for name in _REQUIRED_ENV:
        _set_required_env(monkeypatch)
        monkeypatch.delenv(name)
        assert provider.is_available() is False

    _set_required_env(monkeypatch)
    assert provider.is_available() is True


def test_coder_probe_and_requirements_use_resolved_backend_config(monkeypatch, caplog):
    from hermes_plugin_coder.plugin import CoderTerminalEnvironmentProvider

    for name in _REQUIRED_ENV:
        monkeypatch.delenv(name, raising=False)
    provider = CoderTerminalEnvironmentProvider()
    ready = _resolved_config()

    assert provider.probe_with_config(ready) == ("ready", "")
    assert provider.check_requirements({"backend_config": ready}) is True

    incomplete = {
        "base_url": ready["base_url"],
        "workspace_name": ready["workspace_name"],
    }
    assert provider.probe_with_config(incomplete) == (
        "needs_setup",
        "Configure Coder fields: api_key.",
    )
    assert provider.check_requirements({"backend_config": incomplete}) is False
    assert "api_key" in caplog.text
    assert "secret-token" not in caplog.text


@pytest.mark.parametrize(
    ("overrides", "invalid_field"),
    [
        ({"forward_env": "not-a-list"}, "forward_env"),
        ({"workspace_startup_timeout": True}, "workspace_startup_timeout"),
        ({"workspace_startup_timeout": 0}, "workspace_startup_timeout"),
    ],
)
def test_probe_and_requirements_reject_config_that_factory_rejects(
    overrides, invalid_field, caplog
):
    from hermes_plugin_coder.plugin import CoderTerminalEnvironmentProvider

    provider = CoderTerminalEnvironmentProvider()
    config = _resolved_config(**overrides)

    assert provider.probe_with_config(config) == (
        "needs_setup",
        f"Configure Coder fields: {invalid_field}.",
    )
    assert provider.check_requirements({"backend_config": config}) is False
    assert invalid_field in caplog.text

    with pytest.raises(ValueError, match=invalid_field):
        provider.create_environment(cwd="~", timeout=60, backend_config=config)


@pytest.mark.parametrize(
    ("overrides", "invalid_field"),
    [
        ({"base_url": "http://coder.example"}, "base_url"),
        ({"base_url": "https://coder.example/path"}, "base_url"),
        ({"api_key": "token\nInjected: value"}, "api_key"),
        ({"api_key": "é"}, "api_key"),
        ({"workspace_name": "   "}, "workspace_name"),
    ],
)
def test_probe_requirements_and_factory_share_strict_connection_validation(
    overrides, invalid_field, caplog
):
    from hermes_plugin_coder.plugin import CoderTerminalEnvironmentProvider

    provider = CoderTerminalEnvironmentProvider()
    config = _resolved_config(**overrides)

    assert provider.probe_with_config(config) == (
        "needs_setup",
        f"Configure Coder fields: {invalid_field}.",
    )
    assert provider.check_requirements({"backend_config": config}) is False
    assert invalid_field in caplog.text

    with pytest.raises(ValueError, match=invalid_field):
        provider.create_environment(cwd="~", timeout=60, backend_config=config)


def test_provider_resolves_profile_yaml_with_environment_secret(monkeypatch):
    from hermes_plugin_coder.plugin import CoderTerminalEnvironmentProvider

    monkeypatch.delenv("CODER_URL", raising=False)
    monkeypatch.delenv("CODER_WORKSPACE", raising=False)
    monkeypatch.delenv("TERMINAL_CODER_FORWARD_ENV", raising=False)
    monkeypatch.delenv("TERMINAL_CODER_WORKSPACE_STARTUP_TIMEOUT", raising=False)
    monkeypatch.setenv("CODER_API_KEY", "secret-token")
    raw_config = {
        "base_url": "https://coder.example",
        "workspace_name": "shared-dev",
        "forward_env": ["GITHUB_TOKEN"],
        "workspace_startup_timeout": 500,
    }

    assert CoderTerminalEnvironmentProvider().validated_config(raw_config) == {
        "base_url": "https://coder.example",
        "api_key": "secret-token",
        "workspace_name": "shared-dev",
        "forward_env": ["GITHUB_TOKEN"],
        "workspace_startup_timeout": 500,
    }


def test_factory_builds_coder_environment_from_backend_config_and_host_kwargs(
    monkeypatch,
):
    from hermes_plugin_coder import plugin

    constructor = MagicMock(return_value=MagicMock())
    monkeypatch.setattr(plugin, "CoderEnvironment", constructor)
    provider = plugin.CoderTerminalEnvironmentProvider()

    result = provider.create_environment(
        cwd="/worktree",
        timeout=45,
        task_id="task-coder",
        image="ignored",
        container_config={"container_cpu": 2},
        backend_config=_resolved_config(
            forward_env=["GITHUB_TOKEN", "CUSTOM_VALUE"],
            workspace_startup_timeout=240,
        ),
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


def test_factory_defaults_generated_root_cwd_to_remote_home(monkeypatch):
    from hermes_plugin_coder import plugin

    constructor = MagicMock(return_value=MagicMock())
    monkeypatch.setattr(plugin, "CoderEnvironment", constructor)

    plugin.CoderTerminalEnvironmentProvider().create_environment(
        cwd="/root",
        timeout=60,
        backend_config=_resolved_config(),
    )

    assert constructor.call_args.kwargs["cwd"] == "~"


def test_config_resolver_uses_environment_over_yaml_over_defaults(monkeypatch):
    from hermes_plugin_coder.plugin import resolve_coder_config

    _set_required_env(monkeypatch)
    monkeypatch.setenv("TERMINAL_CODER_FORWARD_ENV", '["ENV_TOKEN"]')
    monkeypatch.setenv("TERMINAL_CODER_WORKSPACE_STARTUP_TIMEOUT", "240")

    assert resolve_coder_config(
        {
            "base_url": "https://yaml.example",
            "api_key": "yaml-token",
            "workspace_name": "yaml-workspace",
            "forward_env": ["YAML_TOKEN"],
            "workspace_startup_timeout": 500,
        }
    ) == _resolved_config(
        forward_env=["ENV_TOKEN"],
        workspace_startup_timeout=240,
    )


def test_config_resolver_uses_yaml_over_defaults_when_environment_absent(monkeypatch):
    from hermes_plugin_coder.plugin import resolve_coder_config

    for name in (
        *_REQUIRED_ENV,
        "TERMINAL_CODER_FORWARD_ENV",
        "TERMINAL_CODER_WORKSPACE_STARTUP_TIMEOUT",
    ):
        monkeypatch.delenv(name, raising=False)

    assert resolve_coder_config(
        {
            "base_url": "https://yaml.example",
            "api_key": "yaml-token",
            "workspace_name": "yaml-workspace",
            "forward_env": ["YAML_TOKEN"],
            "workspace_startup_timeout": 500,
        }
    ) == {
        "base_url": "https://yaml.example",
        "api_key": "yaml-token",
        "workspace_name": "yaml-workspace",
        "forward_env": ["YAML_TOKEN"],
        "workspace_startup_timeout": 500,
    }


def test_config_resolver_supplies_runtime_defaults(monkeypatch):
    from hermes_plugin_coder.plugin import resolve_coder_config

    for name in (
        *_REQUIRED_ENV,
        "TERMINAL_CODER_FORWARD_ENV",
        "TERMINAL_CODER_WORKSPACE_STARTUP_TIMEOUT",
    ):
        monkeypatch.delenv(name, raising=False)

    assert resolve_coder_config({}) == {
        "forward_env": [],
        "workspace_startup_timeout": 180,
    }


def test_config_resolver_preserves_explicit_empty_environment_override(monkeypatch):
    from hermes_plugin_coder.plugin import resolve_coder_config

    monkeypatch.setenv("CODER_URL", "")

    assert resolve_coder_config({"base_url": "https://yaml.example"})["base_url"] == ""


def test_config_resolver_rejects_invalid_environment_override(monkeypatch):
    from hermes_plugin_coder.plugin import resolve_coder_config

    monkeypatch.setenv("TERMINAL_CODER_FORWARD_ENV", "not-json")

    with pytest.raises(ValueError, match="TERMINAL_CODER_FORWARD_ENV"):
        resolve_coder_config({"forward_env": ["YAML_TOKEN"]})


def test_factory_rejects_missing_required_backend_config():
    from hermes_plugin_coder.plugin import CoderTerminalEnvironmentProvider

    with pytest.raises(ValueError, match="api_key"):
        CoderTerminalEnvironmentProvider().create_environment(
            cwd="~",
            timeout=60,
            backend_config={
                "base_url": "https://coder.example",
                "workspace_name": "shared-dev",
            },
        )


@pytest.mark.parametrize("name", ["base_url", "api_key", "workspace_name"])
def test_factory_rejects_non_string_required_backend_config(name):
    from hermes_plugin_coder.plugin import CoderTerminalEnvironmentProvider

    config = _resolved_config()
    config[name] = object()

    with pytest.raises(ValueError, match=name):
        CoderTerminalEnvironmentProvider().create_environment(
            cwd="~", timeout=60, backend_config=config
        )


def test_factory_rejects_boolean_startup_timeout():
    from hermes_plugin_coder.plugin import CoderTerminalEnvironmentProvider

    with pytest.raises(ValueError, match="workspace_startup_timeout"):
        CoderTerminalEnvironmentProvider().create_environment(
            cwd="~",
            timeout=60,
            backend_config=_resolved_config(workspace_startup_timeout=True),
        )


def test_current_host_runtime_resolves_profile_config_and_calls_provider_factory(
    monkeypatch,
):
    from agent.terminal_env_registry import (
        register_provider,
        restore_registration,
        snapshot_registration,
    )
    from hermes_cli import config as config_module
    from tools import terminal_tool

    from hermes_plugin_coder import plugin

    for name in (
        *_REQUIRED_ENV,
        "TERMINAL_CODER_FORWARD_ENV",
        "TERMINAL_CODER_WORKSPACE_STARTUP_TIMEOUT",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CODER_API_KEY", "secret-token")
    monkeypatch.setattr(
        config_module,
        "read_user_config_raw",
        lambda **_kwargs: {
            "terminal": {
                "backends": {
                    "coder": {
                        "base_url": "https://coder.example",
                        "workspace_name": "shared-dev",
                        "workspace_startup_timeout": 240,
                    }
                }
            }
        },
    )
    constructor = MagicMock(return_value=MagicMock())
    monkeypatch.setattr(plugin, "CoderEnvironment", constructor)
    provider = plugin.CoderTerminalEnvironmentProvider()
    previous = snapshot_registration(provider.name)
    register_provider(provider)
    try:
        result = terminal_tool._create_environment(
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
        workspace_startup_timeout=240,
    )
