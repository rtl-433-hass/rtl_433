"""Availability watchdog + timeout resolution for the rtl_433 coordinator.

Device availability has two independent gates, both owned here:

*Per-device silence.* rtl_433 devices do not announce going offline, so
availability is inferred from silence: a periodic watchdog flips a device
unavailable once its last-seen age exceeds the device's effective timeout. This
module holds the timeout-resolution ladder (per-device override → hub default →
device-class default), the event-driven vs periodic classification that supplies
the class default, and the watchdog tick itself.

*Hub connection.* The silence gate is only meaningful while the integration is
listening. Once the WebSocket to the rtl_433 server is down the integration
hears nothing at all, so no device's cached state can be trusted — the same
situation an MQTT availability topic covers with an LWT, and the same gate
``zwave_js`` applies when its driver connection drops (both flip instantly; the
grace window here is this integration's own, see :data:`HUB_OFFLINE_GRACE`).
:meth:`hub_available` is that gate: ``True`` while the socket is open (or has
only just dropped, inside :data:`HUB_OFFLINE_GRACE`), ``False`` once the outage
outlives the grace window, at which point *every* device behind the hub reads
unavailable regardless of its own timeout — including the never-expire
event-driven devices, whose exemption is about silence, not about the transport
being gone. The one exception is ``event`` entities, whose state *is* their
last-fired timestamp and which therefore stay available (see
``Rtl433Event.available``).

A reconnect only counts as a recovery once it has held for a full grace window.
A socket that flaps faster than that delivers nothing, so the outage clock keeps
running across the blips rather than restarting on each drop — otherwise a
server stuck in a crash-restart loop would hold the gate open forever.

The gate is evaluated lazily by the entities (like the silence gate), so it is
always correct between ticks. The coordinator only has to *repaint* on the edge:
``base.py`` arms a one-shot timer when the socket drops and calls
:meth:`_async_sync_hub_availability` on the connect edge, and the watchdog tick
calls it too as a backstop. That method dispatches ``signal_hub_availability``
exactly once per flip.

:class:`_AvailabilityMixin` is mixed into ``Rtl433Coordinator`` (see ``base.py``).
It relies on the runtime state declared in that class's ``__init__``
(``last_seen``, ``available``, ``devices``, ``event_driven_keys``,
``availability_timeout``, ``effective_timeout_resolver``, ``_logged_timeouts``,
``_disconnected_since``, ``_connected_since``, ``_devices_offline``,
``_hub_offline_unsub``) and on ``_dispatch`` from
:class:`._events._EventProcessingMixin`.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

from ..const import (
    AVAILABILITY_TIMEOUT_NEVER,
    CONF_DEVICES,
    DEVICE_FIELDS,
    HUB_OFFLINE_GRACE,
    LOGGER,
    class_default_timeout,
    signal_hub_availability,
)

# How often the availability watchdog evaluates last-seen vs effective timeout.
_WATCHDOG_INTERVAL = timedelta(seconds=30)


class _AvailabilityMixin:
    """Effective-timeout resolution, the hub-connection gate, and the watchdog."""

    # ------------------------------------------------------------------ #
    # Hub-connection availability gate                                   #
    # ------------------------------------------------------------------ #
    @property
    def hub_available(self) -> bool:
        """Whether the hub connection is healthy enough to trust device state.

        ``True`` while the socket is open. On a drop it stays ``True`` for
        :data:`HUB_OFFLINE_GRACE` so the client's reconnect backoff can ride out
        a blip without flapping every device, then goes ``False`` for as long as
        the outage lasts. Also ``True`` before the coordinator has started (no
        connection has been attempted yet, so there is nothing to report).

        Evaluated lazily — entities read it in their own ``available`` — so it is
        correct between the repaint edges too.
        """
        if self.connected:
            return True
        if self._disconnected_since is None:
            return True
        return (dt_util.utcnow() - self._disconnected_since) < HUB_OFFLINE_GRACE

    @callback
    def _async_note_disconnected(self) -> None:
        """Start (or continue) the outage clock on the disconnect edge.

        The clock restarts only when the link had actually *recovered* — held
        open for a full :data:`HUB_OFFLINE_GRACE`. A socket that flaps faster
        than that delivers nothing, so restarting the window on every drop would
        hold the gate open forever through a server stuck in a crash-restart
        loop; in that case the original clock keeps running and the gate closes
        on schedule.

        The library logs the drop at DEBUG under its own logger, which is
        invisible to anyone debugging the *integration*; log it here at INFO so
        the loss of the connection — and the fact that the devices are on the
        clock — is visible in the Home Assistant log without enabling anything.
        Startup arms the same clock before the first connect is even attempted,
        which is not a *loss* of anything, so that case logs at DEBUG.
        """
        now = dt_util.utcnow()
        connected_since = self._connected_since
        self._connected_since = None
        recovered = (
            connected_since is not None and (now - connected_since) >= HUB_OFFLINE_GRACE
        )
        if self._disconnected_since is None or recovered:
            self._disconnected_since = now
        if self._ever_connected:
            LOGGER.info(
                "rtl_433 lost the connection to %s; reconnecting, and marking "
                "every device behind this hub unavailable if it stays down "
                "for %ds",
                self.ws_url,
                int(HUB_OFFLINE_GRACE.total_seconds()),
            )
        else:
            LOGGER.debug("rtl_433 waiting for the first connection to %s", self.ws_url)
        self._async_arm_hub_offline_timer()

    @callback
    def _async_note_connected(self) -> None:
        """Note the connect edge and log the recovery.

        The outage clock is deliberately *not* cleared here — see
        :meth:`_async_note_disconnected`. It is retired by
        :meth:`_async_sync_hub_availability` once this connected span has lasted
        a full grace window, which is what distinguishes a recovery from a flap.
        """
        self._async_cancel_hub_offline_timer()
        since = self._disconnected_since
        self._connected_since = dt_util.utcnow()
        if since is not None and self._ever_connected:
            LOGGER.info(
                "rtl_433 reconnected to %s after %.0fs",
                self.ws_url,
                (self._connected_since - since).total_seconds(),
            )
        self._ever_connected = True
        self._async_sync_hub_availability()

    @callback
    def _async_hub_offline_timer(self, _now: datetime) -> None:
        """One-shot grace timer: repaint if still disconnected, else re-arm.

        The timer runs on the event loop's monotonic clock while the window
        itself is measured against the wall clock, so a clock step (an NTP
        correction on a box with no RTC) can leave the window unexpired when the
        timer fires. Re-arm for whatever is left rather than dropping the repaint
        and leaving the gate to the 30 s watchdog tick.
        """
        self._hub_offline_unsub = None
        self._async_sync_hub_availability()
        if not self.connected and self.hub_available:
            self._async_arm_hub_offline_timer()

    @callback
    def _async_arm_hub_offline_timer(self) -> None:
        """Arm the one-shot timer for what is left of the current grace window."""
        self._async_cancel_hub_offline_timer()
        if self._disconnected_since is None:
            return
        remaining = max(
            HUB_OFFLINE_GRACE - (dt_util.utcnow() - self._disconnected_since),
            timedelta(0),
        )
        self._hub_offline_unsub = async_call_later(
            self.hass, remaining, self._async_hub_offline_timer
        )

    @callback
    def _async_cancel_hub_offline_timer(self) -> None:
        """Cancel a pending grace-window timer (no-op when none is armed)."""
        if self._hub_offline_unsub is not None:
            self._hub_offline_unsub()
            self._hub_offline_unsub = None

    @callback
    def _async_sync_hub_availability(self) -> None:
        """Dispatch a repaint when the hub-connection gate flips, and log it.

        Idempotent: it compares the live gate against the last dispatched value
        and returns without a dispatch when nothing changed, so the connect edge,
        the grace timer, and every watchdog tick can all call it freely.

        Also retires a finished outage clock: once the link has held open for a
        full grace window the reconnect has proven itself a recovery rather than
        a flap, so the next drop starts a fresh window.
        """
        if (
            self.connected
            and self._disconnected_since is not None
            and self._connected_since is not None
            and (dt_util.utcnow() - self._connected_since) >= HUB_OFFLINE_GRACE
        ):
            self._disconnected_since = None
        offline = not self.hub_available
        if offline == self._devices_offline:
            return
        self._devices_offline = offline
        if offline:
            LOGGER.warning(
                "rtl_433 has had no connection to %s for %ds; marking all %d "
                "device(s) behind this hub unavailable until it reconnects",
                self.ws_url,
                int(HUB_OFFLINE_GRACE.total_seconds()),
                self._gated_device_count(),
            )
        else:
            LOGGER.info(
                "rtl_433 connection to %s restored; devices behind this hub are "
                "available again as they report in",
                self.ws_url,
            )
        async_dispatcher_send(self.hass, signal_hub_availability(self.entry.entry_id))

    def _gated_device_count(self) -> int:
        """How many devices the gate actually takes unavailable.

        ``self.devices`` only holds devices that have transmitted *this session*,
        which is empty in the case the gate exists for — Home Assistant restarted
        while the server was already down — so the entity count comes from the
        adopted devices persisted in the config entry, unioned with the live map
        for anything discovered since (the same restart-safe pairing
        :meth:`_known_field_keys` uses).
        """
        adopted = self.entry.data.get(CONF_DEVICES, {})
        return len(set(adopted) | set(self.devices))

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
        """Mark devices unavailable when their last-seen exceeds the timeout.

        Re-checks the hub-connection gate first, as a backstop to the one-shot
        grace timer: whatever the individual timeouts say, a hub that has been
        unreachable past the grace window takes every device with it.
        """
        self._async_sync_hub_availability()
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
