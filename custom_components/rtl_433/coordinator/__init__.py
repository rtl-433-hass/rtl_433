"""WebSocket coordinator package for the rtl_433 integration.

Re-exports the push coordinator, its pending-device record, the cap on how many
candidates are held at once, and its connectivity-check error so callers (the
integration setup in ``__init__.py``, the config flow, the options flow and the
location aggregator) can import from ``custom_components.rtl_433.coordinator``
directly.
"""

from __future__ import annotations

from ._events import MAX_PENDING_CANDIDATES
from .base import CannotConnect, PendingDevice, Rtl433Coordinator

__all__ = [
    "CannotConnect",
    "MAX_PENDING_CANDIDATES",
    "PendingDevice",
    "Rtl433Coordinator",
]
