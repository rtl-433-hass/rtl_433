"""Mutation floor for ``coordinator/_events.py``.

``tests/test_pending_devices.py`` covers the routing decision -- which of
pending, dropped, adopted or promoted a frame gets. This module covers what
survived that: the bookkeeping either side of the decision, and the operator
diagnostics the module exists to emit.

Two kinds of thing are pinned here, and both were chosen because a mutation run
showed nothing else was holding them:

* **Accumulation.** Several of these maps are unions or counters, and the
  difference between ``|=`` and ``=``, or between ``or`` and ``and``, is
  invisible until a *second* device or a *second* frame arrives. Every test
  below that touches one sends two.
* **The log lines themselves.** ``_trace_unmapped_fields`` and the back-online /
  new-device / ignore messages are the only output this module produces for a
  human; a coordinator that silently stops reporting an unmapped field is
  indistinguishable from one with nothing to report. Asserting on the rendered
  message is what makes the wording, the arguments and the once-per-device
  de-duplication load-bearing rather than incidental.
"""

from __future__ import annotations

from datetime import timedelta
import logging
from unittest.mock import patch

from pyrtl_433.normalizer import NormalizedEvent
import pytest

from custom_components.rtl_433.coordinator import Rtl433Coordinator
from homeassistant.util import dt as dt_util

DISPATCH = "custom_components.rtl_433.coordinator.base.async_dispatcher_send"
LOG = "custom_components.rtl_433"
_KEY = "Acurite-606TX-42"
_OTHER = "Acurite-606TX-43"
_MODEL = "Acurite-606TX"
_CONNECTED_AT = dt_util.parse_datetime("2026-05-25T10:00:00+00:00")


@pytest.fixture
def make_coordinator(hass, hub_entry_builder):
    """A coordinator with a chosen adopted/ignored state and a connect anchor."""

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
) -> NormalizedEvent:
    """A live, post-connection frame unless the caller says otherwise."""
    return NormalizedEvent(
        device_key=key,
        model=model,
        fields={"temperature_C": 21.4} if fields is None else fields,
        is_replay=is_replay,
        event_time=_CONNECTED_AT + timedelta(seconds=5),
    )


# --------------------------------------------------------------------------- #
# Accumulation across devices and across frames.                               #
# --------------------------------------------------------------------------- #
async def test_seen_fields_accumulates_across_devices(hass, make_coordinator):
    """``seen_fields`` is a union over every device, not the latest frame's set.

    Diagnostics reports it as every field this hub has decoded, and it is what
    surfaces unmatched keys. Assigning instead of unioning would still look right
    for a single device and quietly forget the first one as soon as a second
    reported anything different -- so this sends two devices with disjoint
    fields and asserts both survive.
    """
    coordinator = make_coordinator(adopted={_KEY, _OTHER})
    with patch(DISPATCH):
        coordinator._on_client_event(_event(fields={"temperature_C": 21.4}))
        coordinator._on_client_event(_event(key=_OTHER, fields={"humidity": 55}))

    assert coordinator.seen_fields == {"temperature_C", "humidity"}
    assert coordinator.device_fields[_KEY] == {"temperature_C"}
    assert coordinator.device_fields[_OTHER] == {"humidity"}


async def test_repeat_sighting_keeps_the_model_when_a_later_frame_has_none(
    hass, make_coordinator
):
    """A later frame without a model must not erase the one already known.

    ``existing.model = normalized.model or existing.model`` is a fallback, not an
    assignment: some frames decode without a model string, and the candidate the
    user is looking at should keep the name it was first heard under. Swapping
    the ``or`` for an ``and`` blanks it on exactly those frames.
    """
    coordinator = make_coordinator()
    with patch(DISPATCH):
        coordinator._on_client_event(_event())
        coordinator._on_client_event(_event(model=""))

    candidate = coordinator.pending[_KEY]
    assert candidate.model == _MODEL
    assert candidate.count == 2


async def test_repeat_sighting_counts_up_and_moves_last_seen(hass, make_coordinator):
    """Repeat frames sharpen one candidate rather than creating another.

    The count and last-seen are how the user tells a sensor that keeps checking
    in from a one-off bad decode, so both have to move on the second frame while
    first-seen stays put.
    """
    coordinator = make_coordinator()
    with patch(DISPATCH):
        coordinator._on_client_event(_event())
        first = coordinator.pending[_KEY]
        first_seen, after_one = first.first_seen, first.last_seen
        coordinator._on_client_event(_event(fields={"temperature_C": 22.9}))

    candidate = coordinator.pending[_KEY]
    assert len(coordinator.pending) == 1
    assert candidate.count == 2
    assert candidate.first_seen == first_seen
    assert candidate.last_seen >= after_one
    # The newest frame is what adoption seeds entities from.
    assert candidate.event.fields == {"temperature_C": 22.9}


# --------------------------------------------------------------------------- #
# The back-online edge.                                                        #
# --------------------------------------------------------------------------- #
async def test_back_online_is_logged_only_after_going_unavailable(
    hass, make_coordinator, caplog
):
    """The recovery line fires on the transition, not on every live frame.

    It reads ``not is_replay and was_available is False``. Loosening the ``and``
    to an ``or``, or pinning ``was_available`` to ``None``, both produce a
    coordinator that either announces recovery constantly or never at all --
    neither of which any other assertion notices, because the log is the whole
    observable effect.
    """
    coordinator = make_coordinator(adopted={_KEY})
    with patch(DISPATCH):
        coordinator._on_client_event(_event())

        # A second live frame while still available: nothing to announce.
        caplog.clear()
        with caplog.at_level(logging.DEBUG, logger=LOG):
            coordinator._on_client_event(_event())
        assert "back online" not in caplog.text

        # Now mark it offline the way the watchdog does, and send a live frame.
        coordinator.available[_KEY] = False
        caplog.clear()
        with caplog.at_level(logging.DEBUG, logger=LOG):
            coordinator._on_client_event(_event())

    assert f"rtl_433 device {_KEY} back online" in caplog.text
    assert coordinator.available[_KEY] is True


async def test_a_replay_does_not_announce_recovery(hass, make_coordinator, caplog):
    """A re-broadcast must not resurrect a device that is genuinely offline.

    The replay still seeds sensor values, so it reaches the same code path; only
    the ``not is_replay`` half of the guard stops it claiming the device is back.
    """
    coordinator = make_coordinator(adopted={_KEY})
    with patch(DISPATCH):
        coordinator._on_client_event(_event())
        coordinator.available[_KEY] = False
        caplog.clear()
        with caplog.at_level(logging.DEBUG, logger=LOG):
            coordinator._on_client_event(_event(is_replay=True))

    assert "back online" not in caplog.text
    # Liveness is untouched by a replay: it stays offline until a live frame.
    assert coordinator.available[_KEY] is False


# --------------------------------------------------------------------------- #
# Operator diagnostics.                                                        #
# --------------------------------------------------------------------------- #
async def test_unmapped_fields_are_reported_once_per_device(
    hass, make_coordinator, caplog
):
    """The unmapped-field notice is de-duplicated per device, not globally.

    ``_logged_unmapped.setdefault(key, set())`` is what scopes the "already said
    this" memory to one device. Keyed on anything else, the first device to
    report a field would silence every other device reporting the same one --
    and the field this exists to surface is exactly the one a *second* sensor
    starts sending after a firmware change.
    """
    coordinator = make_coordinator(adopted={_KEY, _OTHER})
    coordinator.known_field_keys = {"temperature_C"}

    with patch(DISPATCH), caplog.at_level(logging.DEBUG, logger=LOG):
        coordinator._on_client_event(_event(fields={"temperature_C": 1, "weird": 2}))
        first = caplog.text.count("reported unmapped field(s)")

        # Same device, same field again: already said.
        coordinator._on_client_event(_event(fields={"temperature_C": 1, "weird": 3}))
        repeat = caplog.text.count("reported unmapped field(s)")

        # A different device reporting the same field is still news.
        coordinator._on_client_event(
            _event(key=_OTHER, fields={"temperature_C": 1, "weird": 4})
        )
        other = caplog.text.count("reported unmapped field(s)")

    assert first == 1
    assert repeat == 1
    assert other == 2
    assert (
        f"rtl_433 {_KEY} reported unmapped field(s) ['weird'] (no entity)"
        in caplog.text
    )


async def test_unmapped_fields_are_silent_without_a_library(hass, make_coordinator):
    """With no descriptors loaded, every field would look unmapped.

    The early return is what stops a failed library load turning into a log line
    per field per device.
    """
    coordinator = make_coordinator(adopted={_KEY})
    coordinator.known_field_keys = set()

    with patch(DISPATCH):
        coordinator._on_client_event(_event(fields={"anything": 1}))

    assert coordinator._logged_unmapped == {}


async def test_a_new_candidate_is_announced_with_its_key_and_model(
    hass, make_coordinator, caplog
):
    """The first sighting is the one line telling the user there is something to add."""
    coordinator = make_coordinator()
    with patch(DISPATCH), caplog.at_level(logging.INFO, logger=LOG):
        coordinator._on_client_event(_event())
        coordinator._on_client_event(_event())

    assert (
        f"rtl_433 heard a new device {_KEY} (model {_MODEL}); add it from the "
        "hub's options to create it in Home Assistant" in caplog.text
    )
    # Announced on the first sighting only; a repeat is not news.
    assert caplog.text.count("heard a new device") == 1


async def test_an_ignored_key_says_why_it_was_dropped(hass, make_coordinator, caplog):
    """Ignoring is silent to the user otherwise, so the debug line is the trail."""
    coordinator = make_coordinator(ignored={_KEY})
    with patch(DISPATCH), caplog.at_level(logging.DEBUG, logger=LOG):
        coordinator._on_client_event(_event())

    assert f"rtl_433 ignoring device {_KEY} (on the hub's ignore list)" in caplog.text
    assert coordinator.pending == {}


async def test_a_failing_register_callback_is_logged_and_does_not_propagate(
    hass, make_coordinator, caplog
):
    """A bad hook must not kill the event loop, but it must not be silent either.

    The callback is supplied by platform setup; if it raises, the frame is still
    processed and the failure is reported against the key that caused it.
    """
    coordinator = make_coordinator(adopted={_KEY})

    def _boom(key, model, is_replay):
        raise RuntimeError("registration exploded")

    coordinator.new_device_callback = _boom

    with patch(DISPATCH), caplog.at_level(logging.ERROR, logger=LOG):
        coordinator._on_client_event(_event())

    assert f"rtl_433 failed to register an adopted device ({_KEY})" in caplog.text
    assert "registration exploded" in caplog.text
    # The frame still landed: a broken hook costs the device its registration,
    # not its data.
    assert coordinator.devices[_KEY].fields == {"temperature_C": 21.4}


async def test_a_successful_registration_is_logged_once(hass, make_coordinator, caplog):
    """Registration is offered once per process, and says which route it took."""
    coordinator = make_coordinator(adopted={_KEY})
    seen: list[tuple[str, str, bool]] = []
    coordinator.new_device_callback = lambda k, m, r: seen.append((k, m, r))

    with patch(DISPATCH), caplog.at_level(logging.DEBUG, logger=LOG):
        coordinator._on_client_event(_event())
        coordinator._on_client_event(_event())

    assert seen == [(_KEY, _MODEL, False)]
    assert (
        f"rtl_433 registered adopted device {_KEY} (model {_MODEL}, via_replay=False)"
        in caplog.text
    )
    assert caplog.text.count("registered adopted device") == 1
