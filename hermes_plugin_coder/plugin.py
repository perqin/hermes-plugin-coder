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


def _environment_config_and_invalid_fields() -> tuple[dict[str, Any], list[str]]:
    """Load the upstream-compatible environment contract without leaking values."""
    config: dict[str, Any] = {
        "base_url": os.getenv("CODER_URL"),
        "api_key": os.getenv("CODER_API_KEY"),
        "workspace_name": os.getenv("CODER_WORKSPACE"),
        "forward_env": [],
        "workspace_startup_timeout": 180,
    }
    parser_errors: list[str] = []
    try:
        config["forward_env"] = _parse_forward_env()
    except ValueError:
        parser_errors.append("forward_env")
    try:
        config["workspace_startup_timeout"] = _parse_startup_timeout()
    except ValueError:
        parser_errors.append("workspace_startup_timeout")

    invalid = parser_errors + _invalid_config_fields(config)
    return config, list(dict.fromkeys(invalid))


def _load_coder_environment_config() -> dict[str, Any]:
    """Load and validate Coder settings from the active process environment."""
    config, invalid = _environment_config_and_invalid_fields()
    if invalid:
        raise ValueError(
            "Coder environment config has invalid fields: " + ", ".join(invalid)
        )
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
    **_kwargs: Any,
) -> CoderEnvironment:
    """Build one Coder environment from the upstream environment-only contract."""
    config = _load_coder_environment_config()
    return CoderEnvironment(
        base_url=config["base_url"],
        task_id=task_id,
        api_key=config["api_key"],
        workspace_name=config["workspace_name"],
        cwd=_remote_cwd(cwd),
        timeout=timeout,
        forward_env=config["forward_env"],
        workspace_startup_timeout=config["workspace_startup_timeout"],
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

    def is_available(self) -> bool:
        _, invalid = _environment_config_and_invalid_fields()
        return not invalid

    def check_requirements(self, _config: dict[str, Any]) -> bool:
        _, invalid = _environment_config_and_invalid_fields()
        if invalid:
            logger.error(
                "Coder backend has invalid environment fields: %s",
                ", ".join(invalid),
            )
            return False
        return True

    def probe(self) -> tuple[str, str]:
        _, invalid = _environment_config_and_invalid_fields()
        if invalid:
            return (
                "needs_setup",
                f"Configure Coder environment variables: {', '.join(invalid)}.",
            )
        return ("ready", "")

    def setup_instructions(self) -> list[str]:
        return [
            "Set CODER_URL, CODER_API_KEY, and CODER_WORKSPACE in the active profile environment.",
            "Optionally set TERMINAL_CODER_FORWARD_ENV and TERMINAL_CODER_WORKSPACE_STARTUP_TIMEOUT.",
        ]

    def create_environment(
        self,
        *,
        cwd: str,
        timeout: int,
        task_id: str = "default",
        image: str | None = None,
        container_config: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> CoderEnvironment:
        return create_coder_environment(
            cwd=cwd,
            timeout=timeout,
            task_id=task_id,
            image=image,
            container_config=container_config,
            **kwargs,
        )


def register(ctx) -> None:
    """Register the Coder terminal environment provider with Hermes."""
    ctx.register_terminal_environment_provider(CoderTerminalEnvironmentProvider())
