"""Constants for the rtl_433 integration.

This module is the single source of truth for keys and defaults shared across
the coordinator, config/options flow, entity platforms, and tests. Later tasks
(5/6/7/8/9/10) import from here, so names are intended to be stable.
"""

from __future__ import annotations

import logging
from typing import Final

from homeassistant.const import Platform

# Integration domain. Must match the "domain" key in manifest.json.
DOMAIN: Final = "rtl_433"

# Module-level logger; later modules use ``from .const import LOGGER``.
LOGGER: Final[logging.Logger] = logging.getLogger(__package__)

# Platforms forwarded for each per-device config entry.
PLATFORMS: Final[list[Platform]] = [Platform.SENSOR, Platform.BINARY_SENSOR]

# --- Config-entry "type" discriminator -------------------------------------
# A single value in entry.data tells the integration whether a config entry is
# the per-instance hub (owns the WebSocket connection) or a per-device entry
# (owns one physical device's entities). The integration setup branches on this in
# async_setup_entry.
CONF_ENTRY_TYPE: Final = "entry_type"
ENTRY_TYPE_HUB: Final = "hub"
ENTRY_TYPE_DEVICE: Final = "device"

# --- Hub config-entry keys --------------------------------------------------
# Connection target for one rtl_433 HTTP server's WebSocket endpoint.
CONF_HOST: Final = "host"
CONF_PORT: Final = "port"
CONF_PATH: Final = "path"
# Per-hub toggle for surfacing newly observed devices via discovery flow.
CONF_DISCOVERY_ENABLED: Final = "discovery_enabled"
# Effective "go unavailable after this many seconds of silence" window.
# Lives on the hub as the default and may be overridden per device.
CONF_AVAILABILITY_TIMEOUT: Final = "availability_timeout"

# --- Per-device config-entry keys ------------------------------------------
# entry id of the parent hub entry; enables cascade removal when a hub is
# deleted (a device entry records which hub it belongs to).
CONF_HUB_ENTRY_ID: Final = "hub_entry_id"
# Deterministic device identity derived from ``model`` plus the present subset
# of identity fields (id / channel / subtype). Used to scope unique_ids and
# dispatcher signals.
CONF_DEVICE_KEY: Final = "device_key"
# The rtl_433 ``model`` string for the device (e.g. "Acurite-606TXN").
CONF_MODEL: Final = "model"

# --- hass.data keys ---------------------------------------------------------
# Key under ``hass.data[DOMAIN]`` holding the once-loaded mapping library tuple
# ``(registry, skip_keys)``. The library is loaded in an executor during hub
# setup and shared by the coordinator, the entity platforms, and diagnostics so
# nothing re-reads the YAML files on the event loop.
DATA_LIBRARY: Final = "_library"

# --- Defaults ---------------------------------------------------------------
# Default rtl_433 HTTP server port (the documented "-F http" default).
DEFAULT_PORT: Final = 8433
# Default WebSocket path on the rtl_433 HTTP server.
DEFAULT_PATH: Final = "/ws"
# Default availability window in seconds. RF devices signal presence only by
# transmitting; 600 s (10 min) is a conservative default that tolerates slow
# reporters while still detecting genuinely offline devices. Configurable per
# hub and overridable per device, so this is not a magic constant elsewhere.
DEFAULT_AVAILABILITY_TIMEOUT: Final = 600

# --- Dispatcher signal -----------------------------------------------------
# Template for the per-device dispatcher signal. The coordinator sends and the
# entities subscribe using the same formatted key so updates fan out only to
# the device they belong to. Use ``signal_device_update(...)`` to format it.
SIGNAL_DEVICE_UPDATE: Final = "rtl_433_device_update_{hub_entry_id}_{device_key}"


def signal_device_update(hub_entry_id: str, device_key: str) -> str:
    """Return the dispatcher signal name for one device under one hub.

    Coordinator and entities must agree on this key, so both call this helper
    rather than formatting the template independently.
    """
    return SIGNAL_DEVICE_UPDATE.format(hub_entry_id=hub_entry_id, device_key=device_key)
