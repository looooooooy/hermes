"""Server-authoritative session projection domain and repository boundaries."""

from hermes_cloud.modules.projection.domain import (
    ProjectionConflict,
    ProjectionRegression,
    ProjectionTenantMismatch,
    ProjectionWriteResult,
    SessionEventProjection,
    SessionMessageProjection,
    SessionProjection,
)

__all__ = (
    "ProjectionConflict",
    "ProjectionRegression",
    "ProjectionTenantMismatch",
    "ProjectionWriteResult",
    "SessionEventProjection",
    "SessionMessageProjection",
    "SessionProjection",
)
