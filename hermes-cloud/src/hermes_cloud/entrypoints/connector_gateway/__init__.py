"""Connector Gateway deployment entrypoint."""

from . import bootstrap
from .app import ConnectorGatewayApplication
from .bootstrap import app, create_app, decode_connector_frame

__all__ = [
    "ConnectorGatewayApplication",
    "app",
    "create_app",
    "decode_connector_frame",
]

del bootstrap
