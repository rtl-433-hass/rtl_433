"""Resolvers and runtime lookups for the receivers of a location config entry.

A **location** config entry holds one **receiver config subentry** per rtl_433
server. The connection target and the per-receiver radio settings live on the
subentry; everything that describes the location as a whole — the adopted
devices map, the ignore list, the user mappings, the availability defaults —
stays on the entry, because those are decisions about *sensors*, not about which
computer happened to decode them.

This module is where that split is written down exactly once:

* ``_receiver_*`` accessors take whichever of ``entry`` / ``subentry`` actually
  owns the value, so a caller cannot read a per-receiver setting off the wrong
  object;
* :func:`receiver_subentries` enumerates a location's receivers; and
* :func:`receiver_coordinators` / :func:`receiver_coordinator` resolve the
  running coordinators, one per receiver, from ``hass.data``.

``__init__`` (setup + the options-update listener) uses these to build and
reconfigure the coordinators; kept here so that wiring stays readable.
"""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.core import HomeAssistant

from .calibration import normalize_calibration
from .const import (
    CONF_AVAILABILITY_TIMEOUT,
    CONF_DEVICES,
    CONF_HOST,
    CONF_IGNORED_DEVICES,
    CONF_MANAGE_SETTINGS,
    CONF_PATH,
    CONF_PORT,
    DEFAULT_AVAILABILITY_TIMEOUT,
    DEFAULT_MANAGE_SETTINGS,
    DEVICE_CALIBRATION,
    DOMAIN,
    SUBENTRY_TYPE_RECEIVER,
)
from .coordinator import Rtl433Coordinator


# --------------------------------------------------------------------------- #
# Topology: a location's receivers, and their running coordinators.            #
# --------------------------------------------------------------------------- #
def receiver_subentries(entry: ConfigEntry) -> list[ConfigSubentry]:
    """Return a location entry's receiver subentries, in creation order.

    ``entry.subentries`` is a plain dict keyed by subentry id and Python
    preserves insertion order, so the first element is the location's first
    receiver — the one a single-receiver install has, and the one the flows
    treat as the default when an action needs a receiver but was given only a
    location.

    Filtered by ``subentry_type`` rather than returned wholesale so a future
    subentry type of another shape cannot be mistaken for a receiver.
    """
    return [
        subentry
        for subentry in entry.subentries.values()
        if subentry.subentry_type == SUBENTRY_TYPE_RECEIVER
    ]


def receiver_coordinators(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Rtl433Coordinator]:
    """Return this location's running coordinators, keyed by receiver id.

    One coordinator per receiver subentry: the WebSocket transport is per
    endpoint, so a location with two servers runs two of them. Receivers whose
    coordinator is not running (an entry mid-setup, or one that failed) are
    simply absent, so callers iterate over what exists rather than guarding each
    lookup.
    """
    domain_data = hass.data.get(DOMAIN, {})
    result: dict[str, Rtl433Coordinator] = {}
    for subentry in receiver_subentries(entry):
        coordinator = domain_data.get(subentry.subentry_id)
        if coordinator is not None:
            result[subentry.subentry_id] = coordinator
    return result


def running_coordinators(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Rtl433Coordinator]:
    """Return every coordinator this location *started*, keyed by receiver id.

    Unlike :func:`receiver_coordinators`, this is derived from what is running
    rather than from what is stored, so it still finds the coordinator of a
    receiver whose subentry has since been deleted. That difference is the whole
    point: a deleted receiver's socket has to be noticed so it can be stopped,
    and comparing the two sets is how the update listener sees a receiver come or
    go at all.
    """
    return {
        receiver_id: value
        for receiver_id, value in hass.data.get(DOMAIN, {}).items()
        if isinstance(value, Rtl433Coordinator)
        and value.entry.entry_id == entry.entry_id
    }


def receiver_coordinator(
    hass: HomeAssistant, entry: ConfigEntry, receiver_id: str | None = None
) -> Rtl433Coordinator | None:
    """Return one receiver's coordinator, or ``None`` when it is not running.

    ``receiver_id`` names the receiver subentry. Omitting it means "this
    location's first receiver", which is the honest answer for every surface
    that still speaks the one-server model: a single-receiver location has
    exactly one, and the multi-receiver surfaces name the receiver explicitly.
    """
    coordinators = receiver_coordinators(hass, entry)
    if receiver_id is not None:
        return coordinators.get(receiver_id)
    return next(iter(coordinators.values()), None)


# --------------------------------------------------------------------------- #
# Per-receiver settings (owned by the subentry).                               #
# --------------------------------------------------------------------------- #
def _receiver_secure(subentry: ConfigSubentry) -> bool:
    """Return the receiver's ``secure`` (wss) flag, defaulting to False."""
    return bool(subentry.data.get("secure", False))


def _receiver_manage_settings(
    entry: ConfigEntry, subentry: ConfigSubentry | None = None
) -> bool:
    """Resolve a receiver's manage-radio toggle (location options > receiver).

    The toggle belongs to the receiver — it decides whether *that* radio's
    settings are adopted and enforced — and is stored on the subentry by the add
    and reconfigure flows. The location's options are still consulted first
    because the options flow writes a location-wide value there; until that
    surface is re-scoped it remains the override, so an existing "manage
    settings: off" keeps meaning what it did.

    Omitting ``subentry`` asks for the location's first receiver, which is what a
    form that still shows one toggle for the whole location means by it.
    """
    if CONF_MANAGE_SETTINGS in entry.options:
        return bool(entry.options[CONF_MANAGE_SETTINGS])
    if subentry is None:
        first = receiver_subentries(entry)
        if not first:
            return DEFAULT_MANAGE_SETTINGS
        subentry = first[0]
    return bool(subentry.data.get(CONF_MANAGE_SETTINGS, DEFAULT_MANAGE_SETTINGS))


def _receiver_connection(subentry: ConfigSubentry) -> tuple[Any, ...]:
    """Return the receiver's connection target and stable identity, as a tuple.

    ``(host, port, path, secure, unique_id)`` — everything the coordinator's
    WebSocket connection is built from, plus the stable radio id a rebind
    re-points the receiver at. Captured as the coordinator's setup snapshot so
    the update listener can reload the location when a reconfigure / discovery /
    rebind writes a new target into the subentry: those flows deliberately do not
    reload the entry themselves, because Home Assistant forbids combining a
    config-entry update listener with the reloading flow helpers. Only ever
    compared for equality, so the raw stored values are returned as-is.
    """
    return (
        subentry.data.get(CONF_HOST),
        subentry.data.get(CONF_PORT),
        subentry.data.get(CONF_PATH),
        _receiver_secure(subentry),
        subentry.unique_id,
    )


# --------------------------------------------------------------------------- #
# Location-wide settings (owned by the entry).                                 #
# --------------------------------------------------------------------------- #
def _receiver_ignored_devices(entry: ConfigEntry) -> list[str]:
    """Return the location's persisted ignore list, as a list of device keys.

    Read from ``entry.data`` alone -- unlike the timeout and manage-settings
    resolvers there is no options-level override, because ignoring a device is
    not a setting the user tunes on a form but a record the approval surfaces
    append to. A copy is returned so a caller can append to it without mutating
    the entry's stored list in place.

    It lives on the location, not on a receiver: "I do not want my neighbour's
    sensor" is a statement about a sensor, and a second receiver that also hears
    it must not re-offer it.
    """
    return list(entry.data.get(CONF_IGNORED_DEVICES, []))


def _explicit_receiver_timeout(entry: ConfigEntry) -> int | None:
    """Return the location's *explicitly set* availability timeout, or ``None``.

    Unlike :func:`_receiver_availability_timeout`, this distinguishes "user set a
    default" from "unset" by testing membership (``in``) rather than ``.get`` with
    a default. ``None`` means no default was configured, letting the resolver
    fall through to the device-class default. An explicit ``0`` is a real value
    (never-expire) and is returned as ``0``, never treated as unset.
    """
    if CONF_AVAILABILITY_TIMEOUT in entry.options:
        return int(entry.options[CONF_AVAILABILITY_TIMEOUT])
    if CONF_AVAILABILITY_TIMEOUT in entry.data:
        return int(entry.data[CONF_AVAILABILITY_TIMEOUT])
    return None


def _receiver_availability_timeout(entry: ConfigEntry) -> int:
    """Resolve the location's default availability timeout (options > data > default)."""
    explicit = _explicit_receiver_timeout(entry)
    return DEFAULT_AVAILABILITY_TIMEOUT if explicit is None else explicit


def _calibration_map(entry: ConfigEntry) -> dict[str, dict]:
    """Build the per-device calibration map from the location's devices map.

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
