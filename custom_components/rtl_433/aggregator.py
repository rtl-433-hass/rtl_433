"""Location-level fan-in that unions every receiver's view of one RF device.

A location config entry holds one receiver subentry per rtl_433 server, and each
receiver's coordinator fans its decoded frames out on the per-receiver, per-device
signal ``signal_device_update(receiver_id, device_key)``. Two servers within radio
range of the same sensor therefore decode the *same* transmission twice, a few
hundred milliseconds and one host clock apart.

This module is the seam that turns those N streams back into one. The aggregator
subscribes to every receiver's per-device dispatch and re-emits a single
**location-scoped, receiver-agnostic** signal keyed by ``device_key``
(``signal_location_device_update``), which is what the merged device's field
entities actually listen to. Two things happen on the way through:

**The field set is partitioned.** ``rssi`` and ``snr`` (mapped device fields in
``pyrtl_433.library``) and the synthetic ``last_seen`` describe *the link between
one receiver and the sensor*, not the sensor's state. Unioning them would publish
whichever receiver happened to win the race below, which is meaningless for a
signal measurement and actively misleading as a coverage indicator -- "strong at
Attic, weak at Garage" is the whole point of having two receivers. So they are
split out here (:data:`LINK_FIELD_KEYS`), never re-emitted on the location signal
and never deduped; their entities stay subscribed to their own receiver's signal.
Everything else is a property of the sensor and unions.

**The unioned fields are deduped.** ``event_time`` comes from the rtl_433 frame's
own ``time`` field, stamped by *each receiver's host clock* at decode, so two
receivers can disagree by their hosts' clock skew and a strict
newest-``event_time``-wins comparison can be inverted by it. Instead the
aggregator keeps the last-applied ``(event_time, applied_at)`` per
``(device_key, field)`` and applies a debounce window:

* within :data:`_MERGE_DEBOUNCE` of the last applied frame -> the **same
  transmission**, heard twice; the value is ignored (first applied wins);
* clearly **older** -> a stale frame or a reconnect-backlog replay; rejected, so
  a replay can never regress a live reading;
* clearly **newer** -> a genuine new transmission; applied.

A frame the server stamped in a form this integration cannot parse has no
``event_time`` to measure any of that against, and is applied rather than
guessed at -- the same degraded mode the ``event_time_unusable`` repair already
raises for, where the library's own replay suppression is off for the same
reason. Two receivers decoding one transmission carry the *same* value, so the
cost is a redundant state write (and, for an ``event`` entity, a second fire),
where a wrong rejection would drop a real reading for good.

The policy lives here rather than in ``pyrtl_433`` deliberately: it is a
Home-Assistant-side merge over *several* clients, not a property of any single
client's stream, and the library has no idea the other receiver exists.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
import dataclasses
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import (
    async_dispatcher_connect,
    async_dispatcher_send,
)
from homeassistant.util import dt as dt_util

from .const import (
    CONF_DEVICES,
    signal_device_update,
    signal_location_device_update,
    signal_new_device,
)
from .receiver_settings import receiver_coordinators

if TYPE_CHECKING:
    from pyrtl_433.normalizer import NormalizedEvent

    from .coordinator import Rtl433Coordinator

# How far apart two frames for the same ``(device_key, field)`` have to be before
# they count as two transmissions rather than one heard twice.
#
# Sized against what it has to absorb, from below and above. From below: a 433 MHz
# sensor repeats each message several times within a second or so, and two
# receivers add their own decode and delivery latency plus whatever their hosts'
# clocks disagree by -- a few seconds covers all of it on roughly time-synced
# hosts. From above: no periodic sensor this integration maps re-transmits a
# *new* reading faster than a few tens of seconds, so a window of seconds cannot
# swallow a genuine update. Named (rather than inlined) so it can be tuned
# against a deployment whose hosts drift further apart than this.
_MERGE_DEBOUNCE: Final = timedelta(seconds=3)

# The synthetic per-device "Last seen" field key. A sentinel no rtl_433 frame can
# ever carry, so it never reaches the field-driven value path; it lives here
# beside the other two link fields because it is one of them -- "when did *this*
# receiver last hear the sensor" -- and the partition below has to name all three
# in one place. ``sensor.py`` builds its descriptor from this.
LAST_SEEN_FIELD: Final = "__last_seen__"

# The exclusion set: fields that describe the *link* between one receiver and the
# sensor rather than the sensor itself. Never unioned, never deduped, never
# re-emitted on the location signal -- they stay per receiver (see the module
# docstring).
LINK_FIELD_KEYS: Final[frozenset[str]] = frozenset({"rssi", "snr", LAST_SEEN_FIELD})


def is_link_field(field_key: str) -> bool:
    """Return whether ``field_key`` measures the receiver-to-sensor link.

    Callers ask this instead of testing membership themselves, so the exclusion
    set has exactly one definition (see :data:`LINK_FIELD_KEYS`).
    """
    return field_key in LINK_FIELD_KEYS


def partition_fields(
    fields: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split one frame's fields into ``(unioned sensor, per-receiver link)``.

    The single place the union's exclusion set is applied to incoming data.
    Returns two dicts rather than filtering in place so a caller that wants the
    link half (the per-receiver signal entities) gets it without re-deriving the
    partition from the other side and risking the two drifting apart.
    """
    unioned: dict[str, Any] = {}
    link: dict[str, Any] = {}
    for field_key, value in fields.items():
        (link if is_link_field(field_key) else unioned)[field_key] = value
    return unioned, link


@dataclasses.dataclass(slots=True)
class _AppliedFrame:
    """The last frame whose value was actually applied for one device field.

    ``event_time`` is the decoding receiver's own stamp (``None`` when the server
    emits a time this integration cannot parse) and ``applied_at`` is Home
    Assistant's own clock at the moment the value went out. The window is measured
    on ``event_time``; ``applied_at`` is the wall-clock record of when this field
    last changed, which is what a caller reasoning about freshness wants and what
    no other structure here holds.
    """

    event_time: datetime | None
    applied_at: datetime


class Rtl433LocationAggregator:
    """Fan every receiver's device stream into one location-scoped stream.

    One per location config entry, built and started by ``async_setup_entry``
    after the receivers' coordinators are running. It owns no device state of its
    own beyond the dedup bookkeeping: the merged device's entities still read
    values off the events it re-emits.

    Subscriptions are per ``(receiver, device_key)`` because that is the shape of
    the coordinator's dispatch. They are opened for every device in the location's
    devices map at start (which is exactly the adopted set) and for any device
    adopted later, via each receiver's ``signal_new_device``.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Bind the aggregator to one location entry (no subscriptions yet)."""
        self.hass = hass
        self.entry = entry
        # Last-applied frame per ``(device_key, field_key)`` -- the dedup's whole
        # memory. Bounded by the adopted device set times its mapped fields, and
        # cleared per device by :meth:`forget_device`.
        self._applied: dict[tuple[str, str], _AppliedFrame] = {}
        self._subscribed: set[tuple[str, str]] = set()
        self._unsubs: list[Callable[[], None]] = []
        # Coordinators this aggregator hooked a device-remover onto, so
        # :meth:`async_stop` can take it back off again. Deregistering matters:
        # the coordinator outlives one aggregator across a reload, and a stale
        # remover would keep clearing a dead aggregator's state.
        self._hooked: list[Rtl433Coordinator] = []

    # ------------------------------------------------------------------ #
    # Lifecycle                                                          #
    # ------------------------------------------------------------------ #
    @callback
    def async_start(self) -> None:
        """Subscribe to every running receiver's per-device dispatch.

        Also hooks each coordinator's ``device_removers`` so a device the user
        removes from its device page drops its dedup state here too, rather than
        leaving a stale anchor that would reject the first frame after the user
        adds it back.
        """
        for receiver_id, coordinator in receiver_coordinators(
            self.hass, self.entry
        ).items():
            self._unsubs.append(
                async_dispatcher_connect(
                    self.hass,
                    signal_new_device(receiver_id),
                    self._new_device_handler(receiver_id),
                )
            )
            coordinator.device_removers.append(self.forget_device)
            self._hooked.append(coordinator)
            for device_key in self._known_device_keys(coordinator):
                self._subscribe(receiver_id, device_key)

    @callback
    def async_stop(self) -> None:
        """Drop every subscription and the dedup state.

        Called from the location's unload path, so a reload starts from a clean
        slate: the coordinators (and their frame streams) are rebuilt anyway, and
        a surviving anchor would silently reject the first frame after startup.
        """
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        self._subscribed.clear()
        self._applied.clear()
        for coordinator in self._hooked:
            if self.forget_device in coordinator.device_removers:
                coordinator.device_removers.remove(self.forget_device)
        self._hooked.clear()

    @callback
    def forget_device(self, device_key: str) -> None:
        """Forget one device's dedup anchors (it was removed by the user)."""
        for key in [k for k in self._applied if k[0] == device_key]:
            del self._applied[key]

    # ------------------------------------------------------------------ #
    # Subscription bookkeeping                                           #
    # ------------------------------------------------------------------ #
    def _known_device_keys(self, coordinator: Rtl433Coordinator) -> Iterable[str]:
        """Return every device key this receiver could already be dispatching for.

        The location's stored devices map is the adopted set, and a coordinator
        only ever dispatches for an adopted device -- but its runtime view is
        unioned in anyway so a device adopted between setup and this call (or one
        seeded straight into a coordinator by a test) is not missed.
        """
        return set(self.entry.data.get(CONF_DEVICES, {})) | set(coordinator.devices)

    @callback
    def _new_device_handler(self, receiver_id: str) -> Callable[[str, str], None]:
        """Build the ``signal_new_device`` listener for one receiver."""

        @callback
        def _handle_new_device(device_key: str, _model: str) -> None:
            self._subscribe(receiver_id, device_key)

        return _handle_new_device

    @callback
    def _subscribe(self, receiver_id: str, device_key: str) -> None:
        """Subscribe once to one receiver's dispatch for one device."""
        pair = (receiver_id, device_key)
        if pair in self._subscribed:
            return
        self._subscribed.add(pair)
        self._unsubs.append(
            async_dispatcher_connect(
                self.hass,
                signal_device_update(receiver_id, device_key),
                self._device_handler(device_key),
            )
        )

    @callback
    def _device_handler(self, device_key: str) -> Callable[[NormalizedEvent], None]:
        """Build the per-device listener bound to one ``device_key``."""

        @callback
        def _handle(event: NormalizedEvent) -> None:
            self._handle_event(device_key, event)

        return _handle

    # ------------------------------------------------------------------ #
    # The union itself                                                   #
    # ------------------------------------------------------------------ #
    @callback
    def _handle_event(self, device_key: str, event: NormalizedEvent) -> None:
        """Partition, dedup, and re-emit one receiver's frame for the location.

        The event is re-emitted **unconditionally**, even when every field was
        rejected: a dispatch is also how an entity is told to re-read
        ``available`` (the watchdog's availability re-paint carries the device's
        cached frame and no new value at all), so swallowing it would freeze
        availability on the merged device.
        """
        unioned, _link = partition_fields(event.fields)
        now = dt_util.utcnow()
        accepted = {
            field_key: value
            for field_key, value in unioned.items()
            if self._accept(device_key, field_key, event.event_time, now)
        }
        if accepted != event.fields:
            event = dataclasses.replace(event, fields=accepted)
        async_dispatcher_send(
            self.hass,
            signal_location_device_update(self.entry.entry_id, device_key),
            event,
        )

    def _accept(
        self,
        device_key: str,
        field_key: str,
        event_time: datetime | None,
        now: datetime,
    ) -> bool:
        """Decide whether one field's value is a new reading, and record it.

        The rule, against the last frame actually applied for this
        ``(device_key, field_key)``:

        * nothing applied yet -> apply (this is the reading that seeds the
          entity, replay or not);
        * ``|event_time - previous.event_time| <= _MERGE_DEBOUNCE`` -> the same
          transmission reaching us a second time, from this receiver's repeat
          burst or from the other receiver; ignore it, first applied wins;
        * older than that -> a stale frame or reconnect-backlog replay; reject,
          so a replay cannot regress a live value;
        * newer than that -> a genuine new transmission; apply.

        With no parseable ``event_time`` on one side of the comparison the whole
        rule has nothing to measure, so the frame is **applied**: the aggregator
        does not guess. Both failure modes were considered and this is the mild
        one -- two receivers decoding one transmission carry the *same* value, so
        a redundant apply writes the state it already held, whereas a wrong
        rejection would drop a genuine reading for good. (It does cost an event
        entity a second fire. That is the same degraded mode the
        ``event_time_unusable`` repair already raises for, where the library's
        replay suppression is switched off for the same reason.)

        ``applied_at`` is recorded alongside regardless, because it is the only
        wall-clock record of when this field's value was last written and the
        merged-availability work reads it.
        """
        previous = self._applied.get((device_key, field_key))
        if previous is None or event_time is None or previous.event_time is None:
            self._applied[(device_key, field_key)] = _AppliedFrame(event_time, now)
            return True

        delta = event_time - previous.event_time
        if abs(delta) <= _MERGE_DEBOUNCE:
            # The same transmission, heard twice. First applied wins.
            return False
        if delta < timedelta(0):
            # Clearly older: a stale frame or a reconnect backlog replay.
            return False
        self._applied[(device_key, field_key)] = _AppliedFrame(event_time, now)
        return True
