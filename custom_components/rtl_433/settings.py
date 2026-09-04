"""Shared builders for the three settings a hub's owner can edit.

The hub's availability timeout and manage-settings toggle, a device's timeout
override / calibration / motion clear-delay, and the hub's device-library
mapping overrides are each edited from two places now -- the panel's settings
pages (over :mod:`.websocket_api`) and the options flow (:mod:`.options_flow`)
-- and the two must not drift.

Keeping the forms themselves in step is easy. What drifts is the set of rules
about what a submitted value *means*: when a value is stored, when it is dropped
instead, and which of ``entry.data`` / ``entry.options`` it goes in. None of
those is obvious from the code that calls these builders, so they are written
down here:

- A hub timeout of ``None`` means "use the per-device-type defaults" and is
  dropped rather than stored; every ``int`` is a deliberate choice and is stored
  as given. Storing a value where the user meant "defaults" would mask the
  per-device-class defaults and expire event-driven devices -- a doorbell that
  has not rung in ten minutes is not unavailable.
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
    DEVICE_CALIBRATION,
    DEVICE_FIELDS,
    DEVICE_MOTION_CLEAR_DELAY,
    DEVICE_TIMEOUT_OVERRIDE,
    DOMAIN,
)
from .hub_settings import _explicit_hub_timeout, _hub_manage_settings

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

    These come straight from the resolvers :mod:`.hub_settings` uses at runtime,
    rather than from a second copy of the options-then-data-then-default rule --
    including the int/bool coercion, so a value stored as a string still reaches
    a form as a number.

    The timeout reported is the *explicit* one, so ``None`` when the hub has none
    and the per-device-type defaults apply. A form that was handed the resolved
    value instead could not tell "unset" from a hub timeout that happens to equal
    :data:`~.const.DEFAULT_AVAILABILITY_TIMEOUT`, and the two behave differently:
    the first leaves a doorbell never expiring, the second expires it after ten
    minutes. A form wanting a number to pre-fill applies that default itself.
    """
    return {
        CONF_AVAILABILITY_TIMEOUT: _explicit_hub_timeout(entry),
        CONF_MANAGE_SETTINGS: _hub_manage_settings(entry),
    }


def build_hub_options(
    entry: ConfigEntry, availability_timeout: int | None, manage_settings: bool
) -> dict[str, Any]:
    """Return the options a hub-settings submission should persist.

    ``None`` means "use the per-device-type defaults", so the key is dropped
    instead of stored; every ``int`` is a value the user chose -- ``0`` (never
    expire) and :data:`~.const.DEFAULT_AVAILABILITY_TIMEOUT` alike -- and is
    stored as given.

    Intent, not the value, is what decides: a caller whose form cannot express
    "unset" collapses its own sentinel to ``None`` before calling (the options
    flow does, since a ``vol.Required`` number field echoes its default back on
    every save). Deciding it here instead is what made a hub-wide 600 seconds
    unstorable -- the one value a ten-minute default makes it natural to type.

    Everything else already in ``entry.options`` carries over: the hub form owns
    two keys, and the per-device sub-map lives alongside them.
    """
    options = dict(entry.options)
    options[CONF_MANAGE_SETTINGS] = manage_settings
    if availability_timeout is None:
        options.pop(CONF_AVAILABILITY_TIMEOUT, None)
    else:
        options[CONF_AVAILABILITY_TIMEOUT] = availability_timeout
    return options


def entry_registry(hass: HomeAssistant, entry: ConfigEntry) -> Registry | None:
    """Return this hub's merged device-library registry, cached at setup.

    ``None`` while the hub is still loading, which callers read as "cannot tell
    yet" rather than as "no": the motion test below is one such caller, and its
    knob then simply does not appear. The WebSocket reading preview is the
    other.
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
