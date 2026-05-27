"""Number control platform for the rtl_433 hub config entry.

``async_setup_entry`` runs once for the hub config entry. When the hub's
``manage_settings`` toggle is off it creates **no** entities and returns
immediately, so the integration only ever forwards to a platform that has
something to register. When management is on it statically registers one
:class:`Rtl433NumberControl` per ``number``-platform field in the
:data:`~custom_components.rtl_433.sdr_settings.SDR_SETTINGS` registry whose
capability gate is satisfied: center frequency, sample rate, frequency
correction (ppm), gain (dB), and hop interval.

Optimistic-then-confirmed state: ``async_set_native_value`` writes the desired
value via ``coordinator.set_sdr`` (which persists, sends the ``/cmd``, then
reads the server back and emits ``signal_hub_update``). Until that read-back
arrives ``native_value`` shows the just-set desired value (optimistic); the
inherited ``signal_hub_update`` subscription then repaints the control with the
server's confirmed value.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import Rtl433HubControl
from .sdr_settings import SDR_SETTINGS

if TYPE_CHECKING:
    from .coordinator import Rtl433Coordinator
    from .sdr_settings import SdrSetting

# This platform owns only registry settings whose ``platform`` equals this.
PLATFORM = "number"


class Rtl433NumberControl(Rtl433HubControl, NumberEntity):
    """A managed numeric SDR setting exposed as a hub-device Number entity."""

    def __init__(
        self,
        coordinator: Rtl433Coordinator,
        hub_entry_id: str,
        setting: SdrSetting,
    ) -> None:
        """Initialize number-specific description fields from the setting."""
        super().__init__(coordinator, hub_entry_id, setting)
        self._attr_native_min_value = setting.native_min
        self._attr_native_max_value = setting.native_max
        self._attr_native_step = setting.native_step
        self._attr_native_unit_of_measurement = setting.native_unit
        self._attr_mode = setting.mode or NumberMode.BOX
        self._attr_device_class = setting.device_class

    @property
    def native_value(self) -> Any:
        """Return the desired value, falling back to the server's actual value."""
        desired = self._coordinator.get_desired(self._setting.key)
        if desired is not None:
            return desired
        return self._setting.read(self._coordinator.meta)

    async def async_set_native_value(self, value: float) -> None:
        """Write the desired value through the coordinator (optimistic)."""
        await self._coordinator.set_sdr(self._setting.key, value)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Register the hub's managed Number controls (only when managing)."""
    coordinator: Rtl433Coordinator = hass.data[DOMAIN][entry.entry_id]
    if not coordinator.manage_settings:
        return
    async_add_entities(
        Rtl433NumberControl(coordinator, entry.entry_id, setting)
        for setting in SDR_SETTINGS
        if setting.platform == PLATFORM and setting.capability(coordinator.meta)
    )
