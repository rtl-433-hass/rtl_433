"""WebSocket coordinator for one rtl_433 server (hub config entry).

This module owns the WebSocket lifecycle for a single rtl_433 HTTP server:
connect, parse JSON frames, ignore keep-alives/malformed JSON, reconnect with
capped exponential backoff, track per-device last-seen timestamps, run an
availability watchdog, and fan events out via Home Assistant's dispatcher keyed
by device. It is a *push* coordinator (events arrive over the socket) rather
than a polling ``DataUpdateCoordinator``, so it is a plain class.

The coordinator is one class, ``Rtl433Coordinator``, assembled here from three
policy mixins so each concern lives in its own file and ``__init__`` stays the
single place every runtime attribute is declared:

- ``_sdr.py`` (:class:`._sdr._SdrSettingsMixin`) — the managed-SDR desired-state
  Store, write path, adoption, and reconnect enforcement.
- ``_events.py`` (:class:`._events._EventProcessingMixin`) — event normalization
  and replay classification.
- ``_watchdog.py`` (:class:`._watchdog._AvailabilityMixin`) — effective-timeout
  resolution and the silence-based availability watchdog.

The division of labour is **base owns I/O and state; the mixins own policy**:
this module holds every side-effecting call (the WebSocket connect/reconnect
loop, the HTTP ``/cmd`` transport, the Home Assistant dispatcher fan-out) plus
``__init__``, while the mixins decide *what* to send, dispatch, or expire and
delegate the actual I/O back to the methods defined here (``_send_cmd``,
``_refresh_meta``, ``_dispatch``).

Decoupling: the coordinator imports nothing from ``mapping.py``,
``config_flow.py``, or ``entity.py``. To stay file-disjoint and cycle-free it
accepts the pieces it needs as injectable attributes:

- ``skip_keys`` — the set of event keys to drop from measurement fields. Defaults
  to a minimal identity set; the integration setup injects the library skip-keys.
- ``new_device_callback`` — called with ``(device_key, model, is_replay)`` the
  first time an unknown device is seen while discovery is enabled; ``is_replay``
  flags a reconnect-replay/stale-gap frame so the callback can wire up entities
  without raising a "new device" notification for a mere re-broadcast. The
  integration setup wires this to the
  discovery flow; the coordinator never imports the config flow.
- ``effective_timeout_resolver`` — called with ``device_key`` to resolve the
  per-device availability timeout (override → hub default). The integration setup wires this;
  the fallback is the hub default.
- ``effective_clear_delay_resolver`` — called with ``device_key`` to resolve the
  per-device motion clear-delay (override → default). The integration setup wires
  this; the binary_sensor reads it. The fallback is the motion default.
- ``hub_info_callback`` — called (no args) when the SDR device identity
  (``dev_info``/``dev_query``, learned from ``get_dev_info``/``get_dev_query`` on
  each connect) is first seen or changes. The integration setup wires this to
  refresh the hub device-registry entry's model/manufacturer/serial; the
  coordinator never touches the device registry itself.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import contextlib
import dataclasses
from datetime import datetime, timedelta
import json
from typing import Any

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
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
from ..normalizer import DEFAULT_SKIP_KEYS, NormalizedEvent
from ._events import (
    DISCOVERY_BACKLOG_GRACE as DISCOVERY_BACKLOG_GRACE,  # re-export for tests
    REPLAY_STALE_THRESHOLD as REPLAY_STALE_THRESHOLD,  # re-export for tests
    _EventProcessingMixin,
)
from ._sdr import _SdrSettingsMixin, _SdrStore
from ._watchdog import _WATCHDOG_INTERVAL, _AvailabilityMixin

# Backoff bounds for the reconnect loop (seconds). Starts at 1s, doubles on
# each consecutive failure, capped at 60s so the loop never spins hot.
_BACKOFF_MIN = 1.0
_BACKOFF_MAX = 60.0

# Timeout (seconds) for the short-lived connection attempt used by the config
# flow's reachability check.
_VALIDATE_TIMEOUT = 10.0

# Timeout (seconds) for each one-shot ``/cmd`` getter/setter HTTP request.
_GETTER_TIMEOUT = 10.0

# How often ``get_meta`` + ``get_stats`` are re-fetched over HTTP while
# connected, so the hub's SDR config and throughput stay live without depending
# on the streaming socket. Re-polling meta (not only on connect / right after a
# write) lets the "actual" SDR sensors converge to the server's truth within this
# window even when a value changes outside a Home Assistant write or a single
# post-write read-back raced the SDR retune.
_REFRESH_INTERVAL = timedelta(seconds=60)

# Identity keys (besides ``model``) that mark a frame as a decoded-device event.
# Kept in sync with normalizer.IDENTITY_KEYS.
_EVENT_IDENTITY_KEYS = ("id", "channel", "subtype")


class CannotConnect(HomeAssistantError):
    """Raised when the rtl_433 WebSocket endpoint cannot be reached."""


def _build_ws_url(host: str, port: int, path: str, *, secure: bool = False) -> str:
    """Build a ``ws(s)://host:port/path`` URL from connection parameters."""
    scheme = "wss" if secure else "ws"
    if not path.startswith("/"):
        path = f"/{path}"
    return f"{scheme}://{host}:{port}{path}"


def _build_cmd_url(host: str, port: int, *, secure: bool = False) -> str:
    """Build the ``http(s)://host:port/cmd`` URL (server root, never the WS path).

    The ``/cmd`` endpoint always lives at the server root regardless of the
    configured WebSocket ``path``; graceful degradation behind a proxy that hides
    ``/cmd`` depends on this never being derived from ``self.path``. ``secure``
    maps ``wss`` ⇒ ``https`` and ``ws`` ⇒ ``http``.
    """
    scheme = "https" if secure else "http"
    return f"{scheme}://{host}:{port}/cmd"


class Rtl433Coordinator(_SdrSettingsMixin, _EventProcessingMixin, _AvailabilityMixin):
    """Owns the WebSocket connection and runtime state for one rtl_433 hub.

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
        ``connected``: ``bool`` whether the socket is currently open.
        ``meta``: ``dict[str, Any]`` latest SDR/meta configuration (HTTP-sourced).
        ``stats``: ``dict[str, Any]`` latest server-stats payload (HTTP-sourced).

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
        # The coordinator reads raw ``time`` for the replay classification, so it
        # must never reach entities as an (unmatched) measurement field. The
        # library skip-keys (``_skip_keys.yaml``) do not list ``time``, so add it
        # here so ``normalize`` always drops it regardless of the injected set.
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
        self.connected = False
        # First-seen DEBUG dedupe caches: unmapped field keys already logged per
        # device, and the last availability timeout logged per device (so the
        # watchdog only logs a device's timeout when it first resolves or changes).
        self._logged_unmapped: dict[str, set[str]] = {}
        self._logged_timeouts: dict[str, int] = {}

        # High-water mark of the maximum event ``time`` (UTC) ever parsed, used
        # to classify each frame against the reconnect replay (see
        # ``_process_event``). Initially unset: a frame at or below it is an
        # already-seen replay; the cold-start case (mark unset) falls to the age
        # test in ``_process_event``. Spans reconnects so a brief blip's re-sent
        # buffer tail is recognised as already-seen and never re-fires.
        self._event_high_water: datetime | None = None

        # UTC time of the current successful WebSocket connection (``None`` while
        # disconnected). Set on every (re)connect and cleared on drop, it gates
        # device auto-registration to post-connection messages: a server replays
        # up to its last 100 events on connect, and those pre-connection backlog
        # frames must seed runtime state without registering new devices.
        self._connection_time: datetime | None = None

        # Device keys already offered to ``new_device_callback`` this process.
        # Kept separate from ``self.devices`` (which a backlog frame populates for
        # liveness/replay) so a device first seen in the backlog can still register
        # on its first genuine post-connection event. Not persisted.
        self._discovered: set[str] = set()

        # --- Hub-scoped runtime state (rendered by the hub entities) ---------
        # Latest meta/SDR configuration (assembled from the HTTP getters)
        # and the latest server-stats payload. Populated over HTTP, not the socket.
        self.meta: dict[str, Any] = {}
        self.stats: dict[str, Any] = {}
        # SDR device identity, fetched once per (re)connect (static per dongle).
        # ``dev_info`` is the librtlsdr USB label as ``{"vendor", "product",
        # "serial"}``; ``dev_query`` is the ``-d`` selector rtl_433 opened. Both
        # stay empty when no SDR device is open (e.g. ``-D manual``).
        self.dev_info: dict[str, Any] = {}
        self.dev_query: str | None = None
        # Getters (by command name) currently returning malformed JSON. Used to
        # log the "server returned invalid JSON" error once per command until it
        # recovers, so the 60s refresh tick can never flood the log.
        self._malformed_cmds: set[str] = set()

        # --- Managed-SDR desired state (restart-surviving) -------------------
        # ``_desired`` maps a registry key -> the desired value HA wants applied;
        # ``_managed`` is the subset of registry keys HA is actively managing.
        # Both are persisted to a per-hub ``Store`` keyed by ``entry_id`` so a
        # value change never churns the config entry, and loaded once at start.
        # All ``/cmd`` issuance (write path, enforcement replay, read-back) is
        # serialized through ``_cmd_lock`` so a user write and a reconnect replay
        # can never interleave requests to the same server.
        self._desired: dict[str, Any] = {}
        self._managed: set[str] = set()
        # Whether the one-time setup ``initial_center_frequency`` seed has already
        # been applied (persisted with the desired state). Gating the seed on this
        # — rather than on ``_desired`` being empty — makes the configured value
        # win over adopted/persisted state exactly once at setup, and never
        # re-applies after the user later changes the frequency via the control.
        self._initial_freq_seeded: bool = False
        self._cmd_lock = asyncio.Lock()
        self._store: _SdrStore = _SdrStore(
            hass, SDR_STORE_VERSION, sdr_store_key(entry.entry_id)
        )

        # --- Internal lifecycle handles --------------------------------------
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._watchdog_unsub: Callable[[], None] | None = None
        self._refresh_unsub: Callable[[], None] | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None

    @property
    def ws_url(self) -> str:
        """Return the configured WebSocket URL for this hub."""
        return _build_ws_url(self.host, self.port, self.path, secure=self.secure)

    # ------------------------------------------------------------------ #
    # Lifecycle                                                          #
    # ------------------------------------------------------------------ #
    async def async_start(self) -> None:
        """Start the connect loop and the availability watchdog."""
        if self._task is not None:
            return
        # Load the persisted desired state (or wipe it when management is off)
        # before the connect loop can run adoption/enforcement against it.
        await self.async_load_desired_state()
        self._stop_event.clear()
        self._task = self.entry.async_create_background_task(
            self.hass,
            self._connect_loop(),
            name=f"rtl_433 ws {self.entry.entry_id}",
        )
        self._watchdog_unsub = async_track_time_interval(
            self.hass,
            self._async_watchdog,
            _WATCHDOG_INTERVAL,
            name=f"rtl_433 watchdog {self.entry.entry_id}",
        )
        self._refresh_unsub = async_track_time_interval(
            self.hass,
            self._async_refresh_tick,
            _REFRESH_INTERVAL,
            name=f"rtl_433 refresh {self.entry.entry_id}",
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
        """Stop the connect loop, close the socket, and cancel the watchdog."""
        self._stop_event.set()

        if self._watchdog_unsub is not None:
            self._watchdog_unsub()
            self._watchdog_unsub = None

        if self._refresh_unsub is not None:
            self._refresh_unsub()
            self._refresh_unsub = None

        if self._ws is not None and not self._ws.closed:
            await self._ws.close()

        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

        self.connected = False
        LOGGER.debug("rtl_433 coordinator stopped for %s", self.ws_url)

    # ------------------------------------------------------------------ #
    # Connect / reconnect loop                                           #
    # ------------------------------------------------------------------ #
    async def _connect_loop(self) -> None:
        """Connect, stream frames, and reconnect with capped backoff on drop."""
        backoff = _BACKOFF_MIN
        session = async_get_clientsession(self.hass)

        while not self._stop_event.is_set():
            try:
                async with session.ws_connect(self.ws_url, heartbeat=30) as ws:
                    self._ws = ws
                    self.connected = True
                    self._connection_time = dt_util.utcnow()
                    backoff = _BACKOFF_MIN  # reset after a successful connect
                    self._emit_hub_update()
                    LOGGER.debug("rtl_433 connected to %s", self.ws_url)
                    # Anchor the replay classification: every frame's verdict in
                    # ``_process_event`` is judged against these two values, so log
                    # them once per connect to make a lone REPLAY/BACKLOG line
                    # interpretable.
                    LOGGER.debug(
                        "rtl_433 connection anchor for %s: connected_at=%s "
                        "replay_high_water=%s (frames at/below high-water, or "
                        "before connected_at, are suppressed as replays)",
                        self.ws_url,
                        self._connection_time.isoformat(),
                        self._event_high_water.isoformat()
                        if self._event_high_water is not None
                        else "none",
                    )
                    # Seed SDR/meta config and stats over HTTP (never the socket).
                    # Each getter swallows its own failures, so a hidden ``/cmd``
                    # (e.g. behind a proxy) cannot break the connection.
                    await self._refresh_meta()
                    await self._refresh_stats()
                    # Learn the SDR's model/serial so the hub device shows which
                    # physical dongle it is (static per dongle; cheap on reconnect).
                    await self._refresh_dev_info()
                    # Adopt the server's settings on first connect, then replay
                    # the managed desired state on every (re)connect. Both are
                    # best-effort and swallow their own ``/cmd`` errors, but wrap
                    # here too so nothing can break the connection / event stream.
                    if self.manage_settings:
                        try:
                            await self._seed_desired_on_first_connect()
                            await self._enforce_all()
                        except Exception as err:  # noqa: BLE001 - never kill loop
                            LOGGER.debug(
                                "rtl_433 SDR adopt/enforce failed for %s: %s",
                                self.ws_url,
                                err,
                            )
                    await self._read_frames(ws)
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 - resilient: never kill loop
                LOGGER.debug("rtl_433 connection error for %s: %s", self.ws_url, err)
            finally:
                self._ws = None
                self.connected = False
                self._connection_time = None
                self._emit_hub_update()

            if self._stop_event.is_set():
                break

            # A reconnect makes the server replay its recent backlog, so a flaky
            # link is a common source of the REPLAY/BACKLOG bursts users chase;
            # surface the retry cadence to explain them.
            LOGGER.debug(
                "rtl_433 disconnected from %s; reconnecting in %.0fs",
                self.ws_url,
                backoff,
            )
            # Wait for the backoff window or an early stop, then retry.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop_event.wait(), timeout=backoff)
            backoff = min(backoff * 2, _BACKOFF_MAX)

    async def _read_frames(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        """Iterate incoming frames until the socket closes or stop is set."""
        async for msg in ws:
            if self._stop_event.is_set():
                break
            if msg.type is aiohttp.WSMsgType.TEXT:
                self._handle_text_frame(msg.data)
            elif msg.type in (
                aiohttp.WSMsgType.CLOSE,
                aiohttp.WSMsgType.CLOSING,
                aiohttp.WSMsgType.CLOSED,
                aiohttp.WSMsgType.ERROR,
            ):
                break
            # PING/PONG/BINARY and anything else is ignored as keep-alive noise.

    def _handle_text_frame(self, data: str) -> None:
        """Parse one text frame and, if it is a valid event, process it."""
        text = data.strip() if isinstance(data, str) else data
        if not text:
            # Empty frames act as keep-alives.
            return
        try:
            event = json.loads(text)
        except (ValueError, TypeError) as err:
            LOGGER.debug("rtl_433 skipping malformed JSON frame: %s", err)
            return
        if not isinstance(event, dict):
            LOGGER.debug("rtl_433 skipping non-object frame: %r", text[:120])
            return
        self._classify_frame(event)

    def _classify_frame(self, event: dict[str, Any]) -> None:
        """Route a parsed frame by shape (event vs shutdown vs ignored)."""
        is_event = event.get("model") is not None or any(
            event.get(key) is not None for key in _EVENT_IDENTITY_KEYS
        )
        if is_event:
            self._process_event(event)
        elif "shutdown" in event:
            self._handle_shutdown()
        # All other non-event frames (meta / state / result / error) are ignored:
        # meta and stats are sourced over HTTP, so nothing else needs handling here.

    def _handle_shutdown(self) -> None:
        """Handle a ``{"shutdown": ...}`` frame: flip connectivity off."""
        if self.connected:
            LOGGER.debug("rtl_433 server announced shutdown for %s", self.ws_url)
        self.connected = False
        self._emit_hub_update()

    def _emit_hub_update(self) -> None:
        """Notify hub entities that connectivity / meta / stats changed."""
        async_dispatcher_send(self.hass, signal_hub_update(self.entry.entry_id))

    def _dispatch(
        self,
        device_key: str,
        normalized: NormalizedEvent,
        *,
        is_replay: bool | None = None,
        is_repaint: bool = False,
    ) -> None:
        """Fan a normalized event out to the device's entities.

        Called by ``_process_event`` (:class:`._events._EventProcessingMixin`) and
        the availability watchdog (:class:`._watchdog._AvailabilityMixin`); it is
        the single device-update dispatcher seam.

        The replay flag travels on the ``NormalizedEvent`` itself, so the normal
        ``_process_event`` path passes ``is_replay=None`` (the default) to honor
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
    # HTTP ``/cmd`` transport (SDR/meta config + server stats)           #
    # ------------------------------------------------------------------ #
    async def _fetch_cmd(self, command: str) -> Any | None:
        """GET one ``/cmd`` getter; return parsed JSON or None on any failure.

        Uses the shared Home Assistant aiohttp session against the server root
        (never the WS ``path``). Any HTTP/parse error is caught and logged at
        debug so a single getter — or a proxy that hides ``/cmd`` — can never
        raise into the connect loop or the watchdog.
        """
        session = async_get_clientsession(self.hass)
        url = _build_cmd_url(self.host, self.port, secure=self.secure)
        try:
            async with session.get(
                url, params={"cmd": command}, timeout=_GETTER_TIMEOUT
            ) as resp:
                resp.raise_for_status()
                try:
                    # rtl_433 may not set a strict ``application/json`` content-type
                    # for scalar getters, so do not require one.
                    payload = await resp.json(content_type=None)
                except ValueError as err:
                    # The endpoint is reachable and returned 2xx, but the body is
                    # not valid JSON. The known cause is an rtl_433 server-side
                    # bug that truncates/corrupts large ``get_stats`` responses
                    # (the per-protocol ``stats`` array overflows a fixed output
                    # buffer). Unlike a hidden ``/cmd`` (a connection/4xx error,
                    # expected behind a proxy and kept at debug), malformed data is
                    # a genuine server fault worth surfacing at error level -- but
                    # only once per command until it recovers, so the periodic
                    # refresh tick can't flood the log.
                    if command not in self._malformed_cmds:
                        self._malformed_cmds.add(command)
                        LOGGER.error(
                            "rtl_433 server returned invalid data for '%s' (at %s); "
                            "the related hub sensors will not update: %s",
                            command,
                            url,
                            err,
                        )
                    return None
                self._malformed_cmds.discard(command)
                return payload
        except Exception as err:  # noqa: BLE001 - getters must never kill the loop
            LOGGER.debug("rtl_433 getter %s failed at %s: %s", command, url, err)
            return None

    @staticmethod
    def _unwrap_result(payload: Any) -> Any:
        """Unwrap a ``{"result": <value>}`` getter response to its raw value.

        The HTTP ``/cmd`` responder (``rpc_response_jsoncmd``) wraps **every**
        getter reply in a ``result`` envelope — not just the scalar getters
        (``get_gain``/``get_ppm_error``) but the JSON-payload getters
        (``get_meta``/``get_stats``) too. (Only the WebSocket framing sends those
        two as a bare object; this integration uses ``/cmd``.) A bare value is
        accepted too, so this is safe across transports — read defensively.
        """
        if isinstance(payload, dict) and "result" in payload:
            return payload["result"]
        return payload

    async def _send_cmd(
        self, command: str, *, val: int | None = None, arg: str | None = None
    ) -> bool:
        """Issue one setter ``/cmd`` over HTTP; return ``True`` on success.

        Mirrors :meth:`_fetch_cmd` (shared session, server-root URL, never the WS
        ``path``) but adds the ``val``/``arg`` query params and serializes the
        send under ``_cmd_lock`` so a user write and a reconnect replay can never
        interleave. ``val`` is stringified as an integer; ``arg`` is sent
        verbatim — including the empty string, which is the gain "auto" sentinel
        (so the gain command must always pass ``arg``, never omit it). Any
        HTTP/parse error is caught, logged at debug, and returns ``False``.
        Commands go only over ``/cmd`` — never the streaming WebSocket.
        """
        session = async_get_clientsession(self.hass)
        url = _build_cmd_url(self.host, self.port, secure=self.secure)
        params: dict[str, str] = {"cmd": command}
        if val is not None:
            params["val"] = str(int(val))
        if arg is not None:
            params["arg"] = arg
        async with self._cmd_lock:
            try:
                async with session.get(
                    url, params=params, timeout=_GETTER_TIMEOUT
                ) as resp:
                    resp.raise_for_status()
                return True
            except Exception as err:  # noqa: BLE001 - never kill the loop
                LOGGER.debug("rtl_433 /cmd %s failed at %s: %s", command, url, err)
                return False

    async def _refresh_meta(self) -> None:
        """Fetch ``get_meta`` + ``get_gain`` + ``get_ppm_error`` into ``self.meta``."""
        meta = self._unwrap_result(await self._fetch_cmd("get_meta"))
        gain = self._unwrap_result(await self._fetch_cmd("get_gain"))
        ppm = self._unwrap_result(await self._fetch_cmd("get_ppm_error"))

        new_meta: dict[str, Any] = {}
        if isinstance(meta, dict):
            for key in (
                "center_frequency",
                "samp_rate",
                "conversion_mode",
                "frequencies",
                "hop_times",
            ):
                if key in meta:
                    new_meta[key] = meta[key]
            hop_times = meta.get("hop_times")
            if isinstance(hop_times, list) and hop_times:
                new_meta["hop_interval"] = hop_times[0]
        if isinstance(gain, str):
            new_meta["gain"] = gain
        if isinstance(ppm, int) and not isinstance(ppm, bool):
            new_meta["ppm_error"] = ppm

        if new_meta:
            self.meta = {**self.meta, **new_meta}
            self._emit_hub_update()

    async def _refresh_stats(self) -> None:
        """Fetch ``get_stats`` into ``self.stats``."""
        stats = self._unwrap_result(await self._fetch_cmd("get_stats"))
        if isinstance(stats, dict):
            self.stats = stats
            self._emit_hub_update()

    async def _refresh_dev_info(self) -> None:
        """Fetch the SDR device identity into ``dev_info`` / ``dev_query``.

        ``get_dev_info`` is the librtlsdr USB device label as a JSON object
        (``{"vendor": ..., "product": ..., "serial": ...}``); ``get_dev_query`` is
        the ``-d`` selector rtl_433 opened. Both are static for a given dongle, so
        this only runs on (re)connect — re-running on reconnect keeps them correct
        if the dongle behind the server is swapped. Either may be empty/unset when
        no SDR device is open (e.g. ``-D manual``); the stored value is then left
        untouched so the hub keeps its last known identity.

        When the identity changes, ``hub_info_callback`` is invoked so the setup
        layer can refresh the hub device-registry entry. The callback is guarded
        so a registry hiccup can never break the streaming connection.
        """
        info = self._unwrap_result(await self._fetch_cmd("get_dev_info"))
        query = self._unwrap_result(await self._fetch_cmd("get_dev_query"))

        # Over ``/cmd`` the JSON object is embedded directly, but accept a JSON
        # string too (WS framing / a proxy) so the parse is transport-agnostic.
        if isinstance(info, str):
            try:
                info = json.loads(info)
            except ValueError:
                info = None

        changed = False
        if isinstance(info, dict) and info and info != self.dev_info:
            self.dev_info = info
            changed = True
        if isinstance(query, str) and query and query != self.dev_query:
            self.dev_query = query
            changed = True

        if changed and self.hub_info_callback is not None:
            try:
                self.hub_info_callback()
            except Exception as err:  # noqa: BLE001 - never kill the connect loop
                LOGGER.debug("rtl_433 hub_info_callback failed: %s", err)

    async def _async_refresh_tick(self, _now: datetime) -> None:
        """Re-fetch meta + stats on the interval, only while connected.

        Refreshing meta here (not just on connect / right after a write) lets the
        hub's "actual" SDR sensors converge to the server's current values within
        the interval, even when a setting changes outside a Home Assistant write
        or a single post-write read-back raced the SDR retune.
        """
        if self.connected:
            await self._refresh_meta()
            await self._refresh_stats()

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

        Returns ``True`` on success and closes immediately (no side effects).
        Raises :class:`CannotConnect` if the endpoint cannot be reached.
        """
        url = _build_ws_url(host, port, path, secure=secure)
        session = async_get_clientsession(hass)
        try:
            ws = await session.ws_connect(url, timeout=_VALIDATE_TIMEOUT)
        except (aiohttp.ClientError, TimeoutError, OSError) as err:
            raise CannotConnect(f"Cannot connect to {url}: {err}") from err
        else:
            await ws.close()
            return True
