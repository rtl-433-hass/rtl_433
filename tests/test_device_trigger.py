"""Tests for the device-automation triggers exposed for event entities.

These exercise ``custom_components/rtl_433/device_trigger.py`` end-to-end against
a live hub: a seeded ``button`` event device is resolved in the device registry,
``async_get_triggers`` is asserted to enumerate the per-entity base trigger plus
the persisted ``A``/``B`` subtypes, and the firing behaviour is checked by
attaching real triggers and feeding transmissions through the coordinator.

The headline regression guard is the same-value-repeat behaviour: both the base
trigger (core ``state`` trigger, match_all) and the subtyped trigger (custom
``async_track_state_change_event`` listener) must fire on a consecutive
same-value press — the very reason the subtyped path cannot reuse the core
state trigger's ``attribute``/``to`` filter, which dedupes ``old == new``.
"""

from __future__ import annotations

from datetime import timedelta

from freezegun import freeze_time

from custom_components.rtl_433.const import (
    CONF_MODEL,
    DEVICE_EVENT_TYPES,
    DEVICE_FIELDS,
    DOMAIN,
)
from custom_components.rtl_433.device_trigger import (
    CONF_SUBTYPE,
    TRIGGER_TYPE_TRIGGERED,
    TRIGGER_TYPE_TRIGGERED_SUBTYPE,
    async_get_triggers,
)
from homeassistant.const import (
    CONF_DEVICE_ID,
    CONF_DOMAIN,
    CONF_ENTITY_ID,
    CONF_PLATFORM,
    CONF_TYPE,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.trigger import async_initialize_triggers
from homeassistant.util import dt as dt_util

# Module-local helpers from the lifecycle suite (not injectable fixtures): a
# single hub set up through ``async_setup_entry`` with the WebSocket stubbed,
# plus a frame-injection helper that drives the live coordinator.
from tests.test_lifecycle import _coordinator, _feed, _setup_hub

DEVICE_KEY = "Acurite-606TX-42"
MODEL = "Acurite-606TX"
DEVICE_ID = 42


async def _setup_button_hub(hass, hub_entry_builder):
    """Set up a hub seeded with a single ``button`` event device (types A/B)."""
    hub = await _setup_hub(
        hass,
        hub_entry_builder,
        devices={
            DEVICE_KEY: {
                CONF_MODEL: MODEL,
                DEVICE_FIELDS: ["button"],
                DEVICE_EVENT_TYPES: {"button": ["A", "B"]},
            }
        },
    )
    return hub


def _resolve_device_id(hass: HomeAssistant, hub_entry_id: str) -> str:
    """Resolve the nested RF device's HA ``device_id`` from its identifiers."""
    device = dr.async_get(hass).async_get_device(
        identifiers={(DOMAIN, f"{hub_entry_id}:{DEVICE_KEY}")}
    )
    assert device is not None
    return device.id


def _button_frame(code: str) -> dict:
    """Build a single ``button`` transmission frame with the given code."""
    return {"model": MODEL, "id": DEVICE_ID, "button": code}


async def _feed_presses(hass: HomeAssistant, coordinator, codes: list[str]) -> None:
    """Feed each button code as a distinct transmission at a fresh instant.

    ``Rtl433Event`` stamps a new state timestamp per fire, so two genuine presses
    are distinct states — but two presses at the *same* whole-second wall clock
    would render the identical ISO state, which the core ``state`` trigger
    (backing the base path) dedupes. Real consecutive presses are seconds apart,
    so advancing the clock per press models that and keeps the states distinct.
    """
    start = dt_util.utcnow()
    for offset, code in enumerate(codes):
        with freeze_time(start + timedelta(seconds=offset)):
            _feed(coordinator, _button_frame(code))
            await hass.async_block_till_done()


async def _attach(hass: HomeAssistant, trigger: dict) -> tuple[list, callable]:
    """Attach one device trigger; return (captured-calls list, detach callable).

    The action is a plain ``@callback`` that appends the trigger payload, which
    is the simplest reliable way to count fires under the test harness.
    """
    calls: list = []

    @callback
    def _action(run_variables, context=None):
        calls.append(run_variables)

    remove = await async_initialize_triggers(
        hass,
        [trigger],
        _action,
        DOMAIN,
        "test",
        lambda *args, **kwargs: None,
    )
    assert remove is not None
    return calls, remove


# --------------------------------------------------------------------------- #
# Enumeration.                                                                 #
# --------------------------------------------------------------------------- #
async def test_async_get_triggers_enumerates_base_and_subtypes(hass, hub_entry_builder):
    """A seeded button device yields its base trigger + the A/B subtypes."""
    hub = await _setup_button_hub(hass, hub_entry_builder)
    device_id = _resolve_device_id(hass, hub.entry_id)

    ent_reg = er.async_get(hass)
    button_entry = ent_reg.async_get(
        ent_reg.async_get_entity_id(
            "event", DOMAIN, f"{hub.entry_id}:{DEVICE_KEY}:button"
        )
    )
    assert button_entry is not None

    triggers = await async_get_triggers(hass, device_id)

    base = {
        CONF_PLATFORM: "device",
        CONF_DOMAIN: DOMAIN,
        CONF_DEVICE_ID: device_id,
        CONF_ENTITY_ID: button_entry.id,
        CONF_TYPE: TRIGGER_TYPE_TRIGGERED,
    }
    assert base in triggers
    for code in ("A", "B"):
        assert {
            **base,
            CONF_TYPE: TRIGGER_TYPE_TRIGGERED_SUBTYPE,
            CONF_SUBTYPE: code,
        } in triggers

    # Exactly the base + the two subtypes for the single event entity, nothing
    # more (the device has only the one ``button`` event entity).
    assert len(triggers) == 3


# --------------------------------------------------------------------------- #
# Base trigger fires on every transmission, including a same-value repeat.     #
# --------------------------------------------------------------------------- #
async def test_base_trigger_fires_per_transmission_incl_repeat(hass, hub_entry_builder):
    """The base trigger fires once per transmission — A then A => two fires."""
    hub = await _setup_button_hub(hass, hub_entry_builder)
    device_id = _resolve_device_id(hass, hub.entry_id)
    coordinator = _coordinator(hass, hub)

    triggers = await async_get_triggers(hass, device_id)
    base = next(t for t in triggers if t[CONF_TYPE] == TRIGGER_TYPE_TRIGGERED)
    calls, remove = await _attach(hass, base)

    await _feed_presses(hass, coordinator, ["A", "A"])

    assert len(calls) == 2
    remove()


# --------------------------------------------------------------------------- #
# Subtyped trigger fires on every matching press, including a same-value       #
# repeat (the behaviour the custom listener exists to provide).                #
# --------------------------------------------------------------------------- #
async def test_subtype_trigger_fires_on_every_matching_press_incl_repeat(
    hass, hub_entry_builder
):
    """The A-subtyped trigger fires on each matching press — A,A => two fires."""
    hub = await _setup_button_hub(hass, hub_entry_builder)
    device_id = _resolve_device_id(hass, hub.entry_id)
    coordinator = _coordinator(hass, hub)

    triggers = await async_get_triggers(hass, device_id)
    subtype_a = next(
        t
        for t in triggers
        if t[CONF_TYPE] == TRIGGER_TYPE_TRIGGERED_SUBTYPE and t[CONF_SUBTYPE] == "A"
    )
    calls, remove = await _attach(hass, subtype_a)

    await _feed_presses(hass, coordinator, ["A", "A"])

    assert len(calls) == 2
    remove()


# --------------------------------------------------------------------------- #
# Neither trigger re-fires on the entity's restore at HA restart.              #
# --------------------------------------------------------------------------- #
async def test_triggers_do_not_fire_on_restore_at_startup(hass, hub_entry_builder):
    """The restored last event (``old_state is None``) must not re-fire triggers.

    Across a restart HA's ``EventEntity`` restores its last ``event_type`` +
    timestamp for display, which surfaces as a ``state_changed`` with
    ``old_state is None`` carrying the old ``event_type``. Without the listener's
    restore guard this re-delivered a stale event (e.g. a doorbell "ring" from
    days ago) on every HA restart. Both the base and the subtyped trigger must
    ignore it — yet a genuine press afterwards still fires.
    """
    hub = await _setup_button_hub(hass, hub_entry_builder)
    device_id = _resolve_device_id(hass, hub.entry_id)
    coordinator = _coordinator(hass, hub)

    ent_reg = er.async_get(hass)
    entity_id = ent_reg.async_get_entity_id(
        "event", DOMAIN, f"{hub.entry_id}:{DEVICE_KEY}:button"
    )

    triggers = await async_get_triggers(hass, device_id)
    base = next(t for t in triggers if t[CONF_TYPE] == TRIGGER_TYPE_TRIGGERED)
    subtype_a = next(
        t
        for t in triggers
        if t[CONF_TYPE] == TRIGGER_TYPE_TRIGGERED_SUBTYPE and t[CONF_SUBTYPE] == "A"
    )
    base_calls, remove_base = await _attach(hass, base)
    sub_calls, remove_sub = await _attach(hass, subtype_a)

    # Model the restore: drop the entity's current state, then re-set it with the
    # restored event_type so the resulting state_changed carries ``old_state is
    # None`` — exactly what EventEntity's restore produces on startup.
    hass.states.async_remove(entity_id)
    await hass.async_block_till_done()
    hass.states.async_set(
        entity_id,
        dt_util.utcnow().isoformat(),
        {"event_type": "A", "event_types": ["A", "B"]},
    )
    await hass.async_block_till_done()

    assert base_calls == []
    assert sub_calls == []

    # A genuine press afterwards still fires both (old_state is now non-None).
    await _feed_presses(hass, coordinator, ["A"])
    assert len(base_calls) == 1
    assert len(sub_calls) == 1

    remove_base()
    remove_sub()


# --------------------------------------------------------------------------- #
# Subtyped trigger stays silent for a non-matching event_type.                 #
# --------------------------------------------------------------------------- #
async def test_subtype_trigger_silent_for_non_matching_type(hass, hub_entry_builder):
    """The A-subtyped trigger does not fire when a B press arrives."""
    hub = await _setup_button_hub(hass, hub_entry_builder)
    device_id = _resolve_device_id(hass, hub.entry_id)
    coordinator = _coordinator(hass, hub)

    triggers = await async_get_triggers(hass, device_id)
    subtype_a = next(
        t
        for t in triggers
        if t[CONF_TYPE] == TRIGGER_TYPE_TRIGGERED_SUBTYPE and t[CONF_SUBTYPE] == "A"
    )
    calls, remove = await _attach(hass, subtype_a)

    await _feed_presses(hass, coordinator, ["B"])

    assert calls == []
    remove()
