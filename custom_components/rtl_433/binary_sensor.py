"""Binary-sensor platform for the rtl_433 hub config entry.

``async_setup_entry`` runs once for the hub config entry and delegates to the
shared :func:`~custom_components.rtl_433.entity.async_setup_hub_platform`
helper, which resolves the hub coordinator, builds a
:class:`Rtl433BinarySensor` for every device's observed mapped fields whose
descriptor ``platform == "binary_sensor"`` (battery, tamper, contact/reed,
alarm, leak), adds new devices/fields at runtime, and keeps the hub's devices
map current.

Raw values are converted to ``True``/``False`` via ``mapping.apply_transform``,
which applies the descriptor's ``payload`` mapping including the ``battery_ok``
inversion.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later

from .const import DOMAIN
from .entity import Rtl433Entity, Rtl433HubEntity, async_setup_hub_platform
from .mapping import apply_transform

if TYPE_CHECKING:
    from .coordinator import Rtl433Coordinator
    from .mapping import FieldDescriptor

# This platform owns only descriptors whose ``platform`` attribute equals this.
PLATFORM = "binary_sensor"


class Rtl433BinarySensor(Rtl433Entity, BinarySensorEntity):
    """A single boolean state of an rtl_433 device.

    For descriptors carrying a ``clear_delay`` (Z2M occupancy-timeout behaviour),
    the sensor turns ``on`` per detection and synthesizes the ``off`` after the
    effective delay: every fresh detection cancels and reschedules the pending
    timer, so the window restarts on each retrigger.
    """

    def __init__(
        self,
        coordinator: Rtl433Coordinator,
        hub_entry_id: str,
        device_key: str,
        model: str,
        descriptor: FieldDescriptor,
    ) -> None:
        """Initialize binary-sensor-specific description fields."""
        super().__init__(coordinator, hub_entry_id, device_key, model, descriptor)
        self._attr_device_class = descriptor.device_class
        self._attr_force_update = descriptor.force_update

        # Pending synthesized-clear timer unsub handle (clear_delay descriptors).
        self._clear_unsub: CALLBACK_TYPE | None = None

        # Seed from the coordinator's last event if it already carries the field.
        # ``hass`` is not set yet, so any clear timer is (re)started in
        # ``async_added_to_hass`` from the seeded ``is_on``.
        last_event = coordinator.devices.get(device_key)
        if last_event is not None and descriptor.field_key in last_event.fields:
            self._apply_value(last_event.fields[descriptor.field_key])

    def _apply_value(self, raw_value: Any) -> None:
        """Map a raw value to on/off via the descriptor's payload mapping.

        ``apply_transform`` returns ``True``/``False`` (honoring ``payload`` and
        the ``battery_ok`` inversion) or ``None`` when the value matches neither
        token; ``None`` leaves the state unknown.

        For a ``clear_delay`` descriptor, a fresh ``on`` (re)starts the
        synthesized-clear timer. Scheduling is guarded until ``hass`` is set
        (seeding in ``__init__`` runs before the entity is added); the initial
        schedule then happens in :meth:`async_added_to_hass`.
        """
        self._attr_is_on = apply_transform(self._descriptor, raw_value)
        if (
            self._descriptor.clear_delay is not None
            and self._attr_is_on is True
            and self.hass is not None
        ):
            self._schedule_clear()

    async def async_added_to_hass(self) -> None:
        """Start the clear timer if seeded/restored ``on`` before add."""
        await super().async_added_to_hass()
        if self._descriptor.clear_delay is not None and self._attr_is_on is True:
            self._schedule_clear()

    async def async_will_remove_from_hass(self) -> None:
        """Cancel the pending clear timer so it never writes after removal."""
        self._cancel_clear()
        await super().async_will_remove_from_hass()

    def _effective_clear_delay(self) -> int:
        """Resolve the clear delay (per-device override -> descriptor default)."""
        resolver = self._coordinator.effective_clear_delay_resolver
        if resolver is not None:
            try:
                resolved = resolver(self._device_key)
            except Exception:  # noqa: BLE001 - fall back to the descriptor default
                resolved = None
            if resolved is not None:
                return resolved
        return self._descriptor.clear_delay

    def _schedule_clear(self) -> None:
        """Cancel any pending clear and arm a fresh one-shot off timer."""
        self._cancel_clear()
        delay = self._effective_clear_delay()
        self._clear_unsub = async_call_later(self.hass, delay, self._clear)

    def _cancel_clear(self) -> None:
        """Cancel the pending synthesized-clear timer, if armed."""
        if self._clear_unsub is not None:
            self._clear_unsub()
            self._clear_unsub = None

    @callback
    def _clear(self, _now: Any) -> None:
        """Synthesize the auto-off: turn the sensor off and write state."""
        self._clear_unsub = None
        self._attr_is_on = False
        self.async_write_ha_state()

    async def _async_restore_state(self) -> None:
        """Restore the last known on/off state on startup.

        A live value already seeded from the coordinator's last event wins over a
        restored one. For a ``clear_delay`` descriptor a stale ``on`` is never
        restored (it would otherwise linger with no timer to clear it): the
        sensor comes back off/unknown until the next detection.
        """
        if self._descriptor.clear_delay is not None:
            return
        if self._attr_is_on is not None:
            return
        last_state = await self.async_get_last_state()
        if last_state is None or last_state.state in (None, "unknown", "unavailable"):
            return
        self._attr_is_on = last_state.state == "on"


class Rtl433HubConnectivity(Rtl433HubEntity, BinarySensorEntity):
    """Reports whether the hub's WebSocket connection is currently open."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "Connectivity"

    def __init__(self, coordinator: Rtl433Coordinator, hub_entry_id: str) -> None:
        """Initialize the connectivity entity with a stable unique_id."""
        super().__init__(coordinator, hub_entry_id)
        self._attr_unique_id = f"{hub_entry_id}:hub:connectivity"

    @property
    def is_on(self) -> bool:
        """Return True while the hub's WebSocket connection is open."""
        return self._coordinator.connected

    @property
    def available(self) -> bool:
        """Always available: the entity reports connection state itself."""
        return True


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up rtl_433 binary sensors for every device under the hub entry."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([Rtl433HubConnectivity(coordinator, entry.entry_id)])
    await async_setup_hub_platform(
        hass, entry, async_add_entities, PLATFORM, Rtl433BinarySensor
    )
