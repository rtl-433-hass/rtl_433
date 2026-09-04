"""Shared builders for the three settings a hub's owner can edit.

The hub's availability timeout and manage-settings toggle, a device's timeout
override / calibration / motion clear-delay, and the hub's device-library
mapping overrides are each edited from two places now -- the discovery panel's
dialogs (over :mod:`.websocket_api`) and the options flow (:mod:`.options_flow`)
-- and the two must not drift. What drifts is never the form: it is the
*sentinel* rules sitting behind it, every one of which exists for a reason that
is invisible at the call site.

- A hub timeout equal to :data:`~.const.DEFAULT_AVAILABILITY_TIMEOUT` is dropped
  rather than stored, because storing it as an explicit hub-wide timeout would
  mask the per-device-class defaults and expire event-driven devices -- a
  doorbell that has not rung in ten minutes is not unavailable.
- A device's timeout override, calibration and clear-delay each *clear* on a
  blank submission rather than persisting a zero or a default.
- The clear-delay lives in ``entry.options`` while the other two live in
  ``entry.data``; see :func:`build_device_options`.

So this module owns the rules and returns **plain dicts**; it performs no
writes. That split is what lets the options flow keep its own persistence
semantics -- ``async_create_entry`` *is* the options write, and calling
``async_update_entry`` as well would fire the update listener twice and reload
the hub twice -- while the WebSocket path, which has no flow to finish, writes
data and options in a single ``async_update_entry``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pyrtl_433.library import lookup, normalize_overrides

from .calibration import commodity_from_fields, normalize_calibration
from .const import (
    CALIBRATION_COMMODITY,
    COMMODITY_NONE,
    CONF_AVAILABILITY_TIMEOUT,
    CONF_DEVICES,
    CONF_MANAGE_SETTINGS,
    CONF_MODEL,
    CONF_USER_MAPPINGS,
    DATA_ENTRY_LIBRARY,
    DEFAULT_AVAILABILITY_TIMEOUT,
    DEFAULT_MANAGE_SETTINGS,
    DEVICE_CALIBRATION,
    DEVICE_FIELDS,
    DEVICE_MOTION_CLEAR_DELAY,
    DEVICE_TIMEOUT_OVERRIDE,
    DOMAIN,
)

if TYPE_CHECKING:
    from pyrtl_433.library import Registry

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

# Documentation link for the device-mappings editor. Passed to the caller as a
# value rather than written into a translation string, because hassfest forbids
# literal URLs in those.
MAPPINGS_DOCS_URL = (
    "https://github.com/rtl-433-hass/rtl_433#device-library-and-user-overrides"
)


def hub_defaults(entry: ConfigEntry) -> dict[str, Any]:
    """Return the hub-level form's current values.

    Options override data, then the shipped default -- the same precedence
    :mod:`.hub_settings` resolves at runtime, applied here so the form opens
    showing what the hub is actually doing rather than what was last typed.
    """
    return {
        CONF_AVAILABILITY_TIMEOUT: entry.options.get(
            CONF_AVAILABILITY_TIMEOUT,
            entry.data.get(CONF_AVAILABILITY_TIMEOUT, DEFAULT_AVAILABILITY_TIMEOUT),
        ),
        CONF_MANAGE_SETTINGS: entry.options.get(
            CONF_MANAGE_SETTINGS,
            entry.data.get(CONF_MANAGE_SETTINGS, DEFAULT_MANAGE_SETTINGS),
        ),
    }


def build_hub_options(
    entry: ConfigEntry, availability_timeout: int, manage_settings: bool
) -> dict[str, Any]:
    """Return the options a hub-settings submission should persist.

    A submitted timeout equal to the plain default means "use the
    per-device-type defaults", so the key is dropped instead of stored; any
    deliberately chosen value -- ``0`` (never expire) included -- is kept. This
    mirrors the one-time migration that strips the same sentinel from older
    entries, and is what stops an entry from re-acquiring it every time somebody
    opens the form and saves without touching anything.

    Everything else already in ``entry.options`` carries over: the hub form owns
    two keys, and the per-device sub-map lives alongside them.
    """
    options = dict(entry.options)
    options[CONF_MANAGE_SETTINGS] = manage_settings
    if availability_timeout == DEFAULT_AVAILABILITY_TIMEOUT:
        options.pop(CONF_AVAILABILITY_TIMEOUT, None)
    else:
        options[CONF_AVAILABILITY_TIMEOUT] = availability_timeout
    return options


def entry_registry(hass: HomeAssistant, entry: ConfigEntry) -> Registry | None:
    """Return this hub's merged device-library registry, cached at setup.

    ``None`` while the hub is still loading, which callers read as "cannot tell
    yet" rather than as "no": the only thing that consults it is the motion test
    below, whose knob then simply does not appear.
    """
    return (
        hass.data.get(DOMAIN, {})
        .get(DATA_ENTRY_LIBRARY, {})
        .get(entry.entry_id, (None, None))[0]
    )


def commodity_hint(hass: HomeAssistant, entry: ConfigEntry, device_key: str) -> str:
    """Best-effort commodity from the device's most recent decoded event.

    Reads the running coordinator's last ``NormalizedEvent`` for the device and
    derives a hint from its ``MeterType`` / ``ert_type`` fields. Every hop is
    guarded -- a missing coordinator, event or field falls back to ``none`` --
    because this decorates a form and must never be the reason one fails to
    render.
    """
    coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    event = getattr(coordinator, "devices", {}).get(device_key)
    return commodity_from_fields(getattr(event, "fields", None))


def is_motion_bearing(hass: HomeAssistant, entry: ConfigEntry, device_key: str) -> bool:
    """Return ``True`` when the device has a field that auto-clears.

    A device is motion-bearing iff any observed field resolves (model-scoped) to
    a descriptor carrying a truthy ``clear_delay`` -- a motion/event
    binary_sensor that turns itself back off. Only those devices are offered the
    per-device clear-delay knob, because anywhere else it would be a control
    with nothing behind it.
    """
    record = entry.data.get(CONF_DEVICES, {}).get(device_key, {})
    model = record.get(CONF_MODEL)
    registry = entry_registry(hass, entry)
    return any(
        (descriptor := lookup(field_key, model, registry)) is not None
        and descriptor.clear_delay
        for field_key in record.get(DEVICE_FIELDS, [])
    )


def device_label(
    hass: HomeAssistant, entry: ConfigEntry, device_key: str, record: dict[str, Any]
) -> str:
    """Name a device for a picker, annotated with any detected commodity.

    The hint is surfaced *in the picker* so a user with several meters can see
    that one is already recognized as gas or water before choosing it; the
    per-device calibration is otherwise easy to walk straight past.
    """
    label = f"{record.get(CONF_MODEL, device_key)} ({device_key})"
    commodity = commodity_hint(hass, entry, device_key)
    if commodity != COMMODITY_NONE:
        label = f"{label} — {commodity} detected"
    return label


def device_clear_delay(entry: ConfigEntry, device_key: str) -> int | None:
    """Return the device's persisted motion clear-delay override, or ``None``.

    Read from ``entry.options`` first and ``entry.data`` second, because the two
    are written by different eras of this integration: the migration from
    per-device child entries lands the value in ``data``, and every edit since
    has written it to ``options``. Options win for the same reason they win
    everywhere else here -- they are the later, user-made choice.
    """
    from_options = (
        entry.options.get(CONF_DEVICES, {})
        .get(device_key, {})
        .get(DEVICE_MOTION_CLEAR_DELAY)
    )
    if from_options is not None:
        return int(from_options)
    from_data = (
        entry.data.get(CONF_DEVICES, {})
        .get(device_key, {})
        .get(DEVICE_MOTION_CLEAR_DELAY)
    )
    return None if from_data is None else int(from_data)


def device_defaults(
    hass: HomeAssistant, entry: ConfigEntry, device_key: str
) -> dict[str, Any]:
    """Return everything a device-settings form needs to render one device.

    The commodity pre-fill comes from the device's stored calibration when it has
    one and from its decoded meter hint when it does not, which is the order that
    matters: a user who has already said it is a gas meter should not have to say
    so again, and one who has not should find the guess waiting.
    """
    record: dict[str, Any] = entry.data.get(CONF_DEVICES, {}).get(device_key, {})
    existing = normalize_calibration(record.get(DEVICE_CALIBRATION))
    return {
        "device_key": device_key,
        "label": device_label(hass, entry, device_key, record),
        "model": record.get(CONF_MODEL),
        DEVICE_TIMEOUT_OVERRIDE: record.get(DEVICE_TIMEOUT_OVERRIDE),
        DEVICE_MOTION_CLEAR_DELAY: device_clear_delay(entry, device_key),
        "motion": is_motion_bearing(hass, entry, device_key),
        "calibration": existing,
        "commodity": (
            existing[CALIBRATION_COMMODITY]
            if existing is not None
            else commodity_hint(hass, entry, device_key)
        ),
    }


def build_device_data(
    entry: ConfigEntry,
    device_key: str,
    *,
    override: int | None,
    calibration: dict[str, Any] | None,
) -> dict[str, Any]:
    """Return ``entry.data`` with this device's override + calibration applied.

    ``None`` clears rather than stores in both cases: a blank timeout falls back
    to the hub default, and a cleared calibration falls back to the library
    descriptor. Every level is copied, so the caller's ``async_update_entry`` is
    handed a genuinely new mapping and the nested dicts are not shared with the
    live entry.

    Saving also drops any clear-delay left in ``data`` by the migration from
    per-device entries. That value is only ever a leftover -- every edit writes
    the delay to ``options`` (see :func:`build_device_options`), and
    :func:`device_clear_delay` reads options first and falls back to data. Leave
    the leftover in place and a user on a migrated hub could never clear the
    delay: blanking the field empties options, the read falls back to data, and
    the old number reappears. Retiring it here makes options the only copy from
    the first save onwards.

    Both builders are always called together for one ``async_update_entry``, so
    the delay is written to options in the same breath this drops it from data.
    """
    data = dict(entry.data)
    devices = dict(data.get(CONF_DEVICES, {}))
    record = dict(devices.get(device_key, {}))
    if override is None:
        record.pop(DEVICE_TIMEOUT_OVERRIDE, None)
    else:
        record[DEVICE_TIMEOUT_OVERRIDE] = override
    if calibration is None:
        record.pop(DEVICE_CALIBRATION, None)
    else:
        record[DEVICE_CALIBRATION] = calibration
    record.pop(DEVICE_MOTION_CLEAR_DELAY, None)
    devices[device_key] = record
    data[CONF_DEVICES] = devices
    return data


def build_device_options(
    entry: ConfigEntry, device_key: str, *, motion_clear_delay: int | None
) -> dict[str, Any]:
    """Return ``entry.options`` with this device's clear-delay applied.

    The clear-delay is the one per-device knob kept in options rather than data,
    so it is written on its own path. A device whose sub-map empties is dropped
    from the map entirely rather than left as an empty dict, which keeps a hub
    that has never overridden anything from accumulating one entry per device.
    """
    options = dict(entry.options)
    devices = dict(options.get(CONF_DEVICES, {}))
    record = dict(devices.get(device_key, {}))
    if motion_clear_delay is None:
        record.pop(DEVICE_MOTION_CLEAR_DELAY, None)
    else:
        record[DEVICE_MOTION_CLEAR_DELAY] = motion_clear_delay
    if record:
        devices[device_key] = record
    else:
        devices.pop(device_key, None)
    options[CONF_DEVICES] = devices
    return options


def build_mappings_data(entry: ConfigEntry, raw: Any) -> dict[str, Any]:
    """Return ``entry.data`` with normalized mapping overrides applied.

    Validation stays the caller's: :func:`~pyrtl_433.library.validate_user_mappings`
    returns the problems a form has to show, and this is only reached once there
    are none.
    """
    return {**entry.data, CONF_USER_MAPPINGS: normalize_overrides(raw)}
