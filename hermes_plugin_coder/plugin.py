"""Terminal-environment provider adapter for the Coder backend."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from typing import Any

from agent.terminal_env_provider import TerminalEnvironmentProvider

from .backend import (
    CoderEnvironment,
    _normalize_coder_base_url,
    _normalize_coder_workspace_name,
    _validate_coder_api_key,
    _validate_coder_forward_env,
    _validate_coder_startup_timeout,
)

logger = logging.getLogger(__name__)


def _invalid_config_fields(config: Mapping[str, Any]) -> list[str]:
    """Return safe field names whose values the factory would reject."""
    invalid: list[str] = []
    defaults = {"forward_env": [], "workspace_startup_timeout": 180}
    for field, validator in (
        ("base_url", _normalize_coder_base_url),
        ("api_key", _validate_coder_api_key),
        ("workspace_name", _normalize_coder_workspace_name),
        ("forward_env", _validate_coder_forward_env),
        ("workspace_startup_timeout", _validate_coder_startup_timeout),
    ):
        try:
            validator(config.get(field, defaults.get(field)))
        except ValueError:
            invalid.append(field)
    return invalid


def coder_backend_config_available(config: Mapping[str, Any]) -> bool:
    """Return whether resolved config can construct a Coder environment."""
    return not _invalid_config_fields(config)


def _parse_forward_env() -> list[str]:
    raw = os.getenv("TERMINAL_CODER_FORWARD_ENV", "[]")
    try:
        parsed: Any = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError(
            "TERMINAL_CODER_FORWARD_ENV must be a valid JSON list"
        ) from exc
    if not isinstance(parsed, list) or any(
        not isinstance(item, str) for item in parsed
    ):
        raise ValueError("TERMINAL_CODER_FORWARD_ENV must be a JSON list of strings")
    return parsed


def _parse_startup_timeout() -> int:
    raw = os.getenv("TERMINAL_CODER_WORKSPACE_STARTUP_TIMEOUT", "180")
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "TERMINAL_CODER_WORKSPACE_STARTUP_TIMEOUT must be a positive integer"
        ) from exc
    if value <= 0:
        raise ValueError(
            "TERMINAL_CODER_WORKSPACE_STARTUP_TIMEOUT must be a positive integer"
        )
    return value


def resolve_coder_config(
    raw_backend_config: Mapping[str, Any],
) -> dict[str, Any]:
    """Resolve Coder config with environment > profile YAML > defaults."""
    config: dict[str, Any] = {
        "forward_env": [],
        "workspace_startup_timeout": 180,
    }
    config.update(raw_backend_config)

    for config_key, env_name in (
        ("base_url", "CODER_URL"),
        ("api_key", "CODER_API_KEY"),
        ("workspace_name", "CODER_WORKSPACE"),
    ):
        value = os.getenv(env_name)
        if value is not None:
            config[config_key] = value

    if os.getenv("TERMINAL_CODER_FORWARD_ENV") is not None:
        config["forward_env"] = _parse_forward_env()
    if os.getenv("TERMINAL_CODER_WORKSPACE_STARTUP_TIMEOUT") is not None:
        config["workspace_startup_timeout"] = _parse_startup_timeout()
    return config


def _remote_cwd(cwd: str) -> str:
    if cwd in {"", "/root"}:
        return "~"
    return cwd


def create_coder_environment(
    *,
    cwd: str,
    timeout: int,
    task_id: str = "default",
    backend_config: Mapping[str, Any] | None = None,
    **_kwargs: Any,
) -> CoderEnvironment:
    """Build one Coder environment from host and resolved provider config."""
    config = dict(backend_config or {})
    invalid = _invalid_config_fields(config)
    if invalid:
        raise ValueError(
            "Coder backend config has invalid fields: " + ", ".join(invalid)
        )

    forward_env = config.get("forward_env", [])
    startup_timeout = config.get("workspace_startup_timeout", 180)

    return CoderEnvironment(
        base_url=config["base_url"],
        task_id=task_id,
        api_key=config["api_key"],
        workspace_name=config["workspace_name"],
        cwd=_remote_cwd(cwd),
        timeout=timeout,
        forward_env=forward_env,
        workspace_startup_timeout=startup_timeout,
    )


class CoderTerminalEnvironmentProvider(TerminalEnvironmentProvider):
    """Hermes terminal-environment provider for an existing Coder workspace."""

    name = "coder"
    display_name = "Coder"
    description = "Run commands in an existing remote Coder workspace."
    env_description = "a remote Coder workspace (likely Linux)"
    is_remote = True
    is_container = True
    # A Coder workspace is durable user infrastructure, not disposable storage.
    skip_container_guards = False
    cache_path_base = "~/.hermes"
    strip_env_keys = frozenset({"CODER_API_KEY"})

    def get_config_schema(self) -> dict[str, dict[str, Any]]:
        return {
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

    def resolve_config(self, config: Mapping[str, Any]) -> dict[str, Any]:
        return resolve_coder_config(config)

    def is_available(self) -> bool:
        try:
            return coder_backend_config_available(self.validated_config({}))
        except Exception:  # noqa: BLE001 - availability must fail soft
            return False

    def check_requirements(self, config: dict[str, Any]) -> bool:
        backend_config = config.get("backend_config")
        if not isinstance(backend_config, Mapping):
            logger.error("Coder backend configuration was not resolved")
            return False
        invalid = _invalid_config_fields(backend_config)
        if invalid:
            logger.error(
                "Coder backend has invalid configuration fields: %s", ", ".join(invalid)
            )
            return False
        return True

    def probe(self) -> tuple[str, str]:
        try:
            return self.probe_with_config(self.validated_config({}))
        except Exception:  # noqa: BLE001 - picker probes must not raise
            return ("needs_setup", "Coder configuration is invalid.")

    def probe_with_config(self, config: Mapping[str, Any]) -> tuple[str, str]:
        invalid = _invalid_config_fields(config)
        if invalid:
            return ("needs_setup", f"Configure Coder fields: {', '.join(invalid)}.")
        return ("ready", "")

    def setup_instructions(self) -> list[str]:
        api_key_instruction = (
            "Set CODER_API_KEY in the active profile environment or enter it "
            + "in the secret field."
        )
        return [
            "Configure terminal.backends.coder.base_url and workspace_name.",
            api_key_instruction,
        ]

    def create_environment(
        self,
        *,
        cwd: str,
        timeout: int,
        task_id: str = "default",
        image: str | None = None,
        container_config: dict[str, Any] | None = None,
        backend_config: Mapping[str, Any] | None = None,
        **kwargs: Any,
    ) -> CoderEnvironment:
        return create_coder_environment(
            cwd=cwd,
            timeout=timeout,
            task_id=task_id,
            image=image,
            container_config=container_config,
            backend_config=backend_config,
            **kwargs,
        )


def register(ctx) -> None:
    """Register the Coder terminal environment provider with Hermes."""
    ctx.register_terminal_environment_provider(CoderTerminalEnvironmentProvider())
