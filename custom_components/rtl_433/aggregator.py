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
  transmission**, received twice; the value is ignored (first applied wins);
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

**Availability is merged across both gates.** Each receiver has a transport gate
(is its WebSocket up?) and each device has a silence gate (did *that* receiver
hear it within its effective timeout?). OR-ing the two independently -- "any
receiver connected" AND "the newest last_seen is fresh" -- is a correctness bug:
a connected receiver that is deaf to the sensor would keep it alive on an
*offline* receiver's minute-old timestamp. So the pair is evaluated per receiver
and only the *result* is OR-ed: a receiver **vouches** for a device when it is
connected AND received it inside the timeout (:func:`receiver_vouches`), and the
merged device is available when at least one receiver vouches
(:meth:`Rtl433LocationAggregator.device_available`). The per-receiver watchdogs
keep running unchanged; this only unions what they conclude, and the
device-class-aware timeout resolution and never-expire exemption are the
coordinator's own, reused verbatim.

**Per-receiver coverage is kept.** The link half of each partitioned frame is
recorded per ``(device_key, receiver)`` (:class:`ReceiverCoverage`), which is
what lets the panel render "received by Attic (-62 dB) / Garage (-89 dB)" without
anyone enabling the disabled-by-default ``rssi`` / ``snr`` entities.

**Candidates are merged too, by a deliberately simpler rule.** A device the
user has not adopted never reaches the union above -- it is recorded as a
*candidate* in the receiver's own pending map instead (``coordinator/_events.py``),
behind that receiver's replay / backlog gate. :func:`merged_candidates` folds
every receiver's map into one location-wide list keyed by ``device_key``, so a
sensor two receivers receive is **one** row the user approves once, showing
**last-received-wins** data (the most recently arrived frame from any receiver,
with no debounce: a discovery preview only needs the freshest sample, where a
recorded entity value needs the anti-regression guard above) and naming the
receivers that have received it. The candidate cap is applied to that **merged**
list (:func:`enforce_pending_cap`), so a location cannot hold N times as many
candidates by having N receivers.

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
    AVAILABILITY_TIMEOUT_NEVER,
    CONF_DEVICES,
    DATA_AGGREGATOR,
    DOMAIN,
    LOGGER,
    signal_device_update,
    signal_location_device_update,
    signal_new_device,
    signal_pending_update,
)
from .coordinator import MAX_PENDING_CANDIDATES, PendingDevice
from .receiver_settings import receiver_coordinators

if TYPE_CHECKING:
    from pyrtl_433.normalizer import NormalizedEvent

    from .coordinator import Rtl433Coordinator

# How far apart two frames for the same ``(device_key, field)`` have to be before
# they count as two transmissions rather than one received twice.
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


def receiver_vouches(coordinator: Rtl433Coordinator, device_key: str) -> bool:
    """Return whether one receiver can currently vouch for one device.

    A receiver vouches when **both** of its gates hold *for the same receiver*:

    * **transport** -- ``receiver_available`` is the socket state, ``False`` the
      instant the WebSocket drops with no grace window. A receiver that is not
      listening has nothing to vouch with, whatever it received before; and
    * **silence** -- it received this device within the device's effective timeout.
      A timeout of :data:`~.const.AVAILABILITY_TIMEOUT_NEVER` is the never-expire
      exemption: once received, always fresh (but never received is still not received).

    The timeout comes from the coordinator's own ``_effective_timeout``, so the
    device-class-aware resolution ladder (per-device override -> location default
    -> class default) and the never-expire semantics are identical to the
    watchdog's rather than a second copy that could drift.

    Exported because both halves of the availability story call it: the merged
    device OR-s it over the location's receivers
    (:meth:`Rtl433LocationAggregator.device_available`), and a per-receiver link
    entity (``RSSI Attic``) applies it to its own receiver alone -- "how well does
    *this* receiver hear it" is meaningless while *this* receiver is deaf.
    """
    if not coordinator.receiver_available:
        return False
    last_seen = coordinator.last_seen.get(device_key)
    if last_seen is None:
        return False
    timeout = coordinator._effective_timeout(device_key)
    if timeout == AVAILABILITY_TIMEOUT_NEVER:
        return True
    return (dt_util.utcnow() - last_seen) <= timedelta(seconds=timeout)


def location_aggregator(
    hass: HomeAssistant, location_entry_id: str
) -> Rtl433LocationAggregator | None:
    """Return a location's running aggregator, or ``None``.

    The lookup an entity uses to reach the union from its own coordinator.
    ``None`` means the location is mid-setup or being torn down, and every caller
    treats that as "fall back to the receiver I was built by" rather than
    failing: a single-receiver answer is the honest one when there is no union
    running to consult.
    """
    return hass.data.get(DOMAIN, {}).get(DATA_AGGREGATOR, {}).get(location_entry_id)


@dataclasses.dataclass(slots=True, frozen=True)
class ReceiverCoverage:
    """One receiver's view of one device: how well, and how recently, it receives it.

    The union deliberately throws this detail away for the *sensor's* values, so
    it is kept here instead. ``rssi`` / ``snr`` are the last values that receiver
    reported for the device (``None`` until it reports one -- not every decoder
    emits them) and ``last_seen`` is when that receiver last received a real frame,
    which is not the same as the coordinator's ``last_seen``: that one also
    carries the entity's startup baseline, and a coverage display must not report
    a device as "received just now" because Home Assistant restarted.

    ``connected`` and ``vouches`` are **not** recorded: :meth:`coverage` fills
    them in from the receiver's live transport gate and from
    :func:`receiver_vouches` each time it is asked, because a stored answer would
    be wrong the moment a socket dropped. They are here so a caller can tell
    "offline" from "online but deaf to this sensor" -- the distinction the merged
    availability rule turns on.
    """

    receiver_id: str
    connected: bool = False
    vouches: bool = False
    last_seen: datetime | None = None
    rssi: Any = None
    snr: Any = None


@dataclasses.dataclass(slots=True, frozen=True)
class MergedCandidate:
    """One row of the location's add-device page: a sensor, not a sighting.

    ``record`` is the merged :class:`~.coordinator.PendingDevice` every surface
    renders -- the **last-received-wins** view of the candidate, built from the
    most recently *arrived* frame from any receiver (see :func:`_merge_records`
    for why that rule is deliberately simpler than the union's dedup) -- and
    ``coverage`` names the receivers that have received it, in receiver order, so
    the page can show "received by Attic and Garage" before the user commits to
    adding anything.

    ``coverage`` reuses :class:`ReceiverCoverage` rather than inventing a second
    shape for the same answer, but it is built from the candidate's own records:
    the aggregator's coverage map is fed by the per-device dispatch, and a
    *pending* device dispatches nothing at all, so there is nothing in it to
    read. Only receivers that have actually received the candidate appear (unlike
    :meth:`Rtl433LocationAggregator.coverage`, which lists every receiver
    including the deaf ones): before adoption, "has received it" is the whole
    question. ``vouches`` is always ``False`` -- vouching is about an adopted
    device's availability, and a candidate has none.
    """

    record: PendingDevice
    coverage: tuple[ReceiverCoverage, ...]

    @property
    def key(self) -> str:
        """The candidate's ``device_key`` -- the identity the merge is keyed by."""
        return self.record.key

    @property
    def receivers(self) -> tuple[str, ...]:
        """The ids of the receivers that have received this candidate."""
        return tuple(entry.receiver_id for entry in self.coverage)


def _merge_records(sightings: list[tuple[str, PendingDevice]]) -> PendingDevice:
    """Fold one device key's per-receiver candidate records into one row.

    **Last-received-wins, with no debounce.** The records are ordered by when
    Home Assistant *recorded* them (``last_seen``, this process's own clock --
    not the frame's ``event_time``, which is the decoding host's and so carries
    that host's clock skew), and the newest supplies the row's event, its model
    and its ``last_seen``. That is intentionally a simpler rule than the union's
    skew-tolerant dedup (:meth:`Rtl433LocationAggregator._accept`): an adopted
    device's value is *recorded*, so a stale frame regressing it is data loss
    worth guarding against, whereas a candidate row is a preview that is
    re-rendered seconds later and only ever needs to show the freshest sample.

    The rest of the row is the *location's* answer rather than one receiver's:
    ``count`` sums the sightings (the location received it that many times, which
    is the number that separates a real sensor from a one-off bad decode),
    ``first_seen`` is the earliest (when the location first received it, and what
    the candidate order is built on), and ``fields`` accumulates oldest to
    newest so a weather station that splits its readings across transmissions --
    and across receivers -- shows the whole device, with the newest value
    winning any field two receivers both reported.
    """
    ordered = sorted(sightings, key=lambda sighting: sighting[1].last_seen)
    newest = ordered[-1][1]
    fields: dict[str, Any] = {}
    for _receiver_id, record in ordered:
        fields.update(record.fields)
    return PendingDevice(
        key=newest.key,
        # A frame can decode with an empty model; fall back to the newest
        # sighting that named one rather than rendering an unnamed row when
        # another receiver knows what it is.
        model=newest.model
        or next(
            (record.model for _id, record in reversed(ordered) if record.model), ""
        ),
        event=newest.event,
        count=sum(record.count for _id, record in ordered),
        first_seen=min(record.first_seen for _id, record in ordered),
        last_seen=newest.last_seen,
        fields=fields,
    )


def _candidate_coverage(
    coordinators: dict[str, Rtl433Coordinator],
    sightings: list[tuple[str, PendingDevice]],
) -> tuple[ReceiverCoverage, ...]:
    """Build one candidate's per-receiver coverage, in receiver order."""
    return tuple(
        ReceiverCoverage(
            receiver_id=receiver_id,
            connected=coordinators[receiver_id].receiver_available,
            last_seen=record.last_seen,
            rssi=record.fields.get("rssi"),
            snr=record.fields.get("snr"),
        )
        for receiver_id, record in sightings
    )


def _pending_sightings(
    coordinators: dict[str, Rtl433Coordinator],
) -> dict[str, list[tuple[str, PendingDevice]]]:
    """Group every receiver's candidates by ``device_key``, in receiver order."""
    sightings: dict[str, list[tuple[str, PendingDevice]]] = {}
    for receiver_id, coordinator in coordinators.items():
        for device_key, record in coordinator.pending.items():
            sightings.setdefault(device_key, []).append((receiver_id, record))
    return sightings


def merged_candidates(hass: HomeAssistant, entry: ConfigEntry) -> list[MergedCandidate]:
    """Return the location's candidate list: one row per sensor, not per receiver.

    The union add-device page. Two receivers in range of the same sensor each
    decode it and each record a candidate; merging them here is what lets the
    user approve a *sensor* once instead of once per server, and what keeps the
    page from offering the same device twice.

    Ordered exactly as one receiver's list was -- most recently *discovered*
    first, ties broken by key -- so a long list is worked from the top in the
    same order on the options form and in the panel, and a card does not move
    under the cursor every time the device transmits.

    Derived on demand from the receivers' pending maps rather than maintained as
    a fourth copy of the same state: those maps are the ingestion buffers the
    replay / backlog gate writes into (per receiver, because that gate is a
    statement about one receiver's connection), and a cached merge would be one
    more thing to invalidate on every frame.
    """
    coordinators = receiver_coordinators(hass, entry)
    merged = [
        MergedCandidate(
            record=_merge_records(sightings),
            coverage=_candidate_coverage(coordinators, sightings),
        )
        for sightings in _pending_sightings(coordinators).values()
    ]
    merged.sort(
        key=lambda candidate: (candidate.record.first_seen, candidate.key), reverse=True
    )
    return merged


def merged_candidate(
    hass: HomeAssistant, entry: ConfigEntry, device_key: str
) -> MergedCandidate | None:
    """Return one location-wide candidate, or ``None`` if nothing is offering it.

    The single-key form :mod:`~custom_components.rtl_433.adoption` adopts from,
    so the device is built from the same merged record the user was looking at
    when they clicked -- including the fields only the *other* receiver received.
    """
    coordinators = receiver_coordinators(hass, entry)
    sightings = _pending_sightings(coordinators).get(device_key)
    if not sightings:
        return None
    return MergedCandidate(
        record=_merge_records(sightings),
        coverage=_candidate_coverage(coordinators, sightings),
    )


def location_adopted(hass: HomeAssistant, entry: ConfigEntry) -> set[str]:
    """Return every device key the location has adopted.

    The stored devices map is the restart-safe record and each running
    coordinator's mirror is the live one; a key adopted moments ago is in the
    mirrors before the entry write lands, and a key adopted in an earlier
    session is in the map before any receiver has received it this process. The
    union is the only answer that is true in both windows.
    """
    adopted = set(entry.data.get(CONF_DEVICES, {}))
    for coordinator in receiver_coordinators(hass, entry).values():
        adopted |= coordinator.adopted
    return adopted


def enforce_pending_cap(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Drop the coldest candidates until the **merged** list is back under the cap.

    The cap has to be applied to the merged list, not per receiver: a location
    with three receivers would otherwise hold three times the candidates of one
    with a single receiver, and every one of them is rendered into the payload
    pushed to every open panel. So the bound is on what the user is actually
    offered -- and a dropped key is dropped from *every* receiver's map, since
    leaving it in one of them would put the row straight back on the next merge.

    "Coldest" is the merged row's ``last_seen``: the most recent moment *any*
    receiver received the device, so a sensor a second receiver still receives is not
    evicted because the first one lost it. Ordering by that, with the key
    breaking ties, is what makes the eviction deterministic rather than
    dict-order dependent.

    The sum of the per-receiver sizes is an upper bound on the size of their
    union, so a cheap sum rules the whole merge out in the overwhelmingly common
    case of a list nowhere near the cap.
    """
    coordinators = receiver_coordinators(hass, entry)
    if (
        sum(len(coordinator.pending) for coordinator in coordinators.values())
        <= MAX_PENDING_CANDIDATES
    ):
        return

    merged = {
        device_key: _merge_records(sightings)
        for device_key, sightings in _pending_sightings(coordinators).items()
    }
    over = len(merged) - MAX_PENDING_CANDIDATES
    if over <= 0:
        return
    coldest = sorted(merged, key=lambda key: (merged[key].last_seen, key))
    for device_key in coldest[:over]:
        for coordinator in coordinators.values():
            coordinator.pending.pop(device_key, None)
        LOGGER.debug(
            "rtl_433 dropping the coldest candidate %s (over the %d key cap)",
            device_key,
            MAX_PENDING_CANDIDATES,
        )


def clear_pending(hass: HomeAssistant, entry: ConfigEntry) -> int:
    """Forget every candidate on every receiver, and return how many rows went.

    Counted in merged rows rather than per-receiver records, because that is
    what the user was looking at: clearing a list of forty candidates that two
    receivers both hear cleared forty, not eighty.
    """
    coordinators = receiver_coordinators(hass, entry)
    cleared = len(
        {key for coordinator in coordinators.values() for key in coordinator.pending}
    )
    for coordinator in coordinators.values():
        coordinator.pending.clear()
    async_emit_pending_update(hass, entry)
    return cleared


@callback
def async_emit_pending_update(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Announce that the location's merged candidate list changed membership.

    The location-level twin of
    :meth:`~.coordinator.Rtl433Coordinator.emit_pending_update`, for the callers
    that act on the location rather than through one receiver: the adoption
    verbs, which have just written a batch across every receiver and announce it
    once. Both send the same location-scoped signal, so a subscriber has exactly
    one thing to listen to however the change was made.
    """
    async_dispatcher_send(hass, signal_pending_update(entry.entry_id))


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
        # The coverage map: the link half of every frame, kept per
        # ``(device_key, receiver_id)`` instead of being merged away. Real frames
        # only -- no startup baseline -- because this is what a coverage display
        # reads (see :class:`ReceiverCoverage`).
        self._coverage: dict[tuple[str, str], ReceiverCoverage] = {}
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
            coordinator.pending_listeners.append(self._enforce_pending_cap)
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
        self._coverage.clear()
        for coordinator in self._hooked:
            if self.forget_device in coordinator.device_removers:
                coordinator.device_removers.remove(self.forget_device)
            if self._enforce_pending_cap in coordinator.pending_listeners:
                coordinator.pending_listeners.remove(self._enforce_pending_cap)
        self._hooked.clear()

    @callback
    def _enforce_pending_cap(self) -> None:
        """Hold the location's merged candidate list under the cap.

        Registered on every receiver's ``pending_listeners``, so it runs on the
        one thing that can grow the list -- a receiver recording a candidate it
        has not received before -- and before that receiver announces the change.
        The work itself is :func:`enforce_pending_cap`, which is a function over
        the location rather than a method here because nothing about it needs the
        aggregator's own state; this is only the wiring that gives it a trigger.
        """
        enforce_pending_cap(self.hass, self.entry)

    @callback
    def forget_device(self, device_key: str) -> None:
        """Forget one device's dedup anchors and coverage (the user removed it)."""
        for key in [k for k in self._applied if k[0] == device_key]:
            del self._applied[key]
        for key in [k for k in self._coverage if k[0] == device_key]:
            del self._coverage[key]

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
                self._device_handler(receiver_id, device_key),
            )
        )

    @callback
    def _device_handler(
        self, receiver_id: str, device_key: str
    ) -> Callable[[NormalizedEvent], None]:
        """Build the per-device listener bound to one ``(receiver, device_key)``.

        The receiver is bound in as well as the device because the location
        signal is receiver-agnostic by design: once the frame is re-emitted there
        is nothing left on it to say who received it, and the coverage map needs
        exactly that.
        """

        @callback
        def _handle(event: NormalizedEvent) -> None:
            self._handle_event(receiver_id, device_key, event)

        return _handle

    # ------------------------------------------------------------------ #
    # Merged availability + per-receiver coverage                        #
    # ------------------------------------------------------------------ #
    def device_available(self, device_key: str) -> bool:
        """Return whether *any* receiver can currently vouch for this device.

        The merged gate. Each receiver's transport and silence gates are
        evaluated together (:func:`receiver_vouches`) and only the results are
        OR-ed, which is what keeps a device from reading available on an offline
        receiver's stale timestamp while the connected one receives nothing.

        ``False`` for a location with no running coordinator (mid-setup or
        mid-teardown): nothing is listening, so nothing can vouch.
        """
        return any(
            receiver_vouches(coordinator, device_key)
            for coordinator in receiver_coordinators(self.hass, self.entry).values()
        )

    def coverage(self, device_key: str) -> list[ReceiverCoverage]:
        """Return one device's per-receiver coverage, in receiver order.

        The A-vs-B comparison the union hides, served straight from aggregator
        state so the panel can render "received by Attic (-62 dB) / Garage (-89 dB)"
        with **no** entity enabled -- ``rssi`` / ``snr`` ship
        disabled-by-default and a location would otherwise pay
        *sensors x receivers x 2* entities for a detail most users only glance at.

        Every running receiver appears, including one that has never received this
        device at all (``last_seen``/``rssi``/``snr`` all ``None``): "Garage does
        not hear it" is exactly as much a coverage answer as a weak signal is.
        """
        result: list[ReceiverCoverage] = []
        for receiver_id, coordinator in receiver_coordinators(
            self.hass, self.entry
        ).items():
            recorded = self._coverage.get((device_key, receiver_id))
            result.append(
                dataclasses.replace(
                    recorded or ReceiverCoverage(receiver_id=receiver_id),
                    connected=coordinator.receiver_available,
                    vouches=receiver_vouches(coordinator, device_key),
                )
            )
        return result

    @callback
    def _record_coverage(
        self,
        receiver_id: str,
        device_key: str,
        link: dict[str, Any],
        event: NormalizedEvent,
    ) -> None:
        """Fold one frame's link half into this receiver's coverage record.

        A re-paint carries the device's *cached* frame rather than a new
        transmission, so it must not move ``last_seen`` forward -- doing so would
        make a silent sensor look freshly received every watchdog tick, which is the
        opposite of what a coverage display is for. Its link values are equally
        stale, so the whole record is left alone.

        A live frame always stamps ``last_seen`` (that *is* the receiver hearing
        it) and overwrites whichever of ``rssi`` / ``snr`` it carries, keeping the
        previous value for one it does not: not every decoder emits both on every
        frame, and a missing key means "not reported", not "signal lost".
        """
        if event.is_repaint:
            return
        previous = self._coverage.get((device_key, receiver_id))
        self._coverage[(device_key, receiver_id)] = ReceiverCoverage(
            receiver_id=receiver_id,
            last_seen=dt_util.utcnow(),
            rssi=link.get("rssi", previous.rssi if previous else None),
            snr=link.get("snr", previous.snr if previous else None),
        )

    # ------------------------------------------------------------------ #
    # The union itself                                                   #
    # ------------------------------------------------------------------ #
    @callback
    def _handle_event(
        self, receiver_id: str, device_key: str, event: NormalizedEvent
    ) -> None:
        """Partition, dedup, and re-emit one receiver's frame for the location.

        The link half is recorded against this receiver on the way past (see
        :meth:`coverage`) rather than dropped: it is the only record of which
        receiver receives the sensor how well, and the union is about to make the
        frame receiver-agnostic.

        The event is re-emitted **unconditionally**, even when every field was
        rejected: a dispatch is also how an entity is told to re-read
        ``available`` (the watchdog's availability re-paint carries the device's
        cached frame and no new value at all), so swallowing it would freeze
        availability on the merged device.
        """
        unioned, link = partition_fields(event.fields)
        now = dt_util.utcnow()
        self._record_coverage(receiver_id, device_key, link, event)
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
            # The same transmission, received twice. First applied wins.
            return False
        if delta < timedelta(0):
            # Clearly older: a stale frame or a reconnect backlog replay.
            return False
        self._applied[(device_key, field_key)] = _AppliedFrame(event_time, now)
        return True
