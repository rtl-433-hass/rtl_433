"""Sensor platform for rtl_433 per-device config entries.

``async_setup_entry`` runs for a *device* config entry and delegates to the
shared :func:`~custom_components.rtl_433.entity.async_setup_device_platform`
helper, which resolves the parent hub coordinator, builds a :class:`Rtl433Sensor`
for every observed mapped field whose descriptor ``platform == "sensor"``,
persists the observed-field set, and wires dynamic creation of new sensors when
a previously unseen mapped field first arrives (Clarification #9).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import Rtl433Entity, async_setup_device_platform
from .mapping import apply_transform

if TYPE_CHECKING:
    from .coordinator import Rtl433Coordinator
    from .mapping import FieldDescriptor

# This platform owns only descriptors whose ``platform`` attribute equals this.
PLATFORM = "sensor"

# States that should not overwrite a fresh value when restoring.
_NON_RESTORABLE = (None, "unknown", "unavailable")


class Rtl433Sensor(Rtl433Entity, SensorEntity):
    """A single measurement field of an rtl_433 device."""

    def __init__(
        self,
        coordinator: Rtl433Coordinator,
        hub_entry_id: str,
        device_key: str,
        model: str,
        descriptor: FieldDescriptor,
    ) -> None:
        """Initialize sensor-specific description fields."""
        super().__init__(coordinator, hub_entry_id, device_key, model, descriptor)
        self._attr_device_class = descriptor.device_class
        self._attr_state_class = descriptor.state_class
        self._attr_native_unit_of_measurement = descriptor.unit_of_measurement
        self._attr_force_update = descriptor.force_update

        # Seed from the coordinator's last event if it already carries the field
        # (covers a device that transmitted before its entity was added).
        last_event = coordinator.devices.get(device_key)
        if last_event is not None and descriptor.field_key in last_event.fields:
            self._apply_value(last_event.fields[descriptor.field_key])

    def _apply_value(self, raw_value: Any) -> None:
        """Transform and store a raw value as the sensor's native value."""
        self._attr_native_value = apply_transform(self._descriptor, raw_value)

    async def _async_restore_state(self) -> None:
        """Restore the last known native value on startup.

        A live value already seeded from the coordinator's last event wins over a
        restored one.
        """
        if self._attr_native_value is not None:
            return
        last_state = await self.async_get_last_state()
        if last_state is not None and last_state.state not in _NON_RESTORABLE:
            self._attr_native_value = last_state.state


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up rtl_433 sensors for one per-device config entry."""
    await async_setup_device_platform(
        hass, entry, async_add_entities, PLATFORM, Rtl433Sensor
    )
