"""WebSocket coordinator for one rtl_433 server (hub config entry).

This module owns the WebSocket lifecycle for a single rtl_433 HTTP server:
connect, parse JSON frames, ignore keep-alives/malformed JSON, reconnect with
capped exponential backoff, track per-device last-seen timestamps, run an
availability watchdog, and fan events out via Home Assistant's dispatcher keyed
by device. It is a *push* coordinator (events arrive over the socket) rather
than a polling ``DataUpdateCoordinator``, so it is a plain class.

Decoupling: this module imports nothing from ``mapping.py``, ``config_flow.py``,
or ``entity.py``. To stay file-disjoint and cycle-free it accepts the pieces it
needs as injectable attributes:

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
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from ..const import (
    AVAILABILITY_TIMEOUT_NEVER,
    DEFAULT_AVAILABILITY_TIMEOUT,
    DEFAULT_PATH,
    DEFAULT_PORT,
    LOGGER,
    SDR_STORE_VERSION,
    class_default_timeout,
    sdr_store_key,
    signal_device_update,
    signal_hub_update,
)
from ..normalizer import DEFAULT_SKIP_KEYS, NormalizedEvent, normalize
from ..sdr_settings import (
    KEY_CENTER_FREQUENCY,
    KEY_GAIN_AUTO,
    KEY_GAIN_DB,
    SDR_SETTINGS,
    SDR_SETTINGS_BY_KEY,
    gain_command_arg,
)


class _SdrStore(Store[dict[str, Any]]):
    """Per-hub desired-state Store with a Hz->MHz center-frequency migration.

    Version 1 persisted ``values["center_frequency"]`` in Hz; version 2 stores it
    in MHz (matching the control entity and the setup field). ``async_load``
    invokes this migrator when the on-disk version is older than
    :data:`SDR_STORE_VERSION`, so an existing managed hub's frequency converts
    transparently on the next load.
    """

    async def _async_migrate_func(
        self,
        old_major_version: int,
        old_minor_version: int,
        old_data: dict[str, Any],
    ) -> dict[str, Any]:
        """Convert a version-1 payload's center frequency from Hz to MHz."""
        if old_major_version < 2:
            values = old_data.get("values")
            if isinstance(values, dict):
                hz = values.get(KEY_CENTER_FREQUENCY)
                if isinstance(hz, (int, float)):
                    values[KEY_CENTER_FREQUENCY] = float(hz) / 1_000_000
        return old_data


# Backoff bounds for the reconnect loop (seconds). Starts at 1s, doubles on
# each consecutive failure, capped at 60s so the loop never spins hot.
_BACKOFF_MIN = 1.0
_BACKOFF_MAX = 60.0

# How often the availability watchdog evaluates last-seen vs effective timeout.
_WATCHDOG_INTERVAL = timedelta(seconds=30)

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

# Timeout (seconds) for the short-lived connection attempt used by the config
# flow's reachability check.
_VALIDATE_TIMEOUT = 10.0

# Timeout (seconds) for each one-shot ``/cmd`` getter HTTP request.
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


class Rtl433Coordinator:
    """Owns the WebSocket connection and runtime state for one rtl_433 hub.

    All state is scoped to a single config entry, so multiple hubs coexist.

    Public API:
        ``async_start()`` / ``async_stop()`` — lifecycle.
        ``validate_connection(...)`` — staticmethod used by the config flow.

    Injectable attributes (wired by the integration setup in ``__init__.py``):
        ``skip_keys``: ``set[str]`` of keys excluded from measurement fields.
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
        skip_keys: set[str] | frozenset[str] | None = None,
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
        self.skip_keys: set[str] = (
            set(skip_keys) if skip_keys is not None else set(DEFAULT_SKIP_KEYS)
        )
        # The coordinator reads raw ``time`` for the replay classification, so it
        # must never reach entities as an (unmatched) measurement field. The
        # library skip-keys (``_skip_keys.yaml``) do not list ``time``, so add it
        # here so ``normalize`` always drops it regardless of the injected set.
        self.skip_keys.add("time")

        # --- Injectable hooks (wired by the integration setup) ----------------
        self.new_device_callback: Callable[[str, str, bool], None] | None = None
        self.effective_timeout_resolver: Callable[[str], int | None] | None = None
        self.effective_clear_delay_resolver: Callable[[str], int] | None = None

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
        # event while discovery is on (Clarification #4).
        self.device_removers: list[Callable[[str], None]] = []

        # --- Runtime state, all scoped to this config entry ------------------
        self.devices: dict[str, NormalizedEvent] = {}
        self.last_seen: dict[str, datetime] = {}
        self.available: dict[str, bool] = {}
        self.seen_fields: set[str] = set()
        self.device_fields: dict[str, set[str]] = {}
        self.connected = False

        # High-water mark of the maximum event ``time`` (UTC) ever parsed, used
        # to classify each frame against the reconnect replay (see
        # ``_process_event``). Initially unset: a frame at or below it is an
        # already-seen replay; the cold-start case (mark unset) falls to the age
        # test in ``_process_event``. Spans reconnects so a brief blip's re-sent
        # buffer tail is recognised as already-seen and never re-fires.
        self._event_high_water: datetime | None = None

        # --- Hub-scoped runtime state (rendered by the hub entities) ---------
        # Latest meta/SDR configuration (assembled from the HTTP getters in Task 2)
        # and the latest server-stats payload. Populated over HTTP, not the socket.
        self.meta: dict[str, Any] = {}
        self.stats: dict[str, Any] = {}

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
        self._cmd_lock = asyncio.Lock()
        self._store: Store[dict[str, Any]] = _SdrStore(
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
                    backoff = _BACKOFF_MIN  # reset after a successful connect
                    self._emit_hub_update()
                    LOGGER.debug("rtl_433 connected to %s", self.ws_url)
                    # Seed SDR/meta config and stats over HTTP (never the socket).
                    # Each getter swallows its own failures, so a hidden ``/cmd``
                    # (e.g. behind a proxy) cannot break the connection.
                    await self._refresh_meta()
                    await self._refresh_stats()
                    # Adopt the server's settings on first connect, then replay
                    # the managed desired state on every (re)connect. Both are
                    # best-effort and swallow their own ``/cmd`` errors, but wrap
                    # here too so nothing can break the connection / event stream.
                    if self.manage_settings:
                        try:
                            if not self._desired:
                                await self._adopt_from_server()
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
                self._emit_hub_update()

            if self._stop_event.is_set():
                break

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
        """Route a parsed frame by shape (Plan: Frame Classification)."""
        is_event = event.get("model") is not None or any(
            event.get(key) is not None for key in _EVENT_IDENTITY_KEYS
        )
        if is_event:
            self._process_event(event)
        elif "shutdown" in event:
            self._handle_shutdown()
        # All other non-event frames (meta / state / result / error) are ignored:
        # #2/#3 are sourced over HTTP (Task 2), so nothing else needs handling here.

    def _handle_shutdown(self) -> None:
        """Handle a ``{"shutdown": ...}`` frame: flip connectivity off."""
        if self.connected:
            LOGGER.debug("rtl_433 server announced shutdown for %s", self.ws_url)
        self.connected = False
        self._emit_hub_update()

    def _emit_hub_update(self) -> None:
        """Notify hub entities that connectivity / meta / stats changed."""
        async_dispatcher_send(self.hass, signal_hub_update(self.entry.entry_id))

    # ------------------------------------------------------------------ #
    # HTTP ``/cmd`` getters (SDR/meta config + server stats)             #
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
                # rtl_433 may not set a strict ``application/json`` content-type
                # for scalar getters, so do not require one.
                return await resp.json(content_type=None)
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
    # Managed SDR settings: desired-state Store, write path, adoption,    #
    # and reconnect enforcement.                                          #
    # ------------------------------------------------------------------ #
    async def async_load_desired_state(self) -> None:
        """Load the persisted desired state, or wipe it when management is off.

        Called from :meth:`async_start` before the connect loop is created. When
        ``manage_settings`` is ``False`` the Store is removed so a later re-enable
        re-adopts the server's then-current settings from scratch; otherwise the
        ``{"values": ..., "managed": [...]}`` payload is loaded into
        ``self._desired`` / ``self._managed``.
        """
        if not self.manage_settings:
            await self._store.async_remove()  # re-enable will re-adopt
            self._desired, self._managed = {}, set()
            return
        data = await self._store.async_load() or {}
        self._desired = dict(data.get("values", {}))
        self._managed = set(data.get("managed", []))

    async def _persist_desired(self) -> None:
        """Persist the desired-state map + managed-set to the per-hub Store."""
        await self._store.async_save(
            {"values": self._desired, "managed": sorted(self._managed)}
        )

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

    def _command_args(self, key: str) -> tuple[str, int | None, str | None] | None:
        """Build the ``(command, val, arg)`` to send for one managed field.

        Returns ``None`` when the field has no desired value to send. Gain is two
        desired keys (``gain`` dB + ``gain_auto`` bool) but one ``gain`` command,
        so its ``arg`` is composed from the *combined* desired state via
        :func:`gain_command_arg`; both gain keys resolve to the same command and
        the caller is responsible for emitting it only once.
        """
        if key in (KEY_GAIN_DB, KEY_GAIN_AUTO):
            arg = gain_command_arg(
                self._desired.get(KEY_GAIN_DB),
                bool(self._desired.get(KEY_GAIN_AUTO, False)),
            )
            return ("gain", None, arg)
        setting = SDR_SETTINGS_BY_KEY.get(key)
        if setting is None or key not in self._desired:
            return None
        sent = setting.to_command(self._desired[key])
        if setting.arg_kind == "val":
            return (setting.command, int(sent), None)
        return (setting.command, None, str(sent))

    async def set_sdr(self, field: str, value: Any) -> None:
        """Write a desired SDR value: persist it, then enforce it if connected.

        Updates ``self._desired[field]``, marks the field managed, and persists
        the Store first so the intent survives a restart even if the live send
        fails. If connected, the mapped setter command is issued (under the lock)
        and ``self.meta`` is reconciled via a best-effort ``_refresh_meta``; a
        ``/cmd`` failure is swallowed and **leaves the desired value intact**.
        """
        self._desired[field] = value
        self._managed.add(field)
        await self._persist_desired()
        if self.connected:
            await self._enforce_field(field)

    async def _enforce_field(self, field: str) -> None:
        """Send one managed field's command, then read it back (best effort)."""
        args = self._command_args(field)
        if args is None:
            return
        command, val, arg = args
        await self._send_cmd(command, val=val, arg=arg)
        # Reconcile self.meta from the server; swallow its own failures.
        await self._refresh_meta()

    async def _adopt_from_server(self) -> None:
        """Seed the desired state from the server's current ``self.meta``.

        Runs once (when ``_desired`` is empty) on first connect. If the getters
        produced nothing (``self.meta`` empty / ``/cmd`` hidden behind a proxy)
        we adopt nothing and leave the Store empty — the reachability repair on
        the getter path already surfaces that; we never raise here. Guard: skip
        ``center_frequency`` while the server is in hop mode
        (``len(frequencies) > 1``) so HA does not pin a single frequency. The
        gain pair is seeded explicitly: ``gain_auto`` from ``gain == ""`` and the
        ``gain`` dB value parsed from the gain string when not auto.
        """
        meta = self.meta
        if not meta:  # /cmd unreachable -> adopt nothing
            return
        hopping = len(meta.get("frequencies", []) or []) > 1
        for setting in SDR_SETTINGS:
            if setting.key in (KEY_GAIN_DB, KEY_GAIN_AUTO):
                continue  # handled as a pair below
            if setting.key == "center_frequency" and hopping:
                continue
            current = setting.read(meta)
            if current is None:
                continue
            self._desired[setting.key] = current
            self._managed.add(setting.key)

        # Gain pair: only adopt when the server reported a gain string at all.
        if "gain" in meta:
            gain = meta["gain"]
            auto = gain == ""
            self._desired[KEY_GAIN_AUTO] = auto
            self._managed.add(KEY_GAIN_AUTO)
            if not auto:
                try:
                    self._desired[KEY_GAIN_DB] = float(gain)
                    self._managed.add(KEY_GAIN_DB)
                except TypeError, ValueError:
                    pass

        await self._persist_desired()

    async def _enforce_all(self) -> None:
        """Replay every managed field's setter command (gain emitted once).

        Best-effort: each send swallows its own ``/cmd`` errors and never raises,
        so a reconnect replay cannot disturb the connect loop or the event
        stream. The gain pair shares one ``gain`` command, so it is composed from
        the combined desired values and emitted exactly once.
        """
        gain_managed = bool(self._managed & {KEY_GAIN_DB, KEY_GAIN_AUTO})
        for key in sorted(self._managed):
            if key in (KEY_GAIN_DB, KEY_GAIN_AUTO):
                continue  # emitted once below
            args = self._command_args(key)
            if args is None:
                continue
            command, val, arg = args
            await self._send_cmd(command, val=val, arg=arg)
        if gain_managed:
            arg = gain_command_arg(
                self._desired.get(KEY_GAIN_DB),
                bool(self._desired.get(KEY_GAIN_AUTO, False)),
            )
            await self._send_cmd("gain", arg=arg)

    # ------------------------------------------------------------------ #
    # Public read API for the control entities                           #
    # ------------------------------------------------------------------ #
    def get_desired(self, field: str) -> Any:
        """Return the current desired value for a field (``None`` if unset)."""
        return self._desired.get(field)

    def is_managed(self, field: str) -> bool:
        """Return whether Home Assistant is actively managing a field."""
        return field in self._managed

    async def clear_desired_state(self) -> None:
        """Drop all desired state and remove the Store (management turned off)."""
        self._desired, self._managed = {}, set()
        await self._store.async_remove()

    # ------------------------------------------------------------------ #
    # Event handling                                                     #
    # ------------------------------------------------------------------ #
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
        ``event`` entities or refresh ``last_seen`` / ``available`` (see the
        plan's two-signal classification): the high-water mark catches an
        already-seen frame and the event age catches an unseen-but-old gap event.
        """
        normalized = normalize(event, self.skip_keys)
        key = normalized.device_key

        is_new = key not in self.devices

        now = dt_util.utcnow()

        # Read the raw ``time`` independently of ``normalize`` (which drops it)
        # and classify the frame into live / already-seen replay / stale gap.
        event_time = self._parse_event_time(event.get("time"))
        if event_time is None:
            # No usable timestamp -> treat as live ("never drop a real one").
            is_replay = False
        elif (
            self._event_high_water is not None and event_time <= self._event_high_water
        ):
            # At or below the high-water mark -> an already-seen replay (catches
            # the re-sent buffer tail on a brief blip; never re-fires).
            is_replay = True
        elif now - event_time > REPLAY_STALE_THRESHOLD:
            # Newer than the mark (HA never saw it) but old -> a stale gap event
            # that occurred while disconnected. Advance the mark so it is not
            # reconsidered, but do not treat it as live.
            is_replay = True
            self._event_high_water = event_time
        else:
            # Newer than the mark and recent -> a genuine live transmission.
            is_replay = False
            self._event_high_water = event_time

        # Carry the classification on the event object (the dispatch carrier), so
        # every ``_handle_dispatch`` sees a consistent flag with no signature
        # churn and the event platform can log a suppressed transmission's age.
        normalized = dataclasses.replace(
            normalized, is_replay=is_replay, event_time=event_time
        )
        self.devices[key] = normalized

        # Track observed field keys for diagnostics (surfaced as unmatched keys).
        # Done for every outcome so a replay-discovered device's sensors can seed.
        field_keys = set(normalized.fields)
        self.seen_fields |= field_keys
        self.device_fields.setdefault(key, set()).update(field_keys)

        # Only a live frame refreshes liveness; replays / stale gap events leave
        # ``last_seen`` / ``available`` alone so a genuinely-offline device is not
        # resurrected by the reconnect replay.
        was_available = self.available.get(key)
        if not is_replay:
            self.last_seen[key] = now
            self.available[key] = True

        # The new-device callback still fires for a replay-discovered device so
        # its entities exist and can seed; its availability stays governed by
        # liveness (it reads unavailable until a live frame arrives). The
        # ``is_replay`` flag lets the callback wire up the device without raising
        # a "new device" notification for a reconnect re-broadcast (a replay is
        # never a genuine first-time live discovery).
        if is_new and self.discovery_enabled and self.new_device_callback is not None:
            try:
                self.new_device_callback(key, normalized.model, is_replay)
            except Exception:  # noqa: BLE001 - a bad hook must not kill the loop
                LOGGER.exception("rtl_433 new_device_callback failed for %s", key)

        self._dispatch(key, normalized)

        if not is_replay and was_available is False:
            LOGGER.debug("rtl_433 device %s back online", key)

    def _dispatch(
        self,
        device_key: str,
        normalized: NormalizedEvent,
        *,
        is_replay: bool | None = None,
    ) -> None:
        """Fan a normalized event out to the device's entities.

        The replay flag travels on the ``NormalizedEvent`` itself, so the normal
        ``_process_event`` path passes ``is_replay=None`` (the default) to honor
        whatever flag the classification stamped on the object. An explicit
        ``is_replay`` overrides it: the watchdog re-dispatch of a *cached* event
        passes ``is_replay=False`` so its unavailable re-paint is never suppressed
        as a replay even when the cached object happened to be a replay frame.
        Identity is preserved when no rebuild is needed, so the event entity's
        object-identity dedupe of the watchdog re-paint still holds.
        """
        if is_replay is not None and normalized.is_replay != is_replay:
            normalized = dataclasses.replace(normalized, is_replay=is_replay)
        async_dispatcher_send(
            self.hass,
            signal_device_update(self.entry.entry_id, device_key),
            normalized,
        )

    # ------------------------------------------------------------------ #
    # Availability watchdog                                              #
    # ------------------------------------------------------------------ #
    def _class_default_timeout(self, device_key: str) -> int:
        """Return the device-class default timeout from the latest payload.

        The classifier reads the device's last normalized measurement fields
        (``self.devices[device_key].fields`` — the rtl_433 payload with identity
        and skip-keys removed; the event-driven open/close/motion keys are
        measurement fields and survive there). An event-driven device gets the
        longer event default, everything else the periodic default. A device not
        yet seen falls back to the periodic default.
        """
        normalized = self.devices.get(device_key)
        payload = normalized.fields if normalized is not None else None
        return class_default_timeout(payload)

    def _effective_timeout(self, device_key: str) -> int:
        """Resolve the effective timeout for a device.

        Resolution order: per-device override → explicit hub default → device-class
        default (from the latest payload) → ``DEFAULT_AVAILABILITY_TIMEOUT``. The
        resolver returns a concrete int for the two explicit tiers (including
        ``0`` = never-expire) or ``None`` when neither is set, in which case the
        device-class default applies.
        """
        if self.effective_timeout_resolver is not None:
            try:
                resolved = self.effective_timeout_resolver(device_key)
            except Exception:  # noqa: BLE001 - fall back to the class default
                LOGGER.exception(
                    "rtl_433 effective_timeout_resolver failed for %s", device_key
                )
            else:
                if resolved is not None:
                    return resolved
                return self._class_default_timeout(device_key)
        return self.availability_timeout

    async def _async_watchdog(self, _now: datetime) -> None:
        """Mark devices unavailable when their last-seen exceeds the timeout."""
        now = dt_util.utcnow()
        for device_key, seen in list(self.last_seen.items()):
            timeout = self._effective_timeout(device_key)
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
                    # A watchdog re-paint of the cached event is not a replay; it
                    # must not be suppressed or the unavailable-repaint breaks.
                    self._dispatch(device_key, normalized, is_replay=False)

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
