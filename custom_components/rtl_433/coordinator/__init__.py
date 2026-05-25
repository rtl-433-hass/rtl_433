"""WebSocket coordinator package for the rtl_433 integration.

Re-exports the push coordinator and its connectivity-check error so callers
(the integration setup in ``__init__.py`` and the config flow) can import from
``custom_components.rtl_433.coordinator`` directly.
"""

from __future__ import annotations

from .base import CannotConnect, Rtl433Coordinator

__all__ = ["CannotConnect", "Rtl433Coordinator"]
