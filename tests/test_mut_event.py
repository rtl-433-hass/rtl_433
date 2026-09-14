"""Tests for what ``Rtl433Event.__init__`` puts on the entity.

``tests/test_event_trace.py`` drives the *firing* half of the event entity and
only ever reads the constructor's work sideways -- it checks that a mapped type
is "already seeded" before asserting on a dispatch. That leaves the whole
seeding contract unpinned: which descriptor fields reach the entity, what the
persisted ``event_types`` list contributes across a restart, and the doorbell
guarantee Home Assistant requires.

This file constructs the entity directly and reads those attributes back. They
are what Home Assistant itself reads: ``event_types`` feeds ``EventEntity``'s
``@final`` ``capability_attributes`` (and therefore every automation picker and
``device_trigger`` subtype list), and ``device_class`` decides whether a
doorbell is a doorbell to the rest of the system.
"""

from __future__ import annotations

from typing import Any

from pyrtl_433.library import FieldDescriptor
import pytest

from custom_components.rtl_433.const import DEVICE_EVENT_TYPES, DEVICE_FIELDS, DOMAIN
from custom_components.rtl_433.coordinator import Rtl433Coordinator
from custom_components.rtl_433.event import Rtl433Event
from homeassistant.components.event import DoorbellEventType, EventDeviceClass
from homeassistant.helpers import device_registry as dr

_DEVICE_KEY = "Honeywell-Doorbell-7"
_MODEL = "Honeywell-Doorbell"
_FIELD_KEY = "secret_knock"
_SUFFIX = "secret_knock"


def _descriptor(**overrides: Any) -> FieldDescriptor:
    """A doorbell ``secret_knock`` event descriptor, with fields overridable."""
    return FieldDescriptor(
        **{
            "field_key": _FIELD_KEY,
            "platform": "event",
            "name": "Secret knock",
            "object_suffix": _SUFFIX,
            "event_map": {"1": "secret_knock"},
            **overrides,
        }
    )


def _persisted(event_types: list[str]) -> dict[str, Any]:
    """The hub entry ``devices`` map as a restart leaves it for this device."""
    return {
        _DEVICE_KEY: {
            "model": _MODEL,
            DEVICE_FIELDS: [_FIELD_KEY],
            DEVICE_EVENT_TYPES: {_FIELD_KEY: event_types},
        }
    }


@pytest.fixture
async def build_event(hass, hub_entry_builder):
    """Return a factory building one ``Rtl433Event`` against a real hub entry.

    The constructor reads the descriptor and ``coordinator.entry.data``, and the
    base entity looks up the hub device for ``via_device_id``, so the hub device
    ``async_setup_entry`` always registers first is registered here too.
    """

    def _build(
        descriptor: FieldDescriptor | None = None,
        devices: dict[str, Any] | None = None,
    ) -> Rtl433Event:
        entry = hub_entry_builder(devices=devices)
        entry.add_to_hass(hass)
        dr.async_get(hass).async_get_or_create(
            config_entry_id=entry.entry_id, identifiers={(DOMAIN, entry.entry_id)}
        )
        coordinator = Rtl433Coordinator(hass, entry, host="rtl433.local")
        return Rtl433Event(
            coordinator,
            entry.entry_id,
            _DEVICE_KEY,
            _MODEL,
            descriptor if descriptor is not None else _descriptor(),
        )

    return _build


async def test_the_entity_takes_its_device_class_from_the_descriptor(build_event):
    """A doorbell has to read as a doorbell to the rest of Home Assistant.

    ``device_class`` is what the dashboard uses to pick the doorbell card and
    what voice assistants and the automation UI key off. Dropped, the entity
    still fires and still shows the right word -- it is just a generic event,
    and every doorbell-shaped integration downstream stops recognizing it.
    """
    entity = build_event(_descriptor(device_class=EventDeviceClass.DOORBELL.value))

    assert entity.device_class == EventDeviceClass.DOORBELL


async def test_a_field_with_no_device_class_declares_none(build_event):
    """A plain remote button must not inherit a device class it never declared."""
    entity = build_event(_descriptor())

    assert entity.device_class is None


async def test_event_types_seeds_from_the_declared_event_map(build_event):
    """The mapped names must be advertised before the button is ever pressed.

    ``event_types`` is the list an automation's trigger picker offers. Seeded
    only on first press, a brand-new device would offer nothing to automate
    until someone stood at the door and pressed the button.
    """
    entity = build_event(
        _descriptor(event_map={"1": "secret_knock", "2": "long_press"})
    )

    assert entity.event_types == ["secret_knock", "long_press"]


async def test_a_field_with_no_event_map_advertises_an_empty_list(build_event):
    """An unmapped field must advertise ``[]``, never nothing at all.

    ``EventEntity.capability_attributes`` is ``@final`` and raises if
    ``event_types`` was never set, so a plain pass-through button (whose types
    are discovered as values arrive) would fail to add to Home Assistant at all.
    """
    entity = build_event(_descriptor(event_map=None))

    assert entity.event_types == []


async def test_persisted_event_types_survive_a_restart(build_event):
    """Types learned from the air must still be offered after Home Assistant restarts.

    An unmapped value seen once is persisted to the hub entry. If the
    constructor did not read it back, every restart would silently drop the
    trigger from the automation picker -- and any automation already using it
    would look broken until the device happened to transmit that value again.
    """
    entity = build_event(devices=_persisted(["secret_knock", "double_press"]))

    assert entity.event_types == ["secret_knock", "double_press"]


async def test_declared_types_come_first_and_persisted_ones_are_not_duplicated(
    build_event,
):
    """The picker lists the declared names first and shows each name once.

    The persisted list normally re-states what the map already declares, so
    concatenating the two blindly would show a doorbell owner "ring, ring" in
    the trigger picker with no way to tell the entries apart.
    """
    entity = build_event(
        _descriptor(event_map={"1": "secret_knock"}),
        devices=_persisted(["double_press", "secret_knock"]),
    )

    assert entity.event_types == ["secret_knock", "double_press"]


async def test_seeding_copies_the_persisted_list_rather_than_aliasing_it(build_event):
    """Learning a new type at runtime must not edit the config entry in place.

    ``_handle_dispatch`` appends to ``event_types`` and separately schedules the
    persist. If the seed aliased the stored list, the append alone would mutate
    ``entry.data`` -- so a persist that then failed would leave the running
    instance and the stored entry disagreeing, with the discrepancy invisible
    until a restart lost the type.
    """
    stored = ["secret_knock"]
    entity = build_event(devices=_persisted(stored))

    entity.event_types.append("double_press")

    assert stored == ["secret_knock"]


async def test_a_doorbell_always_advertises_ring_first(build_event):
    """A doorbell entity must offer ``ring`` even when its map never names it.

    Home Assistant's doorbell standard requires it: without ``ring`` in
    ``event_types`` the entity logs a deprecation warning today and stops
    working in 2027.4. It leads the list because it is the press everyone
    automates.
    """
    entity = build_event(
        _descriptor(
            device_class=EventDeviceClass.DOORBELL.value,
            event_map={"1": "secret_knock"},
        )
    )

    assert entity.event_types == [DoorbellEventType.RING, "secret_knock"]


async def test_a_doorbell_that_declares_ring_keeps_its_declared_order(build_event):
    """A map that already names ``ring`` is left alone, not given a second one.

    The guarantee is "``ring`` is present", not "``ring`` is prepended": adding
    it unconditionally would list the same trigger twice for the common doorbell
    whose map already declares it.
    """
    entity = build_event(
        _descriptor(
            device_class=EventDeviceClass.DOORBELL.value,
            event_map={"0": "ring", "1": "secret_knock"},
        )
    )

    assert entity.event_types == ["ring", "secret_knock"]


async def test_a_non_doorbell_is_not_given_a_ring_event(build_event):
    """Only doorbells get the ``ring`` guarantee.

    A garage remote or a plain button offered a phantom ``ring`` trigger that
    can never fire is an automation that silently never runs.
    """
    entity = build_event(_descriptor(event_map={"1": "press"}))

    assert entity.event_types == ["press"]


async def test_the_entity_forwards_its_identity_to_the_base_entity(build_event):
    """Identity is what survives a restart: unique_id and the device it hangs under.

    The unique_id is hub-scoped so two hubs hearing the same doorbell do not
    collide, and the device entry carries the model and nests under the hub via
    ``via_device_id``. Get any of it wrong and the restored entity is a new one:
    the old entity id, name and automations point at nothing.
    """
    entity = build_event()
    hub_entry_id = entity._hub_entry_id

    assert entity.unique_id == f"{hub_entry_id}:{_DEVICE_KEY}:{_SUFFIX}"
    device_info = entity.device_info
    assert device_info["identifiers"] == {(DOMAIN, f"{hub_entry_id}:{_DEVICE_KEY}")}
    assert device_info["model"] == _MODEL
    assert device_info["via_device_id"] == dr.async_get_device_id_by_identifier(
        entity._coordinator.hass,
        (DOMAIN, hub_entry_id),
        config_entry_id=hub_entry_id,
    )
    assert entity.name == "Secret knock"


async def test_a_device_with_no_persisted_entry_seeds_from_the_map_alone(build_event):
    """A device heard for the first time still advertises its declared types.

    The persisted lookup walks four levels of the entry's ``devices`` map, none
    of which exist yet for a device that has never been recorded. Every level
    has to fall back to an empty container rather than raising, or the very
    first entity built for a new doorbell would fail to be added at all.
    """
    entity = build_event(devices={"Some-Other-Device-1": {"model": "Other"}})

    assert entity.event_types == ["secret_knock"]


async def test_a_recorded_device_with_no_event_types_yet_seeds_from_the_map(
    build_event,
):
    """A device recorded before it ever fired an event still builds cleanly.

    Sensors are recorded in ``devices`` as soon as a frame arrives, but the
    ``event_types`` sub-map only appears once an unmapped value is learned, so
    this partial shape is the normal state for most of a device's life.
    """
    entity = build_event(
        devices={_DEVICE_KEY: {"model": _MODEL, DEVICE_FIELDS: [_FIELD_KEY]}}
    )

    assert entity.event_types == ["secret_knock"]


async def test_persisted_types_for_another_field_are_not_borrowed(build_event):
    """Each field's learned types stay its own.

    A doorbell that also exposes a battery-low event would otherwise offer the
    other field's names in this entity's trigger picker, firing automations
    wired to a press on something that was never a press.
    """
    entity = build_event(
        devices={
            _DEVICE_KEY: {
                "model": _MODEL,
                DEVICE_FIELDS: [_FIELD_KEY, "battery_low"],
                DEVICE_EVENT_TYPES: {"battery_low": ["depleted"]},
            }
        }
    )

    assert entity.event_types == ["secret_knock"]


async def test_persisted_types_for_another_device_are_not_borrowed(build_event):
    """One device's learned types never leak into another's picker.

    The lookup is keyed by ``device_key``; keyed by anything else, every event
    entity on the hub would advertise the same merged list of every type any
    device ever transmitted.
    """
    entity = build_event(
        devices={
            "Honeywell-Doorbell-9": {
                "model": _MODEL,
                DEVICE_FIELDS: [_FIELD_KEY],
                DEVICE_EVENT_TYPES: {_FIELD_KEY: ["neighbours_bell"]},
            }
        }
    )

    assert entity.event_types == ["secret_knock"]
