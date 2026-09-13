"""Shared base entity and per-receiver platform-setup helper for the integration.

Every ``sensor``/``binary_sensor`` entity created for a device nested under the
location config entry derives from :class:`Rtl433Entity`. The base centralizes the
four concerns the platforms would otherwise duplicate:

* **Device registry** — a single :class:`DeviceInfo` keyed by
  ``{location_entry_id}:{device_key}`` and linked to the **location** device via
  ``via_device_id``. The identity is location-scoped, not receiver-scoped, which
  is what collapses every receiver's view of one physical sensor onto one
  device-registry device: the receivers are subentries of one entry, so they
  register the same identifier under the same owner.
* **Dispatcher subscription** — a unioned sensor field subscribes to the
  location-scoped ``signal_location_device_update(location_entry_id, device_key)``
  that the location aggregator re-emits after deduping every receiver's frames;
  a per-receiver *link* field (``rssi`` / ``snr`` / ``last_seen``, the union's
  exclusion set — see ``aggregator.py``) subscribes to its own receiver's
  ``signal_device_update(receiver_id, device_key)`` instead, because "how well
  does *this* receiver hear it" is not a property of the sensor. Both carry a
  :class:`~pyrtl_433.normalizer.NormalizedEvent` and both unsubscribe in
  ``async_will_remove_from_hass``.
* **Availability** — merged across the location's receivers. A receiver
  *vouches* for a device when it is connected **and** received the device within the
  effective per-device timeout; a unioned field is available when at least one
  receiver vouches, and a link field asks only its own receiver. The two gates
  are evaluated per receiver and only the result is OR-ed, because unioning them
  independently would keep a device alive on an offline receiver's stale
  timestamp. On startup the entity baselines a missing ``last_seen`` to "now" so
  a restored state shows until the timeout elapses ("restore then time out")
  rather than immediately reading unavailable.
* **State restoration** — via :class:`RestoreEntity`; the field-specific
  subclasses pull the last state in their own ``async_added_to_hass``.

The module also hosts :func:`async_setup_receiver_platform`, the shared
``async_setup_entry`` body used by both the ``sensor`` and ``binary_sensor``
platforms. The platforms are forwarded **once**, on the location entry, and run
this once **per receiver subentry**; for each receiver it: creates entities for
every device recorded in ``entry.data[CONF_DEVICES]`` (unioned with the fields
that receiver's coordinator already knows), subscribes to ``signal_new_device``
to add a new device's entities at runtime (the ``dynamic-devices`` Quality Scale
rule), registers a per-device listener on ``signal_device_update`` that adds
entities as previously unseen mapped fields arrive, and keeps
``entry.data[CONF_DEVICES]`` current via the idempotent
:func:`async_upsert_device` helper.

The per-receiver passes share one created-``unique_id`` set, because the entity
identity is location-scoped: the first receiver to reach a device's field builds
that entity and every later receiver finds it already built. That is the entity
half of the union — one physical sensor yields one entity per mapped field
however many receivers decode it — and it is also what keeps Home Assistant from
rejecting the second receiver's duplicate ``unique_id``.

Every RF-device entity is added with **no** ``config_subentry_id``, leaving its
device owned by the location entry itself. That is what keeps a device legal once
two receivers feed it: Home Assistant gives a device exactly one owning subentry,
and adding entities from two subentries that share a device silently moves it
today and raises in HA Core 2027.8. Only receiver-owned entities — the radio
controls here, plus the noise and connectivity sensors in the platform modules —
pass their receiver's subentry id.

The **per-receiver link entities** (``RSSI Attic``) are the one genuinely
counter-intuitive case of that rule, and they follow it too: they are *about* one
receiver but they hang off the *merged* device, so adding them under their
receiver's subentry would be exactly the "entities from several subentries share
a device" the rule forbids. They carry their receiver in ``_attr_name`` and in the
unique_id instead — and the name half is load-bearing, because two entities both
called "RSSI" on one device is how Home Assistant comes to mint
``sensor.<device>_rssi_2`` (issue #132).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
import dataclasses
from typing import TYPE_CHECKING, Any

from pyrtl_433.library import FieldDescriptor, Registry, lookup
from pyrtl_433.naming import display_name, identity_suffix

from homeassistant.components.sensor import SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo, Entity, EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt as dt_util

from .aggregator import is_link_field, location_aggregator, receiver_vouches
from .calibration import COMMODITY_DEVICE_CLASS, normalize_calibration
from .const import (
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
    signal_location_device_update,
    signal_new_device,
    signal_receiver_availability,
    signal_receiver_update,
)
from .receiver_settings import receiver_coordinators
from .sdr_settings import SDR_SETTINGS

if TYPE_CHECKING:
    from pyrtl_433.normalizer import NormalizedEvent

    from .coordinator import Rtl433Coordinator
    from .sdr_settings import SdrSetting


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


def receiver_label(coordinator: Rtl433Coordinator) -> str:
    """Return the human name of one receiver, for use inside an entity name.

    The receiver subentry's title -- what the user called that server ("Attic") --
    falling back to its id only if a subentry somehow carries no title, because a
    nameless label would put two receivers' entities back on the same name.
    """
    return coordinator.subentry.title or coordinator.receiver_id


def field_unique_id(
    location_id: str, device_key: str, receiver_id: str, descriptor: FieldDescriptor
) -> str:
    """Return the ``unique_id`` for one device field, unioned or per receiver.

    A unioned sensor field is receiver-agnostic -- one entity per mapped field
    however many receivers decode it -- so its id is the three-segment
    ``{location_entry_id}:{device_key}:{object_suffix}``.

    A **link** field (``rssi`` / ``snr`` / ``last_seen``) is deliberately *not*
    unioned: "how well does this receiver hear it" is a different measurement per
    receiver, so it gains a fourth segment, the receiver's subentry id, yielding
    one entity per (sensor x receiver) on the merged device. The receiver segment
    sits before the object suffix so both parsers keep recognising the tail
    (``device_trigger.py``), and ``device_replace.py``'s
    ``{entry_id}:{device_key}:`` prefix swap still carries them through a re-key.

    The single definition of the template, called by the entity itself and by the
    platform helper's dedup bookkeeping so the two cannot disagree about whether
    a field is one entity or several.
    """
    if is_link_field(descriptor.field_key):
        return f"{location_id}:{device_key}:{receiver_id}:{descriptor.object_suffix}"
    return f"{location_id}:{device_key}:{descriptor.object_suffix}"


def _combine(unsubs: list[Callable[[], None]]) -> Callable[[], None]:
    """Fold several dispatcher unsubscribes into one callable.

    Lets an entity hold a single ``_unsub_*`` handle whether it subscribed to one
    receiver or to all of them, so the teardown path stays "call it, then clear
    it" rather than growing a list to iterate and a `None`-vs-empty distinction
    to get wrong.
    """

    def _unsubscribe() -> None:
        for unsub in unsubs:
            unsub()

    return _unsubscribe


def _link_field_base_name(descriptor: FieldDescriptor) -> str:
    """Return the receiver-less half of a link field's name ("RSSI", "Last seen").

    Every shipped link descriptor names itself, so this is the descriptor's own
    name virtually always. A user mapping may null the name out to let Home
    Assistant derive one from ``device_class`` -- which a link field cannot do,
    because both receivers' entities would then derive the *same* name and
    collide on the merged device. The object suffix is the fallback: it is the
    one part of a descriptor that is always present and always distinct per
    field.
    """
    if descriptor.name is not None:
        return descriptor.name
    return descriptor.object_suffix.replace("_", " ").capitalize()


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
        receiver_id: str,
        device_key: str,
        model: str,
        descriptor: FieldDescriptor,
    ) -> None:
        """Initialize identity, device info, and entity description fields."""
        self._coordinator = coordinator
        self._receiver_id = receiver_id
        # The location config entry's id -- the scope every merged identity is
        # minted in. Read off the coordinator rather than passed in, because a
        # coordinator belongs to exactly one location and the two could not
        # disagree without minting an entity under the wrong location's scope.
        self._location_id = coordinator.entry.entry_id
        self._device_key = device_key
        self._descriptor = descriptor
        # Whether this field measures the receiver-to-sensor link (``rssi`` /
        # ``snr`` / ``last_seen``) rather than the sensor itself. Link fields are
        # excluded from the union, so they listen to their own receiver
        # (see :meth:`async_added_to_hass`).
        self._is_link_field = is_link_field(descriptor.field_key)
        label = receiver_label(coordinator)

        # Location-scoped unique_id: receiver-agnostic on purpose, so one
        # physical sensor yields one entity per mapped field however many of the
        # location's receivers decode it. Two *locations* that happen to hear the
        # same model+id still cannot collide -- their entry ids differ. A link
        # field is the deliberate exception and carries a receiver segment (see
        # :func:`field_unique_id`).
        self._attr_unique_id = field_unique_id(
            self._location_id, device_key, receiver_id, descriptor
        )

        # Per-field entity metadata common to both platforms. ``_attr_name`` is a
        # device-relative name because ``_attr_has_entity_name`` is set. A
        # descriptor with no name is left UNSET (not None) so HA derives the
        # name from ``device_class`` — setting ``_attr_name = None`` explicitly
        # would instead produce a nameless entity.
        #
        # A link field MUST name its receiver ("RSSI Attic"), and cannot take
        # either of those defaults. One merged device carries one such entity per
        # receiver, so two entities named "RSSI" (or two deriving the same name
        # from ``signal_strength``) would land on one device and Home Assistant
        # would mint ``..._rssi_2`` for the second -- the ``_2`` failure mode of
        # issue #132. The receiver association travels in the name and the
        # unique_id, never in a ``config_subentry_id``.
        if self._is_link_field:
            self._attr_name = f"{_link_field_base_name(descriptor)} {label}"
        elif descriptor.name is not None:
            self._attr_name = descriptor.name
        self._attr_entity_category = _resolve_entity_category(
            descriptor.entity_category
        )
        self._attr_entity_registry_enabled_default = descriptor.enabled_by_default
        if descriptor.icon is not None:
            self._attr_icon = descriptor.icon

        device_name = display_name(model, device_key)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{self._location_id}:{device_key}")},
            name=device_name,
            model=model or None,
            serial_number=identity_suffix(model, device_key),
            manufacturer=MANUFACTURER,
            # Linked to the LOCATION device, not to the receiver that happened to
            # decode this frame: a merged device may be fed by several receivers,
            # and a "via" pointing at one of them would claim the sensor sits
            # behind that server alone. The location device is registered by
            # ``async_setup_entry`` before any platform is forwarded, so the
            # lookup always resolves; ``via_device`` (the identifier tuple) is
            # deprecated and gone from ``DeviceInfo``.
            via_device_id=dr.async_get_device_id_by_identifier(
                coordinator.hass,
                (DOMAIN, self._location_id),
                config_entry_id=self._location_id,
            ),
        )

        self._unsub_dispatcher: Callable[[], None] | None = None
        self._unsub_receiver_availability: Callable[[], None] | None = None

    # ------------------------------------------------------------------ #
    # Availability                                                       #
    # ------------------------------------------------------------------ #
    @property
    def available(self) -> bool:
        """Return whether some receiver still vouches for this device.

        A receiver **vouches** when both of its gates hold *together*
        (``aggregator.receiver_vouches``): its WebSocket is up — ``False`` the
        moment it drops, no grace window, and it overrides even a never-expire
        device, whose exemption is from *silence*, not from the transport being
        gone — **and** it received this device within the device's effective
        timeout, resolved by the coordinator's own ``_effective_timeout`` so the
        device-class ladder and never-expire semantics are the watchdog's, not a
        second copy.

        A **unioned** field is available when *at least one* of the location's
        receivers vouches. The pair is evaluated per receiver and only the result
        is OR-ed, because the two gates cannot be unioned independently: a
        connected receiver that is deaf to the sensor plus an offline one that
        received it a minute ago satisfies "some receiver connected" and "some
        last_seen fresh" while no receiver can actually hear the device, and the
        answer has to be unavailable. The union is asked of the location
        aggregator, which is the thing that knows every receiver; with none
        running (mid-setup, or an entity built outside a location) this falls back
        to the receiver that built the entity, the honest single-receiver answer.

        A **link** field (``RSSI Attic``) reads its own receiver alone. It
        measures that receiver's link to the sensor, so another receiver still
        hearing the device says nothing about whether this reading is current.

        Evaluated lazily, so it is correct between watchdog ticks too. On startup
        the entity baselines ``last_seen`` to "now" (see
        :meth:`async_added_to_hass`), so a restored entity reads available until
        the timeout elapses rather than flicking to unavailable at once.
        """
        if self._is_link_field:
            return receiver_vouches(self._coordinator, self._device_key)
        aggregator = location_aggregator(self._coordinator.hass, self._location_id)
        if aggregator is None:
            return receiver_vouches(self._coordinator, self._device_key)
        return aggregator.device_available(self._device_key)

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

        # A unioned sensor field listens to the location aggregator's deduped,
        # receiver-agnostic stream; a per-receiver link field listens to its own
        # receiver, because the union deliberately leaves those per receiver
        # (see ``aggregator.py``).
        self._unsub_dispatcher = async_dispatcher_connect(
            self.hass,
            signal_device_update(self._receiver_id, self._device_key)
            if self._is_link_field
            else signal_location_device_update(self._location_id, self._device_key),
            self._handle_dispatch,
        )
        # The receiver-connection gate flips for every device at once and is not tied
        # to any device's event stream, so it gets its own receiver-wide signal. It
        # fires only on a connection edge, so this subscription costs one state
        # write per entity per outage.
        #
        # A unioned field subscribes to **every** receiver in the location, not
        # only the one that built it: its availability is the OR over all of
        # them, so any receiver's connection edge can flip it. A link field takes
        # its own receiver's edge alone, which is the only one that changes its
        # answer.
        self._unsub_receiver_availability = _combine(
            [
                async_dispatcher_connect(
                    self.hass,
                    signal_receiver_availability(receiver_id),
                    self._handle_receiver_availability,
                )
                for receiver_id in self._availability_receiver_ids()
            ]
        )

    def _availability_receiver_ids(self) -> list[str]:
        """Return the receivers whose connection edges can flip this entity.

        Every receiver of the location for a unioned field; only this entity's own
        receiver for a link field. Falls back to this entity's receiver if the
        location has no running coordinators to enumerate, so an entity built
        outside a live location still repaints on its own receiver's edge.
        """
        if self._is_link_field:
            return [self._receiver_id]
        others = list(receiver_coordinators(self.hass, self._coordinator.entry))
        return others or [self._receiver_id]

    async def async_will_remove_from_hass(self) -> None:
        """Tear down the dispatcher subscriptions."""
        if self._unsub_dispatcher is not None:
            self._unsub_dispatcher()
            self._unsub_dispatcher = None
        if self._unsub_receiver_availability is not None:
            self._unsub_receiver_availability()
            self._unsub_receiver_availability = None

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

        The apply is unconditional *here* because the decision has already been
        taken upstream: for a unioned field the location aggregator has deduped
        the location's receivers against each other and dropped the near-duplicate
        and the stale replay before re-emitting, so a field that survives into
        ``event.fields`` is by construction a value this entity should take
        (``aggregator.py``). A link field is single-source and has nothing to
        dedup against.
        """
        if self._descriptor.field_key in event.fields:
            self._apply_value(event.fields[self._descriptor.field_key])
        self.async_write_ha_state()

    @callback
    def _handle_receiver_availability(self) -> None:
        """Repaint when the receiver-connection availability gate flips.

        Values are untouched — only ``available`` changed — so this just re-reads
        the entity state. The Last-seen sensor, the one device entity that
        overrides ``available``, is repainted by the same signal.
        """
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


class Rtl433ReceiverEntity(Entity):
    """Base for statically-registered entities on the receiver device itself.

    Unlike :class:`Rtl433Entity` (one per device field, availability gated by the
    per-device timeout), receiver entities are one-per-receiver, attach to the receiver device,
    and re-read the coordinator's receiver state on every ``signal_receiver_update``.

    They also subscribe to ``signal_receiver_availability``, which fires only when the
    receiver-connection gate flips (see ``coordinator/_watchdog.py``), so a
    connection-gated receiver entity repaints on exactly the edge where its
    ``available`` changes. Subclasses that do not read the gate (the connectivity
    sensor, the SDR controls) simply re-write an unchanged state on that edge.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, coordinator: Rtl433Coordinator) -> None:
        """Attach to the receiver device and remember the coordinator.

        Identity comes from the coordinator rather than a passed-in id, because a
        receiver entity is one-per-receiver by construction: its device *is* the
        coordinator's receiver, and the two could not disagree without producing
        an entity attached to the wrong server's device page.
        """
        self._coordinator = coordinator
        self._receiver_id = coordinator.receiver_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.receiver_identity)},
        )
        self._unsub_receiver: Callable[[], None] | None = None
        self._unsub_receiver_availability: Callable[[], None] | None = None

    async def async_added_to_hass(self) -> None:
        """Subscribe to the receiver-update and availability-gate signals."""
        await super().async_added_to_hass()
        self._unsub_receiver = async_dispatcher_connect(
            self.hass,
            signal_receiver_update(self._receiver_id),
            self._handle_receiver_update,
        )
        self._unsub_receiver_availability = async_dispatcher_connect(
            self.hass,
            signal_receiver_availability(self._receiver_id),
            self._handle_receiver_update,
        )

    async def async_will_remove_from_hass(self) -> None:
        """Tear down both receiver subscriptions."""
        if self._unsub_receiver is not None:
            self._unsub_receiver()
            self._unsub_receiver = None
        if self._unsub_receiver_availability is not None:
            self._unsub_receiver_availability()
            self._unsub_receiver_availability = None

    @callback
    def _handle_receiver_update(self) -> None:
        """Re-read receiver state and write the entity state."""
        self.async_write_ha_state()


class Rtl433ReceiverControl(Rtl433ReceiverEntity):
    """Shared base for the managed SDR control entities on the receiver device.

    The ``number`` / ``select`` / ``switch`` control platforms each subclass this
    (alongside the matching HA entity mixin) so the four concerns common to every
    control live in one place: attachment to the receiver device (inherited from
    :class:`Rtl433ReceiverEntity`), the :data:`EntityCategory.CONFIG` category, the
    stable unique_id
    ``f"{location_entry_id}:receiver:{receiver_subentry_id}:{object_suffix}"``,
    and the
    device-relative entity name — all sourced from the field's
    :class:`~custom_components.rtl_433.sdr_settings.SdrSetting`.

    Read-back/repaint is inherited too: :class:`Rtl433ReceiverEntity` subscribes to
    ``signal_receiver_update`` and its ``_handle_receiver_update`` calls
    ``async_write_ha_state``, so after a write the coordinator's post-read-back
    ``signal_receiver_update`` repaints the control with the server's actual value.
    """

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: Rtl433Coordinator,
        setting: SdrSetting,
    ) -> None:
        """Attach to the receiver device and adopt the setting's identity/name."""
        super().__init__(coordinator)
        self._setting = setting
        self._attr_unique_id = (
            f"{coordinator.receiver_identity}:{setting.object_suffix}"
        )
        self._attr_name = setting.name

    @property
    def available(self) -> bool:
        """Apply the setting's runtime availability gate to the current meta.

        Re-evaluated on every ``signal_receiver_update`` (inherited repaint), so a
        control like ``hop_interval`` / ``center_frequency`` appears or hides as
        the server's frequency configuration changes. Defaults to available.
        """
        return self._setting.available(self._coordinator.meta)


async def async_setup_receiver_controls(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
    platform: str,
    control_cls: Callable[[Rtl433Coordinator, SdrSetting], Rtl433ReceiverControl],
) -> None:
    """Register every receiver's managed controls for one control platform.

    Shared by the ``number`` / ``select`` / ``switch`` platforms, which differ
    only in their entity class. The platform is forwarded once on the location, so
    this walks the location's receivers: a receiver whose ``manage_settings``
    toggle is off contributes **no** entities; one with management on statically
    registers one ``control_cls`` per :data:`SDR_SETTINGS` entry whose ``platform``
    matches and whose capability gate is satisfied.

    The controls are added **with** their receiver's ``config_subentry_id``: they
    describe that radio and live on that receiver's device, so the subentry is
    their rightful owner and deleting the receiver takes them with it.
    """
    for receiver_id, coordinator in receiver_coordinators(hass, entry).items():
        if not coordinator.manage_settings:
            continue
        async_add_entities(
            (
                control_cls(coordinator, setting)
                for setting in SDR_SETTINGS
                if setting.platform == platform and setting.capability(coordinator.meta)
            ),
            config_subentry_id=receiver_id,
        )


# --------------------------------------------------------------------------- #
# Devices-map helper.                                                          #
# --------------------------------------------------------------------------- #
def resolve_event_type(descriptor: FieldDescriptor, raw: Any) -> str:
    """Return the event type one raw field value should fire as.

    A descriptor may declare an ``event_map`` naming each value it expects --
    a doorbell's ``0`` / ``1`` becoming ``ring`` / ``secret_knock``. A value the
    map does not name, and a descriptor with no map at all (the plain button
    case), fall back to the value as a string, unchanged.

    Shared with the panel's reading preview
    (:func:`~.websocket_api._reading_state`), which has to show the same word the
    entity will fire, or the preview would name a doorbell press one thing and
    the automation another.
    """
    event_map = descriptor.event_map
    return event_map.get(str(raw), str(raw)) if event_map else str(raw)


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
    """Union observed event types into the receiver devices map, stored sorted.

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
# Receiver-wide platform setup (sensor + binary_sensor use the same flow).          #
# --------------------------------------------------------------------------- #
async def async_setup_receiver_platform(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
    platform: str,
    entity_cls: Callable[..., Rtl433Entity],
    per_device_factory: Callable[[Rtl433Coordinator, str, str, str], Rtl433Entity]
    | None = None,
) -> None:
    """Set up one entity platform for every device of every receiver in a location.

    The platform is forwarded once, on the location entry, so this fans out over
    the location's receivers and runs the same per-receiver body for each.

    Per receiver it:

    1. creates entities for every device in ``entry.data[CONF_DEVICES]`` (unioned
       with the fields that receiver's coordinator already knows for it);
    2. subscribes to ``signal_new_device(receiver_id)`` so a newly observed
       device's entities are created at runtime;
    3. for each device, registers a ``signal_device_update`` listener that adds
       entities as previously unseen mapped fields arrive; and
    4. keeps ``entry.data[CONF_DEVICES]`` current via :func:`async_upsert_device`.

    Both the ``sensor`` and ``binary_sensor`` platforms run this independently;
    each only builds descriptors whose ``platform`` matches, and the devices-map
    writes are idempotent unions so the two converge.

    Every entity here is added with **no** ``config_subentry_id``: an RF device
    belongs to the location, not to whichever receiver happened to decode it, and
    a device that gained entities from two subentries would be silently moved
    today and rejected outright in HA Core 2027.8.

    ``per_device_factory`` is an optional caller-supplied hook for a single
    "extra" per-device entity that is not field-driven (e.g. the sensor
    platform's synthetic Last-seen sensor). When set, it is invoked once per
    device — in both the initial devices-map build and the new-device handler —
    as ``per_device_factory(coordinator, receiver_id, device_key, model)``.
    Passing it as a callable (rather than importing the entity class here) keeps
    the dependency direction clean: ``entity.py`` does not import from the
    platform modules. Callers that omit it (e.g. ``binary_sensor``) create no
    extra entity.

    The per-receiver passes share the created-``unique_id`` bookkeeping below,
    because the entity identity is location-scoped: the first receiver to reach a
    device's field builds that entity and every later receiver finds it already
    built. That is the entity half of the union, and it is also what stops the
    second receiver minting a duplicate ``unique_id`` Home Assistant would reject.
    A link field's id carries a receiver segment (:func:`field_unique_id`), so the
    very same set admits one ``rssi`` / ``snr`` / ``last_seen`` entity per
    receiver onto the merged device -- which is the point of excluding them from
    the union.
    """
    # Created ``unique_id``s per ``device_key`` -- field entities and the optional
    # ``per_device_factory`` extra alike -- shared across the location's
    # receivers (see above).
    created: dict[str, set[str]] = {}
    for coordinator in receiver_coordinators(hass, entry).values():
        _setup_receiver_platform(
            hass,
            entry,
            coordinator,
            async_add_entities,
            platform,
            entity_cls,
            per_device_factory,
            created,
        )
    # The initial devices-map build persists any coordinator-known fields that
    # are not stored yet; done after every receiver has registered its listeners
    # so one receiver's write cannot race another's build.
    for device_key, rec in entry.data.get(CONF_DEVICES, {}).items():
        await async_upsert_device(
            hass,
            entry,
            device_key,
            model=rec.get(CONF_MODEL, ""),
            fields=_known_fields(hass, entry, device_key, rec),
        )


def _known_fields(
    hass: HomeAssistant, entry: ConfigEntry, device_key: str, record: dict[str, Any]
) -> set[str]:
    """Union a stored device record's fields with every receiver's live view."""
    fields = set(record.get(DEVICE_FIELDS, []))
    for coordinator in receiver_coordinators(hass, entry).values():
        fields |= coordinator.device_fields.get(device_key, set())
    return fields


def _setup_receiver_platform(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator: Rtl433Coordinator,
    async_add_entities: AddConfigEntryEntitiesCallback,
    platform: str,
    entity_cls: Callable[..., Rtl433Entity],
    per_device_factory: Callable[[Rtl433Coordinator, str, str, str], Rtl433Entity]
    | None,
    created: dict[str, set[str]],
) -> None:
    """Run :func:`async_setup_receiver_platform`'s body for one receiver.

    ``created`` is the caller's location-wide ``unique_id`` bookkeeping, passed in
    rather than owned here so every receiver's pass sees what the others already
    built (see :func:`async_setup_receiver_platform`).
    """
    receiver_id = coordinator.receiver_id

    # Use the per-entry merged registry (shipped library + this location's user
    # overrides) that setup built and cached, so descriptor lookups never re-read
    # the YAML files on the event loop.
    registry: Registry | None = (
        hass.data[DOMAIN]
        .get(DATA_ENTRY_LIBRARY, {})
        .get(entry.entry_id, (None, None))[0]
    )

    # Per-device ``signal_device_update`` unsubscribe handles, so a removed device's
    # listener can be torn down (and re-registered cleanly if it re-appears).
    # These stay per receiver: the listener's job is to notice a field *this*
    # receiver has started reporting, including the per-receiver link fields the
    # union strips out of the location-scoped stream.
    field_unsubs: dict[str, Callable[[], None]] = {}

    def _calibration_for(device_key: str) -> dict[str, Any] | None:
        """Return the validated per-device calibration record, or ``None``.

        Read from the receiver's per-device record on every build so a reload picks up
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
            unique_id = field_unique_id(
                entry.entry_id, device_key, receiver_id, descriptor
            )
            if unique_id in seen:
                continue
            seen.add(unique_id)
            new_entities.append(
                entity_cls(coordinator, receiver_id, device_key, model, descriptor)
            )
        return new_entities

    def _build_extra(device_key: str, model: str) -> list[Rtl433Entity]:
        """Build the optional extra, non-field-driven entity (e.g. Last-seen).

        Deduped on the built entity's own ``unique_id`` against the same shared
        set :func:`_build` uses, rather than on ``device_key``: "how many of
        these exist per device" is the factory's decision, not this helper's.
        Last-seen is a *link* field, so it is one entity per (device x receiver)
        and every receiver's pass contributes one; a receiver-agnostic extra
        would mint the same id twice and the second call returns ``[]``, exactly
        as before. Returns a list so both creation paths can append it
        unconditionally.
        """
        if per_device_factory is None:
            return []
        entity = per_device_factory(coordinator, receiver_id, device_key, model)
        seen = created.setdefault(device_key, set())
        unique_id = entity.unique_id
        if unique_id is None or unique_id in seen:
            return []
        seen.add(unique_id)
        return [entity]

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
            signal_device_update(receiver_id, device_key),
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
        # No ``config_subentry_id``: the device is the location's, not this
        # receiver's (see the module docstring).
        async_add_entities(
            _build(device_key, model, union) + _build_extra(device_key, model)
        )
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
            signal_new_device(receiver_id),
            _handle_new_device,
        )
    )
