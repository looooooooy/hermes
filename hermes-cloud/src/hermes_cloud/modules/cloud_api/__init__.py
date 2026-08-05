"""External Cloud P0 compatibility application."""

from hermes_cloud.modules.cloud_api.adapters.fastapi import (
    BusinessApiApplication,
    build_fastapi_application,
)

__all__ = ("BusinessApiApplication", "build_fastapi_application")
