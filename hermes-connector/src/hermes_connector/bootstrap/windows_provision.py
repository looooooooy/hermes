from __future__ import annotations

from pathlib import Path

from hermes_connector.adapters.platform.windows.private_state import (
    ensure_private_directory,
)
from hermes_connector.bootstrap.settings import ConnectorRuntimeSettings


def provision_windows_runtime_state(settings: ConnectorRuntimeSettings) -> Path:
    """Create only the current-user private Windows state root for explicit setup."""

    if settings.credential_store != "dpapi":
        raise ValueError("Windows runtime provisioning requires DPAPI credentials")
    return ensure_private_directory(settings.state_directory)


__all__ = ["provision_windows_runtime_state"]
