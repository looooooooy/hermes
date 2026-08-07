"""Windows Connector platform capability boundary.

Concrete adapters are imported directly by the Windows composition root only
after the platform is declared fully available. Keeping the package root light
prevents Linux/macOS capability probes from importing Windows-only stdlib or
Win32 bindings while the platform remains fail-closed.
"""

from hermes_connector.adapters.platform.windows.availability import AVAILABILITY

__all__ = ["AVAILABILITY"]
