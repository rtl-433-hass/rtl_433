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
``zwave_js`` applies when its driver connection drops. :meth:`hub_available` is
that gate, and like every Home Assistant integration that gates on a live
connection flag it flips **immediately**: ``True`` exactly while the socket is
open. With it closed, *every* device behind the hub reads unavailable regardless
of its own timeout — including the never-expire event-driven devices, whose
exemption is about silence, not about the transport being gone.

There is deliberately no grace window (see the note in ``const.py``). Riding out
a blip would mean presenting readings as current while the integration knows it
cannot hear the radio, which is exactly what the Silver-tier
``entity-unavailable`` rule asks integrations not to do. The debounced half of
the story is the *repair issue*: ``repairs._UNREACHABLE_GRACE`` waits before
raising the user-facing "server unreachable" notification, so the entities tell
the truth at once while the notification waits until the outage looks real.

No device entity is exempt, ``event`` entities included: they take the base gate
unmodified, exactly as zigbee2mqtt's event entities carry its bridge-state
availability topic. Nothing re-fires when the connection returns — the reconnect
replay is flagged ``is_replay`` and ``Rtl433Event`` drops it before firing (see
``event.py``).

The gate is evaluated lazily by the entities (like the silence gate), so it is
always correct. The coordinator only has to *repaint*: ``base.py`` calls
:meth:`_async_sync_hub_availability` on both connection edges and the watchdog
tick calls it as a backstop. That method dispatches ``signal_hub_availability``
exactly once per flip.

:class:`_AvailabilityMixin` is mixed into ``Rtl433Coordinator`` (see ``base.py``).
It relies on the runtime state declared in that class's ``__init__``
(``last_seen``, ``available``, ``devices``, ``event_driven_keys``,
``availability_timeout``, ``effective_timeout_resolver``, ``_logged_timeouts``,
``_disconnected_since``, ``_devices_offline``) and on ``_dispatch`` from
:class:`._events._EventProcessingMixin`.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.util import dt as dt_util

from ..const import (
    AVAILABILITY_TIMEOUT_NEVER,
    CONF_DEVICES,
    DEVICE_FIELDS,
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
        """Whether the integration can currently hear this hub at all.

        Exactly the socket state: no grace window, no debounce. While the
        WebSocket is down the integration receives nothing, so no device's cached
        reading means anything and every entity behind the hub reads unavailable.

        Evaluated lazily — entities read it in their own ``available`` — so it is
        correct between the repaint edges too.
        """
        return self.connected

    @callback
    def _async_note_disconnected(self) -> None:
        """Record the outage start on the disconnect edge and log the loss.

        The library logs the drop at DEBUG under its own logger, which is
        invisible to anyone debugging the *integration*; log it here at INFO so
        the loss of the connection — and the fact that it has taken every device
        with it — is visible in the Home Assistant log without enabling anything.
        Startup stamps the same clock before the first connect is even attempted,
        which is not a *loss* of anything, so that case logs at DEBUG.

        ``_disconnected_since`` only feeds diagnostics and the log line below;
        the gate itself reads the socket directly.
        """
        self._disconnected_since = dt_util.utcnow()
        if self._ever_connected:
            LOGGER.info(
                "rtl_433 lost the connection to %s; reconnecting, and marking "
                "all %d device(s) behind this hub unavailable until it is back",
                self.ws_url,
                self._gated_device_count(),
            )
        else:
            LOGGER.debug("rtl_433 waiting for the first connection to %s", self.ws_url)
        self._async_sync_hub_availability()

    @callback
    def _async_note_connected(self) -> None:
        """Clear the outage clock on the connect edge and log the recovery."""
        since = self._disconnected_since
        self._disconnected_since = None
        if since is not None and self._ever_connected:
            LOGGER.info(
                "rtl_433 reconnected to %s after %.0fs",
                self.ws_url,
                (dt_util.utcnow() - since).total_seconds(),
            )
        self._ever_connected = True
        self._async_sync_hub_availability()

    @callback
    def _async_sync_hub_availability(self) -> None:
        """Dispatch a repaint when the hub-connection gate flips.

        Idempotent: it compares the live gate against the last dispatched value
        and returns without a dispatch when nothing changed, so both connection
        edges and every watchdog tick can call it freely.
        """
        offline = not self.hub_available
        if offline == self._devices_offline:
            return
        self._devices_offline = offline
        if not offline:
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

        Re-checks the hub-connection gate first, as a backstop in case a
        connection edge was ever missed: whatever the individual timeouts say, an
        unreachable hub takes every device with it.
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
