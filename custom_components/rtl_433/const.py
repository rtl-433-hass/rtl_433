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
PLATFORMS: Final[list[Platform]] = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.EVENT,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SWITCH,
]

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
# Per-hub toggle: let Home Assistant manage (adopt + enforce) the SDR settings.
# When on, the hub exposes number/select/switch controls and re-applies the
# stored desired state after a reconnect; when off, those controls are not
# created and the integration leaves the receiver's settings untouched.
CONF_MANAGE_SETTINGS: Final = "manage_settings"
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

# --- Hub devices-map keys ---------------------------------------------------
# Key under a hub entry's ``data`` holding the consolidated per-device map for
# that hub. Maps ``device_key`` -> a record with the device's model, the set of
# observed mapped field keys, and an optional per-device availability-timeout
# override. This map is the single source of truth for recreating nested devices
# and their entities on startup, for the dynamic-add listeners, the options-flow
# per-device override, and the 0.1.0 migration.
CONF_DEVICES: Final = "devices"
# Sub-keys inside one ``entry.data[CONF_DEVICES][device_key]`` record. The model
# reuses ``CONF_MODEL`` ("model"); the others are defined here.
DEVICE_FIELDS: Final = "fields"  # sorted list of observed mapped field keys
DEVICE_TIMEOUT_OVERRIDE: Final = "timeout_override"  # int seconds, or absent/None
DEVICE_MOTION_CLEAR_DELAY: Final = "motion_clear_delay"  # int seconds, or absent/None
# Maps ``{field_key: sorted list[str]}`` per device record: the event types
# observed for each event-platform field, persisted so the event entity's
# ``event_types`` survives a restart.
DEVICE_EVENT_TYPES: Final = "event_types"
# Per-device utility-meter calibration sub-record. Holds the user-supplied
# ``{commodity, unit, scale}`` triple that turns a unitless consumption counter
# into an Energy-dashboard-eligible sensor (a real device_class + a convertible
# base unit + ``total_increasing`` + a value scale). Absent (or commodity =
# ``none``) means the consumption field keeps its library/global descriptor.
DEVICE_CALIBRATION: Final = "calibration"
# Per-hub user mapping overrides. Holds the normalized override object (the same
# shape ``merge_overrides`` consumes: flat field entries, an optional ``models``
# block, and an optional ``skip_keys`` list) edited via the options flow and
# merged over the shipped library at setup.
CONF_USER_MAPPINGS: Final = "user_mappings"
# Sub-keys inside the calibration record.
CALIBRATION_COMMODITY: Final = "commodity"  # one of CALIBRATION_COMMODITIES
CALIBRATION_UNIT: Final = "unit"  # an HA-convertible unit for the commodity
CALIBRATION_SCALE: Final = "scale"  # float multiplier on the raw counter

# Commodity choices for the per-device calibration. ``none`` clears the
# calibration; the other three map to a sensor device_class.
COMMODITY_NONE: Final = "none"
COMMODITY_ENERGY: Final = "energy"
COMMODITY_GAS: Final = "gas"
COMMODITY_WATER: Final = "water"
CALIBRATION_COMMODITIES: Final[tuple[str, ...]] = (
    COMMODITY_NONE,
    COMMODITY_ENERGY,
    COMMODITY_GAS,
    COMMODITY_WATER,
)

# The known utility-meter consumption field keys a per-device calibration may be
# applied to (SCM/ERT ``consumption_data``; SCMplus ``consumption``). The
# calibration overlay (config_flow + entity) only touches these field keys, never
# arbitrary fields.
CONSUMPTION_FIELD_KEYS: Final[frozenset[str]] = frozenset(
    {"consumption", "consumption_data"}
)

# --- hass.data keys ---------------------------------------------------------
# Key under ``hass.data[DOMAIN]`` holding the once-loaded **shipped** mapping
# library tuple ``(registry, skip_keys)`` (no user overrides). The library is
# loaded in an executor during hub setup and shared across hubs so nothing
# re-reads the YAML files on the event loop. Per-hub user overrides are merged
# over this and cached separately under ``DATA_ENTRY_LIBRARY``.
DATA_LIBRARY: Final = "_library"
# Key under ``hass.data[DOMAIN]`` holding the per-entry merged library map
# ``{entry_id: (registry, skip_keys)}`` — the shipped library with that hub's
# stored ``CONF_USER_MAPPINGS`` overrides merged in. The coordinator, entity
# platforms, options flow, and diagnostics read their hub's entry here.
DATA_ENTRY_LIBRARY: Final = "_entry_library"

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
# Default seconds after which a motion/event binary_sensor auto-clears to "off"
# when no explicit clear signal arrives. Overridable per device.
DEFAULT_MOTION_CLEAR_DELAY: Final = 90
# Default for the per-hub manage-settings toggle. New hubs adopt and manage the
# SDR settings by default; users can opt out per hub via the options flow.
DEFAULT_MANAGE_SETTINGS: Final = True

# --- Managed-SDR desired-state Store ---------------------------------------
# The coordinator persists the desired SDR settings in a
# ``homeassistant.helpers.storage.Store`` keyed by the hub ``entry_id`` so a
# value change never churns the config entry. ``SDR_STORE_VERSION`` is the Store
# schema version; ``sdr_store_key`` builds the per-hub key.
SDR_STORE_VERSION: Final = 1


def sdr_store_key(entry_id: str) -> str:
    """Return the desired-state Store key for one hub entry."""
    return f"{DOMAIN}.sdr_{entry_id}"


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


# Hub-level "a new (previously unknown) device was observed" signal. The
# coordinator's new-device callback (wired in ``__init__.py``) dispatches this,
# gated by the per-hub discovery toggle; the entity platforms subscribe to it to
# create the nested device and its entities at runtime (the ``dynamic-devices``
# Quality Scale rule). Carries ``(device_key, model)``.
SIGNAL_NEW_DEVICE: Final = "rtl_433_new_device_{hub_entry_id}"


def signal_new_device(hub_entry_id: str) -> str:
    """Return the hub-level new-device dispatcher signal for one hub."""
    return SIGNAL_NEW_DEVICE.format(hub_entry_id=hub_entry_id)


# Hub-level "connectivity / SDR meta / server stats changed" signal. The
# coordinator dispatches this (no payload) whenever the hub's connection state,
# meta/SDR configuration, or server stats change; the statically-registered hub
# entities subscribe and re-read the coordinator's hub state.
SIGNAL_HUB_UPDATE: Final = "rtl_433_hub_update_{hub_entry_id}"


def signal_hub_update(hub_entry_id: str) -> str:
    """Return the hub-level update dispatcher signal for one hub."""
    return SIGNAL_HUB_UPDATE.format(hub_entry_id=hub_entry_id)
