"""Event fan-out for the rtl_433 coordinator.

The transport half — parsing WebSocket frames, normalizing them, and classifying
each frame against the reconnect replay — now lives in ``pyrtl_433.Rtl433Client``
(see ``base.py``). The client hands the coordinator a fully-formed, already
replay-classified :class:`~pyrtl_433.normalizer.NormalizedEvent` (``is_replay``
and ``event_time`` pre-computed) through its ``on_event`` callback. This module
holds only the *Home Assistant side* of the old ``_process_event`` path: it
updates per-device runtime state, registers newly-discovered devices, and hands
the result to ``base.py``'s ``_dispatch`` to fan out to the device's entities.

No normalization or replay classification happens here — doing it a second time
would double-classify what the client already decided. The one verdict the
library does not carry on the event object is ``is_backlog`` (the
pre-connection-backlog flag that gates device registration), so it is re-derived
here from the event's ``event_time`` and the coordinator's connect-edge anchor
(``_connection_time``, set in ``base.py``'s ``_emit_hub_update``) using the same
:data:`DISCOVERY_BACKLOG_GRACE` boundary the library applied.

:class:`_EventProcessingMixin` is mixed into ``Rtl433Coordinator`` (see
``base.py``) and relies on the runtime state declared in that class's
``__init__`` (``devices``, ``last_seen``, ``available``, ``seen_fields``,
``device_fields``, ``known_field_keys``, ``_connection_time``, ``_discovered``,
``_logged_unmapped``, ``discovery_enabled``, ``new_device_callback``) plus
``_dispatch`` (base.py).
"""

from __future__ import annotations

from pyrtl_433.normalizer import NormalizedEvent
from pyrtl_433.replay import (
    DISCOVERY_BACKLOG_GRACE as DISCOVERY_BACKLOG_GRACE,  # re-export for base.py/tests
    REPLAY_STALE_THRESHOLD as REPLAY_STALE_THRESHOLD,  # re-export for base.py/tests
)

from homeassistant.util import dt as dt_util

from ..const import LOGGER


class _EventProcessingMixin:
    """Fan one already-normalized, replay-classified rtl_433 event out to HA."""

    def _on_client_event(self, normalized: NormalizedEvent) -> None:
        """Ingest one event from the client and dispatch it to the entities.

        The client delivers ``normalized`` fully classified: ``is_replay`` and
        ``event_time`` are already stamped, so this method never re-normalizes or
        re-classifies. It only applies the verdict to HA-side runtime state and
        the discovery gate.

        Replays and stale gap events (``is_replay=True``) still seed sensor values
        but must NOT refresh ``last_seen`` / ``available`` or raise a "new device"
        notification, so a genuinely-offline device is not resurrected by the
        reconnect replay.
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

        now = dt_util.utcnow()

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
