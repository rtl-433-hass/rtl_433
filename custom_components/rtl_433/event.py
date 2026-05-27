"""Event platform for the rtl_433 hub config entry.

``async_setup_entry`` runs once for the hub config entry and delegates to the
shared :func:`~custom_components.rtl_433.entity.async_setup_hub_platform`
helper, which resolves the hub coordinator, builds a :class:`Rtl433Event` for
every device's observed mapped fields whose descriptor ``platform == "event"``,
adds new devices/fields at runtime, and keeps the hub's devices map current.

Each :class:`Rtl433Event` fires a Home Assistant event once per genuine
transmission, with the stringified field value as the ``event_type`` and no
extra attributes. It auto-populates ``event_types`` from observed values,
persists them, dedupes the coordinator watchdog's re-dispatch by object
identity, stays always available, and does not replay the coordinator's last
event on construction.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.event import EventEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_DEVICES, DEVICE_EVENT_TYPES
from .entity import Rtl433Entity, async_setup_hub_platform, async_upsert_event_types

if TYPE_CHECKING:
    from .coordinator import Rtl433Coordinator
    from .mapping import FieldDescriptor
    from .normalizer import NormalizedEvent

# This platform owns only descriptors whose ``platform`` attribute equals this.
PLATFORM = "event"


class Rtl433Event(Rtl433Entity, EventEntity):
    """A momentary event field of an rtl_433 device (e.g. a remote button).

    Fires once per genuine transmission with ``str(value)`` as the event type.
    Diverges from the base entity in four places: it overrides
    ``_handle_dispatch`` (dedupe by object identity, then fire), ``available``
    (always True), ``_async_restore_state`` (no-op; HA's ``EventEntity`` restores
    the last displayed event), and seeds ``_attr_event_types`` /
    ``_attr_device_class`` in ``__init__``.
    """

    def __init__(
        self,
        coordinator: Rtl433Coordinator,
        hub_entry_id: str,
        device_key: str,
        model: str,
        descriptor: FieldDescriptor,
    ) -> None:
        """Initialize event-specific description fields from persisted state."""
        super().__init__(coordinator, hub_entry_id, device_key, model, descriptor)
        # ``EventEntity.device_class`` accepts the plain string from the
        # descriptor (an ``EventDeviceClass`` member value or ``None``).
        self._attr_device_class = descriptor.device_class
        # Seed ``event_types`` with a COPY of the persisted list so in-place
        # growth never mutates the persisted dict. HA's ``@final``
        # ``capability_attributes`` reads ``event_types`` and raises if unset, so
        # this must be set in ``__init__`` (an empty list is valid). Do NOT seed
        # from ``coordinator.devices`` — replaying on construction would fire a
        # stale event before the entity is added to hass.
        persisted = (
            coordinator.entry.data.get(CONF_DEVICES, {})
            .get(device_key, {})
            .get(DEVICE_EVENT_TYPES, {})
            .get(descriptor.field_key, [])
        )
        self._attr_event_types = list(persisted)
        self._last_fired_event: NormalizedEvent | None = None

    @callback
    def _handle_dispatch(self, event: NormalizedEvent) -> None:
        """Fire an HA event once per genuine transmission.

        The coordinator's watchdog re-dispatches the cached last event by the
        same object reference when a device goes stale; a live transmission is a
        fresh ``normalize()`` object. So dedupe by object identity (``is``), not
        value-equality: a genuine repeat of the same value is a distinct object
        that must fire (a doorbell pressed twice in 30 s fires twice).
        """
        # Watchdog re-dispatch of the cached last event -> same object -> don't
        # re-fire; just re-read availability.
        if event is self._last_fired_event:
            self.async_write_ha_state()
            return
        field_key = self._descriptor.field_key
        if field_key in event.fields:
            self._last_fired_event = event
            event_type = str(event.fields[field_key])
            # Append a newly-seen type BEFORE firing (HA validates against the
            # current list) and schedule persistence (callback-safe).
            if event_type not in self._attr_event_types:
                self._attr_event_types.append(event_type)
                self.hass.async_create_task(
                    async_upsert_event_types(
                        self.hass,
                        self._coordinator.entry,
                        self._device_key,
                        field_key,
                        [event_type],
                    )
                )
            self._trigger_event(event_type)  # no attributes (YAGNI)
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        """Always available: events are momentary, so timeout-based
        unavailability would hide the entity almost always (mirrors the
        Last-seen sensor)."""
        return True

    async def _async_restore_state(self) -> None:
        """No-op: HA's ``EventEntity.async_internal_added_to_hass`` restores the
        last displayed event; there is no steady measurement state to restore."""

    def _apply_value(self, raw_value: Any) -> None:
        """No-op: ``Rtl433Event`` overrides ``_handle_dispatch`` and never calls
        this."""


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up rtl_433 event entities for every device under the hub entry."""
    await async_setup_hub_platform(
        hass, entry, async_add_entities, PLATFORM, Rtl433Event
    )
