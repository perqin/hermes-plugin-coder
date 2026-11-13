from unittest.mock import MagicMock

import pytest
from agent import secret_scope
from tools.terminal_scope import reset_terminal_scope, set_terminal_scope

from hermes_plugin_coder import backend, plugin


@pytest.mark.parametrize("workspace", ["coding-a", "coding-b", None])
def test_provider_uses_routed_profile_instead_of_process_env(monkeypatch, workspace):
    values = {
        "CODER_URL": "https://profile.example",
        "CODER_API_KEY": "profile-token",
        "TERMINAL_CODER_FORWARD_ENV": '["BUILD_TOKEN"]',
        "TERMINAL_CODER_WORKSPACE_STARTUP_TIMEOUT": "42",
    }
    if workspace is not None:
        values["CODER_WORKSPACE"] = workspace
    for name, value in {
        "CODER_URL": "https://launch.example",
        "CODER_API_KEY": "launch-token",
        "CODER_WORKSPACE": "launch-workspace",
        "TERMINAL_CODER_FORWARD_ENV": "[]",
        "TERMINAL_CODER_WORKSPACE_STARTUP_TIMEOUT": "180",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(secret_scope, "is_multiplex_active", lambda: True)
    constructor = MagicMock()
    monkeypatch.setattr(plugin, "CoderEnvironment", constructor)
    provider = plugin.CoderTerminalEnvironmentProvider()
    token = secret_scope.set_secret_scope(values)
    terminal_token = set_terminal_scope({
        name: value for name, value in values.items() if name.startswith("TERMINAL_")
    })
    try:
        assert provider.is_available() is (workspace is not None)
        assert provider.check_requirements({}) is (workspace is not None)
        if workspace is None:
            assert provider.probe()[0] == "needs_setup"
            with pytest.raises(ValueError, match="workspace_name"):
                provider.create_environment(cwd="~", timeout=60)
            constructor.assert_not_called()
        else:
            assert provider.probe() == ("ready", "")
            provider.create_environment(cwd="~", timeout=60)
            assert constructor.call_args.kwargs["base_url"] == values["CODER_URL"]
            assert constructor.call_args.kwargs["api_key"] == values["CODER_API_KEY"]
            assert constructor.call_args.kwargs["workspace_name"] == workspace
            assert constructor.call_args.kwargs["forward_env"] == ["BUILD_TOKEN"]
            assert constructor.call_args.kwargs["workspace_startup_timeout"] == 42
    finally:
        reset_terminal_scope(terminal_token)
        secret_scope.reset_secret_scope(token)


def test_forwarded_values_use_profile_scope_and_do_not_leak_launch_values(monkeypatch):
    monkeypatch.setenv("BUILD_TOKEN", "launch-token")
    monkeypatch.setenv("LAUNCH_ONLY", "must-not-leak")
    monkeypatch.setattr(secret_scope, "is_multiplex_active", lambda: True)
    token = secret_scope.set_secret_scope({"BUILD_TOKEN": "profile-token"})
    try:
        assert backend._collect_forwarded_env_values(
            ["BUILD_TOKEN", "LAUNCH_ONLY", "CODER_API_KEY"]
        ) == {"BUILD_TOKEN": "profile-token"}
    finally:
        secret_scope.reset_secret_scope(token)
