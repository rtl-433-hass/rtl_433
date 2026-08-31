"""Tests for the connection-backed availability gate.

The per-device availability model infers "is this radio still there?" from
silence, which only means anything while the integration is actually listening.
Once the hub's WebSocket is down the integration hears nothing at all, so no
device's cached state can be trusted — the same thing an MQTT availability topic
covers with an LWT. ``Rtl433Coordinator.hub_available`` is that second gate, and
it follows the socket with no grace window: the moment the connection drops,
*every* device behind the hub is unavailable whatever its own timeout says.

The first half drives the coordinator seam directly (the client's
``on_hub_update`` callback, the watchdog tick, the log lines); the second half
asserts the user-visible end of it through real entities: a never-expire
event-driven device, its ``event`` entity and the Last-seen sensor all go
unavailable while the hub is down and come back when it reconnects.
"""

from __future__ import annotations

from datetime import timedelta
import logging
from unittest.mock import AsyncMock, patch

from freezegun import freeze_time
from pyrtl_433 import Rtl433Client
import pytest
from pytest_homeassistant_custom_component.common import (
    mock_restore_cache_with_extra_data,
)

from custom_components.rtl_433 import const, repairs
from custom_components.rtl_433.const import (
    CONF_MODEL,
    DEVICE_FIELDS,
    DOMAIN,
    signal_hub_availability,
    signal_hub_update,
)
from custom_components.rtl_433.coordinator import Rtl433Coordinator
from homeassistant.core import State
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.restore_state import RestoredExtraData
from homeassistant.util import dt as dt_util
from tests.conftest import mark_hub_connected
from tests.test_lifecycle import _coordinator, _feed, _setup_hub

DISPATCH = "custom_components.rtl_433.coordinator._watchdog.async_dispatcher_send"
_LOGGER_NAME = "custom_components.rtl_433"

# This module *is* the outage suite, so it opts out of the conftest fixture that
# leaves every started coordinator connected and drives the edges itself.
pytestmark = pytest.mark.hub_disconnected


@pytest.fixture(autouse=True)
def no_client_transport():
    """Keep the library client's reconnect loop out of these tests.

    ``_setup_hub`` runs the real setup, which would otherwise start a live
    reconnect loop against a host that does not resolve. Its failures flip the
    client's ``connected`` flag asynchronously, and with an instant availability
    gate that flag is exactly what these tests assert on — so the loop has to be
    off for them to drive the edges themselves.
    """
    with (
        patch.object(Rtl433Client, "start", new=AsyncMock()),
        patch.object(Rtl433Client, "stop", new=AsyncMock()),
    ):
        yield


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
    return coord


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
def test_there_is_no_grace_window():
    """The gate is the socket state, with no delay constant behind it.

    Deliberate: a delay would present readings as current while the integration
    knows it cannot hear the radio. The debounce lives on the *repair issue*
    instead, so the notification waits while the entities tell the truth at once.
    """
    assert not hasattr(const, "HUB_OFFLINE_GRACE")
    assert timedelta(seconds=90) == repairs._UNREACHABLE_GRACE


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


async def test_hub_unavailable_before_start(hass, hub_entry_builder):
    """A coordinator that has never connected is not available.

    Nothing has been received, so there is nothing to report as current — the
    same state ``esphome`` and ``mqtt`` entities start in.
    """
    entry = hub_entry_builder()
    entry.add_to_hass(hass)
    coord = Rtl433Coordinator(hass, entry, host="rtl433.local")
    assert coord.connected is False
    assert coord.hub_available is False


def test_drop_closes_the_gate_immediately(hass, coordinator):
    """The gate follows the socket on the same tick, with no grace period."""
    start = dt_util.utcnow()
    with freeze_time(start):
        _drop(coordinator)
        assert coordinator.hub_available is False
        assert coordinator.disconnected_since == start

    # And it stays closed for as long as the outage lasts.
    with freeze_time(start + timedelta(seconds=600)):
        assert coordinator.hub_available is False


def test_reconnect_reopens_the_gate(hass, coordinator):
    """Reconnecting reopens the gate at once and clears the outage clock."""
    start = dt_util.utcnow()
    with freeze_time(start):
        _drop(coordinator)

    with freeze_time(start + timedelta(seconds=600)):
        assert coordinator.hub_available is False
        _connect(coordinator)
        assert coordinator.hub_available is True
        assert coordinator.disconnected_since is None


def test_a_flapping_socket_is_reported_honestly(hass, coordinator):
    """Each drop and each recovery is reported as it happens.

    A server in a crash-restart loop produces real transitions rather than being
    smoothed over: during each drop the integration genuinely is not listening.
    """
    start = dt_util.utcnow()
    for offset in (0, 20, 40):
        with freeze_time(start + timedelta(seconds=offset)):
            _drop(coordinator)
            assert coordinator.hub_available is False
        with freeze_time(start + timedelta(seconds=offset + 1)):
            _connect(coordinator)
            assert coordinator.hub_available is True


async def test_start_leaves_the_hub_unavailable_until_it_connects(
    hass, hub_entry_builder
):
    """A hub that never connects reports unavailable from the moment it starts.

    Otherwise a Home Assistant restart while the rtl_433 server is down would
    leave every restored device reading available.
    """
    entry = hub_entry_builder()
    entry.add_to_hass(hass)
    coord = Rtl433Coordinator(hass, entry, host="rtl433.local")

    start = dt_util.utcnow()
    with freeze_time(start), patch.object(coord._client, "start"):
        await coord.async_start()
        assert coord.hub_available is False
        assert coord.disconnected_since == start

    await coord.async_stop()


async def test_no_timer_is_ever_armed(hass, coordinator):
    """The gate is edge-driven and lazy: it schedules nothing.

    Guards the regression the grace window used to carry — a one-shot timer that
    could outlive an abandoned coordinator after a failed setup.
    """
    _drop(coordinator)
    assert not hasattr(coordinator, "_hub_offline_unsub")


# --------------------------------------------------------------------------- #
# Repaint: one dispatch per flip                                               #
# --------------------------------------------------------------------------- #
async def test_drop_dispatches_the_repaint_once(hass, coordinator):
    """The disconnect edge repaints every device entity exactly once."""
    entry_id = coordinator.entry.entry_id
    with patch(DISPATCH) as dispatch:
        _drop(coordinator)
        assert len(_availability_signals(dispatch, entry_id)) == 1

        # The gate is already closed: re-checking it does not re-dispatch.
        coordinator._async_sync_hub_availability()
        await coordinator._async_watchdog(dt_util.utcnow())
        assert len(_availability_signals(dispatch, entry_id)) == 1
    assert coordinator._devices_offline is True


async def test_reconnect_dispatches_the_recovery_repaint(hass, coordinator):
    """Coming back repaints the devices once."""
    entry_id = coordinator.entry.entry_id
    _drop(coordinator)

    with patch(DISPATCH) as dispatch:
        _connect(coordinator)
        assert len(_availability_signals(dispatch, entry_id)) == 1
    assert coordinator._devices_offline is False


async def test_watchdog_is_a_backstop_for_a_missed_edge(hass, coordinator):
    """A tick reconciles the gate if a connection edge was ever missed."""
    entry_id = coordinator.entry.entry_id
    # Drop the socket without driving the callback, so no edge was seen.
    coordinator._client.connected = False

    with patch(DISPATCH) as dispatch:
        await coordinator._async_watchdog(dt_util.utcnow())
        assert len(_availability_signals(dispatch, entry_id)) == 1
    assert coordinator._devices_offline is True


async def test_stop_clears_the_gate_state(hass, coordinator):
    """Unloading a hub resets the outage clock and the dispatched-state latch."""
    _drop(coordinator)

    with patch.object(coordinator._client, "stop"):
        await coordinator.async_stop()

    assert coordinator.disconnected_since is None
    assert coordinator._devices_offline is False


# --------------------------------------------------------------------------- #
# Logging: the connection loss is visible without DEBUG                        #
# --------------------------------------------------------------------------- #
async def test_disconnect_logs_the_loss_and_the_device_count(hass, coordinator, caplog):
    """Losing the socket logs the URL and how many devices it took with it."""
    from pyrtl_433.normalizer import NormalizedEvent

    with patch("custom_components.rtl_433.coordinator.base.async_dispatcher_send"):
        coordinator._on_client_event(
            NormalizedEvent(
                device_key="Acurite-606TX-42",
                model="Acurite-606TX",
                fields={"temperature_C": 21.4},
            )
        )

    caplog.set_level(logging.INFO, logger=_LOGGER_NAME)
    _drop(coordinator)

    lines = [m for m in caplog.messages if "lost the connection" in m]
    assert len(lines) == 1
    assert coordinator.ws_url in lines[0]
    assert "1 device(s)" in lines[0]


async def test_the_device_count_is_restart_safe(hass, hub_entry_builder, caplog):
    """The count comes from the persisted device map, not the live session.

    After a restart with the server already down nothing has transmitted, so a
    count taken from the live map would read 0 while every restored entity does
    in fact go unavailable.
    """
    entry = hub_entry_builder(
        devices={
            "Acurite-606TX-42": {
                CONF_MODEL: "Acurite-606TX",
                DEVICE_FIELDS: ["temperature_C"],
            },
            "Acurite-606TX-43": {
                CONF_MODEL: "Acurite-606TX",
                DEVICE_FIELDS: ["temperature_C"],
            },
        }
    )
    entry.add_to_hass(hass)
    coord = Rtl433Coordinator(hass, entry, host="rtl433.local")
    coord._started = True
    _connect(coord)

    caplog.set_level(logging.INFO, logger=_LOGGER_NAME)
    assert coord.devices == {}
    _drop(coord)

    lines = [m for m in caplog.messages if "lost the connection" in m]
    assert len(lines) == 1
    assert "2 device(s)" in lines[0]


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
    """Startup stamps the same clock, but the first connect is not a reconnect."""
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


# --------------------------------------------------------------------------- #
# End to end: the entities behind an offline hub                               #
# --------------------------------------------------------------------------- #
async def test_offline_hub_takes_every_device_entity_unavailable(
    hass, hub_entry_builder
):
    """A sustained outage marks devices unavailable, including never-expire ones.

    The door contact is event-driven, so its silence timeout is never-expire and
    nothing about its own liveness would ever hide it. Its measurement sensor,
    its ``event`` entity and its Last-seen sensor must still go unavailable while
    the hub is unreachable — and come back on reconnect. There is no exception
    for ``event``: zigbee2mqtt publishes the bridge-state availability topic on
    its own event entities, and core's Shelly/ESPHome event entities follow the
    device connection.
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
    # This module opts out of the auto-connect fixture, so put the coordinator
    # on a live connection by hand before exercising the outage.
    mark_hub_connected(coordinator)
    await hass.async_block_till_done()
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

    # Drop the socket: the whole hub's devices go unavailable at once.
    with freeze_time(start + timedelta(seconds=1)):
        _drop(coordinator)
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

    with patch.object(entity, "async_write_ha_state") as write:
        async_dispatcher_send(hass, signal_hub_availability(hub.entry_id))
        _feed(
            _coordinator(hass, hub),
            {"model": "EnergyMeter-2000", "id": 1234, "power_W": 6.0},
        )
        await hass.async_block_till_done()
        write.assert_not_called()


async def test_offline_hub_takes_the_hub_diagnostic_sensors_unavailable(
    hass, hub_entry_builder
):
    """The hub's own diagnostic sensors are gated too, but connectivity is not.

    Every hub diagnostic value is fetched over HTTP ``/cmd``, so an outage
    freezes it; the Connectivity binary sensor must stay available because it is
    what reports the outage.
    """
    hub = await _setup_hub(hass, hub_entry_builder)
    coordinator = _coordinator(hass, hub)
    # This module opts out of the auto-connect fixture, so put the coordinator
    # on a live connection by hand before exercising the outage.
    mark_hub_connected(coordinator)
    await hass.async_block_till_done()
    # The connect edge below would otherwise spawn the managed-SDR adoption task,
    # whose ``/cmd`` fetch fails in the test environment and drops the socket
    # again — noise for a test about the availability gate.
    coordinator.manage_settings = False
    coordinator._client.meta = {"center_frequency": 433_920_000, "samp_rate": 250_000}
    coordinator._client.stats = {"frames": {"count": 7, "fsk": 2, "events": 5}}

    ent_reg = er.async_get(hass)
    freq_eid = ent_reg.async_get_entity_id(
        "sensor", DOMAIN, f"{hub.entry_id}:hub:center_frequency"
    )
    ook_eid = ent_reg.async_get_entity_id(
        "sensor", DOMAIN, f"{hub.entry_id}:hub:ook_frames"
    )
    conn_eid = ent_reg.async_get_entity_id(
        "binary_sensor", DOMAIN, f"{hub.entry_id}:hub:connectivity"
    )
    assert freq_eid is not None
    assert ook_eid is not None
    assert conn_eid is not None

    start = dt_util.utcnow()
    with freeze_time(start):
        _connect(coordinator)  # repaint with the meta/stats above
        await hass.async_block_till_done()
    assert hass.states.get(freq_eid).state != "unavailable"
    assert hass.states.get(ook_eid).state != "unavailable"

    # Dropping the socket makes the frozen readings read unavailable at once...
    with freeze_time(start + timedelta(seconds=1)):
        _drop(coordinator)
        await hass.async_block_till_done()
        assert hass.states.get(freq_eid).state == "unavailable"
        assert hass.states.get(ook_eid).state == "unavailable"
        # ...but connectivity keeps reporting, which is its whole job.
        assert hass.states.get(conn_eid).state == "off"

        _connect(coordinator)
        await hass.async_block_till_done()
        assert hass.states.get(freq_eid).state != "unavailable"
        assert hass.states.get(ook_eid).state != "unavailable"
        assert hass.states.get(conn_eid).state == "on"


async def test_hub_entity_repaints_on_the_availability_signal(hass, hub_entry_builder):
    """Hub entities subscribe to the gate's own signal, not just ``hub_update``.

    ``signal_hub_update`` fires on the connect/disconnect edges but *not* when the
    connection drops, which is the moment a gated hub entity's ``available``
    changes. Asserted by dispatching the signal directly: a subscription bound to
    the wrong name (or missing) leaves the entity unpainted, which the end-to-end
    test above cannot pin down on its own.
    """
    hub = await _setup_hub(hass, hub_entry_builder)
    ent_reg = er.async_get(hass)
    freq_eid = ent_reg.async_get_entity_id(
        "sensor", DOMAIN, f"{hub.entry_id}:hub:center_frequency"
    )
    entity = hass.data["entity_components"]["sensor"].get_entity(freq_eid)
    assert entity is not None

    with patch.object(entity, "async_write_ha_state") as write:
        async_dispatcher_send(hass, signal_hub_availability(hub.entry_id))
        await hass.async_block_till_done()
        write.assert_called_once()

    # A different hub's flip must not repaint this one.
    with patch.object(entity, "async_write_ha_state") as write:
        async_dispatcher_send(hass, signal_hub_availability("some-other-hub"))
        await hass.async_block_till_done()
        write.assert_not_called()


async def test_missing_hub_key_reads_unknown_not_unavailable(hass, hub_entry_builder):
    """A key the server omits is ``unknown`` while connected — not unavailable."""
    hub = await _setup_hub(hass, hub_entry_builder)
    coordinator = _coordinator(hass, hub)
    # This module opts out of the auto-connect fixture, so put the coordinator
    # on a live connection by hand before exercising the outage.
    mark_hub_connected(coordinator)
    await hass.async_block_till_done()
    coordinator._client.meta = {}
    coordinator._client.stats = {}
    _connect(coordinator)
    await hass.async_block_till_done()

    ent_reg = er.async_get(hass)
    freq_eid = ent_reg.async_get_entity_id(
        "sensor", DOMAIN, f"{hub.entry_id}:hub:center_frequency"
    )
    assert hass.states.get(freq_eid).state == "unknown"


async def test_hub_entity_removal_unsubscribes_from_both_signals(
    hass, hub_entry_builder
):
    """A removed hub entity drops the hub-update *and* availability subscriptions."""
    hub = await _setup_hub(hass, hub_entry_builder)
    ent_reg = er.async_get(hass)
    freq_eid = ent_reg.async_get_entity_id(
        "sensor", DOMAIN, f"{hub.entry_id}:hub:center_frequency"
    )
    entity = hass.data["entity_components"]["sensor"].get_entity(freq_eid)
    assert entity._unsub_hub is not None
    assert entity._unsub_hub_availability is not None

    await entity.async_will_remove_from_hass()
    assert entity._unsub_hub is None
    assert entity._unsub_hub_availability is None

    with patch.object(entity, "async_write_ha_state") as write:
        async_dispatcher_send(hass, signal_hub_availability(hub.entry_id))
        async_dispatcher_send(hass, signal_hub_update(hub.entry_id))
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
    # This module opts out of the auto-connect fixture, so put the coordinator
    # on a live connection by hand before exercising the outage.
    mark_hub_connected(coordinator)
    await hass.async_block_till_done()

    start = dt_util.utcnow()
    with freeze_time(start):
        _feed(coordinator, {"model": "EnergyMeter-2000", "id": 1234, "power_W": 5.0})
        await hass.async_block_till_done()
    assert hass.states.get(last_seen_eid).state != "unavailable"

    with freeze_time(start + timedelta(seconds=1)):
        _drop(coordinator)
        await hass.async_block_till_done()
        assert hass.states.get(last_seen_eid).state == "unavailable"


async def test_value_survives_a_restart_taken_during_an_outage(hass, hub_entry_builder):
    """A restart while the gate is closed still restores the device's value.

    Home Assistant writes ``unavailable`` as the *state* whenever ``available``
    is False, so the persisted state string is useless and the restore filters
    drop it. The value rides along in the entity's extra restore data instead —
    without which a never-expire door contact comes back ``unknown`` and stays
    that way until it next transmits, possibly days later.
    """
    device_key = "Acurite-606TX-42"
    restore_entity_id = "sensor.acurite_606tx_42_temperature"

    mock_restore_cache_with_extra_data(
        hass,
        (
            (
                State(restore_entity_id, "unavailable"),
                RestoredExtraData({"native_value": 19.9}).as_dict(),
            ),
        ),
    )

    hub = await _setup_hub(
        hass,
        hub_entry_builder,
        devices={
            device_key: {
                CONF_MODEL: "Acurite-606TX",
                DEVICE_FIELDS: ["temperature_C"],
            }
        },
    )
    mark_hub_connected(_coordinator(hass, hub))
    await hass.async_block_till_done()

    assert hass.states.get(restore_entity_id).state == "19.9"


async def test_diagnostics_report_the_gate(hass, hub_entry_builder):
    """Diagnostics carry the gate and the outage start for support requests."""
    from custom_components.rtl_433.diagnostics import async_get_config_entry_diagnostics

    device_key = "Acurite-606TX-42"
    hub = await _setup_hub(
        hass,
        hub_entry_builder,
        devices={
            device_key: {
                CONF_MODEL: "Acurite-606TX",
                DEVICE_FIELDS: ["temperature_C"],
            }
        },
    )
    coordinator = _coordinator(hass, hub)
    # This module opts out of the auto-connect fixture, so put the coordinator
    # on a live connection by hand before exercising the outage.
    mark_hub_connected(coordinator)
    await hass.async_block_till_done()

    diag = await async_get_config_entry_diagnostics(hass, hub)
    assert diag["hub_available"] is True
    assert diag["disconnected_since"] is None

    start = dt_util.utcnow()
    with freeze_time(start):
        _feed(
            coordinator,
            {"model": "Acurite-606TX", "id": 42, "temperature_C": 21.0},
        )
        await hass.async_block_till_done()
        _drop(coordinator)
        diag = await async_get_config_entry_diagnostics(hass, hub)
    assert diag["hub_available"] is False
    assert diag["disconnected_since"] == start.isoformat()

    # The per-device table must agree with what the entities report: the gate
    # short-circuits the silence verdict, which is reported separately so a dump
    # still distinguishes "device fell silent" from "hub went away".
    device_diag = diag["devices"][device_key]
    assert device_diag["available"] is False
    assert device_diag["silence_available"] is True
