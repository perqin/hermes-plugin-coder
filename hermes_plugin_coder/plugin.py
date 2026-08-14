"""Registration and factory adapter for the Coder terminal backend."""

from __future__ import annotations

import json
import os
from typing import Any

from tools.environments import (
    BackendCapabilities,
    BackendDefinition,
    BackendFactoryRequest,
    ExecutionLocation,
    FilesystemSemantics,
)

from .backend import CoderEnvironment

_REQUIRED_ENV = ("CODER_URL", "CODER_API_KEY", "CODER_WORKSPACE")


def _missing_required_env() -> list[str]:
    return [name for name in _REQUIRED_ENV if not (os.getenv(name) or "").strip()]


def coder_backend_available() -> bool:
    """Return whether mandatory Coder connection configuration is present."""
    return not _missing_required_env()


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


def resolve_coder_config() -> dict[str, Any]:
    """Resolve Coder-specific environment config before factory invocation."""
    missing = _missing_required_env()
    if missing:
        raise ValueError("Coder backend requires " + ", ".join(missing))
    return {
        "base_url": os.environ["CODER_URL"],
        "api_key": os.environ["CODER_API_KEY"],
        "workspace_name": os.environ["CODER_WORKSPACE"],
        "forward_env": _parse_forward_env(),
        "workspace_startup_timeout": _parse_startup_timeout(),
    }


def _remote_cwd(request: BackendFactoryRequest) -> str:
    if request.cwd in {"", "/root"}:
        return "~"
    return request.cwd


def create_coder_environment(request: BackendFactoryRequest) -> CoderEnvironment:
    """Build one Coder environment from host-owned and backend-specific config."""
    config = request.backend_config
    required = ("base_url", "api_key", "workspace_name")
    invalid = [
        name
        for name in required
        if not isinstance(config.get(name), str) or not config[name].strip()
    ]
    if invalid:
        raise ValueError(
            "Coder backend config requires non-empty strings for " + ", ".join(invalid)
        )

    forward_env = config.get("forward_env", [])
    if not isinstance(forward_env, list) or any(
        not isinstance(item, str) for item in forward_env
    ):
        raise ValueError("Coder backend config forward_env must be a list of strings")
    startup_timeout = config.get("workspace_startup_timeout", 180)
    if (
        not isinstance(startup_timeout, int)
        or isinstance(startup_timeout, bool)
        or startup_timeout <= 0
    ):
        raise ValueError(
            "Coder backend config workspace_startup_timeout must be a positive integer"
        )

    return CoderEnvironment(
        base_url=config["base_url"],
        task_id=request.task_id,
        api_key=config["api_key"],
        workspace_name=config["workspace_name"],
        cwd=_remote_cwd(request),
        timeout=request.timeout,
        forward_env=forward_env,
        workspace_startup_timeout=startup_timeout,
    )


def coder_backend_definition() -> BackendDefinition:
    """Return the static descriptor registered with Hermes' backend registry."""
    return BackendDefinition(
        name="coder",
        label="Coder",
        description="a remote Coder workspace (likely Linux)",
        default_cwd="~",
        factory=create_coder_environment,
        availability_check=coder_backend_available,
        capabilities=BackendCapabilities(
            execution_location=ExecutionLocation.REMOTE,
            filesystem_semantics=FilesystemSemantics.ISOLATED,
            accepts_host_cwd=False,
            requires_sandbox_cwd=True,
            supports_image=False,
            supports_resource_limits=False,
            supports_pty=True,
            supports_background_processes=True,
            supports_file_transfer=False,
            supports_persistence=True,
        ),
        config_schema={
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
        },
        config_resolver=resolve_coder_config,
        install_hint=(
            "Set CODER_URL, CODER_API_KEY, and CODER_WORKSPACE in the active "
            "Hermes profile environment."
        ),
        diagnostic_metadata={"transport": "coder-rest-pty"},
    )


def register(ctx) -> None:
    """Register the Coder descriptor; resource creation remains host-owned."""
    ctx.register_terminal_backend(coder_backend_definition())
