"""Business API deployment entrypoint."""

from . import bootstrap
from .bootstrap import app, create_app

__all__ = ["app", "create_app"]

del bootstrap
