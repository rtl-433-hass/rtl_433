"""Integration tests for the motion ``binary_sensor`` timer + migration.

Motion moved off the ``event`` platform and is now a detect-only
``binary_sensor.*_motion`` (device_class ``occupancy``): a detection turns it
``on`` and the integration synthesizes the ``off`` after an effective
clear-delay (default 90s, per-device overridable). These tests drive the timer
lifecycle, the override resolution, the cancel-on-remove path, and the one-shot
``event.*_motion`` -> ``binary_sensor.*_motion`` registry migration through the
live hub harness (reusing ``_setup_hub`` / ``_feed`` from the lifecycle suite).
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from freezegun import freeze_time
import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.rtl_433.const import (
    CONF_DEVICES,
    CONF_MODEL,
    DEFAULT_MOTION_CLEAR_DELAY,
    DEVICE_EVENT_TYPES,
    DEVICE_FIELDS,
    DEVICE_MOTION_CLEAR_DELAY,
    DOMAIN,
)
from custom_components.rtl_433.coordinator.base import Rtl433Client
from custom_components.rtl_433.repairs import ISSUE_MOTION_MOVED
from homeassistant.helpers import entity_registry as er, issue_registry as ir
from homeassistant.util import dt as dt_util
from tests.test_lifecycle import _coordinator, _feed, _setup_hub

# A PIR/occupancy device whose only field is ``motion`` (raw value 1 on detect).
_MODEL = "GenericPIR-Z1"
_DEVICE_KEY = "GenericPIR-Z1-5"
_MOTION_EVENT = {"model": _MODEL, "id": 5, "motion": 1}


@pytest.fixture(autouse=True)
def _no_socket():
    """Stub the connect loop so the coordinator never opens a real WebSocket."""

    async def _noop(self) -> None:
        return None

    with patch.object(Rtl433Client, "start", _noop):
        yield


def _motion_devices(**record):
    """Build a devices map seeding the motion device, with extra record keys."""
    return {_DEVICE_KEY: {CONF_MODEL: _MODEL, DEVICE_FIELDS: ["motion"], **record}}


def _motion_eid(hass, hub):
    """Resolve the device's motion ``binary_sensor`` entity_id (must exist)."""
    ent_reg = er.async_get(hass)
    eid = ent_reg.async_get_entity_id(
        "binary_sensor", DOMAIN, f"{hub.entry_id}:{_DEVICE_KEY}:motion"
    )
    assert eid is not None
    return eid


async def _advance_to(hass, start, seconds):
    """Fire the time-changed clock to ``start + seconds`` and run scheduled work.

    ``async_fire_time_changed`` fires every callback scheduled at or before the
    given wall-clock instant; passing an absolute target derived from a fixed
    ``start`` (rather than a fresh ``utcnow()`` each call) keeps successive
    advances monotonic so a timer armed at ``start`` actually elapses.
    """
    async_fire_time_changed(hass, start + timedelta(seconds=seconds))
    await hass.async_block_till_done()


# --------------------------------------------------------------------------- #
# Detection turns on; the synthesized off fires after the clear delay.         #
# --------------------------------------------------------------------------- #
async def test_detection_turns_on_then_auto_off(hass, hub_entry_builder):
    """A detection sets the motion sensor ``on``; the delay synthesizes ``off``."""
    hub = await _setup_hub(hass, hub_entry_builder, devices=_motion_devices())
    eid = _motion_eid(hass, hub)
    assert hass.states.get(eid).attributes["device_class"] == "occupancy"

    start = dt_util.utcnow()
    _feed(_coordinator(hass, hub), _MOTION_EVENT)
    await hass.async_block_till_done()
    assert hass.states.get(eid).state == "on"

    # Just shy of the default delay: still on.
    await _advance_to(hass, start, DEFAULT_MOTION_CLEAR_DELAY - 5)
    assert hass.states.get(eid).state == "on"

    # Past the default delay: the synthesized off fired.
    await _advance_to(hass, start, DEFAULT_MOTION_CLEAR_DELAY + 5)
    assert hass.states.get(eid).state == "off"


# --------------------------------------------------------------------------- #
# A second detection within the window reschedules the off.                    #
# --------------------------------------------------------------------------- #
async def test_retrigger_reschedules_off(hass, hub_entry_builder):
    """Two detections within the window keep it on; off only after a quiet window."""
    hub = await _setup_hub(hass, hub_entry_builder, devices=_motion_devices())
    coordinator = _coordinator(hass, hub)
    eid = _motion_eid(hass, hub)

    # Drive the whole flow under a single frozen, ticking clock so the re-armed
    # ``async_call_later`` (which anchors its fire instant to ``utcnow()`` at
    # arm time) is re-based at the retrigger instant.
    start = dt_util.utcnow()
    retrigger_at = DEFAULT_MOTION_CLEAR_DELAY - 20
    with freeze_time(start) as frozen:
        _feed(coordinator, _MOTION_EVENT)
        await hass.async_block_till_done()
        assert hass.states.get(eid).state == "on"

        # Advance partway through the window and retrigger -> the timer restarts
        # from this (later) instant.
        frozen.move_to(start + timedelta(seconds=retrigger_at))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()
        assert hass.states.get(eid).state == "on"
        _feed(coordinator, _MOTION_EVENT)
        await hass.async_block_till_done()

        # Past the original (first) window but within the rescheduled one -> on.
        frozen.move_to(start + timedelta(seconds=DEFAULT_MOTION_CLEAR_DELAY + 10))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()
        assert hass.states.get(eid).state == "on"

        # A full quiet window from the last (retrigger) detection elapses -> off.
        frozen.move_to(
            start + timedelta(seconds=retrigger_at + DEFAULT_MOTION_CLEAR_DELAY + 5)
        )
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()
        assert hass.states.get(eid).state == "off"


# --------------------------------------------------------------------------- #
# Per-device override: the resolver honors the device's clear delay.           #
# --------------------------------------------------------------------------- #
async def test_per_device_override_drives_off(hass, hub_entry_builder):
    """With a per-device override, the off fires on the override, not the default."""
    override = 15  # much shorter than the 90s default
    hub = await _setup_hub(
        hass,
        hub_entry_builder,
        devices=_motion_devices(**{DEVICE_MOTION_CLEAR_DELAY: override}),
    )
    eid = _motion_eid(hass, hub)

    start = dt_util.utcnow()
    _feed(_coordinator(hass, hub), _MOTION_EVENT)
    await hass.async_block_till_done()
    assert hass.states.get(eid).state == "on"

    # Still on just before the override elapses (proving it is not instantaneous).
    await _advance_to(hass, start, override - 5)
    assert hass.states.get(eid).state == "on"

    # Past the override but well within the default 90s window -> already off,
    # proving the override (not the default) drove the auto-off.
    await _advance_to(hass, start, override + 2)
    assert hass.states.get(eid).state == "off"
    assert override + 2 < DEFAULT_MOTION_CLEAR_DELAY


# --------------------------------------------------------------------------- #
# Removing the entity with a timer pending must not write or raise later.      #
# --------------------------------------------------------------------------- #
async def test_remove_cancels_pending_timer(hass, hub_entry_builder):
    """Removing the entity with a pending timer produces no late write / error."""
    hub = await _setup_hub(hass, hub_entry_builder, devices=_motion_devices())
    eid = _motion_eid(hass, hub)

    start = dt_util.utcnow()
    _feed(_coordinator(hass, hub), _MOTION_EVENT)
    await hass.async_block_till_done()
    assert hass.states.get(eid).state == "on"

    # Remove the entity from the registry while the clear timer is armed.
    ent_reg = er.async_get(hass)
    ent_reg.async_remove(eid)
    await hass.async_block_till_done()
    assert hass.states.get(eid) is None

    # Fire well past the clear delay: the cancelled timer must not write state
    # back (which would resurrect the removed entity) and must not raise.
    await _advance_to(hass, start, DEFAULT_MOTION_CLEAR_DELAY + 10)
    assert hass.states.get(eid) is None


# --------------------------------------------------------------------------- #
# Migration: a seeded ``event.*_motion`` entity is swept and announced.        #
# --------------------------------------------------------------------------- #
async def test_migration_removes_event_entity_and_raises_issue(hass, hub_entry_builder):
    """A pre-seeded ``event.*_motion`` registry entry is removed at setup.

    Afterward: that event entity is gone, ``motion`` is dropped from the persisted
    ``DEVICE_EVENT_TYPES``, the ``motion_moved_to_binary_sensor`` repairs issue
    exists, and no ``event.*_motion`` entity is (re)created — only the
    ``binary_sensor.*_motion`` remains.
    """
    entry_id = "motionmighub01"
    hub = hub_entry_builder(
        availability_timeout=600,
        entry_id=entry_id,
        devices=_motion_devices(**{DEVICE_EVENT_TYPES: {"motion": ["1"]}}),
    )
    hub.add_to_hass(hass)

    # Pre-seed the orphaned pre-fix ``event.*_motion`` registry entry.
    ent_reg = er.async_get(hass)
    motion_unique_id = f"{entry_id}:{_DEVICE_KEY}:motion"
    event_entry = ent_reg.async_get_or_create(
        "event",
        DOMAIN,
        motion_unique_id,
        config_entry=hub,
    )
    seeded_event_eid = event_entry.entity_id

    assert await hass.config_entries.async_setup(hub.entry_id)
    await hass.async_block_till_done()

    # The orphaned event entity is gone (removed by the migration sweep).
    assert hass.states.get(seeded_event_eid) is None
    assert ent_reg.async_get_entity_id("event", DOMAIN, motion_unique_id) is None

    # ``motion`` was dropped from the persisted event-type slots.
    entry = hass.config_entries.async_get_entry(hub.entry_id)
    persisted = entry.data[CONF_DEVICES][_DEVICE_KEY].get(DEVICE_EVENT_TYPES, {})
    assert "motion" not in persisted

    # The repairs issue announcing the move exists.
    issue = ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_MOTION_MOVED)
    assert issue is not None
    assert issue.translation_key == ISSUE_MOTION_MOVED

    # No motion *event* entity exists; the binary_sensor one does.
    assert ent_reg.async_get_entity_id("event", DOMAIN, motion_unique_id) is None
    assert (
        ent_reg.async_get_entity_id("binary_sensor", DOMAIN, motion_unique_id)
        is not None
    )
