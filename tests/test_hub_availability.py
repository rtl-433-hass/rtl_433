"""Tests for the connection-backed availability gate.

The per-device availability model infers "is this radio still there?" from
silence, which only means anything while the integration is actually listening.
Once the hub's WebSocket is down the integration hears nothing at all, so no
device's cached state can be trusted — the same thing an MQTT availability topic
covers with an LWT. ``Rtl433Coordinator.hub_available`` is that second gate: it
rides out a short reconnect blip (``HUB_OFFLINE_GRACE``) and then takes *every*
device behind the hub unavailable, whatever each device's own timeout says.

The first half drives the coordinator seam directly (the client's
``on_hub_update`` callback, the grace timer, the watchdog tick, the log lines);
the second half asserts the user-visible end of it through real entities: a
never-expire event-driven device, its event entity, and the Last-seen sensor all
go unavailable while the hub is down and come back when it reconnects.
"""

from __future__ import annotations

from datetime import timedelta
import logging
from unittest.mock import patch

from freezegun import freeze_time
import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.rtl_433.const import (
    CONF_MODEL,
    DEVICE_FIELDS,
    DOMAIN,
    HUB_OFFLINE_GRACE,
    signal_hub_availability,
)
from custom_components.rtl_433.coordinator import Rtl433Coordinator
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.util import dt as dt_util
from tests.test_lifecycle import _coordinator, _feed, _setup_hub

DISPATCH = "custom_components.rtl_433.coordinator._watchdog.async_dispatcher_send"
_LOGGER_NAME = "custom_components.rtl_433"


@pytest.fixture
async def coordinator(hass, hub_entry_builder):
    """A started-looking coordinator sitting on a live connection.

    Async so the client is constructed inside the event loop. ``_started`` is set
    by hand (rather than running ``async_start``) so no socket is opened; the
    connect edge below puts the coordinator in the same state a real connect
    would.
    """
    entry = hub_entry_builder(availability_timeout=600)
    entry.add_to_hass(hass)
    coord = Rtl433Coordinator(hass, entry, host="rtl433.local")
    coord._started = True
    _connect(coord)
    yield coord
    # Tests that drop the socket leave the grace timer armed; Home Assistant's
    # cleanup check fails the test on any timer left behind.
    coord._async_cancel_hub_offline_timer()


def _connect(coordinator: Rtl433Coordinator) -> None:
    """Drive the client's connect edge through the real callback."""
    coordinator._client.connected = True
    coordinator._emit_hub_update()


def _drop(coordinator: Rtl433Coordinator) -> None:
    """Drive the client's disconnect edge through the real callback."""
    coordinator._client.connected = False
    coordinator._emit_hub_update()


def _availability_signals(dispatch, entry_id: str) -> list:
    """Pull the availability-gate dispatches out of a patched dispatcher."""
    return [
        call
        for call in dispatch.call_args_list
        if call.args[1] == signal_hub_availability(entry_id)
    ]


# --------------------------------------------------------------------------- #
# Constants: the documented contract                                           #
# --------------------------------------------------------------------------- #
def test_grace_window_is_sixty_seconds():
    """60 s is the window docs/availability.md promises and the client's max backoff."""
    assert timedelta(seconds=60) == HUB_OFFLINE_GRACE


def test_availability_signal_is_scoped_per_hub():
    """The repaint signal is per hub entry, so two hubs never repaint each other."""
    assert signal_hub_availability("hub-a") == "rtl_433_hub_availability_hub-a"
    assert signal_hub_availability("hub-a") != signal_hub_availability("hub-b")


# --------------------------------------------------------------------------- #
# hub_available: the gate itself                                               #
# --------------------------------------------------------------------------- #
def test_hub_available_while_connected(hass, coordinator):
    """An open socket is available, with no outage clock running."""
    assert coordinator.hub_available is True
    assert coordinator.disconnected_since is None


async def test_hub_available_before_start(hass, hub_entry_builder):
    """A coordinator that has never started reports available, not offline.

    Nothing has been attempted yet, so there is no outage to report; the clock
    only starts at ``async_start``.
    """
    entry = hub_entry_builder()
    entry.add_to_hass(hass)
    coord = Rtl433Coordinator(hass, entry, host="rtl433.local")
    assert coord.connected is False
    assert coord.disconnected_since is None
    assert coord.hub_available is True


def test_hub_stays_available_inside_the_grace_window(hass, coordinator):
    """A blip shorter than the grace window never flips the gate."""
    start = dt_util.utcnow()
    with freeze_time(start):
        _drop(coordinator)
        assert coordinator.hub_available is True

    with freeze_time(start + HUB_OFFLINE_GRACE - timedelta(seconds=1)):
        assert coordinator.hub_available is True


def test_hub_unavailable_once_the_grace_window_elapses(hass, coordinator):
    """At exactly the grace window the gate is closed (the boundary is strict)."""
    start = dt_util.utcnow()
    with freeze_time(start):
        _drop(coordinator)

    with freeze_time(start + HUB_OFFLINE_GRACE):
        assert coordinator.hub_available is False
    with freeze_time(start + HUB_OFFLINE_GRACE + timedelta(seconds=30)):
        assert coordinator.hub_available is False


def test_reconnect_reopens_the_gate(hass, coordinator):
    """Reconnecting clears the outage clock, whatever its age."""
    start = dt_util.utcnow()
    with freeze_time(start):
        _drop(coordinator)

    with freeze_time(start + timedelta(seconds=600)):
        assert coordinator.hub_available is False
        _connect(coordinator)
        assert coordinator.hub_available is True
        assert coordinator.disconnected_since is None


async def test_start_arms_the_outage_clock(hass, hub_entry_builder):
    """A hub that never connects goes offline a grace window after start.

    Otherwise a Home Assistant restart while the rtl_433 server is down would
    leave every restored device reading available indefinitely.
    """
    entry = hub_entry_builder()
    entry.add_to_hass(hass)
    coord = Rtl433Coordinator(hass, entry, host="rtl433.local")

    start = dt_util.utcnow()
    with freeze_time(start), patch.object(coord._client, "start"):
        await coord.async_start()
        assert coord.hub_available is True

    with freeze_time(start + HUB_OFFLINE_GRACE):
        assert coord.hub_available is False

    await coord.async_stop()


# --------------------------------------------------------------------------- #
# Repaint: one dispatch per flip                                               #
# --------------------------------------------------------------------------- #
async def test_grace_timer_dispatches_the_repaint_once(hass, coordinator):
    """The armed timer fires one availability dispatch when the grace elapses."""
    entry_id = coordinator.entry.entry_id
    start = dt_util.utcnow()
    with freeze_time(start):
        _drop(coordinator)

    with (
        freeze_time(start + HUB_OFFLINE_GRACE + timedelta(seconds=1)) as frozen,
        patch(DISPATCH) as dispatch,
    ):
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()
        assert len(_availability_signals(dispatch, entry_id)) == 1

        # The gate is already closed: re-checking it does not re-dispatch.
        coordinator._async_sync_hub_availability()
        frozen.move_to(start + HUB_OFFLINE_GRACE + timedelta(seconds=120))
        await coordinator._async_watchdog(dt_util.utcnow())
        assert len(_availability_signals(dispatch, entry_id)) == 1


async def test_watchdog_closes_the_gate_without_the_timer(hass, coordinator):
    """The watchdog tick is a backstop for the one-shot grace timer."""
    entry_id = coordinator.entry.entry_id
    start = dt_util.utcnow()
    with freeze_time(start):
        _drop(coordinator)
        # Drop the armed timer, leaving the watchdog as the only path.
        coordinator._async_cancel_hub_offline_timer()

    with freeze_time(start + HUB_OFFLINE_GRACE), patch(DISPATCH) as dispatch:
        await coordinator._async_watchdog(dt_util.utcnow())
        assert len(_availability_signals(dispatch, entry_id)) == 1
    assert coordinator._devices_offline is True


async def test_reconnect_dispatches_the_recovery_repaint(hass, coordinator):
    """Coming back after the gate closed repaints the devices once."""
    entry_id = coordinator.entry.entry_id
    start = dt_util.utcnow()
    with freeze_time(start):
        _drop(coordinator)
    with freeze_time(start + HUB_OFFLINE_GRACE):
        await coordinator._async_watchdog(dt_util.utcnow())

    with freeze_time(start + HUB_OFFLINE_GRACE), patch(DISPATCH) as dispatch:
        _connect(coordinator)
        assert len(_availability_signals(dispatch, entry_id)) == 1
    assert coordinator._devices_offline is False


async def test_blip_dispatches_nothing(hass, coordinator):
    """A drop and reconnect inside the grace window repaints nothing."""
    entry_id = coordinator.entry.entry_id
    start = dt_util.utcnow()
    with freeze_time(start), patch(DISPATCH) as dispatch:
        _drop(coordinator)
        _connect(coordinator)
        assert _availability_signals(dispatch, entry_id) == []
    assert coordinator._devices_offline is False


async def test_stop_cancels_the_pending_grace_timer(hass, coordinator):
    """Unloading a hub drops the outage clock and its armed timer."""
    start = dt_util.utcnow()
    with freeze_time(start):
        _drop(coordinator)
        assert coordinator._hub_offline_unsub is not None

    with patch.object(coordinator._client, "stop"):
        await coordinator.async_stop()

    assert coordinator._hub_offline_unsub is None
    assert coordinator.disconnected_since is None
    assert coordinator._devices_offline is False

    with freeze_time(start + HUB_OFFLINE_GRACE + timedelta(seconds=1)):
        # No timer left to fire, and nothing reads as an outage any more.
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()
        assert coordinator.hub_available is True


# --------------------------------------------------------------------------- #
# Logging: the connection loss is visible without DEBUG                        #
# --------------------------------------------------------------------------- #
def test_disconnect_logs_the_loss_at_info(hass, coordinator, caplog):
    """Losing the socket logs the URL and the window at INFO."""
    caplog.set_level(logging.INFO, logger=_LOGGER_NAME)
    _drop(coordinator)

    lines = [m for m in caplog.messages if "lost the connection" in m]
    assert len(lines) == 1
    assert coordinator.ws_url in lines[0]
    assert str(int(HUB_OFFLINE_GRACE.total_seconds())) in lines[0]


async def test_offline_gate_logs_a_warning_naming_the_device_count(
    hass, coordinator, caplog
):
    """Closing the gate warns once, naming the hub and how many devices it hides."""
    from pyrtl_433.normalizer import NormalizedEvent

    with patch("custom_components.rtl_433.coordinator.base.async_dispatcher_send"):
        coordinator._on_client_event(
            NormalizedEvent(
                device_key="Acurite-606TX-42",
                model="Acurite-606TX",
                fields={"temperature_C": 21.4},
            )
        )

    caplog.set_level(logging.WARNING, logger=_LOGGER_NAME)
    start = dt_util.utcnow()
    with freeze_time(start):
        _drop(coordinator)
    with freeze_time(start + HUB_OFFLINE_GRACE), patch(DISPATCH):
        await coordinator._async_watchdog(dt_util.utcnow())

    lines = [m for m in caplog.messages if "marking all" in m]
    assert len(lines) == 1
    assert coordinator.ws_url in lines[0]
    assert "1 device(s)" in lines[0]


def test_reconnect_logs_the_outage_duration_at_info(hass, coordinator, caplog):
    """Recovery logs how long the hub was gone."""
    caplog.set_level(logging.INFO, logger=_LOGGER_NAME)
    start = dt_util.utcnow()
    with freeze_time(start):
        _drop(coordinator)
    with freeze_time(start + timedelta(seconds=90)):
        _connect(coordinator)

    lines = [m for m in caplog.messages if "reconnected to" in m]
    assert len(lines) == 1
    assert "90s" in lines[0]


async def test_first_connect_is_not_reported_as_a_recovery(
    hass, hub_entry_builder, caplog
):
    """Startup arms the same clock, but the first connect is not a reconnect."""
    entry = hub_entry_builder()
    entry.add_to_hass(hass)
    coord = Rtl433Coordinator(hass, entry, host="rtl433.local")

    caplog.set_level(logging.INFO, logger=_LOGGER_NAME)
    with patch.object(coord._client, "start"):
        await coord.async_start()
    _connect(coord)

    assert [m for m in caplog.messages if "reconnected to" in m] == []
    assert [m for m in caplog.messages if "lost the connection" in m] == []

    with patch.object(coord._client, "stop"):
        await coord.async_stop()


async def test_stop_does_not_report_teardown_as_an_outage(hass, coordinator, caplog):
    """The socket close ``async_stop`` performs is not logged as a loss."""
    caplog.set_level(logging.INFO, logger=_LOGGER_NAME)

    async def _close(self=None) -> None:
        _drop(coordinator)

    with patch.object(coordinator._client, "stop", _close):
        await coordinator.async_stop()

    assert [m for m in caplog.messages if "lost the connection" in m] == []
    assert coordinator._hub_offline_unsub is None


# --------------------------------------------------------------------------- #
# End to end: the entities behind an offline hub                               #
# --------------------------------------------------------------------------- #
async def test_offline_hub_takes_every_device_entity_unavailable(
    hass, hub_entry_builder
):
    """A sustained outage marks devices unavailable, including never-expire ones.

    The door contact is event-driven, so its silence timeout is never-expire and
    nothing about its own liveness would ever hide it. Its measurement sensor,
    its event entity, and its Last-seen sensor must still go unavailable while
    the hub is unreachable — and all come back on reconnect.
    """
    device_key = "Acurite-606TX-42"
    hub = await _setup_hub(
        hass,
        hub_entry_builder,
        devices={
            device_key: {
                CONF_MODEL: "Acurite-606TX",
                DEVICE_FIELDS: ["temperature_C", "button"],
            }
        },
    )
    coordinator = _coordinator(hass, hub)
    ent_reg = er.async_get(hass)
    temp_eid = ent_reg.async_get_entity_id(
        "sensor", DOMAIN, f"{hub.entry_id}:{device_key}:T"
    )
    button_eid = ent_reg.async_get_entity_id(
        "event", DOMAIN, f"{hub.entry_id}:{device_key}:button"
    )
    assert temp_eid is not None
    assert button_eid is not None

    start = dt_util.utcnow()
    with freeze_time(start):
        _feed(
            coordinator,
            {"model": "Acurite-606TX", "id": 42, "temperature_C": 21.0, "button": "A"},
        )
        await hass.async_block_till_done()

    # The device is event-driven, so silence alone never expires it.
    assert coordinator.is_event_driven_device(device_key) is True
    assert hass.states.get(temp_eid).state != "unavailable"
    assert hass.states.get(button_eid).state != "unavailable"

    # Drop the socket: inside the grace window nothing changes.
    with freeze_time(start + timedelta(seconds=1)):
        _drop(coordinator)
        await hass.async_block_till_done()
        assert hass.states.get(temp_eid).state != "unavailable"
        assert hass.states.get(button_eid).state != "unavailable"

    # Past the grace window the whole hub's devices go unavailable.
    with freeze_time(start + HUB_OFFLINE_GRACE + timedelta(seconds=2)):
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()
        assert hass.states.get(temp_eid).state == "unavailable"
        assert hass.states.get(button_eid).state == "unavailable"

        # Reconnecting repaints them without waiting for a fresh transmission.
        _connect(coordinator)
        await hass.async_block_till_done()
        assert hass.states.get(temp_eid).state != "unavailable"
        assert hass.states.get(button_eid).state != "unavailable"


async def test_removal_unsubscribes_from_both_signals(hass, hub_entry_builder):
    """A removed entity drops the availability *and* device subscriptions.

    Both handlers write state, so a subscription left behind would have a
    removed entity writing after teardown.
    """
    device_key = "EnergyMeter-2000-1234"
    hub = await _setup_hub(
        hass,
        hub_entry_builder,
        devices={
            device_key: {CONF_MODEL: "EnergyMeter-2000", DEVICE_FIELDS: ["power_W"]}
        },
    )
    ent_reg = er.async_get(hass)
    watts_eid = ent_reg.async_get_entity_id(
        "sensor", DOMAIN, f"{hub.entry_id}:{device_key}:watts"
    )
    entity = hass.data["entity_components"]["sensor"].get_entity(watts_eid)
    assert entity is not None
    assert entity._unsub_hub_availability is not None
    assert entity._unsub_dispatcher is not None

    await entity.async_will_remove_from_hass()
    assert entity._unsub_hub_availability is None
    assert entity._unsub_dispatcher is None

    with patch.object(type(entity), "async_write_ha_state") as write:
        async_dispatcher_send(hass, signal_hub_availability(hub.entry_id))
        _feed(
            _coordinator(hass, hub),
            {"model": "EnergyMeter-2000", "id": 1234, "power_W": 6.0},
        )
        await hass.async_block_till_done()
        write.assert_not_called()


async def test_offline_hub_takes_the_last_seen_sensor_unavailable(
    hass, hub_entry_builder
):
    """Last seen survives the silence timeout but not a disconnected hub.

    While the socket is down the timestamp is frozen at whenever the integration
    stopped listening, so leaving it available would read as a device that has
    only just gone quiet.
    """
    from tests.test_lifecycle import _enable_entity

    device_key = "EnergyMeter-2000-1234"
    hub = await _setup_hub(
        hass,
        hub_entry_builder,
        devices={
            device_key: {CONF_MODEL: "EnergyMeter-2000", DEVICE_FIELDS: ["power_W"]}
        },
    )
    ent_reg = er.async_get(hass)
    last_seen_eid = ent_reg.async_get_entity_id(
        "sensor", DOMAIN, f"{hub.entry_id}:{device_key}:last_seen"
    )
    assert last_seen_eid is not None
    await _enable_entity(hass, hub, last_seen_eid)
    coordinator = _coordinator(hass, hub)

    start = dt_util.utcnow()
    with freeze_time(start):
        _feed(coordinator, {"model": "EnergyMeter-2000", "id": 1234, "power_W": 5.0})
        await hass.async_block_till_done()
    assert hass.states.get(last_seen_eid).state != "unavailable"

    with freeze_time(start + timedelta(seconds=1)):
        _drop(coordinator)
    with freeze_time(start + HUB_OFFLINE_GRACE + timedelta(seconds=2)):
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()
        assert hass.states.get(last_seen_eid).state == "unavailable"


async def test_diagnostics_report_the_gate(hass, hub_entry_builder):
    """Diagnostics carry the gate and the outage start for support requests."""
    from custom_components.rtl_433.diagnostics import async_get_config_entry_diagnostics

    hub = await _setup_hub(hass, hub_entry_builder)
    coordinator = _coordinator(hass, hub)

    diag = await async_get_config_entry_diagnostics(hass, hub)
    assert diag["hub_available"] is True
    assert diag["disconnected_since"] is None

    start = dt_util.utcnow()
    with freeze_time(start):
        _drop(coordinator)
    with freeze_time(start + HUB_OFFLINE_GRACE):
        diag = await async_get_config_entry_diagnostics(hass, hub)
    assert diag["hub_available"] is False
    assert diag["disconnected_since"] == start.isoformat()
