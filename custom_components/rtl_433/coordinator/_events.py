"""Event fan-out for the rtl_433 coordinator.

The transport half — parsing WebSocket frames, normalizing them, and classifying
each frame against the reconnect replay — now lives in ``pyrtl_433.Rtl433Client``
(see ``base.py``). The client hands the coordinator a fully-formed, already
replay-classified :class:`~pyrtl_433.normalizer.NormalizedEvent` (``is_replay``
and ``event_time`` pre-computed) through its ``on_event`` callback. This module
holds only the *Home Assistant side* of the old ``_process_event`` path.

Its first job is the routing decision that keeps observation and adoption apart.
A frame for a device the user has approved (``adopted``) follows the full path —
per-device runtime state, then ``base.py``'s ``_dispatch`` to fan out to the
device's entities. A frame for anything else is recorded as a *pending
candidate* by :meth:`_EventProcessingMixin._record_pending` and goes no further,
so nothing reaches the device registry without an explicit user action. Because
the pending path touches none of the runtime state, every consumer of that state
— the availability watchdog, diagnostics, the entity platforms — keeps seeing
exactly the set of devices that exists in Home Assistant.

No normalization or replay classification happens here — doing it a second time
would double-classify what the client already decided. The one verdict the
library does not carry on the event object is ``is_backlog`` (the
pre-connection-backlog flag that keeps a reconnect re-broadcast out of the
pending list), so it is re-derived here from the event's ``event_time`` and the
coordinator's connect-edge anchor (``_connection_time``, set in ``base.py``'s
``_emit_hub_update``) using the same :data:`DISCOVERY_BACKLOG_GRACE` boundary the
library applied.

:class:`_EventProcessingMixin` is mixed into ``Rtl433Coordinator`` (see
``base.py``) and relies on the runtime state declared in that class's
``__init__`` (``adopted``, ``ignored``, ``pending``, ``devices``, ``last_seen``,
``available``, ``seen_fields``, ``device_fields``, ``known_field_keys``,
``_connection_time``, ``_discovered``, ``_logged_unmapped``, ``_evict_floor``,
``discovery_enabled``, ``new_device_callback``) plus ``entry``, ``_dispatch``
and ``forget_device`` (base.py).

:class:`PendingDevice` lives here rather than in ``base.py`` because this is the
module that builds one; ``base.py`` imports it (and re-exports it through the
package) the same way it imports ``_SdrStore`` from ``_sdr.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from pyrtl_433.normalizer import NormalizedEvent
from pyrtl_433.replay import DISCOVERY_BACKLOG_GRACE

from homeassistant.util import dt as dt_util

from ..const import CONF_DEVICES, LOGGER

# Hard cap on how many device keys one hub keeps runtime state for. "One entry
# per device the receiver hears" is not self-limiting: 433 MHz is a shared band
# that produces spurious decodes with arbitrary ids, and several real protocols
# roll their id on a battery change, so an uncapped map grows for the life of the
# config entry.
#
# What it can bound is narrower than that, and worth being plain about: a key
# with entities behind it must keep its state or those entities lose what they
# read, so only keys that never materialized into a device are evictable. With
# discovery on, every key that arrives registers, so the cap holds nothing back
# and the map still grows with the device registry -- discovery off (which the
# docs already recommend in urban areas) is where this does its work. An install
# genuinely running more real devices than this simply exceeds the cap.
#
# Distinct from ``pyrtl_433.client._MAX_TRACKED_DEVICES``, the identically-sized
# cap the library puts on its own replay bookkeeping for the same reason. That
# one is a blind LRU over the library's map; this one is ours and protects real
# devices. Raising one does not raise the other.
_MAX_TRACKED_DEVICE_STATES = 512


@dataclass(slots=True)
class PendingDevice:
    """One device heard but not yet adopted into Home Assistant.

    Held in memory only: the pending list is rebuilt from live traffic after
    every restart or reload by design, so an unwanted device never outlives the
    session that heard it. ``event`` is the most recent frame, kept so adoption
    can seed the device's entities from real data instead of leaving them
    unavailable until the next transmission, and so the approval UI can show what
    the device actually reports before the user commits to it. ``count`` and the
    two timestamps are the discriminators the user judges by: a real sensor
    checks in repeatedly, a bad decode is heard once.
    """

    key: str
    model: str
    event: NormalizedEvent
    count: int
    first_seen: datetime
    last_seen: datetime


class _EventProcessingMixin:
    """Fan one already-normalized, replay-classified rtl_433 event out to HA."""

    def _on_client_event(self, normalized: NormalizedEvent) -> None:
        """Ingest one event from the client and route it by adoption state.

        The client delivers ``normalized`` fully classified: ``is_replay`` and
        ``event_time`` are already stamped, so this method never re-normalizes or
        re-classifies. It only applies the verdict to HA-side runtime state.

        A frame for a device the user has not adopted goes to
        :meth:`_record_pending` and stops there, so everything below that branch
        — runtime state, field tracking, liveness, dispatch — runs for adopted
        devices only.

        Replays and stale gap events (``is_replay=True``) still seed sensor values
        but must NOT refresh ``last_seen`` / ``available``, so a genuinely-offline
        device is not resurrected by the reconnect replay.
        """
        key = normalized.device_key
        is_replay = normalized.is_replay

        # Re-derive the pre-connection-backlog flag (not carried on the event
        # object) from the event's timestamp and this connection's anchor, using
        # the same boundary the library applied. ``_connection_time`` is the
        # HA-side connect anchor set on the connect edge in ``_emit_hub_update``;
        # it is ``None`` while disconnected, which (like a frame with no usable
        # ``event_time``) keeps ``is_backlog`` False -- "never drop a real one".
        conn = self._connection_time
        is_backlog = (
            conn is not None
            and normalized.event_time is not None
            and normalized.event_time < conn - DISCOVERY_BACKLOG_GRACE
        )

        if key not in self.adopted:
            self._record_pending(
                key, normalized, is_replay=is_replay, is_backlog=is_backlog
            )
            return

        now = dt_util.utcnow()

        # ``devices`` doubles as the recency order the cap evicts from, so a
        # key that transmits again moves to the fresh end rather than staying
        # where it first landed.
        self.devices[key] = normalized
        self.devices.move_to_end(key)
        self._evict_cold_devices()

        # Track observed field keys for diagnostics (surfaced as unmatched keys).
        # Done for every outcome so a replay-discovered device's sensors can seed.
        field_keys = set(normalized.fields)
        self.seen_fields |= field_keys
        self.device_fields.setdefault(key, set()).update(field_keys)
        self._trace_unmapped_fields(key, field_keys)

        # Only a live frame refreshes liveness; replays / stale gap events leave
        # ``last_seen`` / ``available`` alone so a genuinely-offline device is not
        # resurrected by the reconnect replay.
        was_available = self.available.get(key)
        if not is_replay:
            self.last_seen[key] = now
            self.available[key] = True

        self._maybe_register_device(
            key, normalized, is_replay=is_replay, is_backlog=is_backlog
        )

        self._dispatch(key, normalized)

        if not is_replay and was_available is False:
            LOGGER.debug("rtl_433 device %s back online", key)

    def _evict_cold_devices(self) -> None:
        """Drop the coldest never-materialized keys until back under the cap.

        Evictable means: not adopted onto the config entry, and not registered
        with Home Assistant this session. Those two cover every key an entity can
        be reading, which is the state that must never be dropped -- what is left
        is the spurious-decode population the cap exists for.

        ``devices`` is ordered oldest-heard first, so this walks from the coldest
        key towards the freshest and stops the moment the map is back under the
        ceiling. The frame just taken is excluded, so an ingest can never evict
        the state it has this moment written; and if everything else is protected
        the map is simply left over the cap, because real devices outrank it.

        A pass that finds nothing to drop records the size it gave up at, and the
        next frame at that size skips the walk. Without it, a hub whose devices
        are all real -- which is every hub running with discovery on, since each
        key registers as it arrives -- would rescan a map that only grows, on
        every frame, forever, to reach the same answer.

        The walk itself is read-only, with the victims dropped afterwards: the
        map cannot be mutated while it is being iterated, and taking a snapshot
        to iterate instead would copy every key on a path that usually drops one.
        """
        if len(self.devices) <= self._evict_floor:
            return
        adopted = self.entry.data.get(CONF_DEVICES, {})
        newest = next(reversed(self.devices))
        remaining = len(self.devices)
        victims: list[str] = []
        for key in self.devices:
            if remaining <= _MAX_TRACKED_DEVICE_STATES:
                break
            # The frame just taken is never its own candidate.
            if key == newest or key in self._discovered or key in adopted:
                continue
            victims.append(key)
            remaining -= 1

        if not victims:
            self._evict_floor = len(self.devices)
            return
        for key in victims:
            LOGGER.debug(
                "rtl_433 evicting cold device state for %s (over the %d key cap)",
                key,
                _MAX_TRACKED_DEVICE_STATES,
            )
            # ``forget_device`` lowers the floor back to the cap for us.
            self.forget_device(key)

    def _trace_unmapped_fields(self, key: str, field_keys: set[str]) -> None:
        """DEBUG-log a device's fields that resolve to no library descriptor.

        Logged once per (device, key): a bad decode often surfaces as an
        unexpected field that maps to no entity. Skipped entirely when
        ``known_field_keys`` is empty (library not wired / failed to load) so it
        cannot flag every field.
        """
        if not self.known_field_keys:
            return
        already = self._logged_unmapped.setdefault(key, set())
        fresh = field_keys - self.known_field_keys - already
        if fresh:
            already |= fresh
            LOGGER.debug(
                "rtl_433 %s reported unmapped field(s) %s (no entity)",
                key,
                sorted(fresh),
            )

    def _record_pending(
        self,
        key: str,
        normalized: NormalizedEvent,
        *,
        is_replay: bool,
        is_backlog: bool,
    ) -> None:
        """Record a device the user has not adopted as a pending candidate.

        Deliberately touches none of the adopted-device runtime state
        (``devices``, ``last_seen``, ``available``, ``seen_fields``,
        ``device_fields``) and never dispatches: a pending device has no Home
        Assistant device and no entities, so letting it into that state would
        have the availability watchdog reporting on devices that do not exist and
        diagnostics presenting them as real.

        Replays and pre-connection backlog frames are re-broadcasts of already
        transmitted events, never a device's first live transmission, so they
        must not create a candidate -- otherwise every reconnect would repopulate
        the list with stale entries. A key on the hub's ignore list is dropped
        outright; that is what makes ignoring a neighbour's sensor stick.
        """
        if is_replay or is_backlog:
            return
        if key in self.ignored:
            LOGGER.debug("rtl_433 ignoring device %s (on the hub's ignore list)", key)
            return

        now = dt_util.utcnow()
        existing = self.pending.get(key)
        if existing is None:
            self.pending[key] = PendingDevice(
                key=key,
                model=normalized.model,
                event=normalized,
                count=1,
                first_seen=now,
                last_seen=now,
            )
            LOGGER.info(
                "rtl_433 heard a new device %s (model %s); add it from the hub's "
                "options to create it in Home Assistant",
                key,
                normalized.model,
            )
            return

        # A repeat sighting sharpens the existing candidate instead of creating a
        # second one. The newest frame is what adoption seeds the entities from,
        # and the count plus last-seen are how the user tells a sensor that keeps
        # checking in apart from a one-off bad decode.
        existing.event = normalized
        existing.model = normalized.model or existing.model
        existing.count += 1
        existing.last_seen = now

    def _maybe_register_device(
        self,
        key: str,
        normalized: NormalizedEvent,
        *,
        is_replay: bool,
        is_backlog: bool,
    ) -> None:
        """Offer an adopted device to ``new_device_callback`` once per process.

        Only reached for adopted devices, so this no longer decides *whether* a
        device may exist in Home Assistant -- the ``adopted`` check in
        :meth:`_on_client_event` does. What it still does is wire a device the
        user approved in an earlier session back up on its first frame of this
        one: ``_discovered`` starts empty each process, while ``adopted`` is
        seeded from the persisted devices map.
        :meth:`~.base.Rtl433Coordinator.adopt_device` fires the same callback for
        a device adopted mid-session, so both routes build the device identically.

        Registration is still held back for a pre-connection backlog frame
        (``is_backlog``), which belongs to the server's reconnect replay and only
        seeds runtime state. A frame with no parseable ``time`` is treated as
        post-connection ("never drop a real one"), as is any frame once
        disconnected (``_connection_time is None``) -- both leave ``is_backlog``
        False -- so a device first seen in the backlog still registers on its
        first true live event.

        The callback fires for a replay frame too, so a device whose first frame
        after a reconnect is a re-broadcast still gets its entities and can seed;
        its availability stays governed by liveness (it reads unavailable until a
        live frame arrives). ``is_replay`` is passed through so the callback knows
        which of the two it is.
        """
        if (
            key in self._discovered
            or is_backlog
            or not self.discovery_enabled
            or self.new_device_callback is None
        ):
            return
        self._discovered.add(key)
        try:
            self.new_device_callback(key, normalized.model, is_replay)
        except Exception:  # noqa: BLE001 - a bad hook must not kill the loop
            LOGGER.exception("rtl_433 failed to register an adopted device (%s)", key)
        else:
            LOGGER.debug(
                "rtl_433 registered adopted device %s (model %s, via_replay=%s)",
                key,
                normalized.model,
                is_replay,
            )
