"""Connector use cases."""

from hermes_connector.application.capability_negotiation import (
    RequiredCapabilityUnavailable,
    negotiate_local_capabilities,
)
from hermes_connector.application.service_runner import (
    ServiceRunner,
    ServiceState,
)
from hermes_connector.application.supervisor import (
    Supervisor,
    SupervisorPhase,
    SupervisorStartError,
    SupervisorStopError,
)

__all__ = [
    "RequiredCapabilityUnavailable",
    "ServiceRunner",
    "ServiceState",
    "Supervisor",
    "SupervisorPhase",
    "SupervisorStartError",
    "SupervisorStopError",
    "negotiate_local_capabilities",
]
