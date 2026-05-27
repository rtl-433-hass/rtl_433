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
- ``new_device_callback`` — called with ``(device_key, model)`` the first time an
  unknown device is seen while discovery is enabled. The integration setup wires this to the
  discovery flow; the coordinator never imports the config flow.
- ``effective_timeout_resolver`` — called with ``device_key`` to resolve the
  per-device availability timeout (override → hub default). The integration setup wires this;
  the fallback is the hub default.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import contextlib
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
    signal_device_update,
    signal_hub_update,
)
from ..normalizer import DEFAULT_SKIP_KEYS, NormalizedEvent, normalize

# Backoff bounds for the reconnect loop (seconds). Starts at 1s, doubles on
# each consecutive failure, capped at 60s so the loop never spins hot.
_BACKOFF_MIN = 1.0
_BACKOFF_MAX = 60.0

# How often the availability watchdog evaluates last-seen vs effective timeout.
_WATCHDOG_INTERVAL = timedelta(seconds=30)

# Timeout (seconds) for the short-lived connection attempt used by the config
# flow's reachability check.
_VALIDATE_TIMEOUT = 10.0

# Timeout (seconds) for each one-shot ``/cmd`` getter HTTP request.
_GETTER_TIMEOUT = 10.0

# How often ``get_stats`` is re-fetched over HTTP while connected, so the hub's
# throughput stays live without depending on the streaming socket.
_STATS_REFRESH_INTERVAL = timedelta(seconds=60)

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
        ``new_device_callback``: ``Callable[[str, str], None] | None``.
        ``effective_timeout_resolver``: ``Callable[[str], int] | None``.

    Runtime state (read by ``diagnostics.py``):
        ``devices``: ``dict[str, NormalizedEvent]`` last event per device key.
        ``last_seen``: ``dict[str, datetime]`` last-seen (UTC) per device key.
        ``available``: ``dict[str, bool]`` current availability per device key.
        ``seen_fields``: ``set[str]`` every measurement field key ever observed.
        ``device_fields``: ``dict[str, set[str]]`` field keys seen per device.
        ``connected``: ``bool`` whether the socket is currently open.
        ``meta``: ``dict[str, Any]`` latest SDR/meta configuration (HTTP-sourced).
        ``stats``: ``dict[str, Any]`` latest server-stats payload (HTTP-sourced).
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
        self.availability_timeout = availability_timeout
        self.skip_keys: set[str] = (
            set(skip_keys) if skip_keys is not None else set(DEFAULT_SKIP_KEYS)
        )

        # --- Injectable hooks (wired by the integration setup) ----------------
        self.new_device_callback: Callable[[str, str], None] | None = None
        self.effective_timeout_resolver: Callable[[str], int] | None = None

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

        # --- Hub-scoped runtime state (rendered by the hub entities) ---------
        # Latest meta/SDR configuration (assembled from the HTTP getters in Task 2)
        # and the latest server-stats payload. Populated over HTTP, not the socket.
        self.meta: dict[str, Any] = {}
        self.stats: dict[str, Any] = {}

        # --- Internal lifecycle handles --------------------------------------
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._watchdog_unsub: Callable[[], None] | None = None
        self._stats_unsub: Callable[[], None] | None = None
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
        self._stats_unsub = async_track_time_interval(
            self.hass,
            self._async_stats_tick,
            _STATS_REFRESH_INTERVAL,
            name=f"rtl_433 stats {self.entry.entry_id}",
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

        if self._stats_unsub is not None:
            self._stats_unsub()
            self._stats_unsub = None

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

        Scalar getters (``get_gain``/``get_ppm_error``) are wrapped in a
        ``result`` envelope over the shared command dispatcher, but a bare scalar
        is accepted too — read defensively.
        """
        if isinstance(payload, dict) and "result" in payload:
            return payload["result"]
        return payload

    async def _refresh_meta(self) -> None:
        """Fetch ``get_meta`` + ``get_gain`` + ``get_ppm_error`` into ``self.meta``."""
        meta = await self._fetch_cmd("get_meta")
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
        stats = await self._fetch_cmd("get_stats")
        if isinstance(stats, dict):
            self.stats = stats
            self._emit_hub_update()

    async def _async_stats_tick(self, _now: datetime) -> None:
        """Re-fetch ``get_stats`` on the interval, only while connected."""
        if self.connected:
            await self._refresh_stats()

    # ------------------------------------------------------------------ #
    # Event handling                                                     #
    # ------------------------------------------------------------------ #
    def _process_event(self, event: dict[str, Any]) -> None:
        """Normalize an event, update state, and dispatch to entities."""
        normalized = normalize(event, self.skip_keys)
        key = normalized.device_key

        is_new = key not in self.devices

        now = dt_util.utcnow()
        self.devices[key] = normalized
        self.last_seen[key] = now

        # Track observed field keys for diagnostics (surfaced as unmatched keys).
        field_keys = set(normalized.fields)
        self.seen_fields |= field_keys
        self.device_fields.setdefault(key, set()).update(field_keys)

        # A device with a fresh event is available again.
        was_available = self.available.get(key)
        self.available[key] = True

        if is_new and self.discovery_enabled and self.new_device_callback is not None:
            try:
                self.new_device_callback(key, normalized.model)
            except Exception:  # noqa: BLE001 - a bad hook must not kill the loop
                LOGGER.exception("rtl_433 new_device_callback failed for %s", key)

        self._dispatch(key, normalized)

        if was_available is False:
            LOGGER.debug("rtl_433 device %s back online", key)

    def _dispatch(self, device_key: str, normalized: NormalizedEvent) -> None:
        """Fan a normalized event out to the device's entities."""
        async_dispatcher_send(
            self.hass,
            signal_device_update(self.entry.entry_id, device_key),
            normalized,
        )

    # ------------------------------------------------------------------ #
    # Availability watchdog                                              #
    # ------------------------------------------------------------------ #
    def _effective_timeout(self, device_key: str) -> int:
        """Resolve the effective timeout for a device (override → hub default)."""
        if self.effective_timeout_resolver is not None:
            try:
                return self.effective_timeout_resolver(device_key)
            except Exception:  # noqa: BLE001 - fall back to the hub default
                LOGGER.exception(
                    "rtl_433 effective_timeout_resolver failed for %s", device_key
                )
        return self.availability_timeout

    async def _async_watchdog(self, _now: datetime) -> None:
        """Mark devices unavailable when their last-seen exceeds the timeout."""
        now = dt_util.utcnow()
        for device_key, seen in list(self.last_seen.items()):
            timeout = self._effective_timeout(device_key)
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
                    self._dispatch(device_key, normalized)

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
