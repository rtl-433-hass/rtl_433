"""Tests for the ``Rtl433Event`` firing/dedupe DEBUG trace (Plan 22).

The coordinator-side ingestion/classification trace lives in
``tests/test_coordinator.py``; this file locks the *event entity* side of the
trace contract: a live press logs ``rtl_433 fired ...``, the watchdog
re-dispatch (same object) logs ``skipped watchdog re-paint``, and a suppressed
replay still logs the pre-existing ``suppressed replayed/stale`` INFO line.

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
    """The watchdog re-dispatch (same object) logs the dedupe line, no re-fire."""
    caplog.set_level(logging.DEBUG, logger=_TRACE_LOGGER)
    event = _live()
    with (
        patch.object(event_entity, "_trigger_event") as trigger,
        patch.object(event_entity, "async_write_ha_state"),
    ):
        event_entity._handle_dispatch(event)  # live -> fires
        caplog.clear()
        # Same object re-dispatched by the watchdog -> deduped by identity.
        event_entity._handle_dispatch(event)

    # Fired exactly once (the first dispatch); the re-paint did not re-fire.
    trigger.assert_called_once_with("secret_knock")
    skipped = [
        m for m in caplog.messages if m.startswith("rtl_433 skipped watchdog re-paint")
    ]
    assert len(skipped) == 1
    assert _DEVICE_KEY in skipped[0]
    assert not [m for m in caplog.messages if m.startswith("rtl_433 fired ")]


def test_suppressed_replay_logs_existing_info_line(event_entity, caplog):
    """A replay frame logs the pre-existing INFO line and never fires."""
    caplog.set_level(logging.INFO, logger=_TRACE_LOGGER)
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
        m for m in caplog.messages if m.startswith("rtl_433 suppressed replayed/stale")
    ]
    assert len(suppressed) == 1
    assert _DEVICE_KEY in suppressed[0]
