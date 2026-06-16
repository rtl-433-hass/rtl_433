"""Switch control platform for the rtl_433 hub config entry.

``async_setup_entry`` runs once for the hub config entry. When the hub's
``manage_settings`` toggle is off it creates **no** entities and returns
immediately; when management is on it statically registers one
:class:`Rtl433SwitchControl` per ``switch``-platform field in the
:data:`~custom_components.rtl_433.sdr_settings.SDR_SETTINGS` registry whose
capability gate is satisfied. Today that is the "Auto gain" switch.

Auto gain is the boolean half of the clarified gain pair: it shares the ``gain``
``/cmd`` with the gain-dB Number, but stores its own ``gain_auto`` desired key.
When on, the coordinator composes an empty ``arg`` (auto); when off, it sends the
paired dB value. The gain-dB Number still exists while Auto is on — its value
simply is not sent.

Optimistic-then-confirmed state: ``async_turn_on`` / ``async_turn_off`` write the
``gain_auto`` desired value via ``coordinator.set_sdr`` (persist + send +
read-back + ``signal_hub_update``). Until the read-back arrives ``is_on`` shows
the just-set desired value (optimistic); the inherited ``signal_hub_update``
subscription then repaints the control with the server's confirmed value.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import Rtl433HubControl, async_setup_hub_controls

if TYPE_CHECKING:
    from .coordinator import Rtl433Coordinator
    from .sdr_settings import SdrSetting

# This platform owns only registry settings whose ``platform`` equals this.
PLATFORM = "switch"


class Rtl433SwitchControl(Rtl433HubControl, SwitchEntity):
    """A managed boolean SDR setting (Auto gain) as a hub-device Switch entity."""

    def __init__(
        self,
        coordinator: Rtl433Coordinator,
        hub_entry_id: str,
        setting: SdrSetting,
    ) -> None:
        """Initialize the switch control from the setting."""
        super().__init__(coordinator, hub_entry_id, setting)

    @property
    def is_on(self) -> bool | None:
        """Return the desired auto-gain flag, falling back to the actual gain.

        The actual fallback reads auto as the server's gain string being empty
        (``gain == ""``); a missing gain string leaves it unknown (``None``).
        """
        desired = self._coordinator.get_desired(self._setting.key)
        if desired is not None:
            return bool(desired)
        gain = self._coordinator.meta.get("gain")
        if gain is None:
            return None
        return gain == ""

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable auto gain (the coordinator sends an empty gain ``arg``)."""
        await self._coordinator.set_sdr(self._setting.key, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable auto gain (the coordinator sends the paired dB value)."""
        await self._coordinator.set_sdr(self._setting.key, False)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Register the hub's managed Switch controls (only when managing)."""
    await async_setup_hub_controls(
        hass, entry, async_add_entities, PLATFORM, Rtl433SwitchControl
    )
