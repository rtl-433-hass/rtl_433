"""Resolvers for a hub config entry's effective settings.

Small pure accessors that read a hub ``ConfigEntry``'s data/options and apply the
"options override data, then default" precedence. ``__init__`` (setup + the
options-update listener) uses these to build and reconfigure the coordinator;
kept here so that wiring stays readable.
"""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry

from .calibration import normalize_calibration
from .const import (
    CONF_AVAILABILITY_TIMEOUT,
    CONF_DEVICES,
    CONF_HOST,
    CONF_MANAGE_SETTINGS,
    CONF_PATH,
    CONF_PORT,
    DEFAULT_AVAILABILITY_TIMEOUT,
    DEFAULT_MANAGE_SETTINGS,
    DEVICE_CALIBRATION,
)


def _hub_secure(entry: ConfigEntry) -> bool:
    """Return the hub entry's ``secure`` (wss) flag, defaulting to False."""
    return bool(entry.data.get("secure", False))


def _explicit_hub_timeout(entry: ConfigEntry) -> int | None:
    """Return the hub's *explicitly set* availability timeout, or ``None``.

    Unlike :func:`_hub_availability_timeout`, this distinguishes "user set a hub
    default" from "unset" by testing membership (``in``) rather than ``.get`` with
    a default. ``None`` means no hub default was configured, letting the resolver
    fall through to the device-class default. An explicit ``0`` is a real value
    (never-expire) and is returned as ``0``, never treated as unset.
    """
    if CONF_AVAILABILITY_TIMEOUT in entry.options:
        return int(entry.options[CONF_AVAILABILITY_TIMEOUT])
    if CONF_AVAILABILITY_TIMEOUT in entry.data:
        return int(entry.data[CONF_AVAILABILITY_TIMEOUT])
    return None


def _hub_availability_timeout(entry: ConfigEntry) -> int:
    """Resolve the hub's default availability timeout (options > data > default)."""
    explicit = _explicit_hub_timeout(entry)
    return DEFAULT_AVAILABILITY_TIMEOUT if explicit is None else explicit


def _hub_manage_settings(entry: ConfigEntry) -> bool:
    """Resolve the hub's manage-settings toggle (options > data > default)."""
    return bool(
        entry.options.get(
            CONF_MANAGE_SETTINGS,
            entry.data.get(CONF_MANAGE_SETTINGS, DEFAULT_MANAGE_SETTINGS),
        )
    )


def _calibration_map(entry: ConfigEntry) -> dict[str, dict]:
    """Build the per-device calibration map from the hub's devices map.

    Returns ``{device_key: {commodity, unit, scale}}`` for every device that
    carries a *valid* calibration (via :func:`normalize_calibration`, which drops
    a ``none``/unknown commodity or an out-of-range unit). Used both to capture
    the coordinator's setup snapshot and to detect a change in the update
    listener; comparing the normalized maps means only a real calibration change
    (never a routine devices-map upsert) is treated as a change.
    """
    result: dict[str, dict] = {}
    for device_key, record in entry.data.get(CONF_DEVICES, {}).items():
        if not isinstance(record, dict):
            continue
        calibration = normalize_calibration(record.get(DEVICE_CALIBRATION))
        if calibration is not None:
            result[device_key] = calibration
    return result


def _hub_connection(entry: ConfigEntry) -> tuple[Any, ...]:
    """Return the hub's connection target and stable identity, as a tuple.

    ``(host, port, path, secure, unique_id)`` — everything the coordinator's
    WebSocket connection is built from, plus the stable radio id a rebind
    re-points the entry at. Captured as the coordinator's setup snapshot so the
    update listener can reload the hub when a reconfigure / discovery / rebind
    writes a new target into ``entry.data``: those flows deliberately do not
    reload the entry themselves, because Home Assistant forbids combining a
    config-entry update listener with the reloading config-flow helpers. Only
    ever compared for equality, so the raw stored values are returned as-is.
    """
    return (
        entry.data.get(CONF_HOST),
        entry.data.get(CONF_PORT),
        entry.data.get(CONF_PATH),
        _hub_secure(entry),
        entry.unique_id,
    )
