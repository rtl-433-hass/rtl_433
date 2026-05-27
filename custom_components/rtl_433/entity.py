"""Shared base entity and hub-wide platform-setup helper for the integration.

Every ``sensor``/``binary_sensor`` entity created for a device nested under the
hub config entry derives from :class:`Rtl433Entity`. The base centralizes the
four concerns the platforms would otherwise duplicate:

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

The module also hosts :func:`async_setup_hub_platform`, the shared
``async_setup_entry`` body used by both the ``sensor`` and ``binary_sensor``
platforms. It runs once on the single hub config entry and: creates entities for
every device recorded in ``entry.data[CONF_DEVICES]`` (unioned with the fields
the coordinator already knows), subscribes to ``signal_new_device`` to add a new
device's entities at runtime (the ``dynamic-devices`` Quality Scale rule),
registers a per-device listener on ``signal_device_update`` that adds entities as
previously unseen mapped fields arrive, and keeps ``entry.data[CONF_DEVICES]``
current via the idempotent :func:`async_upsert_device` helper.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo, Entity, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt as dt_util

from .const import (
    CONF_DEVICES,
    CONF_MODEL,
    DATA_LIBRARY,
    DEVICE_FIELDS,
    DOMAIN,
    signal_device_update,
    signal_hub_update,
    signal_new_device,
)
from .mapping import FieldDescriptor, lookup

if TYPE_CHECKING:
    from .coordinator import Rtl433Coordinator
    from .normalizer import NormalizedEvent


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


class Rtl433HubEntity(Entity):
    """Base for statically-registered entities on the hub device itself.

    Unlike :class:`Rtl433Entity` (one per device field, availability gated by the
    per-device timeout), hub entities are one-per-hub, attach to the hub device,
    and re-read the coordinator's hub state on every ``signal_hub_update``.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, coordinator: Rtl433Coordinator, hub_entry_id: str) -> None:
        """Attach to the hub device and remember the coordinator."""
        self._coordinator = coordinator
        self._hub_entry_id = hub_entry_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, hub_entry_id)},
        )
        self._unsub_hub: Callable[[], None] | None = None

    async def async_added_to_hass(self) -> None:
        """Subscribe to the hub-update dispatcher signal."""
        await super().async_added_to_hass()
        self._unsub_hub = async_dispatcher_connect(
            self.hass,
            signal_hub_update(self._hub_entry_id),
            self._handle_hub_update,
        )

    async def async_will_remove_from_hass(self) -> None:
        """Tear down the hub-update subscription."""
        if self._unsub_hub is not None:
            self._unsub_hub()
            self._unsub_hub = None

    @callback
    def _handle_hub_update(self) -> None:
        """Re-read hub state and write the entity state."""
        self.async_write_ha_state()


# --------------------------------------------------------------------------- #
# Devices-map helper.                                                          #
# --------------------------------------------------------------------------- #
async def async_upsert_device(
    hass: HomeAssistant,
    entry: ConfigEntry,
    device_key: str,
    *,
    model: str | None = None,
    fields: Iterable[str] | None = None,
) -> None:
    """Merge a device's model/fields into ``entry.data[CONF_DEVICES]``.

    The devices map is the authoritative source of truth for recreating nested
    devices/entities on startup. This helper is idempotent and writes only when
    the stored record actually changes: fields are unioned (and stored sorted for
    diff-friendly entries), the model is set when provided. Concurrent
    ``sensor``/``binary_sensor`` setups (and the dynamic-add listeners) converge
    because every write is a union, never a clobber.
    """
    devices = {k: dict(v) for k, v in entry.data.get(CONF_DEVICES, {}).items()}
    rec = devices.setdefault(device_key, {CONF_MODEL: model or "", DEVICE_FIELDS: []})
    changed = False
    if model and rec.get(CONF_MODEL) != model:
        rec[CONF_MODEL] = model
        changed = True
    if fields:
        merged = sorted(set(rec.get(DEVICE_FIELDS, [])) | set(fields))
        if merged != rec.get(DEVICE_FIELDS, []):
            rec[DEVICE_FIELDS] = merged
            changed = True
    if changed:
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_DEVICES: devices}
        )


# --------------------------------------------------------------------------- #
# Hub-wide platform setup (sensor + binary_sensor use the same flow).          #
# --------------------------------------------------------------------------- #
async def async_setup_hub_platform(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
    platform: str,
    entity_cls: Callable[..., Rtl433Entity],
    per_device_factory: Callable[
        [Rtl433Coordinator, str, str, str], Rtl433Entity
    ]
    | None = None,
) -> None:
    """Set up one entity platform for every device nested under the hub entry.

    Runs once on the single hub config entry. It:

    1. creates entities for every device in ``entry.data[CONF_DEVICES]`` (unioned
       with the fields the coordinator already knows for that device);
    2. subscribes to ``signal_new_device(entry_id)`` so a newly observed device's
       entities are created at runtime (gated upstream by the discovery toggle);
    3. for each device, registers a ``signal_device_update`` listener that adds
       entities as previously unseen mapped fields arrive; and
    4. keeps ``entry.data[CONF_DEVICES]`` current via :func:`async_upsert_device`.

    Both the ``sensor`` and ``binary_sensor`` platforms run this independently;
    each only builds descriptors whose ``platform`` matches, and the devices-map
    writes are idempotent unions so the two converge.

    ``per_device_factory`` is an optional caller-supplied hook for a single
    "extra" per-device entity that is not field-driven (e.g. the sensor
    platform's synthetic Last-seen sensor). When set, it is invoked once per
    device — in both the initial devices-map build and the new-device handler —
    as ``per_device_factory(coordinator, entry.entry_id, device_key, model)``.
    Passing it as a callable (rather than importing the entity class here) keeps
    the dependency direction clean: ``entity.py`` does not import from the
    platform modules. Callers that omit it (e.g. ``binary_sensor``) create no
    extra entity.
    """
    coordinator: Rtl433Coordinator = hass.data[DOMAIN][entry.entry_id]

    # Use the merged registry (shipped library + user overrides) that the hub
    # loaded in an executor and cached, so descriptor lookups never re-read the
    # YAML files on the event loop.
    registry: dict[str, FieldDescriptor] | None = hass.data[DOMAIN].get(
        DATA_LIBRARY, (None, None)
    )[0]

    # Track created unique_ids per device_key so neither the initial build nor the
    # dynamic-add handlers double-create entities for the same field.
    created: dict[str, set[str]] = {}
    # Per-device ``signal_device_update`` unsubscribe handles, so a removed device's
    # listener can be torn down (and re-registered cleanly if it re-appears).
    field_unsubs: dict[str, Callable[[], None]] = {}
    # ``device_key``s whose optional ``per_device_factory`` extra entity has been
    # created, so it is made exactly once per device across both creation paths.
    extra_created: set[str] = set()

    def _descriptor_for(field_key: str) -> FieldDescriptor | None:
        """Return a descriptor for this platform, or None to skip the field."""
        descriptor = lookup(field_key, registry)
        if descriptor is None or descriptor.platform != platform:
            return None
        return descriptor

    def _build(
        device_key: str, model: str, field_keys: Iterable[str]
    ) -> list[Rtl433Entity]:
        """Build (and dedupe by unique_id) entities for the given field keys."""
        seen = created.setdefault(device_key, set())
        new_entities: list[Rtl433Entity] = []
        for field_key in field_keys:
            descriptor = _descriptor_for(field_key)
            if descriptor is None:
                continue
            unique_id = f"{entry.entry_id}:{device_key}:{descriptor.object_suffix}"
            if unique_id in seen:
                continue
            seen.add(unique_id)
            new_entities.append(
                entity_cls(coordinator, entry.entry_id, device_key, model, descriptor)
            )
        return new_entities

    def _build_extra(device_key: str, model: str) -> list[Rtl433Entity]:
        """Build the optional once-per-device extra entity (e.g. Last-seen).

        Returns the single extra entity the first time it is asked for a given
        ``device_key`` (and only when a factory was supplied), then ``[]`` on any
        later call so both creation paths can append it unconditionally.
        """
        if per_device_factory is None or device_key in extra_created:
            return []
        extra_created.add(device_key)
        return [per_device_factory(coordinator, entry.entry_id, device_key, model)]

    def _register_field_listener(device_key: str, model: str) -> None:
        """Register a per-device listener that adds entities for new fields.

        Idempotent: if a listener is already registered for this device_key
        (e.g. the new-device handler fires again before a removal) it is left in
        place. The unsubscribe handle is kept in ``field_unsubs`` so a device
        removal can tear it down (rather than relying solely on entry-unload).
        """
        if device_key in field_unsubs:
            return

        @callback
        def _handle_new_fields(event: NormalizedEvent) -> None:
            """Add entities for any newly mapped field of this platform."""
            incoming = set(event.fields)
            new_entities = _build(device_key, model, incoming)
            if not new_entities:
                return
            async_add_entities(new_entities)
            # New entities for this platform means at least one previously unseen
            # mapped field; persist the mapped subset (``async_upsert_device``
            # unions and only writes when the stored set actually grows).
            mapped = {
                field_key
                for field_key in incoming
                if _descriptor_for(field_key) is not None
            }
            hass.async_create_task(
                async_upsert_device(hass, entry, device_key, fields=mapped)
            )

        field_unsubs[device_key] = async_dispatcher_connect(
            hass,
            signal_device_update(entry.entry_id, device_key),
            _handle_new_fields,
        )

    @callback
    def _remove_device(device_key: str) -> None:
        """Forget a device's per-platform state when it is removed.

        Drops the dedup cache and tears down the per-device field listener so a
        later event (with discovery on) recreates the device cleanly rather than
        being skipped as "already created" (Clarification #4).
        """
        created.pop(device_key, None)
        extra_created.discard(device_key)
        unsub = field_unsubs.pop(device_key, None)
        if unsub is not None:
            unsub()

    # Let ``async_remove_config_entry_device`` reach this platform's per-device
    # state; deregister on unload so a reloaded entry starts clean.
    coordinator.device_removers.append(_remove_device)

    @callback
    def _teardown() -> None:
        for unsub in list(field_unsubs.values()):
            unsub()
        field_unsubs.clear()
        if _remove_device in coordinator.device_removers:
            coordinator.device_removers.remove(_remove_device)

    entry.async_on_unload(_teardown)

    # --- Initial build from the devices map ------------------------------- #
    for device_key, rec in entry.data.get(CONF_DEVICES, {}).items():
        model = rec.get(CONF_MODEL, "")
        union = set(rec.get(DEVICE_FIELDS, [])) | coordinator.device_fields.get(
            device_key, set()
        )
        async_add_entities(
            _build(device_key, model, union) + _build_extra(device_key, model)
        )
        # Persist any coordinator-known fields not yet stored in the map.
        await async_upsert_device(hass, entry, device_key, model=model, fields=union)
        _register_field_listener(device_key, model)

    # --- New-device dynamic add ------------------------------------------- #
    @callback
    def _handle_new_device(device_key: str, model: str) -> None:
        """Create a newly observed device's entities for this platform."""
        fields = coordinator.device_fields.get(device_key, set())
        new_entities = _build(device_key, model, fields) + _build_extra(
            device_key, model
        )
        if new_entities:
            async_add_entities(new_entities)
        _register_field_listener(device_key, model)
        hass.async_create_task(
            async_upsert_device(hass, entry, device_key, model=model, fields=fields)
        )

    entry.async_on_unload(
        async_dispatcher_connect(
            hass,
            signal_new_device(entry.entry_id),
            _handle_new_device,
        )
    )
