"""Availability watchdog + timeout resolution for the rtl_433 coordinator.

rtl_433 devices do not announce going offline, so availability is inferred from
silence: a periodic watchdog flips a device unavailable once its last-seen age
exceeds the device's effective timeout. This module holds the timeout-resolution
ladder (per-device override → hub default → device-class default), the
event-driven vs periodic classification that supplies the class default, and the
watchdog tick itself.

:class:`_AvailabilityMixin` is mixed into ``Rtl433Coordinator`` (see ``base.py``).
It relies on the runtime state declared in that class's ``__init__``
(``last_seen``, ``available``, ``devices``, ``event_driven_keys``,
``availability_timeout``, ``effective_timeout_resolver``, ``_logged_timeouts``)
and on ``_dispatch`` from :class:`._events._EventProcessingMixin`.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from homeassistant.util import dt as dt_util

from ..const import (
    AVAILABILITY_TIMEOUT_NEVER,
    CONF_DEVICES,
    DEVICE_FIELDS,
    LOGGER,
    class_default_timeout,
)

# How often the availability watchdog evaluates last-seen vs effective timeout.
_WATCHDOG_INTERVAL = timedelta(seconds=30)


class _AvailabilityMixin:
    """Effective-timeout resolution and the silence-based availability watchdog."""

    def _known_field_keys(self, device_key: str) -> set[str]:
        """Restart-safe set of a device's measurement field keys.

        Unions the persisted adopted fields
        (``entry.data[CONF_DEVICES][key][fields]`` — survives a restart) with the
        latest live payload's fields (``self.devices[key].fields`` — the rtl_433
        payload with identity and skip-keys removed), so a device that has been
        silent since startup is still classified from what it reported before.
        Shared by the availability class-default and the event-driven check so the
        two can never diverge: reading only the live payload was the bug that left
        an event device silent since a restart on the periodic class default and
        let its battery (and other) sensors expire to unavailable.
        """
        keys: set[str] = set()
        device_cfg = self.entry.data.get(CONF_DEVICES, {}).get(device_key)
        if device_cfg:
            keys.update(device_cfg.get(DEVICE_FIELDS, []) or [])
        normalized = self.devices.get(device_key)
        if normalized is not None:
            keys.update(normalized.fields)
        return keys

    def _class_default_timeout(self, device_key: str) -> int:
        """Return the device-class default timeout for a device.

        Classifies event-driven vs periodic from the device's restart-safe field
        set (:meth:`_known_field_keys` — adopted fields unioned with the latest
        live payload) against ``self.event_driven_keys`` (derived from the entry's
        device library). An event-driven device (open/close/motion/button/
        doorbell — no periodic check-in) gets the never-expire default; everything
        else, including a device with no known fields at all, gets the periodic
        default. Reading the adopted fields — not only the live payload — is what
        keeps an event device that has been silent since a restart on never-expire
        rather than wrongly expiring its battery and other sensors at the periodic
        timeout.
        """
        payload = dict.fromkeys(self._known_field_keys(device_key))
        return class_default_timeout(payload, self.event_driven_keys)

    def is_event_driven_device(self, device_key: str) -> bool:
        """Whether the device's known fields mark it event-driven (no check-in).

        True when any of the device's field keys is in ``self.event_driven_keys``
        (open/close/motion/button/doorbell — transmits only on a state change, so
        availability never expires and conveys no freshness). Considers both the
        restart-safe adopted fields and the latest live payload (via
        :meth:`_known_field_keys`), so a device silent since startup is still
        classified from its adopted fields. Used to enable the per-device
        "Last seen" sensor by default for these devices (their only freshness
        signal once availability stops expiring) and to resolve the never-expire
        availability class default.
        """
        if not self.event_driven_keys:
            return False
        return not self.event_driven_keys.isdisjoint(self._known_field_keys(device_key))

    def _resolve_timeout(self, device_key: str) -> tuple[int, str]:
        """Resolve the effective timeout and the tier that produced it.

        Resolution order: per-device override → explicit hub default → device-class
        default (from the latest payload) → ``DEFAULT_AVAILABILITY_TIMEOUT``. The
        resolver returns a concrete int for the two explicit tiers (including
        ``0`` = never-expire) or ``None`` when neither is set, in which case the
        device-class default applies. The second element is a short, DEBUG-only
        label of which tier won (the resolver collapses override and hub default
        into one ``int`` so they cannot be told apart here).
        """
        if self.effective_timeout_resolver is not None:
            try:
                resolved = self.effective_timeout_resolver(device_key)
            except Exception:  # noqa: BLE001 - fall back to the class default
                LOGGER.exception(
                    "rtl_433 failed to determine the availability timeout for %s; "
                    "using the default",
                    device_key,
                )
            else:
                if resolved is not None:
                    return resolved, "override-or-hub"
                return self._class_default_timeout(device_key), "class-default"
        return self.availability_timeout, "hub-default"

    def _effective_timeout(self, device_key: str) -> int:
        """Resolve the effective timeout for a device (see :meth:`_resolve_timeout`)."""
        return self._resolve_timeout(device_key)[0]

    def _log_timeout_change(self, device_key: str, timeout: int, source: str) -> None:
        """Log a device's resolved availability timeout once, then on each change.

        Answers "why did / didn't this device expire": event-driven devices
        (doorbells, contacts) resolve to never-expire, which is otherwise opaque.
        """
        if self._logged_timeouts.get(device_key) == timeout:
            return
        self._logged_timeouts[device_key] = timeout
        shown = "never" if timeout == AVAILABILITY_TIMEOUT_NEVER else f"{timeout}s"
        LOGGER.debug(
            "rtl_433 %s availability timeout=%s (source=%s)",
            device_key,
            shown,
            source,
        )

    async def _async_watchdog(self, _now: datetime) -> None:
        """Mark devices unavailable when their last-seen exceeds the timeout."""
        now = dt_util.utcnow()
        for device_key, seen in list(self.last_seen.items()):
            timeout, source = self._resolve_timeout(device_key)
            self._log_timeout_change(device_key, timeout, source)
            if timeout == AVAILABILITY_TIMEOUT_NEVER:
                # Never-expire: a device seen at least once is never flipped to
                # unavailable due to silence. (The back-online path on a live
                # event still applies.)
                continue
            stale = (now - seen) > timedelta(seconds=timeout)
            currently = self.available.get(device_key, True)
            if stale and currently:
                self.available[device_key] = False
                LOGGER.debug(
                    "rtl_433 device %s went unavailable (no event for %ss)",
                    device_key,
                    timeout,
                )
                normalized = self.devices.get(device_key)
                if normalized is not None:
                    # A watchdog re-paint of the cached event is not a replay (so
                    # measurement entities re-read availability), but it is also not
                    # a transmission: ``is_repaint`` tells ``Rtl433Event`` not to
                    # (re-)fire the stale cached value as a fresh event.
                    self._dispatch(
                        device_key, normalized, is_replay=False, is_repaint=True
                    )
