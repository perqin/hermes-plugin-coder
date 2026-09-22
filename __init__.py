"""Hermes source-plugin entry point for the Coder terminal backend."""

if __package__:
    from .hermes_plugin_coder.plugin import register
else:  # Direct source-tree import (pytest and local development).
    from hermes_plugin_coder.plugin import register

__all__ = ["register"]
