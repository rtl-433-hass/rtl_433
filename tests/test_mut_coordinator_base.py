"""Mutation floor for ``coordinator/base.py``.

``tests/test_coordinator.py`` covers what the coordinator does once events are
flowing. This module covers the parts a running integration reaches only
*incidentally* — at construction, at the lifecycle edges, and through callbacks
the rest of the suite stubs out — and which therefore had almost nothing holding
them:

* **Construction.** ``__init__`` is the single place every runtime attribute is
  declared, and it is also where the transport client and the desired-state
  ``Store`` are wired. Nothing read those attributes back, so the difference
  between ``{}`` and ``None``, or between a client pointed at the configured hub
  and one pointed anywhere else, was invisible. The tests below build a
  coordinator and assert the wiring and the starting state directly.
* **The lifecycle edges.** ``async_start`` arms the availability watchdog and
  ``async_stop`` disarms it and wipes the outage bookkeeping so a reload is not
  reported as an outage. Both are driven by setup/teardown rather than by a test,
  so the arguments the watchdog is armed with — and the flags the stop clears —
  were never observed.
* **The client callbacks.** ``_on_connect`` (managed-SDR adopt/enforce) and
  ``_maybe_refresh_hub_identity`` (the hub device-registry refresh) are handed to
  the library client and fire off a real socket. Every test that opens one stubs
  the transport, so neither was ever called. They are called directly here.
* **The dispatch seams.** ``_emit_hub_update`` (the connect/disconnect edge) and
  ``_dispatch`` (the per-device fan-out) are always observed through the
  entities downstream of them, never through the dispatcher call itself, so the
  signal each one addresses -- and the replay/repaint flags ``_dispatch``
  rewrites onto a cached event -- were free to be anything.
* **``validate_connection``.** The config flow's reachability probe. It was
  pinned only as "something was called", which left the host, port, path, TLS
  flag and the shared aiohttp session all free to be anything at all — a config
  flow that validates a *different* endpoint than the one it then stores would
  have passed.

Two conventions run through the file. Identity assertions (``is False``, ``is
None``) rather than equality, because the sentinel a flag starts life as is the
thing under test: ``None`` and ``False`` compare equal enough to hide a swap.
And exact log *messages* (``in caplog.messages``, not ``in caplog.text``),
because these DEBUG lines are the only report an operator gets when SDR adoption
or a device-registry refresh quietly fails, and a line that renders the wrong
hub — or renders ``None`` where the error should be — is worse than no line.
"""

from __future__ import annotations

from collections import OrderedDict
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from pyrtl_433 import Rtl433Client
from pyrtl_433.normalizer import DEFAULT_SKIP_KEYS, NormalizedEvent
import pytest

from custom_components.rtl_433.const import (
    DEFAULT_AVAILABILITY_TIMEOUT,
    DEFAULT_PATH,
    DEFAULT_PORT,
    SDR_STORE_VERSION,
    sdr_store_key,
    signal_device_update,
    signal_hub_update,
)
from custom_components.rtl_433.coordinator import Rtl433Coordinator
from custom_components.rtl_433.coordinator._events import PendingDevice
from custom_components.rtl_433.coordinator._watchdog import _WATCHDOG_INTERVAL
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util

CLIENT = "custom_components.rtl_433.coordinator.base.Rtl433Client"
CLIENT_VALIDATE = (
    "custom_components.rtl_433.coordinator.base.Rtl433Client.validate_connection"
)
STORE = "custom_components.rtl_433.coordinator.base._SdrStore"
TRACK_INTERVAL = "custom_components.rtl_433.coordinator.base.async_track_time_interval"
DISPATCH = "custom_components.rtl_433.coordinator.base.async_dispatcher_send"
LOG = "custom_components.rtl_433"
_KEY = "Acurite-606TX-42"

# The lifecycle tests drive ``async_start`` / ``async_stop`` themselves and
# assert on what each edge leaves behind, so they opt out of the conftest fixture
# that marks every started coordinator connected for them.
pytestmark = pytest.mark.hub_disconnected


@pytest.fixture(autouse=True)
def client_transport():
    """Keep the library client's socket out of this module.

    ``async_start`` calls straight through to the client's connect loop, which
    would otherwise spend the test resolving a host that does not exist and flip
    the connected flag asynchronously underneath the assertions. Stubbing the two
    transport entry points leaves every line of the coordinator's own lifecycle
    intact while guaranteeing nothing is dialled.
    """
    with (
        patch.object(Rtl433Client, "start", new=AsyncMock()) as start,
        patch.object(Rtl433Client, "stop", new=AsyncMock()) as stop,
    ):
        yield SimpleNamespace(start=start, stop=stop)


@pytest.fixture
async def make_coordinator(hass, hub_entry_builder):
    """Build a coordinator against a real hub entry.

    Async so construction happens inside the event loop: the coordinator builds
    its :class:`pyrtl_433.Rtl433Client` in ``__init__`` and the injected Home
    Assistant aiohttp session needs a running loop.
    """

    def _make(**kwargs):
        entry = hub_entry_builder(entry_id=kwargs.pop("entry_id", None))
        entry.add_to_hass(hass)
        kwargs.setdefault("host", "rtl433.local")
        return Rtl433Coordinator(hass, entry, **kwargs)

    return _make


# --------------------------------------------------------------------------- #
# Construction: the transport client and the desired-state Store.              #
# --------------------------------------------------------------------------- #
async def test_the_client_is_pointed_at_the_configured_hub(hass, hub_entry_builder):
    """Every connection parameter reaches the library client unchanged.

    This is the whole of the user's "where is my receiver?" answer. A port that
    silently reverts to the default, a dropped TLS flag, or a swapped host and
    path means the integration validates one endpoint in the config flow and then
    spends forever reconnecting to another one — with no error message, because
    from the client's point of view nothing is wrong.

    The session matters just as much: Home Assistant owns the shared aiohttp
    session's lifecycle, and a client given its own session would leak one per
    reload and never be closed.
    """
    entry = hub_entry_builder()
    entry.add_to_hass(hass)

    with patch(CLIENT) as client_cls:
        coordinator = Rtl433Coordinator(
            hass,
            entry,
            host="attic.local",
            port=9999,
            path="/stream",
            secure=True,
            skip_keys={"model", "id"},
        )

    client_cls.assert_called_once_with(
        "attic.local",
        port=9999,
        path="/stream",
        secure=True,
        session=async_get_clientsession(hass),
        skip_keys=coordinator.skip_keys,
        on_event=coordinator._on_client_event,
        on_hub_update=coordinator._emit_hub_update,
        event_tz=dt_util.get_default_time_zone(),
    )
    assert coordinator._client is client_cls.return_value


async def test_the_desired_state_store_is_scoped_to_this_hub(hass, hub_entry_builder):
    """The managed-SDR Store is keyed per config entry, at the current version.

    Two hubs on one Home Assistant share nothing: a Store key that did not carry
    the entry id would have the second hub load and then overwrite the first
    hub's gain and frequency on its next connect. The version argument is what
    drives the Hz->MHz centre-frequency migration, so a wrong one leaves an
    existing managed hub tuned three orders of magnitude off.
    """
    entry = hub_entry_builder(entry_id="hub-attic")
    entry.add_to_hass(hass)

    with patch(STORE) as store_cls:
        coordinator = Rtl433Coordinator(hass, entry, host="rtl433.local")

    store_cls.assert_called_once_with(
        hass, SDR_STORE_VERSION, sdr_store_key("hub-attic")
    )
    assert coordinator._store is store_cls.return_value


# --------------------------------------------------------------------------- #
# Construction: configuration read back off the coordinator.                   #
# --------------------------------------------------------------------------- #
async def test_the_hub_configuration_is_readable_back(hass, make_coordinator):
    """Diagnostics, the options flow and the reconfigure gate all read these.

    ``_async_update_listener`` compares the live entry against the coordinator's
    own copy to decide whether a hub needs reloading, and a support dump reports
    them verbatim. An attribute that does not hold what setup passed makes both
    lie — and a dropped ``manage_settings`` would silently start (or stop) Home
    Assistant writing to somebody's receiver.
    """
    coordinator = make_coordinator(
        host="attic.local",
        port=9999,
        path="/stream",
        secure=True,
        manage_settings=False,
        availability_timeout=120,
        initial_center_frequency=868.3,
    )

    assert coordinator.hass is hass
    assert coordinator.host == "attic.local"
    assert coordinator.port == 9999
    assert coordinator.path == "/stream"
    assert coordinator.secure is True
    assert coordinator.manage_settings is False
    assert coordinator.availability_timeout == 120
    assert coordinator.initial_center_frequency == 868.3


async def test_the_entry_is_kept_for_the_signals_keyed_on_it(hass, hub_entry_builder):
    """Every dispatcher signal this coordinator sends is keyed on its entry.

    Held separately from the configuration above because losing it is not a
    misconfiguration but a silent cross-wiring: two hubs would dispatch on the
    same signal name and each other's entities would repaint from the wrong
    radio.
    """
    entry = hub_entry_builder()
    entry.add_to_hass(hass)

    coordinator = Rtl433Coordinator(hass, entry, host="rtl433.local")

    assert coordinator.entry is entry


async def test_a_hub_defaults_to_a_managed_plaintext_endpoint(hass, make_coordinator):
    """The defaults are the documented rtl_433 http server, with TLS off.

    These are what a hub added before an option existed keeps getting. Flipping
    ``secure`` on by default would break every existing plain-HTTP hub at the
    next restart; flipping ``manage_settings`` off would quietly stop Home
    Assistant re-applying the user's gain and frequency after a receiver reboot.
    """
    coordinator = make_coordinator()

    assert coordinator.port == DEFAULT_PORT
    assert coordinator.path == DEFAULT_PATH
    assert coordinator.secure is False
    assert coordinator.manage_settings is True
    assert coordinator.availability_timeout == DEFAULT_AVAILABILITY_TIMEOUT
    assert coordinator.initial_center_frequency is None


async def test_skip_keys_default_to_the_library_set_and_always_drop_time(
    hass, make_coordinator
):
    """Identity keys must never reach entities as measurements.

    ``model``/``id``/``channel`` identify the device; ``time`` is read raw for
    the reconnect-replay classification. Any of them leaking into the measurement
    fields creates a permanent junk sensor on every device the receiver hears.
    """
    coordinator = make_coordinator()

    assert coordinator.skip_keys == set(DEFAULT_SKIP_KEYS) | {"time"}


async def test_injected_skip_keys_replace_the_defaults_but_time_still_goes(
    hass, make_coordinator
):
    """The integration injects the library's skip set; ``time`` is added on top.

    The injected set is the whole answer for which fields are identity rather
    than measurement, so it replaces the library default rather than merging with
    it. ``time`` is the exception, added unconditionally: the replay
    classification reads it raw, and a caller who forgot it would turn every
    device's timestamp into a sensor.
    """
    coordinator = make_coordinator(skip_keys={"model", "id"})

    assert coordinator.skip_keys == {"model", "id", "time"}


async def test_event_driven_keys_are_empty_until_the_library_supplies_them(
    hass, make_coordinator
):
    """With no library loaded, every device is classified as a periodic reporter.

    That is the safe default: a periodic device expires into "unavailable" after
    its timeout, which is visible and recoverable. Classifying an unknown device
    as event-driven instead would mark it never-expire, so a battery that died
    last month would still read as a live temperature.
    """
    coordinator = make_coordinator()

    assert coordinator.event_driven_keys == frozenset()


async def test_injected_event_driven_keys_are_kept(hass, make_coordinator):
    """The setup layer's classification is what the watchdog actually applies."""
    coordinator = make_coordinator(event_driven_keys=frozenset({"motion"}))

    assert coordinator.event_driven_keys == frozenset({"motion"})


async def test_adopted_and_ignored_keys_are_seeded_from_the_entry(
    hass, make_coordinator
):
    """A restart must not re-offer devices the user already decided about.

    ``adopted`` is the gate on the event path and ``ignored`` is the drop list;
    both are the in-memory mirror of ``entry.data``. If either came back empty
    after a restart, every adopted device would be demoted to a pending candidate
    and every device the user hid would be offered again.
    """
    coordinator = make_coordinator(
        adopted_keys={"Acurite-606TX-42"}, ignored_keys={"Nexus-TH-7"}
    )

    assert coordinator.adopted == {"Acurite-606TX-42"}
    assert coordinator.ignored == {"Nexus-TH-7"}


async def test_a_brand_new_hub_adopts_and_ignores_nothing(hass, make_coordinator):
    """A hub added just now has approved nothing, so nothing is auto-created."""
    coordinator = make_coordinator()

    assert coordinator.adopted == set()
    assert coordinator.ignored == set()


async def test_a_fresh_coordinator_holds_no_device_state(hass, make_coordinator):
    """Every per-device map starts empty, and starts as a *map*.

    Diagnostics, the availability watchdog and the entity platforms all read
    these directly and none of them checks first. A map that started as ``None``
    turns the first frame from the first device into an ``AttributeError`` inside
    the client's event callback — which is swallowed, so the hub would simply
    never produce a single entity and never say why.
    """
    coordinator = make_coordinator()

    assert coordinator.pending == OrderedDict()
    assert coordinator.ignored_models == {}
    assert coordinator.devices == {}
    assert coordinator.last_seen == {}
    assert coordinator.available == {}
    assert coordinator.seen_fields == set()
    assert coordinator.device_fields == {}
    assert coordinator._discovered == set()
    assert coordinator._logged_unmapped == {}
    assert coordinator._logged_timeouts == {}


async def test_the_injectable_hooks_start_unwired(hass, make_coordinator):
    """Each hook is ``None`` until setup wires it, and every caller tests for it.

    ``None`` is load-bearing rather than incidental: the coordinator deliberately
    imports nothing from the entity platforms or the device registry, so it
    checks each hook against ``None`` before firing. Any other placeholder — an
    empty string is the one a mutation reaches for — passes those checks and gets
    *called*, turning a hub that is merely half-wired into one that raises on its
    first event.
    """
    coordinator = make_coordinator()

    assert coordinator.new_device_callback is None
    assert coordinator.effective_timeout_resolver is None
    assert coordinator.effective_clear_delay_resolver is None
    assert coordinator.hub_info_callback is None
    assert coordinator.known_field_keys == frozenset()
    assert coordinator.calibration_snapshot == {}
    assert coordinator.user_mappings_snapshot == {}
    assert coordinator.connection_snapshot == ()
    assert coordinator.device_removers == []


async def test_a_fresh_coordinator_is_disconnected_and_unstarted(
    hass, make_coordinator
):
    """Nothing is connected until ``async_start`` says so.

    The connect-edge detection in ``_emit_hub_update`` is a comparison against
    ``_was_connected``, and the outage reporting is a comparison against
    ``_ever_connected``. Starting either at ``True`` means the very first
    connection is not seen as an edge at all: the backlog anchor is never set, so
    a reconnect's replayed backlog is presented to the user as a burst of live
    readings, and managed-SDR adoption never runs.
    """
    coordinator = make_coordinator()

    assert coordinator._connection_time is None
    assert coordinator._was_connected is False
    assert coordinator._disconnected_since is None
    assert coordinator._devices_offline is False
    assert coordinator._ever_connected is False
    assert coordinator._seen_dev_info == {}
    assert coordinator._seen_dev_query is None
    assert coordinator._started is False
    assert coordinator._watchdog_unsub is None


async def test_a_fresh_coordinator_manages_no_sdr_settings(hass, make_coordinator):
    """Nothing is managed until the desired state is loaded or adopted.

    ``_initial_freq_seeded`` in particular is a one-shot latch. Pre-set to
    ``True`` it swallows the centre frequency the user chose when adding the hub,
    leaving the receiver on whatever the server happened to be tuned to.
    """
    coordinator = make_coordinator()

    assert coordinator._desired == {}
    assert coordinator._managed == set()
    assert coordinator._initial_freq_seeded is False


# --------------------------------------------------------------------------- #
# Lifecycle: async_start.                                                      #
# --------------------------------------------------------------------------- #
async def test_async_start_arms_the_watchdog_for_this_hub(hass, make_coordinator):
    """The availability watchdog is the only thing that expires a silent device.

    It is armed exactly once, on Home Assistant's clock, against this hub's own
    tick. Armed on the wrong callback or with no interval, a sensor whose battery
    died keeps showing its last reading forever; the name is what lets an
    operator tell two hubs' tasks apart in the running-tasks list.
    """
    coordinator = make_coordinator()

    with patch(TRACK_INTERVAL) as track:
        await coordinator.async_start()

    track.assert_called_once_with(
        hass,
        coordinator._async_watchdog,
        _WATCHDOG_INTERVAL,
        name=f"rtl_433 watchdog {coordinator.entry.entry_id}",
    )
    assert coordinator._watchdog_unsub is track.return_value
    assert coordinator._started is True


async def test_async_start_stamps_the_outage_clock_before_dialling(
    hass, make_coordinator
):
    """Until the first connect lands, the hub *is* unreachable, and says so.

    Without the stamp, a receiver that is down when Home Assistant restarts shows
    every one of its devices holding the state restored from before the restart —
    yesterday's temperature presented as current — instead of unavailable.
    """
    coordinator = make_coordinator()

    with patch(TRACK_INTERVAL):
        await coordinator.async_start()

    assert coordinator.disconnected_since is not None


async def test_async_start_is_ignored_once_already_started(
    hass, make_coordinator, client_transport
):
    """A second start would arm a second watchdog and leak the first.

    Two watchdogs means every availability transition is evaluated and dispatched
    twice, and the first one's unsubscribe handle is overwritten, so it keeps
    running after the entry is unloaded.
    """
    coordinator = make_coordinator()

    with patch(TRACK_INTERVAL) as track:
        await coordinator.async_start()
        await coordinator.async_start()

    assert track.call_count == 1
    assert client_transport.start.await_count == 1


async def test_async_start_names_the_hub_it_started(hass, make_coordinator, caplog):
    """The start line is how an operator confirms which endpoint was dialled.

    A hub that never produces an entity is usually a hub pointed at the wrong
    URL, and this DEBUG line is the only place the resolved WebSocket URL appears
    before the first frame arrives.
    """
    coordinator = make_coordinator()

    with patch(TRACK_INTERVAL), caplog.at_level(logging.DEBUG, logger=LOG):
        await coordinator.async_start()

    assert f"rtl_433 coordinator started for {coordinator.ws_url}" in caplog.messages


# --------------------------------------------------------------------------- #
# Lifecycle: async_stop.                                                       #
# --------------------------------------------------------------------------- #
async def test_async_stop_disarms_the_watchdog_and_stops_the_client(
    hass, make_coordinator, client_transport
):
    """An unloaded entry must leave nothing running behind it.

    The watchdog is a Home Assistant time-interval subscription: left armed
    across a reload it keeps ticking against a dead coordinator and dispatching
    availability repaints at entities that no longer exist. Dropping the handle
    afterwards is what stops a second stop from calling a stale unsubscribe.
    """
    coordinator = make_coordinator()
    unsub = Mock()
    coordinator._watchdog_unsub = unsub
    coordinator._started = True

    await coordinator.async_stop()

    unsub.assert_called_once_with()
    assert coordinator._watchdog_unsub is None
    assert coordinator._started is False
    client_transport.stop.assert_awaited_once_with()


async def test_async_stop_without_a_watchdog_still_stops_the_client(
    hass, make_coordinator, client_transport
):
    """Stopping a coordinator that never started must not raise.

    Setup can fail between construction and ``async_start``, and Home Assistant
    still tears the entry down. An unguarded unsubscribe there would raise inside
    the unload path and leave the config entry stuck in a failed state the user
    can only clear by restarting.
    """
    coordinator = make_coordinator()

    await coordinator.async_stop()

    client_transport.stop.assert_awaited_once_with()


async def test_async_stop_clears_the_outage_bookkeeping(hass, make_coordinator):
    """A deliberate stop is not an outage, and the next start is not a recovery.

    Closing the socket fires the client's own hub-update callback, so the state
    is reset *after* that has run. If the outage clock survived a reload, the
    next successful connect would log a fabricated "reconnected after 900s" and
    the hub-offline gate would still believe every device behind it is dark.
    """
    coordinator = make_coordinator()
    coordinator._started = True
    coordinator._was_connected = True
    coordinator._connection_time = dt_util.utcnow()
    coordinator._disconnected_since = dt_util.utcnow()
    coordinator._devices_offline = True
    coordinator._ever_connected = True

    await coordinator.async_stop()

    assert coordinator._was_connected is False
    assert coordinator._connection_time is None
    assert coordinator._disconnected_since is None
    assert coordinator._devices_offline is False
    assert coordinator._ever_connected is False


async def test_async_stop_names_the_hub_it_stopped(hass, make_coordinator, caplog):
    """The stop line pairs with the start line when diagnosing reload loops."""
    coordinator = make_coordinator()

    with caplog.at_level(logging.DEBUG, logger=LOG):
        await coordinator.async_stop()

    assert f"rtl_433 coordinator stopped for {coordinator.ws_url}" in caplog.messages


# --------------------------------------------------------------------------- #
# Connect-edge policy: managed-SDR adopt + enforce.                            #
# --------------------------------------------------------------------------- #
async def test_on_connect_adopts_and_enforces_the_managed_settings(
    hass, make_coordinator
):
    """A receiver that rebooted comes back on the settings the user chose.

    rtl_433 forgets its gain and frequency across a restart, so the replay on the
    connect edge is the only thing that puts them back. Skipped, the hub silently
    returns to whatever the server's command line defaults to and the user's 868
    MHz sensors go quiet with nothing reporting an error.
    """
    coordinator = make_coordinator(manage_settings=True)
    coordinator._client.meta = {"center_frequency": 433920000}

    with (
        patch.object(
            coordinator, "_seed_desired_on_first_connect", new=AsyncMock()
        ) as seed,
        patch.object(coordinator, "_enforce_all", new=AsyncMock()) as enforce,
        patch.object(coordinator._client, "refresh_meta", new=AsyncMock()) as refresh,
    ):
        await coordinator._on_connect()

    seed.assert_awaited_once_with()
    enforce.assert_awaited_once_with()
    refresh.assert_not_awaited()


async def test_on_connect_fetches_meta_when_the_client_has_none(hass, make_coordinator):
    """First-connect adoption has nothing to adopt from until meta is fetched.

    The connect-edge callback can fire before the client's own post-connect
    refresh lands, and adoption reads ``self.meta``. Without this one catch-up
    fetch the very first connect adopts nothing, so a hub with management on
    manages no fields at all until something else happens to refresh it.
    """
    coordinator = make_coordinator(manage_settings=True)
    coordinator._client.meta = {}

    with (
        patch.object(coordinator, "_seed_desired_on_first_connect", new=AsyncMock()),
        patch.object(coordinator, "_enforce_all", new=AsyncMock()),
        patch.object(coordinator._client, "refresh_meta", new=AsyncMock()) as refresh,
    ):
        await coordinator._on_connect()

    refresh.assert_awaited_once_with()


async def test_on_connect_leaves_an_unmanaged_receiver_alone(hass, make_coordinator):
    """Management off means Home Assistant never writes to the receiver.

    Users share one rtl_433 server between Home Assistant and other tools, and
    this toggle is the promise that nothing here will retune it. Enforcing anyway
    would overwrite whatever that other tool had set, from a callback the user
    never triggered.
    """
    coordinator = make_coordinator(manage_settings=False)
    coordinator._client.meta = {}

    with (
        patch.object(
            coordinator, "_seed_desired_on_first_connect", new=AsyncMock()
        ) as seed,
        patch.object(coordinator, "_enforce_all", new=AsyncMock()) as enforce,
        patch.object(coordinator._client, "refresh_meta", new=AsyncMock()) as refresh,
    ):
        await coordinator._on_connect()

    seed.assert_not_awaited()
    enforce.assert_not_awaited()
    refresh.assert_not_awaited()


async def test_a_failed_sdr_adoption_is_reported_and_swallowed(
    hass, make_coordinator, caplog
):
    """A receiver that refuses ``/cmd`` must not take the event stream with it.

    ``/cmd`` is commonly unreachable — behind a reverse proxy, or disabled
    server-side — while the WebSocket works fine. Letting that raise out of the
    connect edge would kill the events too, so the failure is swallowed; the
    DEBUG line naming the hub and the error is then the only evidence the user's
    gain and frequency are not actually being applied.
    """
    coordinator = make_coordinator(manage_settings=True)
    coordinator._client.meta = {"center_frequency": 433920000}

    with (
        patch.object(
            coordinator,
            "_seed_desired_on_first_connect",
            new=AsyncMock(side_effect=RuntimeError("cmd unreachable")),
        ),
        patch.object(coordinator, "_enforce_all", new=AsyncMock()) as enforce,
        caplog.at_level(logging.DEBUG, logger=LOG),
    ):
        await coordinator._on_connect()

    assert (
        f"rtl_433 SDR adopt/enforce failed for {coordinator.ws_url}: cmd unreachable"
        in caplog.messages
    )
    enforce.assert_not_awaited()


# --------------------------------------------------------------------------- #
# Connect-edge policy: hub device-registry identity.                           #
# --------------------------------------------------------------------------- #
async def test_the_hub_identity_refresh_fires_once_per_change(hass, make_coordinator):
    """The device-registry write happens on a change, not on every hub update.

    The client fires ``on_hub_update`` on every meta and stats refresh, which is
    every few seconds. Without the comparison against what was last seen, each
    one would rewrite the hub's device-registry entry, and every rewrite is a
    registry-updated event pushed to every open Home Assistant frontend.
    """
    coordinator = make_coordinator()
    calls = []
    coordinator.hub_info_callback = lambda: calls.append(1)
    coordinator._client.dev_info = {"vendor": "Realtek", "serial": "00000001"}
    coordinator._client.dev_query = "0"

    coordinator._maybe_refresh_hub_identity()

    assert len(calls) == 1
    assert coordinator._seen_dev_info == {"vendor": "Realtek", "serial": "00000001"}
    assert coordinator._seen_dev_query == "0"

    coordinator._maybe_refresh_hub_identity()

    assert len(calls) == 1


async def test_the_hub_identity_refresh_fires_when_only_the_usb_label_changes(
    hass, make_coordinator
):
    """Half the identity changing is still the identity changing.

    Swapping the dongle for a different model while the ``-d`` selector stays
    ``0`` is the ordinary case: same slot, different radio. Requiring *both*
    halves to differ would leave the hub's device page naming the receiver the
    user removed.
    """
    coordinator = make_coordinator()
    calls = []
    coordinator.hub_info_callback = lambda: calls.append(1)
    coordinator._client.dev_info = {"vendor": "Realtek"}
    coordinator._client.dev_query = "0"
    coordinator._maybe_refresh_hub_identity()

    coordinator._client.dev_info = {"vendor": "Nooelec"}
    coordinator._maybe_refresh_hub_identity()

    assert len(calls) == 2
    assert coordinator._seen_dev_info == {"vendor": "Nooelec"}


async def test_the_hub_identity_refresh_fires_when_only_the_selector_changes(
    hass, make_coordinator
):
    """The same dongle model moved to another slot is still a new identity.

    Two identical dongles in one machine report the same USB label and differ
    only in the ``-d`` selector rtl_433 opened, so the selector alone has to be
    enough to trigger the refresh.
    """
    coordinator = make_coordinator()
    calls = []
    coordinator.hub_info_callback = lambda: calls.append(1)
    coordinator._client.dev_info = {"vendor": "Realtek"}
    coordinator._client.dev_query = "0"
    coordinator._maybe_refresh_hub_identity()

    coordinator._client.dev_query = "1"
    coordinator._maybe_refresh_hub_identity()

    assert len(calls) == 2
    assert coordinator._seen_dev_query == "1"


async def test_the_hub_identity_is_recorded_even_with_no_callback_wired(
    hass, make_coordinator
):
    """A coordinator whose setup has not wired the refresh yet must not raise.

    The hook is wired by the integration setup after construction, and the client
    can learn the identity in between. Firing the hook unconditionally would call
    ``None`` from inside the client's callback on the very first connect.
    """
    coordinator = make_coordinator()
    coordinator._client.dev_info = {"vendor": "Realtek"}
    coordinator._client.dev_query = "0"

    coordinator._maybe_refresh_hub_identity()

    assert coordinator._seen_dev_query == "0"


async def test_a_failing_hub_identity_callback_is_reported_and_swallowed(
    hass, make_coordinator, caplog
):
    """A device-registry hiccup must not take down the connection.

    The callback reaches into Home Assistant's device registry, which can raise
    for reasons that have nothing to do with the radio. It runs on the connect
    edge, so an escaping exception would break the client's hub-update callback
    and, with it, the availability gate for every device behind the hub. The
    DEBUG line carrying the error text is what makes a hub stuck showing the
    wrong model diagnosable at all.
    """
    coordinator = make_coordinator()

    def _boom() -> None:
        raise RuntimeError("registry unavailable")

    coordinator.hub_info_callback = _boom
    coordinator._client.dev_info = {"vendor": "Realtek"}
    coordinator._client.dev_query = "0"

    with caplog.at_level(logging.DEBUG, logger=LOG):
        coordinator._maybe_refresh_hub_identity()

    assert "rtl_433 hub_info_callback failed: registry unavailable" in caplog.messages
    assert coordinator._seen_dev_info == {"vendor": "Realtek"}


# --------------------------------------------------------------------------- #
# Config-flow connectivity check.                                              #
# --------------------------------------------------------------------------- #
async def test_validate_connection_probes_the_endpoint_the_user_typed(hass):
    """The config flow must validate the same endpoint it is about to store.

    This probe is the only thing standing between a typo and a config entry that
    can never connect. Validating a different host, port, path or scheme than the
    one submitted turns the "cannot connect" error into a false success, and the
    user ends up with a hub that looks configured and never reports anything.
    """
    with patch(CLIENT_VALIDATE) as validate:
        validate.return_value = True

        result = await Rtl433Coordinator.validate_connection(
            hass, "attic.local", 9999, "/stream", secure=True
        )

    assert result is True
    validate.assert_awaited_once_with(
        async_get_clientsession(hass), "attic.local", 9999, "/stream", secure=True
    )


async def test_validate_connection_defaults_to_a_plaintext_endpoint(hass):
    """A caller that names only a host gets the documented rtl_433 defaults.

    The discovery and repair flows call it this way. Probing over TLS by default
    would fail against every plain-HTTP rtl_433 server there is, so a discovered
    hub could never be confirmed.
    """
    with patch(CLIENT_VALIDATE) as validate:
        validate.return_value = True

        await Rtl433Coordinator.validate_connection(hass, "attic.local")

    validate.assert_awaited_once_with(
        async_get_clientsession(hass),
        "attic.local",
        DEFAULT_PORT,
        DEFAULT_PATH,
        secure=False,
    )


# --------------------------------------------------------------------------- #
# Hub-state fan-out: the connect and disconnect edges.                         #
# --------------------------------------------------------------------------- #
def _event(*, is_replay: bool = False, is_repaint: bool = False) -> NormalizedEvent:
    """Build a client-shaped event carrier for the dispatch tests."""
    return NormalizedEvent(
        device_key=_KEY,
        model="Acurite-606TX",
        fields={"temperature_C": 21.4},
        is_replay=is_replay,
        is_repaint=is_repaint,
    )


async def test_the_connect_edge_anchors_the_backlog_gate_and_adopts(
    hass, make_coordinator
):
    """A reconnect runs SDR adoption in the background, not in the callback.

    The client's ``on_hub_update`` is the socket's own callback: awaiting a round
    of ``/cmd`` traffic inside it would stall every frame behind it. The task is
    created against the config entry so Home Assistant cancels it on unload, and
    its name is what identifies a stuck adopt in the running-tasks list.

    ``_connection_time`` is anchored on the same edge; it is the gate that tells
    a reconnect's replayed backlog apart from live transmissions, so without it
    every device behind the hub fires its automations again on every reconnect.
    """
    coordinator = make_coordinator(manage_settings=True)
    coordinator._client.connected = True

    with (
        patch.object(type(coordinator.entry), "async_create_background_task") as task,
        patch(DISPATCH),
    ):
        coordinator._emit_hub_update()

    assert coordinator._was_connected is True
    assert coordinator._connection_time is not None
    task.assert_called_once()
    assert task.call_args.args[0] is hass
    assert task.call_args.kwargs == {
        "name": f"rtl_433 sdr adopt {coordinator.entry.entry_id}"
    }
    # The coroutine was handed to a mock that never awaits it.
    task.call_args.args[1].close()


async def test_the_disconnect_edge_drops_the_backlog_anchor(hass, make_coordinator):
    """A dropped socket must not leave a stale connection anchor behind.

    The anchor is what makes the next reconnect's backlog recognisable as a
    backlog. Left pointing at the *previous* connection, the replayed frames
    after the next reconnect all date from after it and are presented as live —
    an hour-old door-open replayed as a door opening now.
    """
    coordinator = make_coordinator(manage_settings=False)
    coordinator._started = True
    coordinator._client.connected = True

    with patch(DISPATCH):
        coordinator._emit_hub_update()
        coordinator._client.connected = False
        coordinator._emit_hub_update()

    assert coordinator._was_connected is False
    assert coordinator._connection_time is None


async def test_every_hub_update_repaints_this_hubs_own_entities(hass, make_coordinator):
    """The hub-update signal is scoped to this config entry.

    The hub entities — the SDR controls, the actual-value sensors, the connection
    status — all subscribe to it. Dispatched on any other name, a second hub's
    entities would repaint from this hub's meta, or nothing would repaint at all
    and the controls would sit on stale values until a restart.
    """
    coordinator = make_coordinator(manage_settings=False)
    coordinator._client.connected = False

    with patch(DISPATCH) as dispatch:
        coordinator._emit_hub_update()

    dispatch.assert_any_call(hass, signal_hub_update(coordinator.entry.entry_id))


# --------------------------------------------------------------------------- #
# Per-device fan-out.                                                          #
# --------------------------------------------------------------------------- #
async def test_a_device_update_is_addressed_to_that_device_on_this_hub(
    hass, make_coordinator
):
    """The device signal carries both the entry and the device key.

    Every entity of one device subscribes to exactly this name. Keyed on the
    wrong entry, two hubs that both hear the same sensor model would feed each
    other's entities; keyed on the wrong device, one sensor's reading would land
    on another's.
    """
    coordinator = make_coordinator(adopted_keys={_KEY})
    event = _event()

    with patch(DISPATCH) as dispatch:
        coordinator._dispatch(_KEY, event)

    dispatch.assert_called_once_with(
        hass, signal_device_update(coordinator.entry.entry_id, _KEY), event
    )


async def test_an_ordinary_event_is_forwarded_exactly_as_classified(
    hass, make_coordinator
):
    """The library's replay verdict rides on the event and is not second-guessed.

    ``_on_client_event`` passes no explicit flag, so the object must arrive at the
    entities unchanged. Rewriting it here would overwrite the classification the
    library made against the connection anchor — and a replay re-stamped as live
    is a reconnect firing every automation behind the hub a second time.
    """
    coordinator = make_coordinator(adopted_keys={_KEY})
    event = _event(is_replay=True)

    with patch(DISPATCH) as dispatch:
        coordinator._dispatch(_KEY, event)

    assert dispatch.call_args.args[2] is event


async def test_a_watchdog_repaint_overrides_a_cached_replay_flag(
    hass, make_coordinator
):
    """An unavailable re-paint must never be suppressed as a replay.

    The watchdog re-dispatches the device's *cached* last event to mark it
    unavailable. If that cached object happened to be a replay frame, the entity
    would drop the re-paint as a duplicate and go on showing a reading from a
    sensor that stopped transmitting hours ago. The override is copied onto a
    fresh object, so the cached event keeps its own verdict for the next reader.
    """
    coordinator = make_coordinator(adopted_keys={_KEY})
    event = _event(is_replay=True)

    with patch(DISPATCH) as dispatch:
        coordinator._dispatch(_KEY, event, is_replay=False, is_repaint=True)

    sent = dispatch.call_args.args[2]
    assert sent.is_replay is False
    assert sent.is_repaint is True
    assert event.is_replay is True
    assert event.is_repaint is False


async def test_a_repaint_is_flagged_without_touching_the_replay_verdict(
    hass, make_coordinator
):
    """``is_repaint`` alone still has to reach the entity.

    It is what tells ``Rtl433Event`` that this is the availability re-paint of a
    cached frame rather than a new transmission, so it must be stamped even when
    the caller leaves the replay verdict to the object. Dropped, a re-paint
    re-fires the device's trigger as though the remote had been pressed again.
    """
    coordinator = make_coordinator(adopted_keys={_KEY})
    event = _event()

    with patch(DISPATCH) as dispatch:
        coordinator._dispatch(_KEY, event, is_repaint=True)

    sent = dispatch.call_args.args[2]
    assert sent.is_repaint is True
    assert sent.is_replay is False


# --------------------------------------------------------------------------- #
# Un-adoption.                                                                 #
# --------------------------------------------------------------------------- #
async def test_forgetting_a_device_drops_its_logging_memos_too(hass, make_coordinator):
    """A re-adopted device is a device we have no history for.

    The two memos are "already warned about these unmapped fields" and "already
    logged this resolved timeout". Kept across a removal, a device the user
    deletes and re-adds comes back silent: the unmapped-field warning that would
    explain a missing entity, and the timeout line that would explain a
    surprising availability, are both suppressed as things already said.
    """
    coordinator = make_coordinator(adopted_keys={_KEY})
    coordinator._discovered.add(_KEY)
    coordinator._logged_unmapped[_KEY] = {"unknown_field"}
    coordinator._logged_timeouts[_KEY] = 600

    with patch(DISPATCH):
        coordinator.forget_device(_KEY)

    assert coordinator.adopted == set()
    assert coordinator._discovered == set()
    assert coordinator._logged_unmapped == {}
    assert coordinator._logged_timeouts == {}


async def test_forgetting_a_device_with_no_runtime_state_is_harmless(
    hass, make_coordinator
):
    """Removal has to work for a device that never transmitted.

    Home Assistant offers the delete on the device page whether or not the device
    has been heard from this session, and an adopted device that has been silent
    since the restart has no entry in any of the runtime maps. A removal that
    raised there would fail the delete and leave the device stuck on the page.
    """
    coordinator = make_coordinator(adopted_keys={_KEY})

    with patch(DISPATCH):
        coordinator.forget_device(_KEY)

    assert coordinator.adopted == set()


async def test_an_explicit_flag_that_already_matches_changes_nothing(
    hass, make_coordinator
):
    """The rewrite is a *correction*, and a correct event is passed through as is.

    The watchdog names the replay verdict it wants on every re-dispatch, and most
    of the time the cached event already carries it. Minting a fresh copy anyway
    would break the object identity the event entity uses to recognise a frame it
    has already handled, so an ordinary silence check would start re-firing the
    device's trigger once per watchdog tick.
    """
    coordinator = make_coordinator(adopted_keys={_KEY})
    event = _event(is_replay=False)

    with patch(DISPATCH) as dispatch:
        coordinator._dispatch(_KEY, event, is_replay=False)

    assert dispatch.call_args.args[2] is event


async def test_forgetting_a_device_also_withdraws_it_as_a_candidate(
    hass, make_coordinator
):
    """A key cannot be both deleted and still on offer in the approval panel.

    ``forget_device`` drops the key back to "heard but not approved", and the
    pending map is what the discovery panel renders. Leaving a stale record there
    would show the user a candidate card for a device they are in the middle of
    deleting — and adopting it would re-create the device from the record instead
    of from a live transmission.
    """
    coordinator = make_coordinator()
    coordinator.pending[_KEY] = PendingDevice(
        key=_KEY,
        model="Acurite-606TX",
        event=_event(),
        count=1,
        first_seen=dt_util.utcnow(),
        last_seen=dt_util.utcnow(),
        fields={"temperature_C": 21.4},
    )

    with patch(DISPATCH):
        coordinator.forget_device(_KEY)

    assert coordinator.pending == {}
