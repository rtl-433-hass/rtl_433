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
  elapses ("restore then time out") rather than immediately reading unavailable.
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
import dataclasses
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo, Entity, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt as dt_util

from .calibration import COMMODITY_DEVICE_CLASS, normalize_calibration
from .const import (
    AVAILABILITY_TIMEOUT_NEVER,
    CALIBRATION_COMMODITY,
    CALIBRATION_SCALE,
    CALIBRATION_UNIT,
    CONF_DEVICES,
    CONF_MODEL,
    CONSUMPTION_FIELD_KEYS,
    DATA_ENTRY_LIBRARY,
    DEVICE_CALIBRATION,
    DEVICE_EVENT_TYPES,
    DEVICE_FIELDS,
    DOMAIN,
    MANUFACTURER,
    signal_device_update,
    signal_hub_update,
    signal_new_device,
)
from .mapping import FieldDescriptor, Registry, lookup
from .normalizer import _safe_token
from .sdr_settings import SDR_SETTINGS

if TYPE_CHECKING:
    from .coordinator import Rtl433Coordinator
    from .normalizer import NormalizedEvent
    from .sdr_settings import SdrSetting


def _device_display_name(model: str, device_key: str) -> str:
    """Human-readable device name: the model plus its distinguishing id suffix.

    ``device_key`` is ``<model-token>-<id>[-ch..][-st..]`` (see ``normalizer``),
    so naively combining the model with the whole key duplicates the model — e.g.
    ``Fineoffset-WH51 (Fineoffset-WH51-00c50f)``. Strip the model-token prefix and
    keep only the suffix, giving ``Fineoffset-WH51 00c50f``: the canonical rtl_433
    model (matching the device's ``model`` field) plus just the id that
    distinguishes one unit from another. Falls back to the raw ``device_key`` when
    there is no model, and to the bare model for a model-only device (no suffix).
    """
    if not model:
        return device_key
    suffix = device_key.removeprefix(f"{_safe_token(model)}-")
    if not suffix or suffix == device_key:
        # Model-only device (key == model token), or an unexpected key shape.
        return model
    return f"{model} {suffix}"


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


def _apply_calibration(
    descriptor: FieldDescriptor, calibration: dict[str, Any]
) -> FieldDescriptor:
    """Overlay a per-device calibration onto a consumption field descriptor.

    Highest precedence: the calibration's commodity device_class, convertible base
    unit, ``state_class: total_increasing`` and value scale replace the library
    descriptor's, making a unitless counter Energy-dashboard-eligible. The base
    ``consumption``/``consumption_data`` descriptors carry
    ``value_transform: {int: true}``; merging a ``scale`` makes the transform
    float-valued, which is correct for an energy/volume reading. The caller has
    already validated the calibration via :func:`normalize_calibration`.
    """
    commodity = calibration[CALIBRATION_COMMODITY]
    transform = dict(descriptor.value_transform or {})
    transform["scale"] = calibration[CALIBRATION_SCALE]
    return dataclasses.replace(
        descriptor,
        device_class=COMMODITY_DEVICE_CLASS[commodity].value,
        unit_of_measurement=calibration[CALIBRATION_UNIT],
        state_class=SensorStateClass.TOTAL_INCREASING.value,
        value_transform=transform,
    )


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
        # hubs observing the same model+id never collide.
        self._attr_unique_id = f"{hub_entry_id}:{device_key}:{descriptor.object_suffix}"

        # Per-field entity metadata common to both platforms. ``_attr_name`` is a
        # device-relative name because ``_attr_has_entity_name`` is set. A
        # descriptor with no name is left UNSET (not None) so HA derives the
        # name from ``device_class`` — setting ``_attr_name = None`` explicitly
        # would instead produce a nameless entity.
        if descriptor.name is not None:
            self._attr_name = descriptor.name
        self._attr_entity_category = _resolve_entity_category(
            descriptor.entity_category
        )
        self._attr_entity_registry_enabled_default = descriptor.enabled_by_default
        if descriptor.icon is not None:
            self._attr_icon = descriptor.icon

        device_name = _device_display_name(model, device_key)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{hub_entry_id}:{device_key}")},
            name=device_name,
            model=model or None,
            manufacturer=MANUFACTURER,
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
        restored entity reads available until the timeout elapses. A timeout of
        ``0`` is never-expire: once the device has been seen at least once it
        stays available indefinitely (still unavailable when never seen).

        Routes through the coordinator's ``_effective_timeout`` so the
        device-class-aware resolution (and never-expire) is identical to the
        watchdog's.
        """
        last_seen = self._coordinator.last_seen.get(self._device_key)
        if last_seen is None:
            return False
        timeout = self._coordinator._effective_timeout(self._device_key)
        if timeout == AVAILABILITY_TIMEOUT_NEVER:
            return True
        return (dt_util.utcnow() - last_seen) <= timedelta(seconds=timeout)

    # ------------------------------------------------------------------ #
    # Lifecycle                                                          #
    # ------------------------------------------------------------------ #
    async def async_added_to_hass(self) -> None:
        """Restore last state, baseline last-seen, and subscribe to updates."""
        await super().async_added_to_hass()

        # "Restore then time out": if the coordinator has no
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

        A replayed / stale frame (``event.is_replay``) still applies its value and
        writes state here so sensors seed their latest reading from the reconnect
        replay; only ``Rtl433Event`` honors the flag (to not re-fire automations).
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


class Rtl433HubControl(Rtl433HubEntity):
    """Shared base for the managed SDR control entities on the hub device.

    The ``number`` / ``select`` / ``switch`` control platforms each subclass this
    (alongside the matching HA entity mixin) so the four concerns common to every
    control live in one place: attachment to the hub device (inherited from
    :class:`Rtl433HubEntity`), the :data:`EntityCategory.CONFIG` category, the
    stable unique_id ``f"{hub_entry_id}:hub:{object_suffix}"``, and the
    device-relative entity name — all sourced from the field's
    :class:`~custom_components.rtl_433.sdr_settings.SdrSetting`.

    Read-back/repaint is inherited too: :class:`Rtl433HubEntity` subscribes to
    ``signal_hub_update`` and its ``_handle_hub_update`` calls
    ``async_write_ha_state``, so after a write the coordinator's post-read-back
    ``signal_hub_update`` repaints the control with the server's actual value.
    """

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: Rtl433Coordinator,
        hub_entry_id: str,
        setting: SdrSetting,
    ) -> None:
        """Attach to the hub device and adopt the setting's identity/name."""
        super().__init__(coordinator, hub_entry_id)
        self._setting = setting
        self._attr_unique_id = f"{hub_entry_id}:hub:{setting.object_suffix}"
        self._attr_name = setting.name

    @property
    def available(self) -> bool:
        """Apply the setting's runtime availability gate to the current meta.

        Re-evaluated on every ``signal_hub_update`` (inherited repaint), so a
        control like ``hop_interval`` / ``center_frequency`` appears or hides as
        the server's frequency configuration changes. Defaults to available.
        """
        return self._setting.available(self._coordinator.meta)


async def async_setup_hub_controls(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
    platform: str,
    control_cls: Callable[[Rtl433Coordinator, str, SdrSetting], Rtl433HubControl],
) -> None:
    """Register the hub's managed controls for one control platform.

    Shared by the ``number`` / ``select`` / ``switch`` platforms, which differ
    only in their entity class. When the hub's ``manage_settings`` toggle is off
    it creates **no** entities and returns immediately; when management is on it
    statically registers one ``control_cls`` per :data:`SDR_SETTINGS` entry whose
    ``platform`` matches and whose capability gate is satisfied.
    """
    coordinator: Rtl433Coordinator = hass.data[DOMAIN][entry.entry_id]
    if not coordinator.manage_settings:
        return
    async_add_entities(
        control_cls(coordinator, entry.entry_id, setting)
        for setting in SDR_SETTINGS
        if setting.platform == platform and setting.capability(coordinator.meta)
    )


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


async def async_upsert_event_types(
    hass: HomeAssistant,
    entry: ConfigEntry,
    device_key: str,
    field_key: str,
    types: Iterable[str],
) -> None:
    """Union observed event types into the hub devices map, stored sorted.

    Writes ``entry.data[CONF_DEVICES][device_key][DEVICE_EVENT_TYPES][field_key]``
    only when the stored set for that field actually grows (a no-op otherwise),
    mirroring :func:`async_upsert_device`'s idempotent union-write so concurrent
    writes converge. Tolerates a record with no ``DEVICE_EVENT_TYPES`` key yet
    (treated as ``{}``) and deep-copies the per-field dict so the stored data is
    never mutated in place.
    """
    devices = {k: dict(v) for k, v in entry.data.get(CONF_DEVICES, {}).items()}
    rec = devices.setdefault(device_key, {CONF_MODEL: "", DEVICE_FIELDS: []})
    by_field = {k: list(v) for k, v in rec.get(DEVICE_EVENT_TYPES, {}).items()}
    merged = sorted(set(by_field.get(field_key, [])) | set(types))
    if merged == by_field.get(field_key, []):
        return
    by_field[field_key] = merged
    rec[DEVICE_EVENT_TYPES] = by_field
    devices[device_key] = rec
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
    per_device_factory: Callable[[Rtl433Coordinator, str, str, str], Rtl433Entity]
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

    # Use the per-entry merged registry (shipped library + this hub's user
    # overrides) that the hub built at setup and cached, so descriptor lookups
    # never re-read the YAML files on the event loop.
    registry: Registry | None = (
        hass.data[DOMAIN]
        .get(DATA_ENTRY_LIBRARY, {})
        .get(entry.entry_id, (None, None))[0]
    )

    # Track created unique_ids per device_key so neither the initial build nor the
    # dynamic-add handlers double-create entities for the same field.
    created: dict[str, set[str]] = {}
    # Per-device ``signal_device_update`` unsubscribe handles, so a removed device's
    # listener can be torn down (and re-registered cleanly if it re-appears).
    field_unsubs: dict[str, Callable[[], None]] = {}
    # ``device_key``s whose optional ``per_device_factory`` extra entity has been
    # created, so it is made exactly once per device across both creation paths.
    extra_created: set[str] = set()

    def _calibration_for(device_key: str) -> dict[str, Any] | None:
        """Return the validated per-device calibration record, or ``None``.

        Read from the hub's per-device record on every build so a reload picks up
        a freshly-written calibration; ``None`` (no/none calibration) leaves the
        consumption field on its library descriptor.
        """
        record = entry.data.get(CONF_DEVICES, {}).get(device_key, {})
        return normalize_calibration(record.get(DEVICE_CALIBRATION))

    def _descriptor_for(field_key: str, model: str) -> FieldDescriptor | None:
        """Return a descriptor for this platform, or None to skip the field.

        ``model`` makes the lookup model-aware: a model-scoped library entry for
        ``(model, field_key)`` wins over the global flat entry.
        """
        descriptor = lookup(field_key, model, registry)
        if descriptor is None or descriptor.platform != platform:
            return None
        return descriptor

    def _build(
        device_key: str, model: str, field_keys: Iterable[str]
    ) -> list[Rtl433Entity]:
        """Build (and dedupe by unique_id) entities for the given field keys."""
        seen = created.setdefault(device_key, set())
        calibration = _calibration_for(device_key)
        new_entities: list[Rtl433Entity] = []
        for field_key in field_keys:
            descriptor = _descriptor_for(field_key, model)
            if descriptor is None:
                continue
            # Highest precedence: overlay a per-device calibration onto the device's
            # known consumption field(s), overriding the library descriptor.
            if calibration is not None and field_key in CONSUMPTION_FIELD_KEYS:
                descriptor = _apply_calibration(descriptor, calibration)
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
                if _descriptor_for(field_key, model) is not None
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
        being skipped as "already created".
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
