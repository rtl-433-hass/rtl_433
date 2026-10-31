"""Tests for noticing a device that came back under a new transmitter id.

A battery swap gives many 433 MHz sensors a new random id, so the device the user
added goes quiet while an identical-looking candidate appears in the pending
list. :mod:`custom_components.rtl_433.id_change` pairs the two -- only when every
clue agrees and the pairing is unambiguous -- and then either raises a fixable
repair or, for a device that opted in, re-keys it straight away.

The matcher is exercised directly against a real coordinator whose runtime maps
are set by hand, because what matters is the decision on each combination of
clues. The tracker, the repair flow and the automatic path drive the real
``async_setup_entry`` so the replace underneath is the one users get.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from pyrtl_433.normalizer import NormalizedEvent
import pytest
from pytest_homeassistant_custom_component.common import async_capture_events

from custom_components.rtl_433.const import (
    CONF_DEVICES,
    CONF_MODEL,
    DEVICE_AUTO_REPLACE,
    DEVICE_FIELDS,
    DOMAIN,
)
from custom_components.rtl_433.coordinator import PendingDevice
from custom_components.rtl_433.id_change import (
    EVENT_DEVICE_ID_CHANGED,
    FOLLOW_WINDOW,
    HUMIDITY_TOLERANCE,
    ISSUE_ID_CHANGED,
    MIN_SIGHTINGS,
    SILENCE,
    TEMPERATURE_TOLERANCE_C,
    IdChange,
    find_id_changes,
)
from custom_components.rtl_433.settings import build_device_data
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, issue_registry as ir
from homeassistant.util import dt as dt_util

MODEL = "Acurite-606TX"
OLD_KEY = f"{MODEL}-24-ch1"
NEW_KEY = f"{MODEL}-203-ch1"
OLD_RECORD: dict[str, Any] = {CONF_MODEL: MODEL, DEVICE_FIELDS: ["temperature_C"]}
NOW = dt_util.parse_datetime("2026-09-27T22:30:00+00:00")


def _event(
    key: str,
    temperature: float | None = 2.0,
    humidity: float | None = None,
    **identity: Any,
) -> NormalizedEvent:
    """A normalized 606TX frame for ``key``."""
    fields: dict[str, Any] = {"battery_ok": 1, "snr": 18.0}
    if temperature is not None:
        fields["temperature_C"] = temperature
    if humidity is not None:
        fields["humidity"] = humidity
    return NormalizedEvent(
        device_key=key,
        model=MODEL,
        identity={"model": MODEL, **identity},
        fields=fields,
    )


def _pending(
    key: str,
    *,
    count: int = MIN_SIGHTINGS,
    first_seen: datetime | None = None,
    temperature: float | None = 4.0,
    humidity: float | None = None,
    model: str = MODEL,
) -> PendingDevice:
    """A candidate as the coordinator records it."""
    event = _event(key, temperature, humidity)
    first = first_seen or NOW - timedelta(minutes=5)
    return PendingDevice(
        key=key,
        model=model,
        event=event,
        count=count,
        first_seen=first,
        last_seen=NOW,
        fields=dict(event.fields),
    )


async def _setup_hub(hass: HomeAssistant, hub_entry_builder, devices):
    hub = hub_entry_builder(availability_timeout=600, devices=devices)
    hub.add_to_hass(hass)
    assert await hass.config_entries.async_setup(hub.entry_id)
    await hass.async_block_till_done()
    return hub, hass.data[DOMAIN][hub.entry_id]


def _quiet(
    coordinator,
    key: str = OLD_KEY,
    *,
    minutes: float = 20,
    temperature=2.0,
    humidity=None,
):
    """Leave ``key`` last heard ``minutes`` before NOW, with those readings."""
    coordinator.last_seen[key] = NOW - timedelta(minutes=minutes)
    coordinator.devices[key] = _event(key, temperature, humidity)


# --------------------------------------------------------------------------- #
# The matcher: every clue has to agree, and the pairing has to be unique.     #
# --------------------------------------------------------------------------- #
async def test_a_battery_swap_is_recognised(hass, hub_entry_builder, no_socket):
    """Quiet device + one same-kind candidate heard twice, similar reading."""
    hub, coordinator = await _setup_hub(hass, hub_entry_builder, {OLD_KEY: OLD_RECORD})
    _quiet(coordinator)
    coordinator.pending[NEW_KEY] = _pending(NEW_KEY)

    changes = find_id_changes(hub, coordinator, NOW)

    assert [(c.old_key, c.new_key) for c in changes] == [(OLD_KEY, NEW_KEY)]
    assert changes[0].old_temperature == pytest.approx(2.0)
    assert changes[0].new_temperature == pytest.approx(4.0)
    assert changes[0].signal == pytest.approx(18.0)
    # Last heard 20 min ago, candidate first heard 5 min ago.
    assert changes[0].gap == timedelta(minutes=15)
    assert changes[0].may_follow_automatically


async def test_not_while_the_old_device_is_still_talking(
    hass, hub_entry_builder, no_socket
):
    """Heard within ``SILENCE``: the sensor has not actually gone anywhere."""
    hub, coordinator = await _setup_hub(hass, hub_entry_builder, {OLD_KEY: OLD_RECORD})
    _quiet(coordinator, minutes=SILENCE.total_seconds() / 60 - 1)
    coordinator.pending[NEW_KEY] = _pending(NEW_KEY)

    assert find_id_changes(hub, coordinator, NOW) == []


async def test_not_when_the_old_device_was_heard_after_the_candidate_appeared(
    hass, hub_entry_builder, no_socket
):
    """Both transmitting at once means two devices, e.g. a neighbour's."""
    hub, coordinator = await _setup_hub(hass, hub_entry_builder, {OLD_KEY: OLD_RECORD})
    _quiet(coordinator, minutes=15)
    coordinator.pending[NEW_KEY] = _pending(
        NEW_KEY, first_seen=NOW - timedelta(minutes=30)
    )

    assert find_id_changes(hub, coordinator, NOW) == []


async def test_not_for_a_candidate_heard_once(hass, hub_entry_builder, no_socket):
    """A single sighting is as often a bad decode as a device."""
    hub, coordinator = await _setup_hub(hass, hub_entry_builder, {OLD_KEY: OLD_RECORD})
    _quiet(coordinator)
    coordinator.pending[NEW_KEY] = _pending(NEW_KEY, count=MIN_SIGHTINGS - 1)

    assert find_id_changes(hub, coordinator, NOW) == []


@pytest.mark.parametrize(
    "new_key",
    [
        f"{MODEL}-203-ch2",  # another channel
        f"{MODEL}-203",  # no channel at all
        f"{MODEL}-203-ch1-st1",  # a subtype the old one did not have
    ],
)
async def test_not_across_channels_or_subtypes(
    hass, hub_entry_builder, no_socket, new_key
):
    """Only the id changes on a battery swap; everything else must match."""
    hub, coordinator = await _setup_hub(hass, hub_entry_builder, {OLD_KEY: OLD_RECORD})
    _quiet(coordinator)
    coordinator.pending[new_key] = _pending(new_key)

    assert find_id_changes(hub, coordinator, NOW) == []


async def test_not_across_models(hass, hub_entry_builder, no_socket):
    """A different model is a different device, whatever its channel."""
    hub, coordinator = await _setup_hub(hass, hub_entry_builder, {OLD_KEY: OLD_RECORD})
    _quiet(coordinator)
    other = "Acurite-Tower-203-ch1"
    coordinator.pending[other] = _pending(other, model="Acurite-Tower")

    assert find_id_changes(hub, coordinator, NOW) == []


async def test_not_when_the_readings_disagree(hass, hub_entry_builder, no_socket):
    """A fridge probe does not come back reading like an outdoor sensor."""
    hub, coordinator = await _setup_hub(hass, hub_entry_builder, {OLD_KEY: OLD_RECORD})
    _quiet(coordinator, temperature=2.0)
    coordinator.pending[NEW_KEY] = _pending(
        NEW_KEY, temperature=2.0 + TEMPERATURE_TOLERANCE_C + 1
    )

    assert find_id_changes(hub, coordinator, NOW) == []


async def test_a_missing_reading_does_not_block_the_match(
    hass, hub_entry_builder, no_socket
):
    """Temperature is a check only when both sides report one."""
    hub, coordinator = await _setup_hub(hass, hub_entry_builder, {OLD_KEY: OLD_RECORD})
    _quiet(coordinator, temperature=None)
    coordinator.pending[NEW_KEY] = _pending(NEW_KEY)

    assert [c.new_key for c in find_id_changes(hub, coordinator, NOW)] == [NEW_KEY]


async def test_not_when_the_humidity_disagrees(hass, hub_entry_builder, no_socket):
    """Humidity is compared like temperature: an outdoor sensor is not a cellar's."""
    hub, coordinator = await _setup_hub(hass, hub_entry_builder, {OLD_KEY: OLD_RECORD})
    _quiet(coordinator, humidity=40.0)
    coordinator.pending[NEW_KEY] = _pending(
        NEW_KEY, humidity=40.0 + HUMIDITY_TOLERANCE + 1
    )

    assert find_id_changes(hub, coordinator, NOW) == []


async def test_humidity_within_tolerance_still_matches(
    hass, hub_entry_builder, no_socket
):
    """A hand on the sensor moves humidity too; a moderate change is fine."""
    hub, coordinator = await _setup_hub(hass, hub_entry_builder, {OLD_KEY: OLD_RECORD})
    _quiet(coordinator, humidity=40.0)
    coordinator.pending[NEW_KEY] = _pending(
        NEW_KEY, humidity=40.0 + HUMIDITY_TOLERANCE - 1
    )

    assert [c.new_key for c in find_id_changes(hub, coordinator, NOW)] == [NEW_KEY]


async def test_a_device_with_no_comparable_reading_matches_on_the_other_clues(
    hass, hub_entry_builder, no_socket
):
    """A door or motion sensor has nothing to compare; structure decides."""
    hub, coordinator = await _setup_hub(hass, hub_entry_builder, {OLD_KEY: OLD_RECORD})
    _quiet(coordinator, temperature=None)
    coordinator.pending[NEW_KEY] = _pending(NEW_KEY, temperature=None)

    assert [c.new_key for c in find_id_changes(hub, coordinator, NOW)] == [NEW_KEY]


@pytest.mark.parametrize(
    ("gap", "expected"),
    [
        (FOLLOW_WINDOW, True),
        (FOLLOW_WINDOW + timedelta(seconds=1), False),
        (None, False),
    ],
)
def test_only_a_short_gap_is_followed_automatically(gap, expected):
    """Within the window: a battery swap. Longer, or unknown: ask instead."""
    assert IdChange(OLD_KEY, NEW_KEY, gap=gap).may_follow_automatically is expected


async def test_not_when_two_candidates_fit(hass, hub_entry_builder, no_socket):
    """Two plausible successors: guessing would be wrong half the time."""
    hub, coordinator = await _setup_hub(hass, hub_entry_builder, {OLD_KEY: OLD_RECORD})
    _quiet(coordinator)
    coordinator.pending[NEW_KEY] = _pending(NEW_KEY)
    coordinator.pending[f"{MODEL}-99-ch1"] = _pending(f"{MODEL}-99-ch1")

    assert find_id_changes(hub, coordinator, NOW) == []


async def test_not_when_two_quiet_devices_fit_one_candidate(
    hass, hub_entry_builder, no_socket
):
    """Two identical sensors re-batteried at once: leave the choice to the user."""
    second = f"{MODEL}-77-ch1"
    hub, coordinator = await _setup_hub(
        hass, hub_entry_builder, {OLD_KEY: OLD_RECORD, second: dict(OLD_RECORD)}
    )
    _quiet(coordinator)
    _quiet(coordinator, second)
    coordinator.pending[NEW_KEY] = _pending(NEW_KEY)

    assert find_id_changes(hub, coordinator, NOW) == []


async def test_devices_without_an_id_are_never_paired(
    hass, hub_entry_builder, no_socket
):
    """A channel-only key has no id to re-roll."""
    old = "Foo-ch1"
    hub, coordinator = await _setup_hub(
        hass, hub_entry_builder, {old: {CONF_MODEL: "Foo", DEVICE_FIELDS: []}}
    )
    _quiet(coordinator, old)
    coordinator.pending["Foo-5-ch1"] = _pending("Foo-5-ch1", model="Foo")

    assert find_id_changes(hub, coordinator, NOW) == []


async def test_a_device_not_heard_this_session_counts_from_the_connection(
    hass, hub_entry_builder, no_socket
):
    """After a restart the silence is measured from when the hub connected."""
    hub, coordinator = await _setup_hub(hass, hub_entry_builder, {OLD_KEY: OLD_RECORD})
    coordinator.last_seen.pop(OLD_KEY, None)
    coordinator.devices.pop(OLD_KEY, None)
    coordinator.pending[NEW_KEY] = _pending(NEW_KEY)

    coordinator._connection_time = NOW - SILENCE - timedelta(minutes=1)
    changes = find_id_changes(hub, coordinator, NOW)
    assert [c.new_key for c in changes] == [NEW_KEY]
    # When the old device went quiet is unknown, so it is never followed blindly.
    assert changes[0].gap is None

    coordinator._connection_time = NOW - timedelta(minutes=1)
    assert find_id_changes(hub, coordinator, NOW) == []


# --------------------------------------------------------------------------- #
# The tracker: a repair by default, an automatic follow when opted in.        #
# --------------------------------------------------------------------------- #
def _issue(hass, hub):
    return ir.async_get(hass).async_get_issue(
        DOMAIN, f"{ISSUE_ID_CHANGED}_{hub.entry_id}_{OLD_KEY}"
    )


async def _announce(hass, coordinator, *, quiet_minutes: float | None = 20):
    """Make the candidate count and nudge the tracker the way a new one does.

    ``quiet_minutes`` is how long before NOW the old device was last heard (the
    candidate first appeared 5 minutes before NOW); ``None`` leaves it unheard
    this session, as after a restart.
    """
    if quiet_minutes is None:
        coordinator.last_seen.pop(OLD_KEY, None)
        coordinator._connection_time = dt_util.utcnow() - SILENCE * 2
    else:
        _quiet(coordinator, minutes=quiet_minutes)
    coordinator.pending[NEW_KEY] = _pending(NEW_KEY)
    # The tracker compares against the wall clock, so place the timeline there.
    shift = dt_util.utcnow() - NOW
    if quiet_minutes is not None:
        coordinator.last_seen[OLD_KEY] += shift
    coordinator.pending[NEW_KEY].first_seen += shift
    coordinator.pending[NEW_KEY].last_seen += shift
    coordinator.emit_pending_update()
    await hass.async_block_till_done()


async def test_a_match_raises_a_fixable_repair(hass, hub_entry_builder, no_socket):
    """Off by default: the user is asked, with enough on the card to decide."""
    hub, coordinator = await _setup_hub(hass, hub_entry_builder, {OLD_KEY: OLD_RECORD})

    await _announce(hass, coordinator)

    issue = _issue(hass, hub)
    assert issue is not None
    assert issue.is_fixable
    assert issue.data == {
        "entry_id": hub.entry_id,
        "old_key": OLD_KEY,
        "new_key": NEW_KEY,
    }
    assert issue.translation_placeholders["new_key"] == NEW_KEY
    assert issue.translation_placeholders["gap"] == "15 min"
    # Nothing was re-keyed behind the user's back.
    assert OLD_KEY in hub.data[CONF_DEVICES]


async def test_the_repair_clears_when_the_old_device_speaks_again(
    hass, hub_entry_builder, no_socket
):
    """A device that was only briefly out of range withdraws the suggestion."""
    hub, coordinator = await _setup_hub(hass, hub_entry_builder, {OLD_KEY: OLD_RECORD})
    await _announce(hass, coordinator)
    assert _issue(hass, hub) is not None

    coordinator.last_seen[OLD_KEY] = dt_util.utcnow()
    coordinator.emit_pending_update()
    await hass.async_block_till_done()

    assert _issue(hass, hub) is None


async def test_confirming_the_repair_follows_the_new_id(
    hass, hub_entry_builder, no_socket
):
    """The fix flow runs the replace and announces it on the bus."""
    from custom_components.rtl_433.repairs import async_create_fix_flow

    hub, coordinator = await _setup_hub(hass, hub_entry_builder, {OLD_KEY: OLD_RECORD})
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, f"{hub.entry_id}:{OLD_KEY}"), hub.entry_id
    )
    await _announce(hass, coordinator)
    issue = _issue(hass, hub)
    fired = async_capture_events(hass, EVENT_DEVICE_ID_CHANGED)

    flow = await async_create_fix_flow(hass, issue.issue_id, issue.data)
    flow.hass = hass
    shown = await flow.async_step_init()
    assert shown["type"] == "form"
    assert shown["description_placeholders"]["new_key"] == NEW_KEY
    done = await flow.async_step_confirm({})
    await hass.async_block_till_done()

    assert done["type"] == "create_entry"
    assert NEW_KEY in hub.data[CONF_DEVICES]
    assert OLD_KEY not in hub.data[CONF_DEVICES]
    assert [e.data for e in fired] == [
        {
            "entry_id": hub.entry_id,
            "device_id": device.id,
            "name": device.name_by_user or device.name,
            "old_key": OLD_KEY,
            "new_key": NEW_KEY,
            "automatic": False,
        }
    ]


async def test_confirming_a_stale_repair_aborts(hass, hub_entry_builder, no_socket):
    """The device was removed meanwhile: nothing to move, and no traceback."""
    from custom_components.rtl_433.id_change import DeviceIdChangedRepairFlow

    hub, _coordinator = await _setup_hub(hass, hub_entry_builder, {OLD_KEY: OLD_RECORD})
    flow = DeviceIdChangedRepairFlow(hub, "Acurite-606TX-1-ch1", NEW_KEY)
    flow.hass = hass

    result = await flow.async_step_confirm({})

    assert result["type"] == "abort"
    assert result["reason"] == "device_id_change_stale"


async def test_an_opted_in_device_follows_without_asking(
    hass, hub_entry_builder, no_socket
):
    """``auto_replace`` on: re-keyed straight away, announced, no repair."""
    hub, coordinator = await _setup_hub(
        hass,
        hub_entry_builder,
        {OLD_KEY: {**OLD_RECORD, DEVICE_AUTO_REPLACE: True}},
    )
    fired = async_capture_events(hass, EVENT_DEVICE_ID_CHANGED)

    await _announce(hass, coordinator)
    await hass.async_block_till_done()

    assert NEW_KEY in hub.data[CONF_DEVICES]
    assert OLD_KEY not in hub.data[CONF_DEVICES]
    # The opt-in travels with the device, so the next battery swap is followed too.
    assert hub.data[CONF_DEVICES][NEW_KEY][DEVICE_AUTO_REPLACE] is True
    assert _issue(hass, hub) is None
    assert [e.data["automatic"] for e in fired] == [True]


@pytest.mark.parametrize(
    "quiet_minutes",
    [
        # Silent long before the candidate appeared: a dead sensor, maybe a
        # neighbour's new one -- not a battery swap in progress.
        5 + FOLLOW_WINDOW.total_seconds() / 60 + 1,
        # Not heard since a restart: when it went quiet is unknown.
        None,
    ],
)
async def test_an_opted_in_device_is_asked_about_when_the_gap_is_long(
    hass, hub_entry_builder, no_socket, quiet_minutes
):
    """Auto mode only covers a swap in progress; anything else becomes a repair."""
    hub, coordinator = await _setup_hub(
        hass,
        hub_entry_builder,
        {OLD_KEY: {**OLD_RECORD, DEVICE_AUTO_REPLACE: True}},
    )
    fired = async_capture_events(hass, EVENT_DEVICE_ID_CHANGED)

    await _announce(hass, coordinator, quiet_minutes=quiet_minutes)

    assert OLD_KEY in hub.data[CONF_DEVICES]
    assert NEW_KEY not in hub.data[CONF_DEVICES]
    assert _issue(hass, hub) is not None
    assert fired == []


# --------------------------------------------------------------------------- #
# The setting itself.                                                         #
# --------------------------------------------------------------------------- #
async def test_the_switch_is_stored_only_when_on(hass, hub_entry_builder, no_socket):
    """On stores ``True``; off drops the key; ``None`` leaves it as it was."""
    hub, _coordinator = await _setup_hub(hass, hub_entry_builder, {OLD_KEY: OLD_RECORD})

    on = build_device_data(
        hub, OLD_KEY, override=None, calibration=None, auto_replace=True
    )
    assert on[CONF_DEVICES][OLD_KEY][DEVICE_AUTO_REPLACE] is True

    hass.config_entries.async_update_entry(hub, data=on)
    untouched = build_device_data(hub, OLD_KEY, override=None, calibration=None)
    assert untouched[CONF_DEVICES][OLD_KEY][DEVICE_AUTO_REPLACE] is True

    off = build_device_data(
        hub, OLD_KEY, override=None, calibration=None, auto_replace=False
    )
    assert DEVICE_AUTO_REPLACE not in off[CONF_DEVICES][OLD_KEY]


@pytest.mark.parametrize(
    ("gap", "shown"),
    [
        (None, "an unknown time"),
        (timedelta(minutes=6), "6 min"),
        (timedelta(minutes=119), "119 min"),
        (timedelta(hours=5), "5 h"),
        (timedelta(days=3), "3 days"),
    ],
)
def test_the_gap_reads_naturally_on_the_card(gap, shown):
    """The repair card says how long the device was quiet before the new id."""
    from custom_components.rtl_433.id_change import _duration

    assert _duration(gap) == shown
