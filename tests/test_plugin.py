from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from tools.environments import (
    BackendDefinition,
    BackendFactoryRequest,
    ExecutionLocation,
    FilesystemSemantics,
)


_REQUIRED_ENV = {
    "CODER_URL": "https://coder.example",
    "CODER_API_KEY": "secret-token",
    "CODER_WORKSPACE": "shared-dev",
}


def _set_required_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in _REQUIRED_ENV.items():
        monkeypatch.setenv(name, value)


def test_register_adds_coder_backend_definition(monkeypatch):
    from hermes_plugin_coder.plugin import register

    ctx = MagicMock()
    register(ctx)

    ctx.register_terminal_backend.assert_called_once()
    definition = ctx.register_terminal_backend.call_args.args[0]
    assert isinstance(definition, BackendDefinition)
    assert definition.name == "coder"
    assert definition.capabilities.execution_location is ExecutionLocation.REMOTE
    assert definition.capabilities.filesystem_semantics is FilesystemSemantics.ISOLATED
    assert definition.capabilities.requires_sandbox_cwd is True
    assert definition.capabilities.accepts_host_cwd is False
    assert definition.capabilities.supports_image is False
    assert definition.default_cwd == "~"
    assert callable(definition.config_resolver)


def test_coder_availability_requires_all_connection_environment(monkeypatch):
    from hermes_plugin_coder.plugin import coder_backend_definition

    definition = coder_backend_definition()
    for name in _REQUIRED_ENV:
        _set_required_env(monkeypatch)
        monkeypatch.delenv(name)
        assert definition.is_available() is False

    _set_required_env(monkeypatch)
    assert definition.is_available() is True


def test_factory_builds_coder_environment_from_backend_config_and_host_request(
    monkeypatch,
):
    from hermes_plugin_coder import plugin

    for name in _REQUIRED_ENV:
        monkeypatch.delenv(name, raising=False)
    constructor = MagicMock(return_value=MagicMock())
    monkeypatch.setattr(plugin, "CoderEnvironment", constructor)

    request = BackendFactoryRequest(
        backend_name="coder",
        task_id="task-coder",
        cwd="/worktree",
        timeout=45,
        image="ignored",
        host_cwd="/host/must-not-leak",
        backend_config={
            "base_url": "https://coder.example",
            "api_key": "secret-token",
            "workspace_name": "shared-dev",
            "forward_env": ["GITHUB_TOKEN", "CUSTOM_VALUE"],
            "workspace_startup_timeout": 240,
        },
    )
    result = plugin.create_coder_environment(request)

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

    for name in _REQUIRED_ENV:
        monkeypatch.delenv(name, raising=False)
    constructor = MagicMock(return_value=MagicMock())
    monkeypatch.setattr(plugin, "CoderEnvironment", constructor)

    plugin.create_coder_environment(
        BackendFactoryRequest(
            backend_name="coder",
            task_id="task",
            cwd="/root",
            backend_config={
                "base_url": "https://coder.example",
                "api_key": "secret-token",
                "workspace_name": "shared-dev",
                "forward_env": [],
                "workspace_startup_timeout": 180,
            },
        )
    )

    assert constructor.call_args.kwargs["cwd"] == "~"


def test_config_resolver_parses_plugin_environment(monkeypatch):
    from hermes_plugin_coder.plugin import resolve_coder_config

    _set_required_env(monkeypatch)
    monkeypatch.setenv("TERMINAL_CODER_FORWARD_ENV", '["GITHUB_TOKEN", "CUSTOM_VALUE"]')
    monkeypatch.setenv("TERMINAL_CODER_WORKSPACE_STARTUP_TIMEOUT", "240")

    assert resolve_coder_config() == {
        "base_url": "https://coder.example",
        "api_key": "secret-token",
        "workspace_name": "shared-dev",
        "forward_env": ["GITHUB_TOKEN", "CUSTOM_VALUE"],
        "workspace_startup_timeout": 240,
    }


def test_config_resolver_rejects_invalid_forward_env_json(monkeypatch):
    from hermes_plugin_coder.plugin import resolve_coder_config

    _set_required_env(monkeypatch)
    monkeypatch.setenv("TERMINAL_CODER_FORWARD_ENV", "not-json")

    with pytest.raises(ValueError, match="TERMINAL_CODER_FORWARD_ENV"):
        resolve_coder_config()


def test_factory_rejects_missing_required_backend_config():
    from hermes_plugin_coder.plugin import create_coder_environment

    with pytest.raises(ValueError, match="api_key"):
        create_coder_environment(
            BackendFactoryRequest(
                backend_name="coder",
                backend_config={
                    "base_url": "https://coder.example",
                    "workspace_name": "shared-dev",
                },
            )
        )


@pytest.mark.parametrize("name", ["base_url", "api_key", "workspace_name"])
def test_factory_rejects_non_string_required_backend_config(name):
    from hermes_plugin_coder.plugin import create_coder_environment

    config = {
        "base_url": "https://coder.example",
        "api_key": "secret",
        "workspace_name": "shared-dev",
    }
    config[name] = object()

    with pytest.raises(ValueError, match=name):
        create_coder_environment(
            BackendFactoryRequest(backend_name="coder", backend_config=config)
        )


def test_factory_rejects_boolean_startup_timeout():
    from hermes_plugin_coder.plugin import create_coder_environment

    with pytest.raises(ValueError, match="positive integer"):
        create_coder_environment(
            BackendFactoryRequest(
                backend_name="coder",
                backend_config={
                    "base_url": "https://coder.example",
                    "api_key": "secret",
                    "workspace_name": "shared-dev",
                    "workspace_startup_timeout": True,
                },
            )
        )
