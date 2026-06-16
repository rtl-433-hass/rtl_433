"""Select control platform for the rtl_433 hub config entry.

``async_setup_entry`` runs once for the hub config entry. When the hub's
``manage_settings`` toggle is off it creates **no** entities and returns
immediately; when management is on it statically registers one
:class:`Rtl433SelectControl` per ``select``-platform field in the
:data:`~custom_components.rtl_433.sdr_settings.SDR_SETTINGS` registry whose
capability gate is satisfied. Today that is the conversion-mode select
(``native`` / ``si`` / ``customary``).

The select trades in human-readable labels while the ``convert`` command and the
desired/actual state are integers, so it maps both ways through the registry's
``conversion_label_to_val`` / ``conversion_val_to_label`` helpers.

Optimistic-then-confirmed state: ``async_select_option`` writes the mapped int
via ``coordinator.set_sdr`` (persist + send + read-back + ``signal_hub_update``).
Until the read-back arrives ``current_option`` shows the just-selected desired
label (optimistic); the inherited ``signal_hub_update`` subscription then
repaints the control with the server's confirmed value.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import Rtl433HubControl, async_setup_hub_controls
from .sdr_settings import conversion_label_to_val, conversion_val_to_label

if TYPE_CHECKING:
    from .coordinator import Rtl433Coordinator
    from .sdr_settings import SdrSetting

# This platform owns only registry settings whose ``platform`` equals this.
PLATFORM = "select"


class Rtl433SelectControl(Rtl433HubControl, SelectEntity):
    """A managed enumerated SDR setting exposed as a hub-device Select entity."""

    def __init__(
        self,
        coordinator: Rtl433Coordinator,
        hub_entry_id: str,
        setting: SdrSetting,
    ) -> None:
        """Initialize select-specific options from the setting."""
        super().__init__(coordinator, hub_entry_id, setting)
        self._attr_options = list(setting.options or ())

    @property
    def current_option(self) -> str | None:
        """Return the desired option (or the server's actual) as a label.

        Both the desired value and the actual fallback (``setting.read``) are the
        integer ``convert`` ``val``; either is mapped to a label that is a member
        of ``options``, or ``None`` when unknown/unset.
        """
        value = self._coordinator.get_desired(self._setting.key)
        if value is None:
            value = self._setting.read(self._coordinator.meta)
        if value is None:
            return None
        return conversion_val_to_label(int(value))

    async def async_select_option(self, option: str) -> None:
        """Map the label to its int and write it through the coordinator."""
        await self._coordinator.set_sdr(
            self._setting.key, conversion_label_to_val(option)
        )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Register the hub's managed Select controls (only when managing)."""
    await async_setup_hub_controls(
        hass, entry, async_add_entities, PLATFORM, Rtl433SelectControl
    )
