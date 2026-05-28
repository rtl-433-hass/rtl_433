"""Button platform for the rtl_433 hub config entry.

``async_setup_entry`` runs once for the hub config entry. When the hub's
``manage_settings`` toggle is off it creates **no** entities and returns
immediately; when management is on it statically registers exactly one
:class:`Rtl433ResyncButton` on the hub device.

The button replaces the documented off → restart rtl_433 → on dance with a
one-click "Re-sync SDR settings from server" action: pressing it re-adopts the
rtl_433 server's current SDR settings into Home Assistant's managed desired
state via :meth:`~custom_components.rtl_433.coordinator.Rtl433Coordinator.async_resync_sdr`.

The press is best-effort and never raises: ``async_resync_sdr`` is refresh-first
and a no-op when the server's ``/cmd`` is unreachable (it leaves the current
desired state intact — no data loss). The button defines **no** ``available``
override, so — like the existing managed controls — it is always available.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import Rtl433HubEntity

if TYPE_CHECKING:
    from .coordinator import Rtl433Coordinator


class Rtl433ResyncButton(Rtl433HubEntity, ButtonEntity):
    """Hub button that re-adopts the server's current SDR settings on press.

    It is a hub-level action rather than a per-``SdrSetting`` control, so it
    subclasses :class:`Rtl433HubEntity` directly (not ``Rtl433HubControl``). It
    still reuses the hub-device attachment, the ``signal_hub_update`` repaint
    subscription, and the ``f"{hub_entry_id}:hub:{suffix}"`` unique_id convention.
    """

    _attr_name = "Re-sync SDR settings from server"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: Rtl433Coordinator, hub_entry_id: str) -> None:
        """Attach to the hub device and set the stable unique_id."""
        super().__init__(coordinator, hub_entry_id)
        self._attr_unique_id = f"{hub_entry_id}:hub:resync_sdr"

    async def async_press(self) -> None:
        """Re-sync the server's current SDR settings (best-effort, never raises)."""
        await self._coordinator.async_resync_sdr()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Register the hub's re-sync Button (only when managing settings)."""
    coordinator: Rtl433Coordinator = hass.data[DOMAIN][entry.entry_id]
    if not coordinator.manage_settings:
        return
    async_add_entities([Rtl433ResyncButton(coordinator, entry.entry_id)])
