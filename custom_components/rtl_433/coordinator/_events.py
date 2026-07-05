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
from typing import Any

from pyrtl_433.normalizer import NormalizedEvent, normalize
from pyrtl_433.replay import (
    DISCOVERY_BACKLOG_GRACE as DISCOVERY_BACKLOG_GRACE,  # re-export for base.py/tests
    REPLAY_STALE_THRESHOLD as REPLAY_STALE_THRESHOLD,  # re-export for base.py/tests
    classify_replay,
    parse_event_time,
)

from homeassistant.util import dt as dt_util

from ..const import LOGGER


class _EventProcessingMixin:
    """Normalize, classify, and dispatch one decoded rtl_433 event frame."""

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
        event_time = parse_event_time(event.get("time"))
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
