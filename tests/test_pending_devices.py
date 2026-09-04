"""The coordinator's observation-vs-adoption routing contract.

``tests/test_coordinator.py`` covers what the coordinator does with an *adopted*
device's frames. This module covers the decision made one step earlier: which of
the four outcomes — pending, dropped, adopted, or promoted — a frame gets, and
what each outcome is and is not allowed to touch.

That decision is the core of issues #128 and #131. Before adoption existed, every
frame the server decoded created a Home Assistant device, a set of entities, and
a persistent notification, so a noisy location filled the device registry with
neighbours' sensors and bad decodes. Now a frame only reaches that machinery if
its key is in ``adopted``; everything else becomes an in-memory
:class:`~custom_components.rtl_433.coordinator._events.PendingDevice` the user
can approve or ignore, and a replay or backlog re-broadcast becomes nothing at
all.

The isolation between the two paths is load-bearing rather than tidy: the
availability watchdog, diagnostics, and the entity platforms all read
``devices`` / ``last_seen`` / ``available`` / ``device_fields`` and assume every
key in them exists in Home Assistant. A pending device leaking into any of those
maps would have the watchdog announcing that a device the user never added has
gone offline. Several assertions here exist only to pin that down.

Events are injected the way ``tests/test_coordinator.py`` does — by calling the
coordinator's ``on_event`` seam with a ``NormalizedEvent`` carrying the replay
verdict the library already stamped — so these tests exercise the routing branch
without re-testing the library's classification. The registry-level half of the
contract (no device, no entities, adoption equivalence) lives in
``tests/test_lifecycle.py``, which drives the real config entry.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from freezegun import freeze_time
from pyrtl_433.normalizer import NormalizedEvent
import pytest

from custom_components.rtl_433.const import signal_pending_update
from custom_components.rtl_433.coordinator import Rtl433Coordinator
from homeassistant.util import dt as dt_util

DISPATCH = "custom_components.rtl_433.coordinator.base.async_dispatcher_send"
_KEY = "Acurite-606TX-42"
_MODEL = "Acurite-606TX"
# The connect-edge anchor every test pins, so "before the connection" (backlog)
# and "after it" (live) are unambiguous rather than wall-clock dependent.
_CONNECTED_AT = dt_util.parse_datetime("2026-05-25T10:00:00+00:00")


def _dispatched(dispatch) -> list[str]:
    """Return the signal names one patched ``async_dispatcher_send`` recorded.

    The coordinator sends several *different* dispatcher signals through the one
    function these tests patch, and this module cares which: a device-update
    (:func:`~custom_components.rtl_433.const.signal_device_update`) is the fan-out
    that reaches a device's entities and must never happen for a device the user
    has not adopted, while a pending-update
    (:func:`~custom_components.rtl_433.const.signal_pending_update`) carries no
    device payload at all -- it only tells the discovery panel that the candidate
    list changed. Asserting on the names states that distinction; a bare
    ``assert_not_called`` could only say "nothing at all", which stopped being
    true once the panel needed telling and was always a coarser claim than the
    thing these tests exist to protect.
    """
    return [call.args[1] for call in dispatch.call_args_list]


@pytest.fixture
def make_coordinator(hass, hub_entry_builder):
    """Return a factory for a coordinator with a chosen adopted/ignored state.

    A factory rather than a fixture because the whole point of this module is
    varying which lists a key starts in. Called from inside an async test so the
    coordinator's :class:`pyrtl_433.Rtl433Client` is built on a running loop, and
    with ``_connection_time`` pre-set so the backlog gate has an anchor to
    compare against (it is otherwise ``None``, which means "never a backlog").
    """

    def _make(*, adopted: set[str] | None = None, ignored: set[str] | None = None):
        entry = hub_entry_builder(availability_timeout=600)
        entry.add_to_hass(hass)
        coordinator = Rtl433Coordinator(
            hass,
            entry,
            host="rtl433.local",
            availability_timeout=600,
            skip_keys={"model", "id", "channel", "subtype", "time", "mic"},
            adopted_keys=adopted,
            ignored_keys=ignored,
        )
        coordinator._connection_time = _CONNECTED_AT
        return coordinator

    return _make


def _event(
    key: str = _KEY,
    model: str = _MODEL,
    *,
    fields=None,
    is_replay: bool = False,
    event_time=None,
) -> NormalizedEvent:
    """Build a client-shaped NormalizedEvent, live and post-connection by default."""
    return NormalizedEvent(
        device_key=key,
        model=model,
        fields={"temperature_C": 21.4} if fields is None else fields,
        is_replay=is_replay,
        event_time=_CONNECTED_AT + timedelta(seconds=5)
        if event_time is None
        else event_time,
    )


# --------------------------------------------------------------------------- #
# The routing decision itself.                                                 #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("is_replay", "is_backlog", "ignored", "adopted", "expect"),
    [
        (False, False, False, False, "pending"),
        (True, False, False, False, "dropped"),
        (False, True, False, False, "dropped"),
        (False, False, True, False, "dropped"),
        (False, False, False, True, "adopted"),
    ],
    ids=["live-unknown", "replay", "backlog", "ignored", "live-adopted"],
)
async def test_frame_routing_matrix(
    hass, make_coordinator, is_replay, is_backlog, ignored, adopted, expect
):
    """One frame, five starting states, three outcomes.

    Table-driven because the five cases differ only in which gate fires, and
    writing them out longhand would obscure that they are one decision. The
    outcomes:

    * ``pending`` — the default for anything the user has not ruled on. This is
      the case that replaced auto-add.
    * ``dropped`` — a replay or a pre-connection backlog frame is a re-broadcast
      of something already transmitted, so it must not look like a first
      sighting; without those two gates every reconnect would refill the pending
      list with stale candidates. An ignored key is dropped because that is what
      makes ignoring a neighbour's sensor stick.
    * ``adopted`` — the pre-existing path, unchanged: runtime state, the
      registration callback, and dispatch to the device's entities.

    Neither ``dropped`` nor ``pending`` may offer the device for registration or
    dispatch a *device-update*, since either would put a device in front of the
    user that they never asked for. ``pending`` does dispatch one thing -- a
    pending-update, so the discovery panel learns its candidate list grew -- and
    the assertion names the signals rather than counting them, so "no entity
    fan-out" is stated directly instead of being inferred from silence.
    """
    coordinator = make_coordinator(
        adopted={_KEY} if adopted else None,
        ignored={_KEY} if ignored else None,
    )
    registered: list[str] = []
    coordinator.new_device_callback = lambda key, model, replay: registered.append(key)

    event_time = (
        _CONNECTED_AT - timedelta(minutes=1)
        if is_backlog
        else _CONNECTED_AT + timedelta(seconds=5)
    )
    with patch(DISPATCH) as dispatch:
        coordinator._on_client_event(_event(is_replay=is_replay, event_time=event_time))

    if expect == "adopted":
        assert coordinator.pending == {}
        assert coordinator.devices[_KEY].fields == {"temperature_C": 21.4}
        assert coordinator.available[_KEY] is True
        assert registered == [_KEY]
        dispatch.assert_called_once()
        return

    assert set(coordinator.pending) == ({_KEY} if expect == "pending" else set())
    assert _KEY not in coordinator.devices
    assert _KEY not in coordinator.adopted
    assert registered == []
    # Exactly which signals each outcome may send, by name. ``pending`` fires one
    # pending-update and nothing else: a candidate appearing is a membership
    # change the discovery panel subscribes to, and it carries no device payload
    # -- it says "the list changed", not "here is a device". ``dropped`` changed
    # no state at all, so it says nothing. Neither may send a device-update,
    # which is the fan-out that reaches entities and would put a device in front
    # of the user that they never asked for. That is the claim this test has
    # always been making; naming the signals is what finally states it exactly.
    assert _dispatched(dispatch) == (
        [signal_pending_update(coordinator.entry.entry_id)]
        if expect == "pending"
        else []
    )


# --------------------------------------------------------------------------- #
# A pending device is invisible to every consumer of adopted runtime state.    #
# --------------------------------------------------------------------------- #
async def test_pending_frame_touches_no_adopted_runtime_state(hass, make_coordinator):
    """A pending candidate stays out of every map that describes real devices.

    ``devices`` / ``last_seen`` / ``available`` / ``seen_fields`` /
    ``device_fields`` are read by the availability watchdog, diagnostics, and the
    entity platforms, all of which assume each key in them is a device that
    exists in Home Assistant. If a pending frame wrote ``last_seen``, the
    watchdog would flip a device the user never added to unavailable and dispatch
    a repaint for entities that do not exist — the regression this asserts
    against. The watchdog is actually run here rather than reasoned about.

    The frame does dispatch one signal, and should: a pending-update, which tells
    the discovery panel that the candidate list changed. It names no device and
    reaches no entity, so it is not the leak this test guards — the assertion
    below pins the dispatched signal *by name* to exactly that one, which rules a
    device-update out far more precisely than "nothing was dispatched" ever did.
    """
    coordinator = make_coordinator()
    start = dt_util.utcnow()

    with freeze_time(start), patch(DISPATCH) as dispatch:
        coordinator._on_client_event(_event(fields={"temperature_C": 21.4}))

    record = coordinator.pending[_KEY]
    assert list(coordinator.pending) == [_KEY]
    assert record.model == _MODEL
    assert record.count == 1
    assert record.first_seen == record.last_seen
    assert record.event.fields == {"temperature_C": 21.4}

    assert _KEY not in coordinator.devices
    assert _KEY not in coordinator.last_seen
    assert _KEY not in coordinator.available
    assert _KEY not in coordinator.device_fields
    assert coordinator.seen_fields == set()
    # Not offered for registration either, so nothing downstream can build it.
    assert _KEY not in coordinator._discovered
    # The one signal a new candidate is allowed to send, and the one it must:
    # a pending-update, which tells the discovery panel its list changed without
    # naming a device to build. No device-update, because there are no entities
    # to fan out to -- that is the regression this whole test guards.
    assert _dispatched(dispatch) == [signal_pending_update(coordinator.entry.entry_id)]

    # Long past any timeout: the watchdog has nothing to say about a device that
    # was only ever heard.
    with freeze_time(start + timedelta(seconds=3600)), patch(DISPATCH) as dispatch:
        await coordinator._async_watchdog(dt_util.utcnow())
    assert coordinator.available == {}
    dispatch.assert_not_called()


async def test_repeat_sightings_sharpen_one_record(hass, make_coordinator):
    """Hearing a device again refines its candidate instead of duplicating it.

    Sighting count and last-seen are how the approval form lets a user tell a
    real sensor that checks in every minute from a one-off bad decode, and the
    stored event is what adoption seeds the new entities from — so a repeat must
    bump the count, move ``last_seen``, and replace the event while leaving
    ``first_seen`` alone. A second record for the same key would show the device
    twice in the form and halve its apparent sighting count.
    """
    coordinator = make_coordinator()
    start = dt_util.utcnow()

    with freeze_time(start), patch(DISPATCH):
        coordinator._on_client_event(_event(fields={"temperature_C": 21.4}))
    first_seen = coordinator.pending[_KEY].first_seen

    with freeze_time(start + timedelta(minutes=1)), patch(DISPATCH):
        coordinator._on_client_event(
            _event(fields={"temperature_C": 22.9, "humidity": 61})
        )

    assert list(coordinator.pending) == [_KEY]
    record = coordinator.pending[_KEY]
    assert record.count == 2
    assert record.first_seen == first_seen
    assert record.last_seen == start + timedelta(minutes=1)
    # The newest frame wins: adoption seeds humidity as well as temperature.
    assert record.event.fields == {"temperature_C": 22.9, "humidity": 61}
    # Still nothing has leaked into the adopted-device state.
    assert coordinator.seen_fields == set()
    assert _KEY not in coordinator.devices


# --------------------------------------------------------------------------- #
# Approving and refusing a candidate.                                          #
# --------------------------------------------------------------------------- #
async def test_adopt_device_promotes_the_stored_event(hass, make_coordinator):
    """Adoption seeds from the latest stored frame and re-opens the adopted path.

    Seeding from the stored event rather than waiting for the next transmission
    is what makes a freshly added device arrive with real values instead of an
    unavailable placeholder. After the promotion the device must be on the
    ordinary adopted path — the following frame updates runtime state and
    dispatches — which is what makes an adopted device indistinguishable from one
    the old auto-add path created.
    """
    coordinator = make_coordinator()
    registered: list[tuple[str, str, bool]] = []
    coordinator.new_device_callback = lambda key, model, replay: registered.append(
        (key, model, replay)
    )

    with patch(DISPATCH):
        coordinator._on_client_event(_event(fields={"temperature_C": 21.4}))
        coordinator._on_client_event(_event(fields={"temperature_C": 22.9}))
    heard_at = coordinator.pending[_KEY].last_seen

    record = coordinator.adopt_device(_KEY)

    assert record is not None
    assert record.count == 2
    assert coordinator.pending == {}
    assert _KEY in coordinator.adopted
    assert coordinator.devices[_KEY].fields == {"temperature_C": 22.9}
    assert coordinator.last_seen[_KEY] == heard_at
    assert coordinator.available[_KEY] is True
    assert coordinator.device_fields[_KEY] == {"temperature_C"}
    assert coordinator.seen_fields >= {"temperature_C"}
    # The same seam the auto-add path used, and fired as a live (not replay) add.
    assert registered == [(_KEY, _MODEL, False)]

    # The next frame now takes the adopted path all the way to the entities, and
    # does not re-offer the device for registration.
    with patch(DISPATCH) as dispatch:
        coordinator._on_client_event(_event(fields={"temperature_C": 23.5}))
    assert coordinator.devices[_KEY].fields == {"temperature_C": 23.5}
    assert len(registered) == 1
    dispatch.assert_called_once()


async def test_adopt_device_seeds_every_field_seen_not_just_the_last_frame(
    hass, make_coordinator
):
    """A device that splits its readings across frames gets all its entities.

    A weather station sends wind in one frame and rain in the next. The card
    shows both, because the candidate accumulates every field it has seen. If
    adoption seeded from the last frame alone, the user would press Add while a
    rain frame happened to be latest and get only the rain entities -- the rest
    appearing minutes later, whenever a frame carrying them arrived.
    """
    coordinator = make_coordinator()
    with patch(DISPATCH):
        coordinator._on_client_event(_event(fields={"wind_avg_km_h": 12.0}))
        coordinator._on_client_event(_event(fields={"rain_mm": 3.5}))

    # The candidate remembers both, though only the newest frame is stored.
    assert set(coordinator.pending[_KEY].fields) == {"wind_avg_km_h", "rain_mm"}

    coordinator.adopt_device(_KEY)

    assert coordinator.device_fields[_KEY] == {"wind_avg_km_h", "rain_mm"}
    assert coordinator.seen_fields >= {"wind_avg_km_h", "rain_mm"}


async def test_adopt_device_on_a_key_that_is_not_pending_creates_nothing(
    hass, make_coordinator
):
    """Adopting an unknown key is a no-op that reports the miss.

    The approval form is rendered from a snapshot of the pending list, so by the
    time the user submits, a key may already have been adopted by another submit
    or dropped by a reload. Returning ``None`` — rather than fabricating a device
    from no data — is what lets the caller skip it quietly.
    """
    coordinator = make_coordinator()
    registered: list[str] = []
    coordinator.new_device_callback = lambda key, model, replay: registered.append(key)

    assert coordinator.adopt_device("Ghost-Device-1") is None

    assert coordinator.adopted == set()
    assert coordinator.devices == {}
    assert coordinator.last_seen == {}
    assert coordinator.available == {}
    assert registered == []


async def test_ignore_device_drops_the_candidate_and_bars_its_return(
    hass, make_coordinator
):
    """Ignoring drops the candidate and keeps later frames from re-creating it.

    Adding the key to ``ignored`` in memory (the caller persists it to
    ``entry.data``) is what makes ignoring take effect on the device's very next
    transmission rather than at the next reload. Without that, a chatty
    neighbour's sensor would reappear in the list seconds after being ignored.
    """
    coordinator = make_coordinator()
    with patch(DISPATCH):
        coordinator._on_client_event(_event())
    assert _KEY in coordinator.pending

    coordinator.ignore_device(_KEY)
    assert coordinator.pending == {}
    assert _KEY in coordinator.ignored

    with patch(DISPATCH) as dispatch:
        coordinator._on_client_event(_event())
    assert coordinator.pending == {}
    assert _KEY not in coordinator.devices
    dispatch.assert_not_called()
