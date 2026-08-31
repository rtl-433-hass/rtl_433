"""Push coordinator for one rtl_433 server (hub config entry).

This module is the Home Assistant adapter over :class:`pyrtl_433.Rtl433Client`.
The client owns the transport — the WebSocket connect/reconnect loop, JSON frame
parsing, event normalization + replay classification, and the HTTP ``/cmd``
getters/setters — while this coordinator owns the *framework policy* the library
deliberately leaves out: injecting Home Assistant's shared aiohttp session,
fanning normalized events out over HA's dispatcher keyed by device, adopting and
enforcing the managed SDR settings on every (re)connect, running the availability
watchdog, and refreshing the hub device-registry identity.

The coordinator is one class, ``Rtl433Coordinator``, assembled here from three
policy mixins so each concern lives in its own file and ``__init__`` stays the
single place every runtime attribute is declared:

- ``_sdr.py`` (:class:`._sdr._SdrSettingsMixin`) — the managed-SDR desired-state
  Store, write path, adoption, and reconnect enforcement.
- ``_events.py`` (:class:`._events._EventProcessingMixin`) — the HA side of event
  fan-out: per-device state, discovery registration, and dispatch. Normalization
  and replay classification are done by the client, not here.
- ``_watchdog.py`` (:class:`._watchdog._AvailabilityMixin`) — effective-timeout
  resolution and the silence-based availability watchdog.

The client's callbacks are wired into this coordinator: ``on_event`` ->
:meth:`._events._EventProcessingMixin._on_client_event` (HA-side dispatch), and
``on_hub_update`` -> :meth:`_emit_hub_update` (connect/disconnect edge handling,
hub-identity refresh, and the ``signal_hub_update`` dispatch). The library client
does not own the managed-SDR policy or the availability watchdog, so those are
driven here off the connect edge and a HA time-interval respectively.

Decoupling: the coordinator imports nothing from ``mapping.py``,
``config_flow.py``, or ``entity.py``. To stay file-disjoint and cycle-free it
accepts the pieces it needs as injectable attributes:

- ``skip_keys`` — the set of event keys to drop from measurement fields. Defaults
  to a minimal identity set; the integration setup injects the library skip-keys.
- ``new_device_callback`` — called with ``(device_key, model, is_replay)`` the
  first time an unknown device is seen while discovery is enabled; ``is_replay``
  flags a reconnect-replay/stale-gap frame so the callback can wire up entities
  without raising a "new device" notification for a mere re-broadcast. The
  integration setup wires this to the discovery flow; the coordinator never
  imports the config flow.
- ``effective_timeout_resolver`` — called with ``device_key`` to resolve the
  per-device availability timeout (override → hub default). The integration setup
  wires this; the fallback is the hub default.
- ``effective_clear_delay_resolver`` — called with ``device_key`` to resolve the
  per-device motion clear-delay (override → default). The integration setup wires
  this; the binary_sensor reads it. The fallback is the motion default.
- ``hub_info_callback`` — called (no args) when the SDR device identity
  (``dev_info``/``dev_query``, learned by the client on each connect) is first
  seen or changes. The integration setup wires this to refresh the hub
  device-registry entry's model/manufacturer/serial; the coordinator never
  touches the device registry itself.
"""

from __future__ import annotations

from collections.abc import Callable
import dataclasses
from datetime import datetime
from typing import Any

from pyrtl_433 import CannotConnect as CannotConnect, Rtl433Client
from pyrtl_433.normalizer import DEFAULT_SKIP_KEYS, NormalizedEvent

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from ..const import (
    DEFAULT_AVAILABILITY_TIMEOUT,
    DEFAULT_PATH,
    DEFAULT_PORT,
    LOGGER,
    SDR_STORE_VERSION,
    sdr_store_key,
    signal_device_update,
    signal_hub_update,
)
from ._events import _EventProcessingMixin
from ._sdr import _SdrSettingsMixin, _SdrStore
from ._watchdog import _WATCHDOG_INTERVAL, _AvailabilityMixin

# ``CannotConnect`` is the library's error, re-exported here (and via
# ``coordinator/__init__.py``) so existing import sites — ``config_flow.py``,
# ``repairs.py`` — keep importing it from the coordinator package unchanged.


class Rtl433Coordinator(_SdrSettingsMixin, _EventProcessingMixin, _AvailabilityMixin):
    """HA adapter that owns and drives a :class:`pyrtl_433.Rtl433Client` for one hub.

    All state is scoped to a single config entry, so multiple hubs coexist.
    Behavior is grouped into the mixins listed in the module docstring; every
    runtime attribute those mixins read is declared in :meth:`__init__` below.

    Public API:
        ``async_start()`` / ``async_stop()`` — lifecycle.
        ``validate_connection(...)`` — staticmethod used by the config flow.

    Injectable attributes (wired by the integration setup in ``__init__.py``):
        ``skip_keys``: ``set[str]`` of keys excluded from measurement fields.
        ``event_driven_keys``: ``frozenset[str]`` of rtl_433 field keys that mark
            a device as event-driven (never-expire availability default).
        ``discovery_enabled``: ``bool`` per-hub new-device discovery toggle.
        ``manage_settings``: ``bool`` per-hub toggle for adopting + enforcing the
            managed SDR settings. When ``True`` the coordinator adopts the
            server's current settings on first connect, persists the desired
            state to a ``Store``, and replays it on every reconnect; when
            ``False`` the desired-state ``Store`` is wiped on load and the
            receiver's settings are left untouched.
        ``new_device_callback``: ``Callable[[str, str, bool], None] | None``.
        ``effective_timeout_resolver``: ``Callable[[str], int | None] | None``.
            Returns the device's explicit timeout (per-device override → explicit
            hub default), or ``None`` when neither is set so the coordinator
            applies the device-class default from the device's latest payload.
        ``effective_clear_delay_resolver``: ``Callable[[str], int] | None``.

    Runtime state (read by ``diagnostics.py``):
        ``devices``: ``dict[str, NormalizedEvent]`` last event per device key.
        ``last_seen``: ``dict[str, datetime]`` last-seen (UTC) per device key.
        ``available``: ``dict[str, bool]`` current availability per device key.
        ``seen_fields``: ``set[str]`` every measurement field key ever observed.
        ``device_fields``: ``dict[str, set[str]]`` field keys seen per device.
        ``connected``: ``bool`` whether the client's socket is currently open
            (delegates to the client).
        ``meta``: ``dict[str, Any]`` latest SDR/meta configuration (client-sourced).
        ``stats``: ``dict[str, Any]`` latest server-stats payload (client-sourced).

    Managed-SDR desired state (restart-surviving, persisted to a ``Store`` keyed
    by ``sdr_store_key(entry_id)``):
        ``_desired``: ``dict[str, Any]`` desired value per managed registry key
            (gain is two keys: ``gain`` dB float + ``gain_auto`` bool).
        ``_managed``: ``set[str]`` registry keys Home Assistant is managing.
        The public read API for the control entities is ``get_desired(field)``,
        ``is_managed(field)``, plus ``set_sdr(field, value)`` to write and
        ``clear_desired_state()`` to drop management.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        *,
        host: str,
        port: int = DEFAULT_PORT,
        path: str = DEFAULT_PATH,
        secure: bool = False,
        discovery_enabled: bool = True,
        manage_settings: bool = True,
        availability_timeout: int = DEFAULT_AVAILABILITY_TIMEOUT,
        initial_center_frequency: float | None = None,
        skip_keys: set[str] | frozenset[str] | None = None,
        event_driven_keys: frozenset[str] | None = None,
    ) -> None:
        """Initialize the coordinator with connection params and runtime state."""
        self.hass = hass
        self.entry = entry

        self.host = host
        self.port = port
        self.path = path
        self.secure = secure

        # --- Per-hub configuration (may be updated by the options flow) -------
        self.discovery_enabled = discovery_enabled
        # Default ``True`` so every existing construction site (including tests
        # and pre-Task-3 wiring) keeps adopting + enforcing the SDR settings.
        self.manage_settings = manage_settings
        self.availability_timeout = availability_timeout
        # One-shot center frequency (MHz) chosen at add time. Seeded into the
        # managed desired state on the first-ever connect (over adoption) and
        # never re-applied once desired state is persisted; ``None`` means "adopt
        # the server's current frequency". Only consulted when managing settings.
        self.initial_center_frequency = initial_center_frequency
        self.skip_keys: set[str] = (
            set(skip_keys) if skip_keys is not None else set(DEFAULT_SKIP_KEYS)
        )
        # ``time`` is read raw for the replay classification and must never reach
        # entities as a measurement field. The client always excludes it too, but
        # keep it here so the injected skip-key set the client receives is explicit.
        self.skip_keys.add("time")
        # rtl_433 field keys whose presence marks a device as event-driven (no
        # periodic check-in -> never-expire availability). Derived from the
        # entry's device library by the setup layer (``event_driven_field_keys``)
        # and passed in; an empty set means "classify everything as periodic".
        self.event_driven_keys: frozenset[str] = (
            event_driven_keys if event_driven_keys is not None else frozenset()
        )

        # --- Injectable hooks (wired by the integration setup) ----------------
        self.new_device_callback: Callable[[str, str, bool], None] | None = None
        self.effective_timeout_resolver: Callable[[str], int | None] | None = None
        self.effective_clear_delay_resolver: Callable[[str], int] | None = None
        # Field keys that resolve to a library descriptor (the merged registry's
        # global ``flat`` table). Used only to flag observed fields with no
        # mapping at DEBUG (a bad decode often shows up as an unexpected field).
        # Empty until wired by the integration setup in ``__init__.py``; while
        # empty the unmapped-field trace is skipped so a failed library load
        # cannot flag every field.
        self.known_field_keys: frozenset[str] = frozenset()
        # Called (no args) when the SDR device identity (``dev_info``/``dev_query``)
        # is first learned or changes on a (re)connect, so the setup layer can
        # refresh the hub device-registry entry's model/manufacturer/serial.
        self.hub_info_callback: Callable[[], None] | None = None

        # Per-device calibration snapshot captured at setup (analogous to
        # ``manage_settings``): ``{device_key: {commodity, unit, scale}}`` for
        # every device with a calibration. ``_async_update_listener`` reloads the
        # hub only when the live calibration map differs from this snapshot, so a
        # routine devices-map upsert (which leaves calibrations untouched) never
        # triggers a reload. Wired by the integration setup in ``__init__.py``.
        self.calibration_snapshot: dict[str, dict[str, Any]] = {}

        # Per-hub user mappings snapshot captured at setup (analogous to
        # ``calibration_snapshot``): the stored ``entry.data[CONF_USER_MAPPINGS]``
        # override object. ``_async_update_listener`` reloads the hub only when the
        # live mappings differ from this snapshot, so a routine devices-map upsert
        # never triggers a reload. Wired by the integration setup in ``__init__.py``.
        self.user_mappings_snapshot: dict[str, Any] = {}

        # Per-device removal callbacks registered by the entity platforms. When a
        # device is removed (async_remove_config_entry_device) each is called with
        # the device_key so the platforms can drop their per-device dedup cache and
        # field listeners; without this the device could not re-appear on a later
        # event while discovery is on.
        self.device_removers: list[Callable[[str], None]] = []

        # --- Runtime state, all scoped to this config entry ------------------
        self.devices: dict[str, NormalizedEvent] = {}
        self.last_seen: dict[str, datetime] = {}
        self.available: dict[str, bool] = {}
        self.seen_fields: set[str] = set()
        self.device_fields: dict[str, set[str]] = {}
        # First-seen DEBUG dedupe caches: unmapped field keys already logged per
        # device, and the last availability timeout logged per device (so the
        # watchdog only logs a device's timeout when it first resolves or changes).
        self._logged_unmapped: dict[str, set[str]] = {}
        self._logged_timeouts: dict[str, int] = {}

        # UTC time of the current successful connection (``None`` while
        # disconnected). Set on the connect edge and cleared on drop in
        # :meth:`_emit_hub_update`; it anchors the pre-connection-backlog gate that
        # :meth:`._events._EventProcessingMixin._on_client_event` re-derives (the
        # library owns the replay classification but does not carry ``is_backlog``
        # on the event object). The library also keeps its own connection anchor
        # for the classification; this HA-side copy is captured on the same connect
        # edge, so the two agree to within the callback latency (well inside the
        # ``DISCOVERY_BACKLOG_GRACE`` window).
        self._connection_time: datetime | None = None
        # Previous client-connected state, so :meth:`_emit_hub_update` can detect
        # the connect/disconnect edge (the client's ``on_hub_update`` also fires on
        # every meta/stats refresh, where connectivity is unchanged).
        self._was_connected = False
        # Last hub identity seen, so :meth:`_emit_hub_update` fires
        # ``hub_info_callback`` only when ``dev_info``/``dev_query`` actually change
        # (the client's ``refresh_dev_info`` fires ``on_hub_update`` on change, but
        # the callback that refreshes the device-registry entry is HA-side policy).
        self._seen_dev_info: dict[str, Any] = {}
        self._seen_dev_query: str | None = None

        # Device keys already offered to ``new_device_callback`` this process.
        # Kept separate from ``self.devices`` (which a backlog frame populates for
        # liveness/replay) so a device first seen in the backlog can still register
        # on its first genuine post-connection event. Not persisted.
        self._discovered: set[str] = set()

        # --- Managed-SDR desired state (restart-surviving) -------------------
        # ``_desired`` maps a registry key -> the desired value HA wants applied;
        # ``_managed`` is the subset of registry keys HA is actively managing.
        # Both are persisted to a per-hub ``Store`` keyed by ``entry_id`` so a
        # value change never churns the config entry, and loaded once at start.
        # All ``/cmd`` issuance is serialized inside the client (its own ``/cmd``
        # lock) so a user write and a reconnect replay can never interleave.
        self._desired: dict[str, Any] = {}
        self._managed: set[str] = set()
        # Whether the one-time setup ``initial_center_frequency`` seed has already
        # been applied (persisted with the desired state). Gating the seed on this
        # — rather than on ``_desired`` being empty — makes the configured value
        # win over adopted/persisted state exactly once at setup, and never
        # re-applies after the user later changes the frequency via the control.
        self._initial_freq_seeded: bool = False
        self._store: _SdrStore = _SdrStore(
            hass, SDR_STORE_VERSION, sdr_store_key(entry.entry_id)
        )

        # --- The transport client (owns the WS/HTTP transport) ---------------
        # The injected HA shared session means the client will NOT close it on
        # ``stop()`` (Home Assistant owns the session lifecycle). Events flow to
        # ``_on_client_event`` (HA-side dispatch) and hub-state changes to
        # ``_emit_hub_update``. No ``clock`` is injected: the coordinator has no
        # deterministic time source of its own, so the client defaults to
        # ``datetime.now(UTC)`` — the same wall clock ``dt_util.utcnow`` reads.
        # ``event_tz`` is HA's configured zone, so an offset-less rtl_433 ``time``
        # stamp is classified in that zone (matching the pre-extraction
        # ``dt_util.as_utc`` behavior) rather than the host process zone — the two
        # diverge when the container/OS zone differs from HA's configured zone.
        self._client = Rtl433Client(
            host,
            port=port,
            path=path,
            secure=secure,
            session=async_get_clientsession(hass),
            skip_keys=self.skip_keys,
            on_event=self._on_client_event,
            on_hub_update=self._emit_hub_update,
            event_tz=dt_util.get_default_time_zone(),
        )

        # --- Internal lifecycle handles --------------------------------------
        self._started = False
        self._watchdog_unsub: Callable[[], None] | None = None

    # ------------------------------------------------------------------ #
    # Client-backed read-only state                                      #
    # ------------------------------------------------------------------ #
    @property
    def ws_url(self) -> str:
        """Return the configured WebSocket URL for this hub."""
        return self._client.ws_url

    @property
    def connected(self) -> bool:
        """Whether the client's socket is currently open."""
        return self._client.connected

    @property
    def meta(self) -> dict[str, Any]:
        """Latest SDR/meta configuration (client-sourced over HTTP ``/cmd``)."""
        return self._client.meta

    @property
    def stats(self) -> dict[str, Any]:
        """Latest server-stats payload (client-sourced over HTTP ``/cmd``)."""
        return self._client.stats

    @property
    def dev_info(self) -> dict[str, Any]:
        """The SDR's librtlsdr USB device label (client-sourced)."""
        return self._client.dev_info

    @property
    def dev_query(self) -> str | None:
        """The ``-d`` selector rtl_433 opened (client-sourced)."""
        return self._client.dev_query

    @property
    def noise_level(self) -> float | None:
        """Estimated receiver noise level in dB (client-parsed "Auto Level" logs).

        Socket-sourced: rtl_433 surfaces its noise floor only as "Auto Level"
        log frames (requires ``-Y autolevel`` and/or ``-M noise`` server-side);
        the client parses them into this snapshot. ``None`` until the first
        such frame arrives.
        """
        return self._client.noise_level

    @property
    def min_level(self) -> float | None:
        """Auto-adjusted minimum detection level in dB (requires ``-Y autolevel``)."""
        return self._client.min_level

    # ------------------------------------------------------------------ #
    # Lifecycle                                                          #
    # ------------------------------------------------------------------ #
    async def async_start(self) -> None:
        """Load desired state, start the client, and arm the watchdog."""
        if self._started:
            return
        self._started = True
        # Load the persisted desired state (or wipe it when management is off)
        # before the client can connect and the connect-edge adoption/enforcement
        # can run against it.
        await self.async_load_desired_state()
        await self._client.start()
        self._watchdog_unsub = async_track_time_interval(
            self.hass,
            self._async_watchdog,
            _WATCHDOG_INTERVAL,
            name=f"rtl_433 watchdog {self.entry.entry_id}",
        )
        LOGGER.debug("rtl_433 coordinator started for %s", self.ws_url)

    def forget_device(self, device_key: str) -> None:
        """Drop a device's runtime state so its next event is treated as new.

        Called when a device is removed from its device page
        (``async_remove_config_entry_device``). Without this eviction the device
        would stay in ``devices`` and a later event would not be treated as new,
        so a re-transmitting device could never re-appear while discovery is on.
        """
        self.devices.pop(device_key, None)
        self.last_seen.pop(device_key, None)
        self.available.pop(device_key, None)
        self.device_fields.pop(device_key, None)
        # Re-arm discovery so a later live event re-registers the device.
        self._discovered.discard(device_key)

    async def async_stop(self) -> None:
        """Stop the client and cancel the watchdog (never close the HA session)."""
        self._started = False

        if self._watchdog_unsub is not None:
            self._watchdog_unsub()
            self._watchdog_unsub = None

        # The client closes its own socket and cancels its loops; it never closes
        # the injected HA shared session.
        await self._client.stop()

        self._was_connected = False
        self._connection_time = None
        LOGGER.debug("rtl_433 coordinator stopped for %s", self.ws_url)

    # ------------------------------------------------------------------ #
    # Client callbacks: hub-state fan-out + connect-edge policy          #
    # ------------------------------------------------------------------ #
    def _emit_hub_update(self) -> None:
        """Handle the client's ``on_hub_update``: edges, identity, dispatch.

        Wired as the client's ``on_hub_update`` callback, so it runs on every
        connectivity change and every meta/stats/dev_info refresh. It (1) detects
        the connect/disconnect edge to anchor the backlog gate and drive the
        managed-SDR adoption the library does not own, (2) refreshes the hub
        device-registry identity when it changes, and (3) fans the hub update out
        to the hub entities over the dispatcher — exactly as before.
        """
        connected = self._client.connected
        if connected and not self._was_connected:
            # (Re)connect edge: anchor the HA-side backlog gate and adopt/enforce
            # the managed SDR settings. The library client refreshes meta/stats/
            # dev_info on connect but owns none of the managed-SDR policy.
            self._was_connected = True
            self._connection_time = dt_util.utcnow()
            if self.manage_settings:
                self.entry.async_create_background_task(
                    self.hass,
                    self._on_connect(),
                    name=f"rtl_433 sdr adopt {self.entry.entry_id}",
                )
        elif not connected and self._was_connected:
            self._was_connected = False
            self._connection_time = None

        self._maybe_refresh_hub_identity()
        async_dispatcher_send(self.hass, signal_hub_update(self.entry.entry_id))

    async def _on_connect(self) -> None:
        """Adopt + enforce the managed SDR settings on a (re)connect.

        The old connect loop ran this after connecting; the library client owns
        the transport but not this policy, so it is driven here off the connect
        edge. A best-effort ``refresh_meta`` first guarantees ``self.meta`` is
        populated for first-connect adoption — the connect-edge ``on_hub_update``
        can fire before the client's own post-connect refresh lands. Every step is
        best-effort and swallows its own ``/cmd`` errors, so nothing here can break
        the event stream.
        """
        if not self.manage_settings:
            return
        # The library client runs its own post-connect ``refresh_meta``; only
        # fetch here when ``meta`` is still empty (first connect, before that
        # refresh lands) so adoption has data. Avoids a duplicate ``/cmd`` GET on
        # every reconnect.
        if not self._client.meta:
            await self._client.refresh_meta()
        try:
            await self._seed_desired_on_first_connect()
            await self._enforce_all()
        except Exception as err:  # noqa: BLE001 - never break on SDR adopt/enforce
            LOGGER.debug(
                "rtl_433 SDR adopt/enforce failed for %s: %s", self.ws_url, err
            )

    def _maybe_refresh_hub_identity(self) -> None:
        """Refresh the hub device-registry entry when the SDR identity changes.

        The client learns ``dev_info``/``dev_query`` on each (re)connect and fires
        ``on_hub_update`` when they change, but mapping the identity onto the hub
        device registry entry is HA-side policy. Fire ``hub_info_callback`` only on
        an actual change (the client only ever advances these to non-empty values),
        guarded so a registry hiccup can never break the connection.
        """
        info = self._client.dev_info
        query = self._client.dev_query
        if info == self._seen_dev_info and query == self._seen_dev_query:
            return
        self._seen_dev_info = info
        self._seen_dev_query = query
        if self.hub_info_callback is not None:
            try:
                self.hub_info_callback()
            except Exception as err:  # noqa: BLE001 - never kill the loop
                LOGGER.debug("rtl_433 hub_info_callback failed: %s", err)

    def _dispatch(
        self,
        device_key: str,
        normalized: NormalizedEvent,
        *,
        is_replay: bool | None = None,
        is_repaint: bool = False,
    ) -> None:
        """Fan a normalized event out to the device's entities.

        Called by ``_on_client_event`` (:class:`._events._EventProcessingMixin`)
        and the availability watchdog (:class:`._watchdog._AvailabilityMixin`); it
        is the single device-update dispatcher seam.

        The replay flag travels on the ``NormalizedEvent`` itself, so the normal
        ``_on_client_event`` path passes ``is_replay=None`` (the default) to honor
        whatever flag the classification stamped on the object. An explicit
        ``is_replay`` overrides it: the watchdog re-dispatch of a *cached* event
        passes ``is_replay=False`` so its unavailable re-paint is never suppressed
        as a replay even when the cached object happened to be a replay frame.

        ``is_repaint=True`` (watchdog re-paint only) additionally marks the frame
        as an availability re-paint of the *cached* last event rather than a new
        transmission, so ``Rtl433Event`` skips (re-)firing regardless of the cached
        value or object identity -- the identity dedupe alone is unreliable here
        because a replay-seeded cache leaves the event entity's anchor unset after
        a restart (and the ``is_replay`` rewrite below mints a fresh object).
        """
        if (is_replay is not None and normalized.is_replay != is_replay) or (
            is_repaint and not normalized.is_repaint
        ):
            normalized = dataclasses.replace(
                normalized,
                is_replay=normalized.is_replay if is_replay is None else is_replay,
                is_repaint=is_repaint or normalized.is_repaint,
            )
        async_dispatcher_send(
            self.hass,
            signal_device_update(self.entry.entry_id, device_key),
            normalized,
        )

    # ------------------------------------------------------------------ #
    # SDR ``/cmd`` seams (delegated to the client)                       #
    # ------------------------------------------------------------------ #
    async def _send_cmd(
        self, command: str, *, val: int | None = None, arg: str | None = None
    ) -> bool:
        """Issue one setter ``/cmd`` via the client; return ``True`` on success.

        Thin delegate to the client's ``/cmd`` setter primitive (the only public
        write path in pyrtl_433 0.1.0 is the underscore-prefixed ``_send_cmd``).
        The client serializes sends under its own lock, so a user write and a
        reconnect enforcement replay can never interleave.
        """
        return await self._client._send_cmd(command, val=val, arg=arg)

    async def _refresh_meta(self) -> None:
        """Re-fetch the SDR/meta configuration via the client into ``self.meta``.

        Delegates to the client's ``refresh_meta`` (which fires ``on_hub_update``
        when the values change, repainting the hub entities). Used by the SDR
        write path to reconcile ``self.meta`` after a setter send.
        """
        await self._client.refresh_meta()

    # ------------------------------------------------------------------ #
    # Config-flow connectivity check                                     #
    # ------------------------------------------------------------------ #
    @staticmethod
    async def validate_connection(
        hass: HomeAssistant,
        host: str,
        port: int = DEFAULT_PORT,
        path: str = DEFAULT_PATH,
        *,
        secure: bool = False,
    ) -> bool:
        """Attempt a short-lived WebSocket connection to verify reachability.

        Delegates to :meth:`pyrtl_433.Rtl433Client.validate_connection` with Home
        Assistant's shared aiohttp session. Returns ``True`` on success and closes
        immediately (no side effects). Raises :class:`pyrtl_433.CannotConnect` if
        the endpoint cannot be reached.
        """
        session = async_get_clientsession(hass)
        return await Rtl433Client.validate_connection(
            session, host, port, path, secure=secure
        )
