"""Shared base entity and platform-setup helper for the rtl_433 integration.

Every ``sensor``/``binary_sensor`` entity created for a per-device config entry
derives from :class:`Rtl433Entity`. The base centralizes the four concerns the
platforms would otherwise duplicate:

* **Device registry** — a single :class:`DeviceInfo` keyed by
  ``{hub_entry_id}:{device_key}`` and linked to the hub device via
  ``via_device`` so every device groups under its hub.
* **Dispatcher subscription** — each entity subscribes to the per-device signal
  ``signal_device_update(hub_entry_id, device_key)`` that the coordinator fans a
  :class:`~custom_components.rtl_433.normalizer.NormalizedEvent` out on, and
  unsubscribes in ``async_will_remove_from_hass``.
* **Availability** — computed from the coordinator's ``last_seen`` timestamp
  versus the effective per-device timeout. On startup the entity baselines a
  missing ``last_seen`` to "now" so a restored state shows until the timeout
  elapses ("restore then time out", Clarification #10) rather than immediately
  reading unavailable.
* **State restoration** — via :class:`RestoreEntity`; the field-specific
  subclasses pull the last state in their own ``async_added_to_hass``.

The module also hosts :func:`async_setup_device_platform`, the shared
``async_setup_entry`` body used by both the ``sensor`` and ``binary_sensor``
platforms: it resolves the parent hub coordinator, builds entities for the
device's observed mapped fields of one platform, persists the observed-field set
to ``entry.options`` (Clarification #9), and registers a dispatcher listener that
adds new entities on the fly as previously unseen mapped fields arrive.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt as dt_util

from .const import (
    CONF_DEVICE_KEY,
    CONF_HUB_ENTRY_ID,
    CONF_MODEL,
    DOMAIN,
    signal_device_update,
)
from .mapping import FieldDescriptor, lookup

if TYPE_CHECKING:
    from .coordinator import Rtl433Coordinator
    from .normalizer import NormalizedEvent

# Persisted under ``entry.options`` so observed fields survive a restart.
CONF_OBSERVED_FIELDS = "observed_fields"


def _resolve_entity_category(value: str | None) -> EntityCategory | None:
    """Map a descriptor's ``entity_category`` string to the HA enum.

    The library stores the category as a plain string (e.g. ``"diagnostic"``);
    an unrecognized value is treated as "no category" rather than raising.
    """
    if value is None:
        return None
    try:
        return EntityCategory(value)
    except ValueError:
        return None


class Rtl433Entity(RestoreEntity):
    """Base entity for one mapped field of one rtl_433 device.

    Subclasses (``Rtl433Sensor`` / ``Rtl433BinarySensor``) supply the value
    handling; this class owns identity, device info, availability, and the
    dispatcher lifecycle.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        coordinator: Rtl433Coordinator,
        hub_entry_id: str,
        device_key: str,
        model: str,
        descriptor: FieldDescriptor,
    ) -> None:
        """Initialize identity, device info, and entity description fields."""
        self._coordinator = coordinator
        self._hub_entry_id = hub_entry_id
        self._device_key = device_key
        self._descriptor = descriptor

        # Instance-scoped unique_id: scoping by the parent hub entry id means two
        # hubs observing the same model+id never collide (Success Criteria #1).
        self._attr_unique_id = f"{hub_entry_id}:{device_key}:{descriptor.object_suffix}"

        # Per-field entity metadata common to both platforms. ``_attr_name`` is a
        # device-relative name because ``_attr_has_entity_name`` is set.
        self._attr_name = descriptor.name
        self._attr_entity_category = _resolve_entity_category(
            descriptor.entity_category
        )
        self._attr_entity_registry_enabled_default = descriptor.enabled_by_default
        if descriptor.icon is not None:
            self._attr_icon = descriptor.icon

        device_name = f"{model} ({device_key})" if model else device_key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{hub_entry_id}:{device_key}")},
            name=device_name,
            model=model or None,
            manufacturer="rtl_433",
            via_device=(DOMAIN, hub_entry_id),
        )

        self._unsub_dispatcher: Callable[[], None] | None = None

    # ------------------------------------------------------------------ #
    # Availability                                                       #
    # ------------------------------------------------------------------ #
    @property
    def available(self) -> bool:
        """Return whether the device has been seen within its effective timeout.

        Mirrors the coordinator's watchdog logic but evaluated lazily so the
        value is correct between watchdog ticks too. On startup the entity
        baselines ``last_seen`` to "now" (see :meth:`async_added_to_hass`), so a
        restored entity reads available until the timeout elapses.
        """
        last_seen = self._coordinator.last_seen.get(self._device_key)
        if last_seen is None:
            return False
        timeout = self._effective_timeout()
        return (dt_util.utcnow() - last_seen) <= timedelta(seconds=timeout)

    def _effective_timeout(self) -> int:
        """Resolve the per-device timeout (override -> hub default)."""
        resolver = self._coordinator.effective_timeout_resolver
        if resolver is not None:
            try:
                return resolver(self._device_key)
            except Exception:  # noqa: BLE001 - fall back to the hub default
                pass
        return self._coordinator.availability_timeout

    # ------------------------------------------------------------------ #
    # Lifecycle                                                          #
    # ------------------------------------------------------------------ #
    async def async_added_to_hass(self) -> None:
        """Restore last state, baseline last-seen, and subscribe to updates."""
        await super().async_added_to_hass()

        # "Restore then time out" (Clarification #10): if the coordinator has no
        # last-seen for this device yet (fresh start, device silent so far),
        # baseline it to now so the restored state shows until the timeout
        # elapses instead of reading unavailable immediately. A real event
        # overwrites this with its true timestamp.
        if self._device_key not in self._coordinator.last_seen:
            self._coordinator.last_seen[self._device_key] = dt_util.utcnow()
            self._coordinator.available[self._device_key] = True

        # Let the subclass re-apply its restored value (if any).
        await self._async_restore_state()

        self._unsub_dispatcher = async_dispatcher_connect(
            self.hass,
            signal_device_update(self._hub_entry_id, self._device_key),
            self._handle_dispatch,
        )

    async def async_will_remove_from_hass(self) -> None:
        """Tear down the dispatcher subscription."""
        if self._unsub_dispatcher is not None:
            self._unsub_dispatcher()
            self._unsub_dispatcher = None

    # ------------------------------------------------------------------ #
    # Update handling                                                    #
    # ------------------------------------------------------------------ #
    @callback
    def _handle_dispatch(self, event: NormalizedEvent) -> None:
        """Handle a dispatched event for this device.

        Two cases drive a state write:
        * the event carries this entity's field -> update the value, and
        * the event is a watchdog re-dispatch with the field absent -> the value
          stays put but availability may have flipped.
        In both cases ``async_write_ha_state`` re-reads ``available``.
        """
        if self._descriptor.field_key in event.fields:
            self._apply_value(event.fields[self._descriptor.field_key])
        self.async_write_ha_state()

    # ------------------------------------------------------------------ #
    # Subclass hooks                                                     #
    # ------------------------------------------------------------------ #
    def _apply_value(self, raw_value: Any) -> None:
        """Apply a fresh raw value to the entity's state. Overridden."""
        raise NotImplementedError

    async def _async_restore_state(self) -> None:
        """Re-apply the last known state on startup. Overridden."""
        raise NotImplementedError


# --------------------------------------------------------------------------- #
# Shared per-device platform setup (sensor + binary_sensor use the same flow). #
# --------------------------------------------------------------------------- #
async def async_setup_device_platform(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
    platform: str,
    entity_cls: Callable[..., Rtl433Entity],
) -> None:
    """Build entities for one platform of one per-device entry, plus dynamic add.

    Resolves the parent hub coordinator (raising :class:`ConfigEntryNotReady` if
    the hub has not loaded yet), unions the persisted observed-field set with the
    fields the coordinator already knows for this device, creates the entities
    whose descriptor matches ``platform``, then keeps a dispatcher listener alive
    that adds new entities — and persists the field — as new mapped fields arrive
    (Clarification #9). Entities are deduped by unique_id within this setup.
    """
    hub_entry_id: str = entry.data[CONF_HUB_ENTRY_ID]
    device_key: str = entry.data[CONF_DEVICE_KEY]
    model: str = entry.data.get(CONF_MODEL, "")

    domain_data = hass.data.get(DOMAIN, {})
    coordinator: Rtl433Coordinator | None = domain_data.get(hub_entry_id)
    if coordinator is None:
        raise ConfigEntryNotReady(
            f"Hub {hub_entry_id} not loaded yet for device {device_key}"
        )

    # Union persisted fields with whatever the coordinator has already seen so a
    # field observed before this setup ran is not lost.
    observed: set[str] = set(entry.options.get(CONF_OBSERVED_FIELDS, []))
    observed |= coordinator.device_fields.get(device_key, set())

    created_unique_ids: set[str] = set()

    def _descriptor_for(field_key: str) -> FieldDescriptor | None:
        """Return a descriptor for this platform, or None to skip the field."""
        descriptor = lookup(field_key)
        if descriptor is None or descriptor.platform != platform:
            return None
        return descriptor

    def _build_for_fields(field_keys: set[str]) -> list[Rtl433Entity]:
        """Build (and dedupe by unique_id) entities for the given field keys."""
        new_entities: list[Rtl433Entity] = []
        for field_key in field_keys:
            descriptor = _descriptor_for(field_key)
            if descriptor is None:
                continue
            unique_id = f"{hub_entry_id}:{device_key}:{descriptor.object_suffix}"
            if unique_id in created_unique_ids:
                continue
            created_unique_ids.add(unique_id)
            new_entities.append(
                entity_cls(coordinator, hub_entry_id, device_key, model, descriptor)
            )
        return new_entities

    # Initial set from the unioned observed fields.
    async_add_entities(_build_for_fields(observed))

    # Persist the (possibly expanded) observed-field set so a restart recreates
    # the same entities, including any the coordinator had seen but that were not
    # yet persisted.
    await async_persist_observed_fields(hass, entry, observed)

    @callback
    def _handle_new_fields(event: NormalizedEvent) -> None:
        """On each event, add entities for any newly mapped field of this platform."""
        incoming = set(event.fields)
        fresh = {
            field_key
            for field_key in incoming
            if (descriptor := _descriptor_for(field_key)) is not None
            and f"{hub_entry_id}:{device_key}:{descriptor.object_suffix}"
            not in created_unique_ids
        }
        if fresh:
            new_entities = _build_for_fields(fresh)
            if new_entities:
                async_add_entities(new_entities)

        # Persist the expanded observed-field set (union of everything seen),
        # even for fields owned by the other platform, so both platforms agree.
        expanded = set(entry.options.get(CONF_OBSERVED_FIELDS, [])) | incoming
        if expanded - set(entry.options.get(CONF_OBSERVED_FIELDS, [])):
            hass.async_create_task(async_persist_observed_fields(hass, entry, expanded))

    entry.async_on_unload(
        async_dispatcher_connect(
            hass,
            signal_device_update(hub_entry_id, device_key),
            _handle_new_fields,
        )
    )


async def async_persist_observed_fields(
    hass: HomeAssistant, entry: ConfigEntry, observed: set[str]
) -> None:
    """Persist the observed-field set into ``entry.options`` if it grew.

    Stored as a sorted list for deterministic, diff-friendly entry options.
    Only the union is ever written, so concurrent ``sensor``/``binary_sensor``
    setups (and the dynamic-add listeners) converge without clobbering each
    other.
    """
    current = set(entry.options.get(CONF_OBSERVED_FIELDS, []))
    union = current | observed
    if union == current:
        return
    new_options = {**entry.options, CONF_OBSERVED_FIELDS: sorted(union)}
    hass.config_entries.async_update_entry(entry, options=new_options)
