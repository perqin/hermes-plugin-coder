"""Coder terminal environment provider plugin package."""

from .backend import CoderEnvironment, coder_workspace_exists
from .plugin import (
    CoderTerminalEnvironmentProvider,
    create_coder_environment,
    register,
    resolve_coder_config,
)

__all__ = [
    "CoderEnvironment",
    "CoderTerminalEnvironmentProvider",
    "coder_workspace_exists",
    "create_coder_environment",
    "register",
    "resolve_coder_config",
]
