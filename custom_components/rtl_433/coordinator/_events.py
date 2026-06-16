"""Event frame processing for the rtl_433 coordinator.

Turns one decoded JSON event into a normalized event, classifies it against the
reconnect replay (live vs already-seen replay vs stale gap vs pre-connection
backlog), updates per-device runtime state, registers newly-discovered devices,
and hands the result to ``base.py``'s ``_dispatch`` to fan out to the device's
entities.

The replay classification — historically the densest branch in the coordinator —
lives in the pure :func:`classify_replay` helper so it can be reasoned about and
unit-tested in isolation; :meth:`_EventProcessingMixin._process_event` just
applies its verdict.

:class:`_EventProcessingMixin` is mixed into ``Rtl433Coordinator`` (see
``base.py``) and relies on the runtime state declared in that class's
``__init__`` (``devices``, ``last_seen``, ``available``, ``seen_fields``,
``device_fields``, ``skip_keys``, ``known_field_keys``, ``_event_high_water``,
``_connection_time``, ``_discovered``, ``_logged_unmapped``,
``discovery_enabled``, ``new_device_callback``) plus ``_dispatch`` (base.py).
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta
from typing import Any

from homeassistant.util import dt as dt_util

from ..const import LOGGER
from ..normalizer import NormalizedEvent, normalize

# Age boundary that separates a genuinely-fresh "live" event from a stale "gap"
# event in the reconnect replay. On every (re)connect the server replays up to
# its last 100 events; a frame whose ``time`` is newer than the high-water mark
# but older than this threshold occurred while Home Assistant was disconnected
# (a gap event) and must NOT re-fire automations or refresh liveness. Sized
# generously enough to absorb modest rtl_433-vs-HA clock skew + transmission
# latency (so a real live event is never misjudged stale — "never drop a real
# one") while staying shorter than a typical HA-restart outage (so restart gap
# events are suppressed). Assumes the server and HA clocks are roughly NTP-synced.
REPLAY_STALE_THRESHOLD = timedelta(seconds=30)

# Skew grace for the post-connection device-registration gate. A previously
# unknown device auto-registers only once a frame timestamped at or after
# ``_connection_time - DISCOVERY_BACKLOG_GRACE`` is seen; older frames are the
# server's pre-connection backlog and seed runtime state without registering.
# The grace absorbs modest rtl_433-vs-HA clock skew + transmission latency and
# assumes the server and HA clocks are roughly NTP-synced (a frame with no
# parseable ``time`` is treated as live, preserving "never drop a real one").
DISCOVERY_BACKLOG_GRACE = timedelta(seconds=5)


@dataclasses.dataclass(frozen=True, slots=True)
class ReplayVerdict:
    """How one event frame was classified against the reconnect replay.

    ``is_replay`` is the headline outcome: a replay / stale gap / backlog frame
    seeds sensor values but must NOT re-fire ``event`` entities or refresh
    liveness. ``is_backlog`` is the independent "timestamped before this
    connection" signal that gates device auto-registration (a backlog frame can
    also be an already-seen replay). ``label`` is a short DEBUG-only description.
    ``new_high_water``, when not ``None``, is the value the caller should advance
    the event high-water mark to (``None`` leaves the mark unchanged).
    """

    is_replay: bool
    is_backlog: bool
    label: str
    new_high_water: datetime | None


def classify_replay(
    event_time: datetime | None,
    now: datetime,
    *,
    high_water: datetime | None,
    connection_time: datetime | None,
) -> ReplayVerdict:
    """Classify an event frame as live / replay / stale gap / backlog.

    Pure function of the frame's timestamp and three pieces of coordinator state,
    so the (otherwise dense) decision can be unit-tested directly. Two signals
    catch a non-live frame: the high-water mark catches an already-seen frame,
    and the event age catches an unseen-but-old gap event. A third signal, the
    connection-time gate, marks a recent-but-pre-connection frame as replayed
    backlog (the HA-restart re-delivery case).

    ``event_time is None`` (no usable timestamp) and ``connection_time is None``
    (disconnected, or a direct unit-test feed) both keep ``is_backlog`` False and
    a timestamped frame live, preserving "never drop a real one".
    """
    # A frame timestamped before this connection began is part of the server's
    # reconnect-replay backlog (it occurred while HA was disconnected), so it must
    # never count as a live transmission -- even when recent enough to pass the
    # staleness test. This gate is independent of the replay outcome below because
    # an already-seen replay frame can also be pre-connection backlog.
    is_backlog = (
        connection_time is not None
        and event_time is not None
        and event_time < connection_time - DISCOVERY_BACKLOG_GRACE
    )
    if event_time is None:
        # No usable timestamp -> treat as live ("never drop a real one").
        return ReplayVerdict(False, False, "LIVE (no-timestamp)", None)
    if high_water is not None and event_time <= high_water:
        # At or below the high-water mark -> an already-seen replay (catches the
        # re-sent buffer tail on a brief blip; never re-fires). Leave the mark.
        return ReplayVerdict(True, is_backlog, "REPLAY (event_time<=high_water)", None)
    if now - event_time > REPLAY_STALE_THRESHOLD:
        # Newer than the mark (HA never saw it) but old -> a stale gap event that
        # occurred while disconnected. Advance the mark so it is not reconsidered.
        return ReplayVerdict(True, is_backlog, "STALE-GAP (age>threshold)", event_time)
    if is_backlog:
        # Newer than the mark and recent, but timestamped before this connection
        # -> a replayed backlog frame, not a live transmission. Suppress it (so a
        # restart does not re-fire events) while advancing the mark.
        return ReplayVerdict(True, True, "BACKLOG (pre-connection)", event_time)
    # Newer than the mark and recent -> a genuine live transmission. Clamp the
    # high-water advance to ``now``: a frame stamped in the future (server clock
    # ahead of HA, or a one-off glitched timestamp) must not push the mark past
    # wall-clock time, or every subsequent correctly-stamped live frame would fall
    # at-or-below it and be wrongly suppressed as a replay -- stalling availability
    # and silencing event entities until wall-clock caught up. The frame still
    # fires as live; only the mark is bounded.
    return ReplayVerdict(
        False, False, "LIVE (event_time>high_water)", min(event_time, now)
    )


class _EventProcessingMixin:
    """Normalize, classify, and dispatch one decoded rtl_433 event frame."""

    @staticmethod
    def _parse_event_time(raw: Any) -> datetime | None:
        """Parse an rtl_433 ``time`` value to a comparable UTC instant, or ``None``.

        rtl_433 stamps ``time`` either as a local ``"YYYY-MM-DD HH:MM:SS"`` string
        (optionally with fractional seconds) or as ISO-8601 with an offset / ``Z``,
        depending on server config. This reduces both to a single UTC basis so the
        replay classification can compare them: local-naive values are interpreted
        in HA's configured time zone (the NTP-sync assumption documented on
        :data:`REPLAY_STALE_THRESHOLD`), offset-aware values are converted as-is.

        A missing, blank, or unparsable value yields ``None`` ("no usable
        timestamp" — the frame is then treated as live). Never raises into the
        frame loop.
        """
        if not isinstance(raw, str):
            return None
        text = raw.strip()
        if not text:
            return None
        try:
            parsed = dt_util.parse_datetime(text)
            if parsed is None:
                # ``parse_datetime`` rejects the space-separated local form
                # without an offset; parse it explicitly as a naive datetime.
                for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
                    try:
                        parsed = datetime.strptime(text, fmt)  # noqa: DTZ007 - local
                        break
                    except ValueError:
                        continue
            if parsed is None:
                return None
            # ``as_utc`` treats a naive datetime as DEFAULT_TIME_ZONE and leaves an
            # aware one alone, so both forms reduce to a single comparable basis.
            return dt_util.as_utc(parsed)
        except ValueError, TypeError, OverflowError:
            return None

    def _process_event(self, event: dict[str, Any]) -> None:
        """Normalize an event, classify it (live vs replay), and dispatch.

        Replays and stale gap events seed sensor values but must NOT re-fire
        ``event`` entities or refresh ``last_seen`` / ``available``. The
        classification is delegated to :func:`classify_replay`; this method
        applies its verdict to runtime state and the discovery gate.
        """
        normalized = normalize(event, self.skip_keys)
        key = normalized.device_key

        now = dt_util.utcnow()

        # Read the raw ``time`` independently of ``normalize`` (which drops it)
        # and classify the frame into live / already-seen replay / stale gap /
        # pre-connection backlog.
        event_time = self._parse_event_time(event.get("time"))
        verdict = classify_replay(
            event_time,
            now,
            high_water=self._event_high_water,
            connection_time=self._connection_time,
        )
        is_replay = verdict.is_replay
        is_backlog = verdict.is_backlog
        if verdict.new_high_water is not None:
            self._event_high_water = verdict.new_high_water

        # Carry the classification on the event object (the dispatch carrier), so
        # every ``_handle_dispatch`` sees a consistent flag with no signature
        # churn and the event platform can log a suppressed transmission's age.
        normalized = dataclasses.replace(
            normalized, is_replay=is_replay, event_time=event_time
        )

        # Compact ingestion/classification trace for every event frame reaching
        # this point -- emitted upstream of the registration / discovery gate so
        # unregistered, disabled, and discovery-off devices are still logged.
        LOGGER.debug(
            "rtl_433 RX %s fields=%s time=%s -> %s",
            key,
            normalized.fields,
            event_time.isoformat() if event_time is not None else "none",
            verdict.label,
        )

        self.devices[key] = normalized

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

    def _maybe_register_device(
        self,
        key: str,
        normalized: NormalizedEvent,
        *,
        is_replay: bool,
        is_backlog: bool,
    ) -> None:
        """Offer a not-yet-discovered device to ``new_device_callback`` once.

        Registration is gated to post-connection messages: a pre-connection
        backlog frame (``is_backlog``) belongs to the server's reconnect replay
        and must seed runtime state without creating a device. A frame with no
        parseable ``time`` is treated as post-connection ("never drop a real
        one"), as is any frame once disconnected (``_connection_time is None``) --
        both leave ``is_backlog`` False. The gate keys off ``self._discovered``
        (not "is new") so a device first seen in the backlog still registers on
        its first true live event.

        The callback still fires for a replay-discovered device so its entities
        exist and can seed; its availability stays governed by liveness (it reads
        unavailable until a live frame arrives). The ``is_replay`` flag lets the
        callback wire up the device without raising a "new device" notification
        for a reconnect re-broadcast (never a genuine first-time live discovery).
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
            LOGGER.exception(
                "rtl_433 failed to register a newly discovered device (%s)", key
            )
        else:
            LOGGER.debug(
                "rtl_433 discovered new device %s (model %s, via_replay=%s)",
                key,
                normalized.model,
                is_replay,
            )
