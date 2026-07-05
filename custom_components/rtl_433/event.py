"""Event platform for the rtl_433 hub config entry.

``async_setup_entry`` runs once for the hub config entry and delegates to the
shared :func:`~custom_components.rtl_433.entity.async_setup_hub_platform`
helper, which resolves the hub coordinator, builds a :class:`Rtl433Event` for
every device's observed mapped fields whose descriptor ``platform == "event"``,
adds new devices/fields at runtime, and keeps the hub's devices map current.

Each :class:`Rtl433Event` fires a Home Assistant event once per genuine
transmission, with the field value resolved to an ``event_type`` and no extra
attributes. By default the ``event_type`` is the stringified field value; when
the descriptor declares an ``event_map`` the raw value is mapped to a named
type instead (e.g. a doorbell's ``0``/``1`` map to ``ring``/``secret_knock``).
It seeds ``event_types`` from the declared map values, auto-populates further
observed values, persists them, ignores the coordinator watchdog's availability
re-paint (``is_repaint``) so a stale cached value never re-fires, stays always
available, and does not replay the coordinator's last event on construction. A
doorbell-class entity always advertises ``DoorbellEventType.RING`` to satisfy
Home Assistant's doorbell standard.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.event import (
    DoorbellEventType,
    EventDeviceClass,
    EventEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import CONF_DEVICES, DEVICE_EVENT_TYPES, LOGGER
from .entity import Rtl433Entity, async_setup_hub_platform, async_upsert_event_types

if TYPE_CHECKING:
    from pyrtl_433.normalizer import NormalizedEvent

    from .coordinator import Rtl433Coordinator
    from .mapping import FieldDescriptor

# This platform owns only descriptors whose ``platform`` attribute equals this.
PLATFORM = "event"


class Rtl433Event(Rtl433Entity, EventEntity):
    """A momentary event field of an rtl_433 device (e.g. a remote button).

    Fires once per genuine transmission. The event type is ``str(value)`` unless
    the descriptor declares an ``event_map``, in which case the raw value is
    mapped to a named type (unmapped values still pass through as ``str(value)``).
    Diverges from the base entity in five places: it overrides
    ``_handle_dispatch`` (ignore watchdog re-paints, suppress replays, then fire
    the resolved type), ``available`` (always True), ``_async_restore_state``
    (no-op; HA's ``EventEntity`` restores the last displayed event),
    ``async_added_to_hass`` (also persists the declared ``event_map`` types), and
    seeds ``_attr_event_types`` / ``_attr_device_class`` in ``__init__``.
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
        # Seed ``event_types`` from the descriptor's declared ``event_map`` types
        # (declared first, stable order) unioned with the persisted list. HA's
        # ``@final`` ``capability_attributes`` reads ``event_types`` and raises if
        # unset, so this must be set in ``__init__`` (an empty list is valid). Do
        # NOT seed from ``coordinator.devices`` — replaying on construction would
        # fire a stale event before the entity is added to hass.
        persisted = (
            coordinator.entry.data.get(CONF_DEVICES, {})
            .get(device_key, {})
            .get(DEVICE_EVENT_TYPES, {})
            .get(descriptor.field_key, [])
        )
        # De-duplicate the declared map values while preserving insertion order
        # (``dict.fromkeys`` keeps first-seen order), then append any persisted
        # types not already declared. This is a fresh list, so in-place growth in
        # ``_handle_dispatch`` never mutates the persisted dict.
        declared = list(dict.fromkeys((descriptor.event_map or {}).values()))
        seed = declared + [t for t in persisted if t not in declared]
        # A doorbell-class entity must advertise ``DoorbellEventType.RING`` or HA
        # logs a deprecation warning (and the entity stops working in 2027.4);
        # guarantee it is present even if no map supplied one.
        if (
            descriptor.device_class == EventDeviceClass.DOORBELL
            and DoorbellEventType.RING not in seed
        ):
            seed.insert(0, DoorbellEventType.RING)
        self._attr_event_types = seed

    async def async_added_to_hass(self) -> None:
        """Subscribe to updates and persist the declared ``event_map`` types.

        Persisting the declared types on add (rather than only on first observed
        press) means ``device_trigger``'s persisted-preferred lookup lists the
        mapped subtypes (e.g. ``ring`` / ``secret_knock``) across restarts even
        before any press. ``async_upsert_event_types`` is idempotent (it only
        writes when the stored set grows), so re-adding is a no-op.
        """
        await super().async_added_to_hass()

        declared = list(dict.fromkeys((self._descriptor.event_map or {}).values()))
        if declared:
            self.hass.async_create_task(
                async_upsert_event_types(
                    self.hass,
                    self._coordinator.entry,
                    self._device_key,
                    self._descriptor.field_key,
                    declared,
                )
            )

    @callback
    def _handle_dispatch(self, event: NormalizedEvent) -> None:
        """Fire an HA event once per genuine transmission.

        A genuine repeat of the same value is a distinct live transmission that
        must fire (a doorbell pressed twice in 30 s fires twice), so firing keys
        off the frame's classification, not value-equality.

        Two classes of dispatch are *not* transmissions and must never fire:

        * A watchdog availability re-paint (``event.is_repaint``) re-dispatches the
          device's cached last frame only so measurement entities re-read
          availability. Its value is stale (e.g. a doorbell's last
          ``secret_knock=0`` -> ``ring``), so firing it would emit a phantom event.
        * A replayed / stale frame (``event.is_replay``) is the reconnect-replay
          case: it must NOT fire, append to ``event_types``, or persist — but it
          still writes state (which only re-reads ``available``, always ``True``
          for events). A suppressed transmission that *would* have fired is logged
          at DEBUG (it happens routinely on every reconnect).
        """
        field_key = self._descriptor.field_key
        if event.is_repaint:
            # The availability watchdog re-dispatched the device's cached last
            # frame purely to re-paint availability. The cached value is stale (it
            # is whatever the device last transmitted, e.g. a doorbell's last
            # ``secret_knock=0`` -> ``ring``), so firing it would emit a phantom
            # event. Events are always available, so there is nothing to repaint --
            # just re-read state and return without firing or persisting.
            LOGGER.debug(
                "rtl_433 skipped watchdog re-paint for %s (no re-fire)",
                self._device_key,
            )
            self.async_write_ha_state()
            return
        if event.is_replay:
            if field_key in event.fields:
                value = event.fields[field_key]
                event_time = event.event_time
                age = (
                    f"{(dt_util.utcnow() - event_time).total_seconds():.0f}s"
                    if event_time is not None
                    else "unknown"
                )
                LOGGER.debug(
                    "rtl_433 ignored an old/duplicate %s reading '%s' for %s "
                    "(model %s, reading time %s, age %s)",
                    field_key,
                    str(value),
                    self._device_key,
                    self._coordinator.devices[self._device_key].model
                    if self._device_key in self._coordinator.devices
                    else "",
                    event_time.isoformat() if event_time is not None else "unknown",
                    age,
                )
            self.async_write_ha_state()
            return
        if field_key in event.fields:
            # Resolve the event type: a declared ``event_map`` maps the raw value
            # to a named type (e.g. doorbell ``0``/``1`` -> ``ring``/
            # ``secret_knock``); unmapped values and the no-map button path fall
            # back to the stringified raw value unchanged.
            raw = event.fields[field_key]
            event_map = self._descriptor.event_map
            event_type = event_map.get(str(raw), str(raw)) if event_map else str(raw)
            LOGGER.debug(
                "rtl_433 fired %s for %s field=%s value=%s",
                event_type,
                self._device_key,
                field_key,
                raw,
            )
            # Append a newly-seen type BEFORE firing (HA validates against the
            # current list) and schedule persistence (callback-safe).
            if event_type not in self._attr_event_types:
                LOGGER.debug(
                    "rtl_433 %s registered new event_type %s",
                    self._device_key,
                    event_type,
                )
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
            self._trigger_event(event_type)  # the type is the whole payload
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
