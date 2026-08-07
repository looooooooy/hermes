"""Runtime identity and health primitives.

This package intentionally contains only runtime boundary objects.
It must not own Agent execution or SessionDB state.
"""

from .descriptor import RuntimeDescriptor
from .health import RuntimeHealth, RuntimeHealthState

__all__ = ["RuntimeDescriptor", "RuntimeHealth", "RuntimeHealthState"]
