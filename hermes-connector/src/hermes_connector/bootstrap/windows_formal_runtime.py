"""Compatibility alias for the verified Windows formal runtime builder."""

from hermes_connector.bootstrap.windows import (
    build_windows_runtime as build_windows_formal_runtime,
)

__all__ = ["build_windows_formal_runtime"]
