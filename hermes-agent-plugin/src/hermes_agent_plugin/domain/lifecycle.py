r"""Domain state for the Local Gateway runtime lifecycle.

State changes are deliberately small and explicit:

    NEW -> INSTALLED -> STARTING -> READY -> DRAINING -> STOPPING -> STOPPED
                               \---------------> STOPPING
                    \--------------------------> STOPPING
                                                              |
                                STARTING <---------------------+

Allowed transitions:

    NEW        : INSTALLED
    INSTALLED  : STARTING, STOPPING
    STARTING   : READY, STOPPING
    READY      : DRAINING
    DRAINING   : STOPPING
    STOPPING   : STOPPED
    STOPPED    : STARTING
"""

from __future__ import annotations

from enum import Enum


class GatewayState(str, Enum):
    NEW = "new"
    INSTALLED = "installed"
    STARTING = "starting"
    READY = "ready"
    DRAINING = "draining"
    STOPPING = "stopping"
    STOPPED = "stopped"


ALLOWED_LIFECYCLE_TRANSITIONS = {
    GatewayState.NEW: frozenset({GatewayState.INSTALLED}),
    GatewayState.INSTALLED: frozenset({GatewayState.STARTING, GatewayState.STOPPING}),
    GatewayState.STARTING: frozenset({GatewayState.READY, GatewayState.STOPPING}),
    GatewayState.READY: frozenset({GatewayState.DRAINING}),
    GatewayState.DRAINING: frozenset({GatewayState.STOPPING}),
    GatewayState.STOPPING: frozenset({GatewayState.STOPPED}),
    GatewayState.STOPPED: frozenset({GatewayState.STARTING}),
}


class LifecycleError(RuntimeError):
    """Base class for lifecycle failures without payload-bearing context."""


class LifecycleNotReady(LifecycleError):
    """Raised when a local handshake is attempted outside READY."""


class LifecycleCancelled(LifecycleError):
    """Raised when cooperative cancellation interrupts startup."""


class LifecycleDeadlineExceeded(LifecycleError):
    """Raised when a bounded lifecycle operation reaches its deadline."""


class LifecycleTransitionError(LifecycleError):
    """Raised when a caller asks for an undocumented state transition."""
