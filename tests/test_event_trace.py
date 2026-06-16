"""Tests for the ``Rtl433Event`` firing/dedupe DEBUG trace (Plan 22).

The coordinator-side ingestion/classification trace lives in
``tests/test_coordinator.py``; this file locks the *event entity* side of the
trace contract: a live press logs ``rtl_433 fired ...``, the watchdog
availability re-paint (``is_repaint``) logs ``skipped watchdog re-paint`` and
never fires, and a suppressed replay logs the ``ignored an old/duplicate``
DEBUG line.

The entity is exercised directly: ``_handle_dispatch`` is the callback the
dispatcher invokes, so we construct a minimal ``Rtl433Event`` and call it with
crafted ``NormalizedEvent`` carriers, patching the two HA hooks it would invoke
(``_trigger_event`` / ``async_write_ha_state``) so no entity-platform wiring is
needed.
"""

from __future__ import annotations

import logging
from unittest.mock import patch

import pytest

from custom_components.rtl_433.coordinator import Rtl433Coordinator
from custom_components.rtl_433.event import Rtl433Event
from custom_components.rtl_433.mapping import FieldDescriptor
from custom_components.rtl_433.normalizer import NormalizedEvent

_TRACE_LOGGER = "custom_components.rtl_433"
_DEVICE_KEY = "Honeywell-Doorbell-7"
_FIELD_KEY = "secret_knock"


@pytest.fixture
def event_entity(hass, hub_entry_builder) -> Rtl433Event:
    """A doorbell ``secret_knock`` event entity wired to a bare coordinator.

    ``__init__`` only reads ``coordinator.entry.data`` and the descriptor, so a
    plain coordinator is enough; the dispatch hooks are patched per-test.
    """
    entry = hub_entry_builder()
    entry.add_to_hass(hass)
    coordinator = Rtl433Coordinator(hass, entry, host="rtl433.local")
    descriptor = FieldDescriptor(
        field_key=_FIELD_KEY,
        platform="event",
        name="Secret knock",
        object_suffix="secret_knock",
        event_map={"1": "secret_knock"},
    )
    return Rtl433Event(
        coordinator, entry.entry_id, _DEVICE_KEY, "Honeywell-Doorbell", descriptor
    )


def _live(value: int = 1) -> NormalizedEvent:
    """A fresh (non-replay) event carrier for the doorbell field."""
    return NormalizedEvent(
        device_key=_DEVICE_KEY,
        model="Honeywell-Doorbell",
        fields={_FIELD_KEY: value},
        is_replay=False,
    )


def _repaint(value: int = 1) -> NormalizedEvent:
    """A watchdog availability re-paint carrier (cached frame, ``is_repaint``)."""
    return NormalizedEvent(
        device_key=_DEVICE_KEY,
        model="Honeywell-Doorbell",
        fields={_FIELD_KEY: value},
        is_replay=False,
        is_repaint=True,
    )


def test_live_press_logs_fired_line(event_entity, caplog):
    """A live transmission logs the ``rtl_433 fired ...`` DEBUG line and fires."""
    caplog.set_level(logging.DEBUG, logger=_TRACE_LOGGER)
    with (
        patch.object(event_entity, "_trigger_event") as trigger,
        patch.object(event_entity, "async_write_ha_state"),
    ):
        event_entity._handle_dispatch(_live())

    trigger.assert_called_once_with("secret_knock")
    fired = [m for m in caplog.messages if m.startswith("rtl_433 fired ")]
    assert len(fired) == 1
    assert _DEVICE_KEY in fired[0]
    assert f"field={_FIELD_KEY}" in fired[0]


def test_watchdog_re_paint_logs_skip_and_does_not_refire(event_entity, caplog):
    """A watchdog re-paint (``is_repaint``) logs the skip line and never re-fires."""
    caplog.set_level(logging.DEBUG, logger=_TRACE_LOGGER)
    with (
        patch.object(event_entity, "_trigger_event") as trigger,
        patch.object(event_entity, "async_write_ha_state"),
    ):
        event_entity._handle_dispatch(_live())  # live -> fires
        caplog.clear()
        # The watchdog re-dispatches the cached frame marked ``is_repaint``.
        event_entity._handle_dispatch(_repaint())

    # Fired exactly once (the first dispatch); the re-paint did not re-fire.
    trigger.assert_called_once_with("secret_knock")
    skipped = [
        m for m in caplog.messages if m.startswith("rtl_433 skipped watchdog re-paint")
    ]
    assert len(skipped) == 1
    assert _DEVICE_KEY in skipped[0]
    assert not [m for m in caplog.messages if m.startswith("rtl_433 fired ")]


def test_watchdog_re_paint_without_prior_fire_does_not_fire(event_entity, caplog):
    """A re-paint of a replay-seeded cache must not fire (the restart-doorbell bug).

    After a restart the entity never fired a live event (the cached frame arrived
    via a suppressed reconnect replay), so there is no object-identity anchor. The
    first watchdog re-paint must still not emit a phantom ``ring`` -- firing keys
    off ``is_repaint``, not whether a live event was seen first.
    """
    caplog.set_level(logging.DEBUG, logger=_TRACE_LOGGER)
    with (
        patch.object(event_entity, "_trigger_event") as trigger,
        patch.object(event_entity, "async_write_ha_state") as write,
    ):
        event_entity._handle_dispatch(_repaint())

    trigger.assert_not_called()
    write.assert_called_once()
    assert not [m for m in caplog.messages if m.startswith("rtl_433 fired ")]


def test_suppressed_replay_logs_debug_line(event_entity, caplog):
    """A replay frame logs the suppression DEBUG line and never fires."""
    caplog.set_level(logging.DEBUG, logger=_TRACE_LOGGER)
    replay = NormalizedEvent(
        device_key=_DEVICE_KEY,
        model="Honeywell-Doorbell",
        fields={_FIELD_KEY: 1},
        is_replay=True,
    )
    with (
        patch.object(event_entity, "_trigger_event") as trigger,
        patch.object(event_entity, "async_write_ha_state"),
    ):
        event_entity._handle_dispatch(replay)

    trigger.assert_not_called()
    suppressed = [
        m for m in caplog.messages if m.startswith("rtl_433 ignored an old/duplicate")
    ]
    assert len(suppressed) == 1
    assert _DEVICE_KEY in suppressed[0]


def _registered_lines(caplog) -> list[str]:
    """Return the ``rtl_433 <key> registered new event_type ...`` DEBUG lines."""
    return [m for m in caplog.messages if "registered new event_type" in m]


def test_new_event_type_logs_registration_once(event_entity, caplog):
    """The first press of an unseen event_type logs the registration line once.

    ``secret_knock`` (the mapped value ``1``) is pre-seeded into
    ``_attr_event_types`` in ``__init__``, so it is NOT "new". An unmapped raw
    value falls back to its stringified form, which IS a fresh type on first
    sight: it logs the registration line; the same type a second time does not.
    """
    caplog.set_level(logging.DEBUG, logger=_TRACE_LOGGER)
    assert "secret_knock" in event_entity._attr_event_types
    assert "2" not in event_entity._attr_event_types

    with (
        patch.object(event_entity, "_trigger_event"),
        patch.object(event_entity, "async_write_ha_state"),
        patch.object(event_entity, "hass") as hass_mock,
    ):
        hass_mock.async_create_task = lambda coro: coro.close()
        event_entity._handle_dispatch(_live(value=2))  # fresh fallback type "2"
        caplog.clear()
        event_entity._handle_dispatch(_live(value=2))  # same type: no re-log

    assert "2" in event_entity._attr_event_types
    assert _registered_lines(caplog) == []  # second press did not re-log


def test_new_event_type_registration_carries_key_and_type(event_entity, caplog):
    """The registration line includes the device key and the new event_type."""
    caplog.set_level(logging.DEBUG, logger=_TRACE_LOGGER)
    with (
        patch.object(event_entity, "_trigger_event"),
        patch.object(event_entity, "async_write_ha_state"),
        patch.object(event_entity, "hass") as hass_mock,
    ):
        hass_mock.async_create_task = lambda coro: coro.close()
        event_entity._handle_dispatch(_live(value=2))

    lines = _registered_lines(caplog)
    assert len(lines) == 1
    assert _DEVICE_KEY in lines[0]
    assert "registered new event_type 2" in lines[0]


def test_mapped_preseeded_event_type_does_not_register(event_entity, caplog):
    """A press of the pre-seeded mapped type logs no registration line.

    ``secret_knock`` is already in ``_attr_event_types`` from ``__init__``, so the
    first live press of value ``1`` fires but does NOT log a "registered new
    event_type" line.
    """
    caplog.set_level(logging.DEBUG, logger=_TRACE_LOGGER)
    with (
        patch.object(event_entity, "_trigger_event") as trigger,
        patch.object(event_entity, "async_write_ha_state"),
    ):
        event_entity._handle_dispatch(_live(value=1))

    trigger.assert_called_once_with("secret_knock")
    assert _registered_lines(caplog) == []
