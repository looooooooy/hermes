"""Async Worker deployment entrypoint."""

from . import app, bootstrap
from .bootstrap import create_worker, worker

__all__ = ["create_worker", "worker"]

del app, bootstrap
