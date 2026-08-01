"""Coder terminal backend plugin package."""

from .backend import CoderEnvironment, coder_workspace_exists
from .plugin import (
    coder_backend_definition,
    create_coder_environment,
    register,
    resolve_coder_config,
)

__all__ = [
    "CoderEnvironment",
    "coder_backend_definition",
    "coder_workspace_exists",
    "create_coder_environment",
    "register",
    "resolve_coder_config",
]
