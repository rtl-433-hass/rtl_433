"""Mutation-killing tests for custom_components/rtl_433/options_flow.py.

The flow tests in ``test_config_flow.py`` walk each options step down its happy
path: pick a device, submit a plausible value, assert what was stored. That
leaves the parts of a form a user only meets when something is *not* plausible
untested — the boundary a number selector accepts, what a submit with an empty
form does, which key an error lands under, the exact strings a picker renders,
and the defaults the form comes back pre-filled with. Those are the branches
this file pins, so a change to any of them fails here rather than reaching a
user's Home Assistant as a form that silently does the wrong thing.

Every test drives the real flow (or, for the pure label helpers, calls them
directly) against a ``MockConfigEntry`` that is added to hass but deliberately
not set up: the options flow only reads ``entry.data`` / ``entry.options`` and
``hass.data[DOMAIN][entry_id]``, so a stub coordinator is both faster and a
sharper instrument than a live hub.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from pyrtl_433.library import FieldDescriptor, Registry
from pyrtl_433.normalizer import NormalizedEvent
import pytest
import voluptuous as vol

from custom_components.rtl_433.calibration import COMMODITY_UNITS
from custom_components.rtl_433.const import (
    CALIBRATION_COMMODITIES,
    CALIBRATION_COMMODITY,
    CALIBRATION_SCALE,
    CALIBRATION_UNIT,
    COMMODITY_ENERGY,
    COMMODITY_GAS,
    COMMODITY_NONE,
    COMMODITY_WATER,
    CONF_AVAILABILITY_TIMEOUT,
    CONF_DEVICES,
    CONF_IGNORED_DEVICES,
    CONF_MANAGE_SETTINGS,
    CONF_MODEL,
    CONF_USER_MAPPINGS,
    DATA_LIBRARY,
    DEFAULT_AVAILABILITY_TIMEOUT,
    DEFAULT_MOTION_CLEAR_DELAY,
    DEVICE_CALIBRATION,
    DEVICE_FIELDS,
    DEVICE_MOTION_CLEAR_DELAY,
    DEVICE_TIMEOUT_OVERRIDE,
    DOMAIN,
)
from custom_components.rtl_433.coordinator import PendingDevice
from custom_components.rtl_433.device_replace import DeviceReplaceError
from custom_components.rtl_433.options_flow import (
    CONF_ADD_DEVICES,
    CONF_DEVICE,
    CONF_IGNORE_DEVICES,
    CONF_UNIGNORE_DEVICES,
    Rtl433OptionsFlow,
    _model_label,
    _pending_label,
)
from custom_components.rtl_433.settings import MAPPINGS_DOCS_URL
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from homeassistant.util import dt as dt_util

# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #


def _marker(result: dict[str, Any], key: str):
    """Return the voluptuous marker for a form field, by its key name."""
    for marker in result["data_schema"].schema:
        if marker == key:
            return marker
    raise AssertionError(f"no field {key!r} in schema")


def _default(result: dict[str, Any], key: str):
    """Return the rendered default for a form field key."""
    default = getattr(_marker(result, key), "default", None)
    return default() if callable(default) else default


def _suggested(result: dict[str, Any], key: str):
    """Return the ``suggested_value`` a form field is pre-filled with."""
    description = getattr(_marker(result, key), "description", None) or {}
    return description.get("suggested_value")


def _keys(result: dict[str, Any]) -> set[str]:
    """Return the set of field key names present in a form result's schema."""
    return {
        marker.schema if hasattr(marker, "schema") else str(marker)
        for marker in result["data_schema"].schema
    }


def _selector(result: dict[str, Any], key: str):
    """Return the selector validating a form field."""
    for marker, validator in result["data_schema"].schema.items():
        if marker == key:
            return validator
    raise AssertionError(f"no field {key!r} in schema")


def _options(result: dict[str, Any], key: str) -> list[tuple[str, str]]:
    """Return ``[(value, label), ...]`` for a SelectSelector form field."""
    return [
        (option["value"], option["label"])
        for option in _selector(result, key).config["options"]
    ]


class _StubCoordinator:
    """The slice of the coordinator the approval steps actually touch.

    ``add_devices`` and ``ignored_devices`` reach the coordinator for the pending
    list, the mirrored ignore set and the panel announcement; nothing else about
    a running hub is observable from either step. Standing in for it here keeps
    these tests off the socket and the entity build entirely, so a form-shape
    assertion costs a form render rather than a hub setup.
    """

    def __init__(self, pending: dict[str, PendingDevice] | None = None) -> None:
        self.pending: dict[str, PendingDevice] = pending or {}
        self.ignored: set[str] = set()
        self.ignored_models: dict[str, str] = {}
        self.adopted: dict[str, Any] = {}
        self.devices: dict[str, Any] = {}
        self.emitted = 0

    def pending_candidates(self) -> list[PendingDevice]:
        """Most recently heard first, the order the discovery panel renders."""
        return sorted(self.pending.values(), key=lambda r: r.last_seen, reverse=True)

    def adopt_device(self, device_key: str) -> PendingDevice | None:
        """Move a key off the pending list, as the real adoption seam does."""
        record = self.pending.pop(device_key, None)
        if record is not None:
            self.adopted[device_key] = record
        return record

    def ignore_device(self, device_key: str) -> None:
        """Drop a key from pending and mirror it into the ignore set."""
        self.pending.pop(device_key, None)
        self.ignored.add(device_key)

    def emit_pending_update(self) -> None:
        """Count the panel announcement instead of pushing it to subscribers."""
        self.emitted += 1


def _pending(key: str, model: str, *, count: int = 1, last_seen=None, **fields):
    """Build one pending record the way a heard frame would leave it."""
    now = last_seen or dt_util.utcnow()
    return PendingDevice(
        key=key,
        model=model,
        event=NormalizedEvent(device_key=key, model=model, fields=dict(fields)),
        count=count,
        first_seen=now,
        last_seen=now,
        fields=dict(fields),
    )


def _install(hass, entry, coordinator: _StubCoordinator) -> _StubCoordinator:
    """Publish a coordinator where ``Rtl433OptionsFlow._coordinator`` looks."""
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    return coordinator


def _install_motion_library(hass, field_key: str = "motion") -> None:
    """Register a descriptor carrying a ``clear_delay`` so a device is motion-bearing."""
    registry = Registry(
        flat={
            field_key: FieldDescriptor(
                field_key=field_key,
                platform="binary_sensor",
                name="Motion",
                object_suffix="motion",
                clear_delay=30,
            )
        },
        models={},
    )
    hass.data.setdefault(DOMAIN, {})[DATA_LIBRARY] = (registry, set())


async def _menu(hass, entry, step: str):
    """Open the options flow and pick one step off the menu."""
    result = await hass.config_entries.options.async_init(entry.entry_id)
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": step}
    )


async def _device_settings(hass, entry, device_key: str):
    """Walk menu -> device picker -> the device-settings form for one device."""
    result = await _menu(hass, entry, "device")
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_DEVICE: device_key}
    )


# --------------------------------------------------------------------------- #
# device_settings — the form a user sees                                       #
# --------------------------------------------------------------------------- #
DEVICE_KEY = "Acurite-606TX-42"


def _entry_with_device(hub_entry_builder, record: dict[str, Any] | None = None, **kw):
    """A hub whose devices map holds exactly ``DEVICE_KEY``."""
    return hub_entry_builder(
        devices={
            DEVICE_KEY: record
            or {CONF_MODEL: "Acurite-606TX", DEVICE_FIELDS: ["temperature_C"]}
        },
        **kw,
    )


async def test_device_settings_names_the_device_it_is_about(hass, hub_entry_builder):
    """The settings form has no other clue which device it is editing.

    The picker is a separate step, so by the time these knobs are on screen the
    device the user chose is out of view. The dialog title is built from the
    ``device`` placeholder alone: lose it and a user with three identical
    Acurites has no way to tell which one they are about to give a ten-minute
    timeout to.
    """
    entry = _entry_with_device(hub_entry_builder)
    entry.add_to_hass(hass)

    result = await _device_settings(hass, entry, DEVICE_KEY)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "device_settings"
    assert result["description_placeholders"] == {
        "device": f"Acurite-606TX ({DEVICE_KEY})"
    }


async def test_device_settings_offers_every_commodity_including_none(
    hass, hub_entry_builder
):
    """The commodity dropdown must offer "none", or a calibration is unclearable.

    ``none`` is not decoration: it is the only way to take a calibration back off
    a device that was mis-identified as a gas meter, and it has to sit in the
    same list as the real commodities. The labels are the raw values because the
    selector carries a ``commodity`` translation key — Home Assistant looks the
    display text up from that, and a picker that lost the key would show a user
    four untranslated strings.
    """
    entry = _entry_with_device(hub_entry_builder)
    entry.add_to_hass(hass)

    result = await _device_settings(hass, entry, DEVICE_KEY)

    assert _options(result, CALIBRATION_COMMODITY) == [
        (value, value) for value in CALIBRATION_COMMODITIES
    ]
    assert COMMODITY_NONE in dict(_options(result, CALIBRATION_COMMODITY))
    config = _selector(result, CALIBRATION_COMMODITY).config
    assert config["translation_key"] == "commodity"
    assert config["mode"] == "dropdown"


async def test_device_settings_pre_fills_the_stored_timeout_as_a_suggestion(
    hass, hub_entry_builder
):
    """The timeout is *suggested*, not defaulted, so clearing it can mean clear it.

    A ``default`` would be re-submitted by the frontend whenever the user emptied
    the box, making a per-device override impossible to remove once set. Pinning
    the value to ``suggested_value`` is what makes an emptied field arrive as an
    absent key, which the submit path reads as "fall back to the hub default".
    """
    entry = _entry_with_device(
        hub_entry_builder,
        {
            CONF_MODEL: "Acurite-606TX",
            DEVICE_FIELDS: ["temperature_C"],
            DEVICE_TIMEOUT_OVERRIDE: 1800,
        },
    )
    entry.add_to_hass(hass)

    result = await _device_settings(hass, entry, DEVICE_KEY)

    assert _suggested(result, DEVICE_TIMEOUT_OVERRIDE) == 1800
    # A `default` here would defeat the clear path entirely.
    assert _default(result, DEVICE_TIMEOUT_OVERRIDE) is vol.UNDEFINED


async def test_device_settings_leaves_the_timeout_blank_when_nothing_is_stored(
    hass, hub_entry_builder
):
    """A device with no override shows an empty box, not the hub's number.

    Pre-filling the inherited hub value would make every visit to this form look
    like the device already has an override, and saving the form unchanged would
    then freeze that number onto the device — so a later change to the hub
    default would silently stop applying to it.
    """
    entry = _entry_with_device(hub_entry_builder)
    entry.add_to_hass(hass)

    result = await _device_settings(hass, entry, DEVICE_KEY)

    assert _suggested(result, DEVICE_TIMEOUT_OVERRIDE) is None


async def test_device_settings_accepts_zero_as_the_never_expire_timeout(
    hass, hub_entry_builder
):
    """Zero seconds means "never mark this device unavailable", and must be storable.

    It is the setting for a device that transmits only on an event — a doorbell,
    a door contact — where any silence timeout eventually reports a working
    sensor as unavailable. A lower bound that excluded zero would take that
    escape hatch away.
    """
    entry = _entry_with_device(hub_entry_builder)
    entry.add_to_hass(hass)

    result = await _device_settings(hass, entry, DEVICE_KEY)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {DEVICE_TIMEOUT_OVERRIDE: 0, CALIBRATION_COMMODITY: COMMODITY_NONE},
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.data[CONF_DEVICES][DEVICE_KEY][DEVICE_TIMEOUT_OVERRIDE] == 0


async def test_device_settings_refuses_a_negative_timeout(hass, hub_entry_builder):
    """A negative timeout is meaningless and is rejected at the form, not stored.

    The watchdog compares "seconds since last seen" against this number; a
    negative one would expire a device that had just transmitted, so the form
    keeps it out of ``entry.data`` rather than letting the coordinator discover
    it at runtime.
    """
    entry = _entry_with_device(hub_entry_builder)
    entry.add_to_hass(hass)

    result = await _device_settings(hass, entry, DEVICE_KEY)
    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(
            result["flow_id"],
            {DEVICE_TIMEOUT_OVERRIDE: -1, CALIBRATION_COMMODITY: COMMODITY_NONE},
        )


async def test_device_settings_submitted_empty_clears_the_stored_override(
    hass, hub_entry_builder
):
    """Emptying the timeout box and saving is how a user gives the override back.

    Every field on this form is optional, so a form whose boxes were blanked
    arrives carrying no keys at all. That has to read as "no override" rather
    than as an error or as a no-op, because emptying the box and pressing save is
    the only undo the form offers — and the device then inherits the hub default
    again, which is the whole point of an override being removable.
    """
    entry = _entry_with_device(
        hub_entry_builder,
        {
            CONF_MODEL: "Acurite-606TX",
            DEVICE_FIELDS: ["temperature_C"],
            DEVICE_TIMEOUT_OVERRIDE: 1800,
        },
    )
    entry.add_to_hass(hass)

    result = await _device_settings(hass, entry, DEVICE_KEY)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert DEVICE_TIMEOUT_OVERRIDE not in entry.data[CONF_DEVICES][DEVICE_KEY]


async def test_device_settings_commodity_none_clears_a_stored_calibration(
    hass, hub_entry_builder
):
    """Choosing "none" is the only way to take a mis-set calibration back off.

    A device wrongly calibrated as a gas meter produces a consumption sensor in
    m³ that the Energy dashboard will happily accept; picking "none" has to clear
    the whole triple from the record rather than leaving the old unit behind, or
    the bad reading survives the correction.
    """
    entry = _entry_with_device(
        hub_entry_builder,
        {
            CONF_MODEL: "Acurite-606TX",
            DEVICE_FIELDS: ["consumption_data"],
            DEVICE_CALIBRATION: {
                CALIBRATION_COMMODITY: COMMODITY_GAS,
                CALIBRATION_UNIT: "m³",
                CALIBRATION_SCALE: 1.0,
            },
        },
    )
    entry.add_to_hass(hass)

    result = await _device_settings(hass, entry, DEVICE_KEY)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CALIBRATION_COMMODITY: COMMODITY_NONE}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert DEVICE_CALIBRATION not in entry.data[CONF_DEVICES][DEVICE_KEY]


async def test_device_settings_finishes_without_renaming_the_entry(
    hass, hub_entry_builder
):
    """An options save must not retitle the hub in the user's integrations page.

    ``async_create_entry`` on an options flow writes ``entry.options``; a
    non-empty title would overwrite the hub's own title with it, so the device a
    user happened to edit last would become the hub's name.
    """
    entry = _entry_with_device(hub_entry_builder)
    entry.add_to_hass(hass)
    title = entry.title

    result = await _device_settings(hass, entry, DEVICE_KEY)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    await hass.async_block_till_done()

    assert result["title"] == ""
    assert entry.title == title


# --------------------------------------------------------------------------- #
# device_settings — the motion clear-delay knob                                #
# --------------------------------------------------------------------------- #
MOTION_KEY = "GenericMotion-X1-7"


def _motion_entry(hub_entry_builder, **record):
    """A hub with one motion-bearing device, optionally carrying overrides."""
    return hub_entry_builder(
        devices={
            MOTION_KEY: {
                CONF_MODEL: "GenericMotion-X1",
                DEVICE_FIELDS: ["motion"],
                **record,
            }
        }
    )


async def test_device_settings_accepts_a_one_second_clear_delay(
    hass, hub_entry_builder
):
    """One second is the shortest useful clear delay and must not be rejected.

    A motion sensor covering a doorway is exactly the case for the smallest
    delay the form allows: the user wants the binary sensor to fall back to
    "clear" almost immediately after the last frame. A lower bound above one
    would make that unreachable through the UI.
    """
    entry = _motion_entry(hub_entry_builder)
    entry.add_to_hass(hass)
    _install_motion_library(hass)

    result = await _device_settings(hass, entry, MOTION_KEY)
    assert DEVICE_MOTION_CLEAR_DELAY in _keys(result)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CALIBRATION_COMMODITY: COMMODITY_NONE, DEVICE_MOTION_CLEAR_DELAY: 1},
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_DEVICES][MOTION_KEY][DEVICE_MOTION_CLEAR_DELAY] == 1


async def test_device_settings_refuses_a_zero_clear_delay(hass, hub_entry_builder):
    """Zero seconds would clear motion in the same instant it was detected.

    The delay exists because an RF motion sensor reports detections, never the
    "all clear"; a zero would make the binary sensor flick on and straight back
    off, so no automation could ever trigger on it. The form rejects it rather
    than storing a setting that breaks the entity.
    """
    entry = _motion_entry(hub_entry_builder)
    entry.add_to_hass(hass)
    _install_motion_library(hass)

    result = await _device_settings(hass, entry, MOTION_KEY)
    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(
            result["flow_id"],
            {CALIBRATION_COMMODITY: COMMODITY_NONE, DEVICE_MOTION_CLEAR_DELAY: 0},
        )


async def test_device_settings_pre_fills_the_clear_delay_from_the_stored_override(
    hass, hub_entry_builder
):
    """Re-opening the form must show the delay in force, not the library default.

    A user who set 45 seconds and comes back to adjust it would otherwise see 90
    and, saving without touching the field, silently double their own setting.
    """
    entry = _motion_entry(hub_entry_builder, **{DEVICE_MOTION_CLEAR_DELAY: 45})
    entry.add_to_hass(hass)
    _install_motion_library(hass)

    result = await _device_settings(hass, entry, MOTION_KEY)

    assert _default(result, DEVICE_MOTION_CLEAR_DELAY) == 45


async def test_device_settings_clear_delay_falls_back_to_the_constant(
    hass, hub_entry_builder
):
    """With nothing stored the field shows the shipped default, never a blank.

    The field is ``vol.Required``-shaped in practice — it always submits — so a
    blank default would make the first save of a motion device fail validation
    rather than persist the sensible value the integration already ships.
    """
    entry = _motion_entry(hub_entry_builder)
    entry.add_to_hass(hass)
    _install_motion_library(hass)

    result = await _device_settings(hass, entry, MOTION_KEY)

    assert _default(result, DEVICE_MOTION_CLEAR_DELAY) == DEFAULT_MOTION_CLEAR_DELAY


async def test_device_settings_hides_the_clear_delay_for_a_non_motion_device(
    hass, hub_entry_builder
):
    """A thermometer must not be offered a motion setting that can never apply.

    The knob is read only by the binary-sensor clear timer, so showing it on a
    temperature sensor would offer a user a setting that does nothing —  and
    persist a value into options for a device that will never read it.
    """
    entry = _entry_with_device(hub_entry_builder)
    entry.add_to_hass(hass)
    _install_motion_library(hass)

    result = await _device_settings(hass, entry, DEVICE_KEY)

    assert DEVICE_MOTION_CLEAR_DELAY not in _keys(result)


async def test_device_settings_carries_the_timeout_and_delay_through_calibration(
    hass, hub_entry_builder
):
    """Choosing a commodity must not throw away the other two knobs on the form.

    Picking "gas" sends the user to a second page for the unit and scale, so the
    timeout override and clear delay they typed on the first page are held in
    flow state until that page is submitted. Dropping either would mean a user
    who calibrates a meter silently loses the availability timeout they set in
    the same breath.
    """
    entry = hub_entry_builder(
        devices={
            MOTION_KEY: {
                CONF_MODEL: "GenericMotion-X1",
                DEVICE_FIELDS: ["motion", "consumption_data"],
            }
        }
    )
    entry.add_to_hass(hass)
    _install_motion_library(hass)

    result = await _device_settings(hass, entry, MOTION_KEY)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            DEVICE_TIMEOUT_OVERRIDE: 1234,
            CALIBRATION_COMMODITY: COMMODITY_GAS,
            DEVICE_MOTION_CLEAR_DELAY: 37,
        },
    )
    assert result["step_id"] == "calibration"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CALIBRATION_UNIT: "m³", CALIBRATION_SCALE: 2.0}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    record = entry.data[CONF_DEVICES][MOTION_KEY]
    assert record[DEVICE_TIMEOUT_OVERRIDE] == 1234
    assert record[DEVICE_CALIBRATION][CALIBRATION_COMMODITY] == COMMODITY_GAS
    assert entry.options[CONF_DEVICES][MOTION_KEY][DEVICE_MOTION_CLEAR_DELAY] == 37


async def test_device_settings_carries_a_cleared_timeout_through_calibration(
    hass, hub_entry_builder
):
    """Blanking the timeout while calibrating must clear it, not resurrect the old one.

    The value is carried across two pages in flow state; a carry that fell back
    to the stored record instead of to the submitted absence would make the
    override impossible to remove for any device the user also calibrates.
    """
    entry = hub_entry_builder(
        devices={
            DEVICE_KEY: {
                CONF_MODEL: "Acurite-606TX",
                DEVICE_FIELDS: ["consumption_data"],
                DEVICE_TIMEOUT_OVERRIDE: 900,
            }
        }
    )
    entry.add_to_hass(hass)

    result = await _device_settings(hass, entry, DEVICE_KEY)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CALIBRATION_COMMODITY: COMMODITY_ENERGY}
    )
    assert result["step_id"] == "calibration"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CALIBRATION_UNIT: "Wh", CALIBRATION_SCALE: 1.0}
    )
    await hass.async_block_till_done()

    record = entry.data[CONF_DEVICES][DEVICE_KEY]
    assert DEVICE_TIMEOUT_OVERRIDE not in record
    assert record[DEVICE_CALIBRATION][CALIBRATION_COMMODITY] == COMMODITY_ENERGY


async def test_device_settings_pre_fills_the_commodity_from_a_stored_calibration(
    hass, hub_entry_builder
):
    """A device already calibrated as gas comes back to the form saying so.

    Otherwise every return visit resets the dropdown to "none", and a user who
    opened the form to adjust the availability timeout and saved would silently
    delete the calibration they set up earlier.
    """
    entry = _entry_with_device(
        hub_entry_builder,
        {
            CONF_MODEL: "Acurite-606TX",
            DEVICE_FIELDS: ["consumption_data"],
            DEVICE_CALIBRATION: {
                CALIBRATION_COMMODITY: COMMODITY_GAS,
                CALIBRATION_UNIT: "m³",
                CALIBRATION_SCALE: 1.0,
            },
        },
    )
    entry.add_to_hass(hass)

    result = await _device_settings(hass, entry, DEVICE_KEY)

    assert _default(result, CALIBRATION_COMMODITY) == COMMODITY_GAS


# --------------------------------------------------------------------------- #
# ignored_devices                                                              #
# --------------------------------------------------------------------------- #
IGNORED_A = "Acurite-606TX-11"
IGNORED_B = "GenericDoor-X1-88"
IGNORED_C = "ZWeather-9-3"


async def test_ignored_devices_lists_the_keys_alphabetically(hass, hub_entry_builder):
    """The un-ignore picker is sorted, not in the order the user ignored things.

    The stored list grows in ignore order, which is meaningless months later. A
    user hunting for the one sensor they ignored by mistake among a dozen rows
    can only scan for it if the rows are in a predictable order, so the step
    sorts rather than rendering the raw list.
    """
    entry = hub_entry_builder(ignored_devices=[IGNORED_C, IGNORED_A, IGNORED_B])
    entry.add_to_hass(hass)
    _install(hass, entry, _StubCoordinator())

    result = await _menu(hass, entry, "ignored_devices")

    assert [value for value, _ in _options(result, CONF_UNIGNORE_DEVICES)] == [
        IGNORED_A,
        IGNORED_B,
        IGNORED_C,
    ]


async def test_ignored_devices_names_a_row_by_its_key_when_no_model_is_stored(
    hass, hub_entry_builder
):
    """An ignored device usually has no record, and its key must stand alone.

    Devices are almost always ignored while still pending — long before anything
    is stored about them — so the label falls back to the bare key. A blank model
    prefixed onto it would render as " (Acurite-606TX-11)", which reads like a
    bug in the picker.
    """
    entry = hub_entry_builder(
        devices={IGNORED_B: {CONF_MODEL: "GenericDoor-X1", DEVICE_FIELDS: ["closed"]}},
        ignored_devices=[IGNORED_A, IGNORED_B],
    )
    entry.add_to_hass(hass)
    _install(hass, entry, _StubCoordinator())

    result = await _menu(hass, entry, "ignored_devices")

    assert _options(result, CONF_UNIGNORE_DEVICES) == [
        (IGNORED_A, IGNORED_A),
        (IGNORED_B, f"GenericDoor-X1 ({IGNORED_B})"),
    ]


async def test_ignored_devices_renders_an_empty_multi_select_list(
    hass, hub_entry_builder
):
    """Nothing is already selected, and several rows can be un-ignored in one pass.

    Un-ignoring is a batch job — a user who ignored a neighbour's whole weather
    station is un-ignoring several keys at once — and a picker that defaulted to
    selecting rows would un-ignore everything for a user who just wanted to look
    at the list and press cancel-by-save.
    """
    entry = hub_entry_builder(ignored_devices=[IGNORED_A, IGNORED_B])
    entry.add_to_hass(hass)
    _install(hass, entry, _StubCoordinator())

    result = await _menu(hass, entry, "ignored_devices")

    assert _default(result, CONF_UNIGNORE_DEVICES) == []
    config = _selector(result, CONF_UNIGNORE_DEVICES).config
    assert config["multiple"] is True
    assert config["mode"] == "list"


async def test_ignored_devices_submitted_with_nothing_selected_writes_nothing(
    hass, hub_entry_builder
):
    """Opening the list and saving it unchanged must not un-ignore anything.

    The picker defaults to an empty selection, so this is exactly what a user who
    only wanted to see what was on the list does. Reading an absent selection as
    anything other than "none" would empty a hub's whole ignore list by accident.
    """
    entry = hub_entry_builder(ignored_devices=[IGNORED_A, IGNORED_B])
    entry.add_to_hass(hass)
    coordinator = _install(hass, entry, _StubCoordinator())
    coordinator.ignored = {IGNORED_A, IGNORED_B}

    result = await _menu(hass, entry, "ignored_devices")
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.data[CONF_IGNORED_DEVICES] == [IGNORED_A, IGNORED_B]
    assert coordinator.ignored == {IGNORED_A, IGNORED_B}


async def test_ignored_devices_finish_preserves_every_unrelated_option(
    hass, hub_entry_builder
):
    """Un-ignoring writes ``entry.data``; the options it hands back must be intact.

    An options flow finishes by *replacing* ``entry.options`` wholesale, so a
    step that only meant to touch ``entry.data`` has to pass the existing options
    straight back. Handing back anything else would wipe the hub's
    manage-settings toggle and every per-device clear delay in one un-ignore.
    """
    options = {
        CONF_MANAGE_SETTINGS: True,
        CONF_DEVICES: {IGNORED_B: {DEVICE_MOTION_CLEAR_DELAY: 42}},
    }
    entry = hub_entry_builder(ignored_devices=[IGNORED_A, IGNORED_B], options=options)
    entry.add_to_hass(hass)
    _install(hass, entry, _StubCoordinator())
    snapshot = deepcopy(options)

    result = await _menu(hass, entry, "ignored_devices")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_UNIGNORE_DEVICES: [IGNORED_A]}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == ""
    assert dict(entry.options) == snapshot
    assert entry.data[CONF_IGNORED_DEVICES] == [IGNORED_B]


async def test_ignored_devices_checks_the_hub_is_loaded_before_the_empty_list(
    hass, hub_entry_builder
):
    """An unloaded hub with nothing ignored still reports the loading problem.

    Both aborts are reachable at once, and the order decides which one the user
    is told about. "No ignored devices" on a hub that is merely unloaded would
    send them looking for a list that is actually there, so the coordinator guard
    is checked first.
    """
    entry = hub_entry_builder()
    entry.add_to_hass(hass)

    result = await _menu(hass, entry, "ignored_devices")

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "hub_not_loaded"


# --------------------------------------------------------------------------- #
# mappings                                                                     #
# --------------------------------------------------------------------------- #
GOOD_MAPPING = {
    "temperature_C": {
        "platform": "sensor",
        "name": "Kelvin Temp",
        "object_suffix": "K",
        "unit_of_measurement": "K",
    }
}


async def test_mappings_form_offers_the_docs_link_and_no_problems_yet(
    hass, hub_entry_builder
):
    """The YAML editor is unusable without the link to the schema it expects.

    Nobody writes a device-library override from memory, so the docs URL is part
    of the form from the first render — not something that appears only after the
    user has already got it wrong. The problems slot starts empty so the
    description does not open with a stray separator.
    """
    entry = hub_entry_builder()
    entry.add_to_hass(hass)

    result = await _menu(hass, entry, "mappings")

    assert result["step_id"] == "mappings"
    assert result["description_placeholders"] == {
        "problems": "",
        "docs_url": MAPPINGS_DOCS_URL,
    }
    assert not result["errors"]


async def test_mappings_form_pre_fills_the_overrides_already_stored(
    hass, hub_entry_builder
):
    """Editing overrides means editing them, not retyping them from scratch.

    The YAML editor is the only interface to this setting, so an empty form would
    make every change a full rewrite — and a user who added one field and saved
    would delete every other override on the hub.
    """
    entry = hub_entry_builder()
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_USER_MAPPINGS: GOOD_MAPPING}
    )

    result = await _menu(hass, entry, "mappings")

    assert _default(result, CONF_USER_MAPPINGS) == GOOD_MAPPING


async def test_mappings_rejection_names_the_problems_it_found(hass, hub_entry_builder):
    """A rejected override has to say what was wrong with it, in the dialog.

    The editor holds free-form YAML, so "invalid" on its own leaves a user
    guessing which of a dozen field definitions the validator objected to. Every
    problem is joined into the one description placeholder the dialog renders --
    all of them, separated readably, because a dialog that named only the first
    fault would make fixing a broken override a game of whack-a-mole. The error
    lands under ``base`` because the fault is with the object as a whole rather
    than with one form field.
    """
    entry = hub_entry_builder()
    entry.add_to_hass(hass)
    snapshot = deepcopy(dict(entry.data))

    result = await _menu(hass, entry, "mappings")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_USER_MAPPINGS: {
                "bad_one": {"name": "X", "object_suffix": "X"},
                "bad_two": {"name": "Y", "object_suffix": "Y"},
            }
        },
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "mappings"
    assert result["errors"] == {"base": "invalid_mappings"}
    # Every problem, in one readable line: a user with two broken field
    # definitions has to be told about both, or fixing the first only earns
    # them the same dialog again.
    assert result["description_placeholders"]["problems"] == (
        "bad_one: missing required 'platform'; bad_two: missing required 'platform'"
    )
    assert result["description_placeholders"]["docs_url"] == MAPPINGS_DOCS_URL
    assert dict(entry.data) == snapshot


async def test_mappings_rejection_re_renders_the_rejected_text_to_fix(
    hass, hub_entry_builder
):
    """A rejected submit must not throw the user's YAML away.

    The re-rendered editor is pre-filled from ``entry.data`` — which the rejected
    submit deliberately did not touch — so what comes back is the last *valid*
    state, and the previously stored overrides are still there to work from
    rather than an empty box.
    """
    entry = hub_entry_builder()
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_USER_MAPPINGS: GOOD_MAPPING}
    )

    result = await _menu(hass, entry, "mappings")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_USER_MAPPINGS: {"bad_field": {"name": "X"}}}
    )

    assert result["type"] is FlowResultType.FORM
    assert _default(result, CONF_USER_MAPPINGS) == GOOD_MAPPING


async def test_mappings_submitted_empty_clears_every_override(hass, hub_entry_builder):
    """Emptying the editor is how a user removes an override they no longer want.

    The submitted object arrives as ``None`` when the editor is cleared, and that
    has to be treated as the empty mapping — validated, accepted, and stored —
    rather than as "no change". Otherwise a bad override could never be taken
    back off a hub except by deleting and re-adding it.
    """
    entry = hub_entry_builder()
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_USER_MAPPINGS: GOOD_MAPPING}
    )

    result = await _menu(hass, entry, "mappings")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_USER_MAPPINGS: None}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.data[CONF_USER_MAPPINGS] == {}


async def test_mappings_save_leaves_the_hub_options_alone(hass, hub_entry_builder):
    """Saving mappings writes ``entry.data``; options must survive untouched.

    As with the approval steps, finishing an options flow replaces
    ``entry.options`` outright, so this step hands the existing options back
    verbatim. A hub whose availability timeout vanished the first time anyone
    edited a mapping would be a very hard bug to attribute.
    """
    options = {CONF_MANAGE_SETTINGS: True}
    entry = hub_entry_builder(options=options)
    entry.add_to_hass(hass)
    snapshot = deepcopy(options)

    result = await _menu(hass, entry, "mappings")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_USER_MAPPINGS: GOOD_MAPPING}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == ""
    assert dict(entry.options) == snapshot
    assert entry.data[CONF_USER_MAPPINGS]["temperature_C"]["object_suffix"] == "K"


# --------------------------------------------------------------------------- #
# add_devices                                                                  #
# --------------------------------------------------------------------------- #


async def test_add_devices_renders_one_selector_shape_for_both_columns(
    hass, hub_entry_builder
):
    """Add and ignore must offer the same rows, multi-select, nothing pre-picked.

    The two lists are deliberately identical so one pass down a long candidate
    list can adopt some devices and ignore others. If either defaulted to a
    selection, opening the step and saving would adopt or bury every device the
    hub had heard.
    """
    entry = hub_entry_builder()
    entry.add_to_hass(hass)
    _install(
        hass,
        entry,
        _StubCoordinator({IGNORED_A: _pending(IGNORED_A, "Acurite-606TX")}),
    )

    result = await _menu(hass, entry, "add_devices")

    assert result["step_id"] == "add_devices"
    assert not result["errors"]
    for key in (CONF_ADD_DEVICES, CONF_IGNORE_DEVICES):
        assert _default(result, key) == []
        config = _selector(result, key).config
        assert config["multiple"] is True
        assert config["mode"] == "list"
    assert _options(result, CONF_ADD_DEVICES) == _options(result, CONF_IGNORE_DEVICES)


async def test_add_devices_submitted_with_nothing_selected_just_closes(
    hass, hub_entry_builder
):
    """Looking at the candidate list and saving it must adopt and ignore nothing.

    Both multi-selects are optional, so a bare submit carries neither key. Read
    as anything but two empty lists that would adopt — or permanently bury — the
    whole pending list of a user who only came to look.
    """
    entry = hub_entry_builder()
    entry.add_to_hass(hass)
    coordinator = _install(
        hass,
        entry,
        _StubCoordinator({IGNORED_A: _pending(IGNORED_A, "Acurite-606TX")}),
    )

    result = await _menu(hass, entry, "add_devices")
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == ""
    assert entry.data.get(CONF_DEVICES, {}) == {}
    assert CONF_IGNORED_DEVICES not in entry.data
    assert coordinator.ignored == set()
    assert set(coordinator.pending) == {IGNORED_A}


async def test_add_devices_checks_the_hub_is_loaded_before_the_pending_list(
    hass, hub_entry_builder
):
    """An unloaded hub is reported as unloaded, not as "nothing has transmitted".

    The pending list lives only in the coordinator's memory, so an unloaded hub
    has no list to show at all — telling the user that nothing has been heard
    would send them to check their antenna instead of their hub connection.
    """
    entry = hub_entry_builder()
    entry.add_to_hass(hass)

    result = await _menu(hass, entry, "add_devices")

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "hub_not_loaded"


async def test_add_devices_aborts_when_the_hub_has_heard_nothing(
    hass, hub_entry_builder
):
    """An empty pending list aborts with an explanation, not a form with no rows.

    The list is rebuilt from live traffic after every restart, so being empty
    shortly after a reload is normal. A form with two empty pickers would read as
    a broken dialog; the abort says what is actually going on.
    """
    entry = hub_entry_builder()
    entry.add_to_hass(hass)
    _install(hass, entry, _StubCoordinator())

    result = await _menu(hass, entry, "add_devices")

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_pending_devices"


async def test_add_devices_conflict_error_lands_under_base(hass, hub_entry_builder):
    """The add/ignore contradiction is a form-wide error, not a field one.

    Neither multi-select is individually wrong — it is the pair that contradicts
    — so the error key has to be ``base`` for the dialog to render it above the
    form. Attached to a field it would point at one of two equally innocent
    lists, and nothing at all is written either way.
    """
    entry = hub_entry_builder()
    entry.add_to_hass(hass)
    coordinator = _install(
        hass,
        entry,
        _StubCoordinator({IGNORED_A: _pending(IGNORED_A, "Acurite-606TX")}),
    )

    result = await _menu(hass, entry, "add_devices")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_ADD_DEVICES: [IGNORED_A], CONF_IGNORE_DEVICES: [IGNORED_A]},
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "add_devices"
    assert result["errors"] == {"base": "add_and_ignore_conflict"}
    assert entry.data.get(CONF_DEVICES, {}) == {}
    assert coordinator.ignored == set()


# --------------------------------------------------------------------------- #
# The pure label helpers                                                       #
# --------------------------------------------------------------------------- #


def test_model_label_drops_the_parentheses_when_no_model_is_known():
    """A device with no decoded model is named by its key, with nothing around it.

    A bad decode can carry no model at all, and an ignored device usually has no
    stored record; " (Acurite-606TX-42)" in a picker reads as a rendering bug
    rather than as a device the user has to make a decision about.
    """
    assert _model_label("", DEVICE_KEY) == DEVICE_KEY
    assert _model_label("Acurite-606TX", DEVICE_KEY) == f"Acurite-606TX ({DEVICE_KEY})"


def test_pending_label_carries_model_count_signal_and_age():
    """The picker row is the only evidence a user has for judging a candidate.

    A neighbour's sensor, a one-off bad decode and the device being waited for
    are indistinguishable by name, so the row has to carry the three things that
    do discriminate: how often it has been heard, how strong the last frame was,
    and how long ago that was — in that order, separated by em dashes, so a long
    list reads as columns.
    """
    now = dt_util.utcnow()
    record = _pending(
        DEVICE_KEY,
        "Acurite-606TX",
        count=7,
        last_seen=now - timedelta(minutes=5),
        snr=11.5,
    )

    label = _pending_label(record, now)

    # Segment by segment rather than by substring: a row that merely *contained*
    # the right words in the wrong order, or carried a fourth column nobody
    # asked for, would still read as a broken picker.
    segments = label.split(" — ")
    assert segments[:3] == [
        f"Acurite-606TX ({DEVICE_KEY})",
        "seen 7x",
        "11.5 dB",
    ]
    assert len(segments) == 4
    assert segments[3].startswith("last seen ")
    assert segments[3].endswith(" ago")


def test_pending_label_omits_the_signal_when_the_receiver_reports_none():
    """A receiver started without ``-M level`` must not render a fake 0.0 dB.

    Zero decibels is a real (and terrible) reading, so printing it for a device
    whose frames simply carry no level would tell a user their working sensor is
    on the edge of range.
    """
    now = dt_util.utcnow()
    record = _pending(DEVICE_KEY, "Acurite-606TX", count=1, last_seen=now)

    label = _pending_label(record, now)

    assert " dB" not in label
    assert label.startswith(f"Acurite-606TX ({DEVICE_KEY}) — seen 1x — last seen ")


def test_pending_label_names_an_unknown_model_rather_than_leaving_a_gap():
    """A frame that decoded without a model still needs a name in the picker.

    The pending row is a decision the user has to make, and "unknown" is honest
    about what is known; a blank would make the row look like the picker had
    failed to render rather than like a device worth ignoring.
    """
    now = dt_util.utcnow()
    record = _pending(DEVICE_KEY, "", count=1, last_seen=now)

    assert _pending_label(record, now).startswith(f"unknown ({DEVICE_KEY}) — seen 1x")


def test_pending_label_clamps_a_future_sighting_instead_of_raising():
    """A form render must never raise because a clock disagreed.

    ``dt_util.get_age`` raises on a future timestamp, and a device heard a
    fraction of a second after the render's clock was sampled is entirely
    ordinary — a hub whose host clock runs slightly fast makes it routine. The
    sighting is clamped to the render's own clock so the picker still draws.
    """
    now = dt_util.utcnow()
    record = _pending(
        DEVICE_KEY, "Acurite-606TX", count=1, last_seen=now + timedelta(hours=2)
    )

    assert _pending_label(record, now).endswith(" ago")


def test_replacement_model_prefers_the_stored_record_over_what_was_heard():
    """A replacement candidate with a stored record is named from that record.

    The stored model is what the rest of Home Assistant already shows for the
    device, so the picker has to agree with it; falling through to the runtime
    sighting would let one device appear under two names in the same dialog.
    """
    flow = Rtl433OptionsFlow()
    devices = {DEVICE_KEY: {CONF_MODEL: "Acurite-606TX"}}
    heard = {DEVICE_KEY: SimpleNamespace(model="Something-Else")}

    assert flow._replacement_model(DEVICE_KEY, devices, heard) == "Acurite-606TX"


def test_replacement_model_falls_back_to_a_pending_sighting():
    """A pending device has no stored record, and is exactly what replace must offer.

    The replacement for a battery-swapped sensor is by definition a device the
    user has never added, so nothing is stored about it. Without the fallback the
    one candidate the step exists to offer would render as a bare key.
    """
    flow = Rtl433OptionsFlow()
    heard = {DEVICE_KEY: SimpleNamespace(model="Acurite-606TX")}

    assert flow._replacement_model(DEVICE_KEY, {}, heard) == "Acurite-606TX"


def test_replacement_model_degrades_to_empty_for_a_device_nobody_can_name():
    """A candidate known by neither store renders as its key, never as a crash.

    A record can be missing, an entry can be missing, and a stored model can be
    blank; all three have to end in the bare key, because a replace dialog that
    raised would leave a user with a battery-swapped sensor no way to recover its
    history.
    """
    flow = Rtl433OptionsFlow()

    assert flow._replacement_model(DEVICE_KEY, {}, {}) == ""
    assert flow._replacement_model(DEVICE_KEY, {DEVICE_KEY: {}}, {}) == ""
    assert flow._replacement_model(DEVICE_KEY, {DEVICE_KEY: {CONF_MODEL: ""}}, {}) == ""


# --------------------------------------------------------------------------- #
# _apply_add_and_ignore                                                        #
# --------------------------------------------------------------------------- #


async def test_add_devices_applies_both_halves_of_one_submit(hass, hub_entry_builder):
    """One pass down a long candidate list adopts some devices and buries others.

    The reporter in issue #128 heard 77 devices in a day; working that list a
    device at a time is not a workflow. Both halves of the submit have to land
    from the single save, and the step has to close by handing ``entry.options``
    straight back — the two adoption services write ``entry.data``, so an options
    payload built from anything else would wipe the hub's settings on the way
    out.
    """
    keep, bury = "Acurite-606TX-11", "GenericDoor-X1-88"
    options = {CONF_MANAGE_SETTINGS: True}
    entry = hub_entry_builder(options=options)
    entry.add_to_hass(hass)
    coordinator = _install(
        hass,
        entry,
        _StubCoordinator(
            {
                keep: _pending(keep, "Acurite-606TX", temperature_C=21.4),
                bury: _pending(bury, "GenericDoor-X1", closed=0),
            }
        ),
    )
    snapshot = deepcopy(options)

    result = await _menu(hass, entry, "add_devices")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_ADD_DEVICES: [keep], CONF_IGNORE_DEVICES: [bury]},
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == ""
    assert set(entry.data[CONF_DEVICES]) == {keep}
    assert entry.data[CONF_IGNORED_DEVICES] == [bury]
    assert coordinator.ignored == {bury}
    assert coordinator.pending == {}
    assert dict(entry.options) == snapshot


async def test_add_devices_shrugs_off_a_key_that_stopped_being_pending(
    hass, hub_entry_builder
):
    """A stale selection closes the dialog instead of erroring at the user.

    A key can stop being a candidate between the render and the submit — the
    panel adopted it in another tab, or the same form was submitted twice. By
    then the dialog has already closed, so there is nobody to tell; the list it
    renders next time is rebuilt from live state anyway.
    """
    gone = "Acurite-606TX-11"
    entry = hub_entry_builder()
    entry.add_to_hass(hass)
    coordinator = _install(
        hass, entry, _StubCoordinator({gone: _pending(gone, "Acurite-606TX")})
    )

    result = await _menu(hass, entry, "add_devices")
    coordinator.pending.clear()  # adopted from the panel while the form was open
    coordinator.pending["other"] = _pending("other", "Other-1")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_ADD_DEVICES: [gone]}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.data.get(CONF_DEVICES, {}) == {}


# --------------------------------------------------------------------------- #
# hub                                                                          #
# --------------------------------------------------------------------------- #


async def test_hub_step_reads_the_plain_default_as_unset(hass, hub_entry_builder):
    """Saving the hub form untouched must not freeze 600 seconds onto the entry.

    The timeout field is required and pre-filled, so it echoes a number back on
    every save even when the user never touched it. Persisting that number would
    mask the per-device-type defaults — most damagingly it would start expiring
    event-driven devices (doorbells, motion, contacts) that must never go
    unavailable on silence. The plain default is therefore read as "unset" and
    the key is dropped.
    """
    entry = hub_entry_builder(options={CONF_AVAILABILITY_TIMEOUT: 1800})
    entry.add_to_hass(hass)

    result = await _menu(hass, entry, "hub")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_AVAILABILITY_TIMEOUT: DEFAULT_AVAILABILITY_TIMEOUT,
            CONF_MANAGE_SETTINGS: True,
        },
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert CONF_AVAILABILITY_TIMEOUT not in entry.options
    assert entry.options[CONF_MANAGE_SETTINGS] is True


async def test_hub_step_stores_zero_as_never_expire(hass, hub_entry_builder):
    """Zero is a real hub-wide choice — "never mark anything unavailable".

    It is the setting for a hub full of event-driven sensors, and it is not the
    sentinel: only the plain default means "unset". A lower bound that excluded
    zero, or a sentinel check that swallowed it, would take the choice away.
    """
    entry = hub_entry_builder()
    entry.add_to_hass(hass)

    result = await _menu(hass, entry, "hub")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_AVAILABILITY_TIMEOUT: 0, CONF_MANAGE_SETTINGS: False},
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_AVAILABILITY_TIMEOUT] == 0
    assert entry.options[CONF_MANAGE_SETTINGS] is False


async def test_hub_step_refuses_a_negative_timeout(hass, hub_entry_builder):
    """A negative hub timeout would expire every device the moment it transmitted.

    The watchdog subtracts the last sighting from now and compares; a negative
    budget is always exceeded, so the whole hub would report unavailable. The
    form rejects it rather than letting it reach the coordinator.
    """
    entry = hub_entry_builder()
    entry.add_to_hass(hass)

    result = await _menu(hass, entry, "hub")
    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(
            result["flow_id"],
            {CONF_AVAILABILITY_TIMEOUT: -1, CONF_MANAGE_SETTINGS: True},
        )


async def test_hub_step_keeps_the_per_device_options_it_does_not_own(
    hass, hub_entry_builder
):
    """The hub form owns two keys; the per-device sub-map has to survive it.

    Per-device clear delays live in ``entry.options`` alongside the hub's own
    settings, so a hub save that rebuilt options from its two fields alone would
    silently reset every motion sensor on the hub to the library default.
    """
    entry = hub_entry_builder(
        options={CONF_DEVICES: {MOTION_KEY: {DEVICE_MOTION_CLEAR_DELAY: 42}}}
    )
    entry.add_to_hass(hass)

    result = await _menu(hass, entry, "hub")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_AVAILABILITY_TIMEOUT: 1800, CONF_MANAGE_SETTINGS: True},
    )
    await hass.async_block_till_done()

    assert entry.options[CONF_DEVICES] == {MOTION_KEY: {DEVICE_MOTION_CLEAR_DELAY: 42}}
    assert entry.options[CONF_AVAILABILITY_TIMEOUT] == 1800


# --------------------------------------------------------------------------- #
# calibration                                                                  #
# --------------------------------------------------------------------------- #


async def _calibration_form(hass, entry, device_key, commodity):
    """Walk to the calibration form for one device and commodity."""
    result = await _device_settings(hass, entry, device_key)
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {CALIBRATION_COMMODITY: commodity}
    )


def _meter_entry(hub_entry_builder):
    """A hub with one device carrying a consumption field."""
    return hub_entry_builder(
        devices={
            DEVICE_KEY: {
                CONF_MODEL: "Acurite-606TX",
                DEVICE_FIELDS: ["consumption_data"],
            }
        }
    )


async def test_calibration_offers_only_units_the_commodity_can_convert(
    hass, hub_entry_builder
):
    """A gas meter must not be offered kilowatt-hours.

    The unit list is what makes the resulting consumption sensor eligible for the
    Energy dashboard: Home Assistant will only accept a statistic whose unit is
    convertible for the sensor's device class. Offering a unit outside that set
    produces a sensor the dashboard silently refuses.
    """
    entry = _meter_entry(hub_entry_builder)
    entry.add_to_hass(hass)

    result = await _calibration_form(hass, entry, DEVICE_KEY, COMMODITY_GAS)

    assert result["step_id"] == "calibration"
    assert result["description_placeholders"] == {"commodity": COMMODITY_GAS}
    assert _options(result, CALIBRATION_UNIT) == [
        (unit, unit) for unit in COMMODITY_UNITS[COMMODITY_GAS]
    ]
    assert _selector(result, CALIBRATION_UNIT).config["mode"] == "dropdown"


async def test_calibration_scale_is_a_free_typed_positive_number(
    hass, hub_entry_builder
):
    """The scale is typed, not dragged, and fractions of a unit are the normal case.

    A meter's raw counter is rarely a whole unit per tick — 0.01 m³ per pulse is
    ordinary — so the box has to accept any step rather than snapping to whole
    units, and it has to be a box rather than a slider for a number with no
    meaningful upper bound to drag along.
    """
    entry = _meter_entry(hub_entry_builder)
    entry.add_to_hass(hass)

    result = await _calibration_form(hass, entry, DEVICE_KEY, COMMODITY_GAS)

    config = _selector(result, CALIBRATION_SCALE).config
    assert config["step"] == "any"
    assert config["mode"] == "box"
    assert config["min"] == 0


async def test_calibration_rejects_a_negative_scale(hass, hub_entry_builder):
    """A negative multiplier would make a consumption meter count backwards.

    Consumption statistics must be monotonic for the Energy dashboard to accept
    them, so a sign flip does not produce a wrong number — it produces a sensor
    the dashboard rejects outright, long after the user set it.
    """
    entry = _meter_entry(hub_entry_builder)
    entry.add_to_hass(hass)

    result = await _calibration_form(hass, entry, DEVICE_KEY, COMMODITY_GAS)
    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                CALIBRATION_UNIT: COMMODITY_UNITS[COMMODITY_GAS][0],
                CALIBRATION_SCALE: -1.0,
            },
        )


async def test_calibration_writes_the_commodity_chosen_on_the_previous_step(
    hass, hub_entry_builder
):
    """The commodity is carried from the settings form, not re-read from storage.

    The unit page never asks which commodity it is calibrating, so the answer has
    to survive the page change. A device being calibrated for the first time has
    nothing stored to fall back on, which is exactly when losing it would write a
    meter with no commodity at all.
    """
    entry = _meter_entry(hub_entry_builder)
    entry.add_to_hass(hass)

    result = await _calibration_form(hass, entry, DEVICE_KEY, COMMODITY_WATER)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CALIBRATION_UNIT: COMMODITY_UNITS[COMMODITY_WATER][0],
            CALIBRATION_SCALE: 0.01,
        },
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == ""
    calibration = entry.data[CONF_DEVICES][DEVICE_KEY][DEVICE_CALIBRATION]
    assert calibration[CALIBRATION_COMMODITY] == COMMODITY_WATER
    assert calibration[CALIBRATION_UNIT] == COMMODITY_UNITS[COMMODITY_WATER][0]
    assert calibration[CALIBRATION_SCALE] == pytest.approx(0.01)


# --------------------------------------------------------------------------- #
# init, device and replace pickers                                             #
# --------------------------------------------------------------------------- #


async def test_options_menu_is_the_init_step(hass, hub_entry_builder):
    """The options dialog opens on ``init``, which is what Home Assistant asks for.

    ``async_init`` calls ``async_step_init`` by name and the rendered menu has to
    identify itself as that step; a menu under any other id leaves the frontend
    unable to route the user's choice back into the flow.
    """
    entry = hub_entry_builder()
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "init"
    assert result["menu_options"] == [
        "add_devices",
        "ignored_devices",
        "hub",
        "device",
        "mappings",
        "replace",
    ]


TWO_DEVICES = {
    "ZWeather-9-3": {CONF_MODEL: "ZWeather-9", DEVICE_FIELDS: ["temperature_C"]},
    "Acurite-606TX-42": {CONF_MODEL: "Acurite-606TX", DEVICE_FIELDS: ["temperature_C"]},
}


@pytest.mark.parametrize("step", ["device", "replace"])
async def test_device_pickers_list_devices_alphabetically(
    hass, hub_entry_builder, step
):
    """Both pickers sort, so the same hub reads the same way on either page.

    The devices map grows in adoption order, which is meaningless to a user
    months later. The settings picker and the replace picker show the same
    hardware, and a user who saw it in two different orders would have to check
    each row rather than reaching for a position they remember.
    """
    entry = hub_entry_builder(devices=TWO_DEVICES)
    entry.add_to_hass(hass)

    result = await _menu(hass, entry, step)

    assert result["step_id"] == step
    assert _options(result, CONF_DEVICE) == [
        ("Acurite-606TX-42", "Acurite-606TX (Acurite-606TX-42)"),
        ("ZWeather-9-3", "ZWeather-9 (ZWeather-9-3)"),
    ]
    assert _selector(result, CONF_DEVICE).config["mode"] == "dropdown"


@pytest.mark.parametrize("step", ["device", "replace"])
async def test_device_pickers_abort_on_a_hub_with_no_devices(
    hass, hub_entry_builder, step
):
    """A hub that has adopted nothing says so rather than showing an empty dropdown.

    Both steps derive everything from a stored record, so neither has anything to
    offer before the first device is added. The abort points the user back at the
    add-devices step; an empty dropdown would look like the list failed to load.
    """
    entry = hub_entry_builder()
    entry.add_to_hass(hass)

    result = await _menu(hass, entry, step)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_devices"


# --------------------------------------------------------------------------- #
# replace_target                                                               #
# --------------------------------------------------------------------------- #


async def _replace_target(hass, entry, old_key):
    """Walk menu -> replace picker -> the replace-target form."""
    result = await _menu(hass, entry, "replace")
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_DEVICE: old_key}
    )


async def test_replace_target_names_the_device_being_kept(hass, hub_entry_builder):
    """The target form must say whose history is about to be re-keyed.

    Picking the wrong row here merges a working sensor's history onto the wrong
    hardware, and the choice is two pages away from the picker that made it. The
    ``device`` placeholder is the only reminder on screen of which device is
    being kept.
    """
    entry = hub_entry_builder(devices=TWO_DEVICES)
    entry.add_to_hass(hass)

    result = await _replace_target(hass, entry, "Acurite-606TX-42")

    assert result["step_id"] == "replace_target"
    assert result["description_placeholders"] == {
        "device": "Acurite-606TX (Acurite-606TX-42)"
    }
    assert not result["errors"]
    assert _selector(result, CONF_DEVICE).config["mode"] == "dropdown"


async def test_replace_target_excludes_the_device_being_kept(hass, hub_entry_builder):
    """A device cannot replace itself, so it must not be in its own candidate list.

    Picking it would ask :func:`async_replace_device` to re-key a device onto its
    own identity; keeping it out of the list is what makes that unreachable
    rather than merely ill-advised.
    """
    entry = hub_entry_builder(devices=TWO_DEVICES)
    entry.add_to_hass(hass)

    result = await _replace_target(hass, entry, "Acurite-606TX-42")

    assert [value for value, _ in _options(result, CONF_DEVICE)] == ["ZWeather-9-3"]


async def test_replace_target_sorts_by_key_when_the_kept_device_has_no_model(
    hass, hub_entry_builder
):
    """An unnamed device must not drag every other unnamed device to the top.

    Same-model candidates sort first because a battery swap keeps the model —
    but a kept device with no model at all matches every other model-less
    candidate, which is not a signal, it is an absence. Promoting them would put
    the least identifiable rows first in the most consequential dialog in the
    flow, so a blank model disables the grouping and the list falls back to key
    order.
    """
    old_key = "Mystery-0"
    entry = hub_entry_builder(
        devices={
            old_key: {DEVICE_FIELDS: ["temperature_C"]},
            "Apple-1": {CONF_MODEL: "AppleModel", DEVICE_FIELDS: ["temperature_C"]},
            "Zebra-2": {DEVICE_FIELDS: ["temperature_C"]},
        }
    )
    entry.add_to_hass(hass)

    result = await _replace_target(hass, entry, old_key)

    assert [value for value, _ in _options(result, CONF_DEVICE)] == [
        "Apple-1",
        "Zebra-2",
    ]


async def test_replace_target_puts_same_model_candidates_first(hass, hub_entry_builder):
    """A battery swap keeps the model, so the matching rows lead the list.

    The replacement for a sensor whose transmitter id changed is almost always
    the same model heard under a new key. Sorting those to the top is what makes
    the right row the first one on a hub with dozens of devices.
    """
    old_key = "Acurite-606TX-42"
    entry = hub_entry_builder(
        devices={
            old_key: {CONF_MODEL: "Acurite-606TX", DEVICE_FIELDS: ["temperature_C"]},
            "AAA-1": {CONF_MODEL: "Other-Model", DEVICE_FIELDS: ["temperature_C"]},
            "ZZZ-9": {CONF_MODEL: "Acurite-606TX", DEVICE_FIELDS: ["temperature_C"]},
        }
    )
    entry.add_to_hass(hass)

    result = await _replace_target(hass, entry, old_key)

    assert [value for value, _ in _options(result, CONF_DEVICE)] == ["ZZZ-9", "AAA-1"]


async def test_replace_target_marks_a_pending_candidate_as_not_added_yet(
    hass, hub_entry_builder
):
    """Adopting a never-added device onto another's history needs saying out loud.

    A pending row in this list means something different from a stored one: the
    user is about to adopt hardware they have never added, onto the history of
    one they have. The row is described the way the add-devices step describes it
    — the sighting count is what identifies a sensor put back into service — and
    then explicitly marked.
    """
    old_key = "Acurite-606TX-42"
    new_key = "Acurite-606TX-9999"
    entry = hub_entry_builder(
        devices={
            old_key: {CONF_MODEL: "Acurite-606TX", DEVICE_FIELDS: ["temperature_C"]}
        }
    )
    entry.add_to_hass(hass)
    _install(
        hass, entry, _StubCoordinator({new_key: _pending(new_key, "Acurite-606TX")})
    )

    result = await _replace_target(hass, entry, old_key)

    labels = dict(_options(result, CONF_DEVICE))
    assert list(labels) == [new_key]
    assert labels[new_key].startswith(f"Acurite-606TX ({new_key}) — seen 1x — ")
    assert labels[new_key].endswith(" — not added yet")


async def test_replace_target_aborts_on_a_single_device_hub(hass, hub_entry_builder):
    """With nothing to replace it with, the step says so instead of offering nothing.

    A one-device hub whose coordinator has heard nothing new has an empty
    candidate set. A dropdown with no rows is a dead end the user cannot leave
    except by cancelling; the abort explains why.
    """
    entry = hub_entry_builder(
        devices={
            DEVICE_KEY: {CONF_MODEL: "Acurite-606TX", DEVICE_FIELDS: ["temperature_C"]}
        }
    )
    entry.add_to_hass(hass)

    result = await _replace_target(hass, entry, DEVICE_KEY)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_replacement_candidates"


async def test_replace_target_reports_a_failed_replace_as_a_form_error(
    hass, hub_entry_builder
):
    """A stale picker is a user-facing outcome, not a traceback in the log.

    The candidate list is rendered against a snapshot of the entry, and the
    replace itself reloads that entry — so a submit can legitimately arrive after
    the world moved. Re-showing the form with the list rebuilt is what lets the
    user simply pick again; an escaping exception would leave the dialog dead.
    """
    entry = hub_entry_builder(devices=TWO_DEVICES)
    entry.add_to_hass(hass)

    result = await _replace_target(hass, entry, "Acurite-606TX-42")
    with patch(
        "custom_components.rtl_433.options_flow.async_replace_device",
        side_effect=DeviceReplaceError("gone"),
    ):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_DEVICE: "ZWeather-9-3"}
        )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "replace_target"
    assert result["errors"] == {"base": "replace_failed"}
    assert set(entry.data[CONF_DEVICES]) == set(TWO_DEVICES)


async def test_replace_target_finish_hands_back_the_options_untouched(
    hass, hub_entry_builder
):
    """The helper already wrote ``entry.data``; the flow only has to close cleanly.

    ``async_replace_device`` does the whole re-key and reloads the entry, so this
    step's ``async_create_entry`` exists purely to shut the dialog. Building an
    options payload out of anything but the current options would undo hub
    settings as a side effect of a battery-swap recovery.
    """
    options = {CONF_MANAGE_SETTINGS: True, CONF_AVAILABILITY_TIMEOUT: 1800}
    entry = hub_entry_builder(devices=TWO_DEVICES, options=options)
    entry.add_to_hass(hass)
    snapshot = deepcopy(options)

    result = await _replace_target(hass, entry, "Acurite-606TX-42")
    with patch(
        "custom_components.rtl_433.options_flow.async_replace_device"
    ) as replace:
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_DEVICE: "ZWeather-9-3"}
        )
    await hass.async_block_till_done()

    assert replace.await_args.args[2:] == ("Acurite-606TX-42", "ZWeather-9-3")
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == ""
    assert dict(entry.options) == snapshot


async def test_replace_target_survives_a_coordinator_that_is_not_loaded(
    hass, hub_entry_builder
):
    """With the hub unloaded the stored devices map alone is the candidate set.

    The options flow can be opened against an entry that never loaded, and this
    step reaches for the coordinator's pending and adopted maps. Both have to
    degrade to empty rather than raising, or a user whose server is unreachable
    cannot even see the replace dialog.
    """
    entry = hub_entry_builder(devices=TWO_DEVICES)
    entry.add_to_hass(hass)

    result = await _replace_target(hass, entry, "Acurite-606TX-42")

    assert result["type"] is FlowResultType.FORM
    assert [value for value, _ in _options(result, CONF_DEVICE)] == ["ZWeather-9-3"]


async def test_hub_step_refuses_a_fractional_timeout(hass, hub_entry_builder):
    """The availability timeout is whole seconds, and the form says so.

    The number is compared against a second-resolution "last seen" age and is
    round-tripped through the config entry's JSON store, so half a second is not
    a finer setting — it is a value that means nothing to the watchdog and reads
    back looking like a typo. The field takes an integer and rejects anything
    else rather than quietly truncating it.
    """
    entry = hub_entry_builder()
    entry.add_to_hass(hass)

    result = await _menu(hass, entry, "hub")
    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(
            result["flow_id"],
            {CONF_AVAILABILITY_TIMEOUT: 600.5, CONF_MANAGE_SETTINGS: True},
        )


async def test_device_settings_refuses_a_fractional_timeout(hass, hub_entry_builder):
    """A per-device timeout override is whole seconds too, for the same reason.

    It overrides the hub value and is read by the same watchdog, so a form that
    accepted a float here would let one device carry a setting shaped unlike
    every other timeout in the entry.
    """
    entry = _entry_with_device(hub_entry_builder)
    entry.add_to_hass(hass)

    result = await _device_settings(hass, entry, DEVICE_KEY)
    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(
            result["flow_id"],
            {DEVICE_TIMEOUT_OVERRIDE: 30.5, CALIBRATION_COMMODITY: COMMODITY_NONE},
        )


async def test_device_settings_refuses_a_fractional_clear_delay(
    hass, hub_entry_builder
):
    """The motion clear delay is whole seconds: it schedules a timer callback.

    A sub-second component buys nothing an RF motion sensor can use — the frames
    themselves arrive seconds apart — and would persist a value that reads back
    from the options store looking like a mistake.
    """
    entry = _motion_entry(hub_entry_builder)
    entry.add_to_hass(hass)
    _install_motion_library(hass)

    result = await _device_settings(hass, entry, MOTION_KEY)
    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(
            result["flow_id"],
            {CALIBRATION_COMMODITY: COMMODITY_NONE, DEVICE_MOTION_CLEAR_DELAY: 45.5},
        )
