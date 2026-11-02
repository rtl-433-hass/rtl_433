"""Direct-call tests for the shared settings builders.

``custom_components/rtl_433/settings.py`` owns the rules about what a submitted
value *means* -- when it is stored, when it clears, and whether it lands in
``entry.data`` or ``entry.options``. Every existing test reaches those rules
through a caller (the options flow or the WebSocket settings handler), and a
caller fixes several inputs at once: it always passes a real ``device_key`` that
is already in ``entry.data[devices]``, always has a hub registry loaded, and
never submits a clear on a device that has nothing stored. The edges of these
builders are therefore reachable only by calling them directly, which is what
this file does.

The functions touch Home Assistant only through ``hass.data``, so the tests use
a small stand-in rather than a live ``hass``; that keeps them synchronous and
lets each one state the exact hub state it is describing.
"""

from __future__ import annotations

from typing import Any

from pyrtl_433.library import FieldDescriptor, Registry
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.rtl_433.const import (
    CALIBRATION_COMMODITY,
    CALIBRATION_SCALE,
    CALIBRATION_UNIT,
    COMMODITY_GAS,
    COMMODITY_NONE,
    COMMODITY_WATER,
    CONF_AVAILABILITY_TIMEOUT,
    CONF_DEVICES,
    CONF_MANAGE_SETTINGS,
    CONF_MODEL,
    DATA_ENTRY_LIBRARY,
    DEVICE_CALIBRATION,
    DEVICE_FIELDS,
    DEVICE_MOTION_CLEAR_DELAY,
    DEVICE_TIMEOUT_OVERRIDE,
    DOMAIN,
)
from custom_components.rtl_433.settings import (
    build_device_data,
    build_device_options,
    build_receiver_options,
    device_defaults,
    is_motion_bearing,
)
from homeassistant.const import CONF_HOST, UnitOfVolume

DEVICE = "acme-pir-7"
OTHER_DEVICE = "acme-therm-3"
MODEL = "Acme-PIR"

# A calibration the user has already saved, and the normalized form the form is
# handed back.
STORED_CALIBRATION = {
    CALIBRATION_COMMODITY: COMMODITY_WATER,
    CALIBRATION_UNIT: UnitOfVolume.CUBIC_METERS,
    CALIBRATION_SCALE: 10,
}
NORMALIZED_CALIBRATION = {
    CALIBRATION_COMMODITY: COMMODITY_WATER,
    CALIBRATION_UNIT: UnitOfVolume.CUBIC_METERS,
    CALIBRATION_SCALE: 10.0,
}

# The exact key set a device-settings form reads. Both editors index this dict
# by name, so a renamed key is a form that silently loses a field.
DEVICE_DEFAULT_KEYS = {
    "device_key",
    "label",
    "model",
    DEVICE_TIMEOUT_OVERRIDE,
    DEVICE_MOTION_CLEAR_DELAY,
    "motion",
    "calibration",
    "commodity",
}


def _descriptor(field_key: str, *, clear_delay: int | None) -> FieldDescriptor:
    """Build a binary_sensor descriptor, auto-clearing or not."""
    return FieldDescriptor(
        field_key=field_key,
        platform="binary_sensor",
        name=field_key.replace("_", " ").title(),
        object_suffix=field_key,
        clear_delay=clear_delay,
    )


# A hub registry deliberately unlike the shipped library: ``pir_trip`` exists
# only here, and ``contact`` auto-clears only on one model. Both are how a
# user's own ``user_mappings`` reach the motion test.
HUB_REGISTRY = Registry(
    flat={
        "pir_trip": _descriptor("pir_trip", clear_delay=30),
        "contact": _descriptor("contact", clear_delay=None),
        "vibration": _descriptor("vibration", clear_delay=20),
    },
    models={
        MODEL: {"contact": _descriptor("contact", clear_delay=45)},
        "Quiet-Contact": {"vibration": _descriptor("vibration", clear_delay=None)},
    },
)


class _FakeHass:
    """The slice of ``hass`` these builders touch: ``hass.data``, nothing else."""

    def __init__(self, data: dict[str, Any] | None = None) -> None:
        self.data = data or {}


class _FakeEvent:
    """A stand-in for the coordinator's last ``NormalizedEvent`` for a device."""

    def __init__(self, fields: dict[str, Any]) -> None:
        self.fields = fields


class _FakeCoordinator:
    """A stand-in for the running coordinator's ``devices`` cache."""

    def __init__(self, devices: dict[str, _FakeEvent]) -> None:
        self.devices = devices


def _entry(
    *, data: dict[str, Any] | None = None, options: dict[str, Any] | None = None
) -> MockConfigEntry:
    """Build a hub entry carrying exactly the state a test describes."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="test hub",
        data=data if data is not None else {},
        options=options if options is not None else {},
        version=2,
    )


def _hass(
    entry: MockConfigEntry,
    *,
    registry: Registry | None = None,
    fields: dict[str, Any] | None = None,
) -> _FakeHass:
    """Put a loaded registry and/or a last-seen event where the builders look."""
    domain_data: dict[str, Any] = {}
    if registry is not None:
        domain_data[DATA_ENTRY_LIBRARY] = {entry.entry_id: (registry, set())}
    if fields is not None:
        domain_data[entry.entry_id] = _FakeCoordinator({DEVICE: _FakeEvent(fields)})
    return _FakeHass({DOMAIN: domain_data})


def _device_entry(
    record: dict[str, Any], *, options: dict[str, Any] | None = None
) -> MockConfigEntry:
    """Build an entry whose device map holds ``record`` under :data:`DEVICE`."""
    return _entry(
        data={CONF_HOST: "rtl433.local", CONF_DEVICES: {DEVICE: record}},
        options=options,
    )


# ---------------------------------------------------------------------------
# is_motion_bearing -- which devices are offered a clear-delay knob
# ---------------------------------------------------------------------------


def test_is_motion_bearing_reads_the_hubs_own_registry_not_the_shipped_library():
    """A field a user mapped themselves must still earn the clear-delay knob.

    ``pir_trip`` exists in no shipped library file. If the test consulted the
    default library instead of the hub's merged registry, every device whose
    only auto-clearing field came from the user's own ``user_mappings`` would be
    denied the knob -- the binary_sensor would turn on and stay on, with the one
    control that fixes it hidden.
    """
    entry = _device_entry({CONF_MODEL: MODEL, DEVICE_FIELDS: ["pir_trip"]})
    assert is_motion_bearing(_hass(entry, registry=HUB_REGISTRY), entry, DEVICE) is True


def test_is_motion_bearing_resolves_the_field_against_the_devices_own_model():
    """A model-scoped mapping is the point of scoping: it must be consulted.

    ``contact`` does not auto-clear globally but does on this model. Resolving
    without the model would fall back to the global entry and hide the knob on
    exactly the devices a model-scoped mapping was written for.
    """
    entry = _device_entry({CONF_MODEL: MODEL, DEVICE_FIELDS: ["contact"]})
    assert is_motion_bearing(_hass(entry, registry=HUB_REGISTRY), entry, DEVICE) is True


def test_is_motion_bearing_lets_a_model_scoped_mapping_take_the_clear_delay_away():
    """Scoping must work in both directions, or the knob appears where it does nothing.

    ``vibration`` auto-clears globally; this model's own mapping says it does
    not. The knob has to follow the model's answer, not the global one.
    """
    entry = _device_entry({CONF_MODEL: "Quiet-Contact", DEVICE_FIELDS: ["vibration"]})
    assert (
        is_motion_bearing(_hass(entry, registry=HUB_REGISTRY), entry, DEVICE) is False
    )


def test_is_motion_bearing_ignores_a_field_that_resolves_without_a_clear_delay():
    """A thermometer must not be offered a clear-delay, or the knob controls nothing.

    ``contact`` resolves to a real descriptor on an unscoped model, but one with
    no ``clear_delay``. Resolving is not enough -- the descriptor has to actually
    auto-clear.
    """
    entry = _device_entry({CONF_MODEL: "Generic-Contact", DEVICE_FIELDS: ["contact"]})
    assert (
        is_motion_bearing(_hass(entry, registry=HUB_REGISTRY), entry, DEVICE) is False
    )


def test_is_motion_bearing_is_true_when_only_one_of_several_fields_auto_clears():
    """One auto-clearing field is enough; devices rarely report only that field.

    A PIR that also reports battery and temperature is still a motion device,
    and the unresolvable and non-clearing fields alongside it must neither hide
    the knob nor make the lookup raise.
    """
    entry = _device_entry(
        {
            CONF_MODEL: MODEL,
            DEVICE_FIELDS: ["battery_unknown_to_the_library", "contact", "pir_trip"],
        }
    )
    assert is_motion_bearing(_hass(entry, registry=HUB_REGISTRY), entry, DEVICE) is True


def test_is_motion_bearing_is_false_for_a_device_with_no_observed_fields_yet():
    """A device adopted before its first event must not break the settings form.

    The record exists but has no ``fields`` list yet. Treating that as "cannot
    tell" and answering ``False`` is what keeps the form rendering; raising
    would take the whole device-settings page down.
    """
    entry = _device_entry({CONF_MODEL: MODEL})
    assert (
        is_motion_bearing(_hass(entry, registry=HUB_REGISTRY), entry, DEVICE) is False
    )


def test_is_motion_bearing_is_false_for_a_device_the_hub_has_never_stored():
    """A stale device key from an open form must answer, not raise.

    The panel can ask about a device that has since been removed or replaced.
    The answer is "no knob", not a traceback in the middle of a settings page.
    """
    entry = _device_entry({CONF_MODEL: MODEL, DEVICE_FIELDS: ["pir_trip"]})
    assert (
        is_motion_bearing(_hass(entry, registry=HUB_REGISTRY), entry, "never-seen")
        is False
    )


def test_is_motion_bearing_is_false_before_the_hub_has_stored_any_devices():
    """A hub that has adopted nothing has no device map at all in ``entry.data``.

    Reading straight through the absent key would raise on a brand-new hub, so
    the missing map has to read as an empty one.
    """
    entry = _entry(data={CONF_HOST: "rtl433.local"})
    assert (
        is_motion_bearing(_hass(entry, registry=HUB_REGISTRY), entry, DEVICE) is False
    )


def test_is_motion_bearing_is_false_while_the_hub_registry_is_still_loading():
    """Before the library is cached, "cannot tell yet" must read as "no knob".

    ``entry_registry`` returns ``None`` during setup. The field keys here exist
    only in the hub registry, so a lookup made without it finds nothing -- and
    the knob simply does not appear rather than the form failing.
    """
    entry = _device_entry({CONF_MODEL: MODEL, DEVICE_FIELDS: ["pir_trip"]})
    assert is_motion_bearing(_FakeHass(), entry, DEVICE) is False


# ---------------------------------------------------------------------------
# device_defaults -- what a device-settings form is pre-filled with
# ---------------------------------------------------------------------------


def test_device_defaults_returns_exactly_the_keys_a_settings_form_reads():
    """Both editors index this dict by name, so a renamed key loses a whole field.

    The panel's device page and the options flow each pull these out by literal
    key. A key that drifts does not fail anywhere -- the field just renders
    empty and the user's stored value looks lost.
    """
    entry = _device_entry({CONF_MODEL: MODEL, DEVICE_FIELDS: ["pir_trip"]})
    result = device_defaults(_hass(entry, registry=HUB_REGISTRY), entry, DEVICE)
    assert set(result) == DEVICE_DEFAULT_KEYS


def test_device_defaults_describes_the_device_the_form_was_opened_for():
    """Every value must be read off the asked-for device, not an incidental one.

    The label, model and motion flag are what tell a user which of several
    identical-looking sensors they are editing; sourcing any of them from the
    wrong key or the wrong record would mislabel the page.
    """
    entry = _device_entry({CONF_MODEL: MODEL, DEVICE_FIELDS: ["pir_trip"]})
    result = device_defaults(_hass(entry, registry=HUB_REGISTRY), entry, DEVICE)
    assert result["device_key"] == DEVICE
    assert result["model"] == MODEL
    assert result["label"] == f"{MODEL} ({DEVICE})"
    assert result["motion"] is True


def test_device_defaults_pre_fills_the_stored_timeout_and_clear_delay():
    """A form that forgets the saved values reads as "unset" and clears them on save.

    The override lives in ``entry.data`` and the clear-delay in
    ``entry.options``; both have to arrive in the form, or the next save writes
    blanks over settings the user deliberately made.
    """
    entry = _device_entry(
        {CONF_MODEL: MODEL, DEVICE_FIELDS: ["pir_trip"], DEVICE_TIMEOUT_OVERRIDE: 900},
        options={CONF_DEVICES: {DEVICE: {DEVICE_MOTION_CLEAR_DELAY: 45}}},
    )
    result = device_defaults(_hass(entry, registry=HUB_REGISTRY), entry, DEVICE)
    assert result[DEVICE_TIMEOUT_OVERRIDE] == 900
    assert result[DEVICE_MOTION_CLEAR_DELAY] == 45


def test_device_defaults_prefers_a_saved_calibration_over_the_decoded_hint():
    """A user who has already said "water" must not be asked again -- or contradicted.

    This meter decodes as gas but the user has saved a water calibration. The
    saved answer wins, and the form is handed the normalized triple rather than
    the raw stored one.
    """
    entry = _device_entry(
        {
            CONF_MODEL: MODEL,
            DEVICE_FIELDS: ["pir_trip"],
            DEVICE_CALIBRATION: STORED_CALIBRATION,
        }
    )
    hass = _hass(entry, registry=HUB_REGISTRY, fields={"MeterType": "Gas"})
    result = device_defaults(hass, entry, DEVICE)
    assert result["calibration"] == NORMALIZED_CALIBRATION
    assert result["commodity"] == COMMODITY_WATER


def test_device_defaults_falls_back_to_the_decoded_hint_when_nothing_is_saved():
    """The guess waiting in the form is what makes per-device calibration findable.

    With no saved calibration the commodity comes from the device's own last
    decoded event -- for the device being edited, not for whichever device
    happens to be first in the coordinator's cache.
    """
    entry = _device_entry({CONF_MODEL: MODEL, DEVICE_FIELDS: ["pir_trip"]})
    hass = _hass(entry, registry=HUB_REGISTRY, fields={"MeterType": "Gas"})
    result = device_defaults(hass, entry, DEVICE)
    assert result["calibration"] is None
    assert result["commodity"] == COMMODITY_GAS


def test_device_defaults_answers_blankly_for_a_device_the_hub_has_never_stored():
    """An open form outliving its device must render empty, not crash the page.

    Every field falls back to "nothing stored" and the label degrades to the raw
    key, so the settings page still renders and the user can back out of it.
    """
    entry = _device_entry({CONF_MODEL: MODEL, DEVICE_FIELDS: ["pir_trip"]})
    result = device_defaults(_hass(entry, registry=HUB_REGISTRY), entry, "never-seen")
    assert result["model"] is None
    assert result["label"] == "never-seen (never-seen)"
    assert result[DEVICE_TIMEOUT_OVERRIDE] is None
    assert result[DEVICE_MOTION_CLEAR_DELAY] is None
    assert result["calibration"] is None
    assert result["commodity"] == COMMODITY_NONE
    assert result["motion"] is False


def test_device_defaults_works_before_the_hub_has_stored_any_devices():
    """A hub with no device map yet must not raise from the settings path.

    ``entry.data`` carries no ``devices`` key at all until the first adoption;
    reading straight through it would make the page fail on a new hub.
    """
    entry = _entry(data={CONF_HOST: "rtl433.local"})
    result = device_defaults(_hass(entry, registry=HUB_REGISTRY), entry, DEVICE)
    assert result["model"] is None
    assert result["commodity"] == COMMODITY_NONE


# ---------------------------------------------------------------------------
# build_device_data -- the override + calibration half of a device save
# ---------------------------------------------------------------------------


def test_build_device_data_clears_an_override_and_calibration_on_a_blank_save():
    """Blanking a field has to remove it, not leave the old value behind.

    A cleared timeout must fall back to the hub default and a cleared
    calibration to the library descriptor. If either key survived the save, the
    user would blank the field, save, and find the old number still in force.
    """
    entry = _device_entry(
        {
            CONF_MODEL: MODEL,
            DEVICE_FIELDS: ["pir_trip"],
            DEVICE_TIMEOUT_OVERRIDE: 900,
            DEVICE_CALIBRATION: STORED_CALIBRATION,
        }
    )
    record = build_device_data(entry, DEVICE, override=None, calibration=None)[
        CONF_DEVICES
    ][DEVICE]
    assert DEVICE_TIMEOUT_OVERRIDE not in record
    assert DEVICE_CALIBRATION not in record
    # The identity of the device survives the clear.
    assert record[CONF_MODEL] == MODEL
    assert record[DEVICE_FIELDS] == ["pir_trip"]


def test_build_device_data_stores_the_values_it_is_given():
    """The saved numbers are what the availability timer and the meter scale read.

    Storing something other than what was submitted would silently apply a
    different timeout, or a different multiplier on a utility meter.
    """
    entry = _device_entry({CONF_MODEL: MODEL, DEVICE_FIELDS: ["pir_trip"]})
    record = build_device_data(
        entry, DEVICE, override=900, calibration=NORMALIZED_CALIBRATION
    )[CONF_DEVICES][DEVICE]
    assert record[DEVICE_TIMEOUT_OVERRIDE] == 900
    assert record[DEVICE_CALIBRATION] == NORMALIZED_CALIBRATION


def test_build_device_data_retires_a_clear_delay_left_in_data_by_the_migration():
    """A migrated hub could otherwise never clear its motion delay.

    The delay is read from options first and data second. Leave the migrated
    copy in data and blanking the field empties options, the read falls back to
    data, and the old number reappears on the next reload.
    """
    entry = _device_entry(
        {
            CONF_MODEL: MODEL,
            DEVICE_FIELDS: ["pir_trip"],
            DEVICE_MOTION_CLEAR_DELAY: 60,
        }
    )
    record = build_device_data(entry, DEVICE, override=900, calibration=None)[
        CONF_DEVICES
    ][DEVICE]
    assert DEVICE_MOTION_CLEAR_DELAY not in record


def test_build_device_data_clears_cleanly_on_a_device_that_has_nothing_stored():
    """Saving a form the user never edited must not raise.

    A device adopted and opened but left alone has none of these keys. Every
    save submits all three fields, so a clear on an absent key is the common
    case, not an edge one.
    """
    entry = _device_entry({CONF_MODEL: MODEL, DEVICE_FIELDS: ["pir_trip"]})
    record = build_device_data(entry, DEVICE, override=None, calibration=None)[
        CONF_DEVICES
    ][DEVICE]
    assert record == {CONF_MODEL: MODEL, DEVICE_FIELDS: ["pir_trip"]}


def test_build_device_data_creates_the_record_for_a_device_not_yet_in_the_map():
    """The first save on a hub must create the device map, not read through it.

    ``entry.data`` carries no ``devices`` key until something is written there,
    and a settings save can be the thing that writes it -- a device whose
    adoption is still in flight, or a form submitted against a record another
    save has just removed. Either way the save has to land, not raise.
    """
    entry = _entry(data={CONF_HOST: "rtl433.local"})
    data = build_device_data(entry, DEVICE, override=900, calibration=None)
    assert data[CONF_DEVICES] == {DEVICE: {DEVICE_TIMEOUT_OVERRIDE: 900}}


def test_build_device_data_leaves_the_rest_of_the_hub_alone():
    """One device's save must not disturb the hub's other devices or its connection.

    This dict replaces ``entry.data`` wholesale, so anything it drops is gone:
    the host the hub connects to, and every other adopted device's record.
    """
    entry = _entry(
        data={
            CONF_HOST: "rtl433.local",
            CONF_DEVICES: {
                DEVICE: {CONF_MODEL: MODEL},
                OTHER_DEVICE: {CONF_MODEL: "Acme-Therm", DEVICE_TIMEOUT_OVERRIDE: 120},
            },
        }
    )
    data = build_device_data(entry, DEVICE, override=900, calibration=None)
    assert data[CONF_HOST] == "rtl433.local"
    assert data[CONF_DEVICES][OTHER_DEVICE] == {
        CONF_MODEL: "Acme-Therm",
        DEVICE_TIMEOUT_OVERRIDE: 120,
    }


def test_build_device_data_does_not_alias_the_live_entry():
    """The caller hands this to ``async_update_entry``; sharing it would corrupt it.

    Every level is copied, so building the new mapping cannot mutate the entry
    that is still live -- a half-applied save is worse than a failed one.
    """
    entry = _device_entry({CONF_MODEL: MODEL, DEVICE_TIMEOUT_OVERRIDE: 120})
    data = build_device_data(entry, DEVICE, override=900, calibration=None)
    assert data is not entry.data
    assert data[CONF_DEVICES] is not entry.data[CONF_DEVICES]
    assert entry.data[CONF_DEVICES][DEVICE][DEVICE_TIMEOUT_OVERRIDE] == 120


# ---------------------------------------------------------------------------
# build_device_options -- the clear-delay half of a device save
# ---------------------------------------------------------------------------


def test_build_device_options_stores_the_delay_under_the_device():
    """The motion binary_sensor's auto-off timer is read straight from this value."""
    entry = _entry(data={CONF_HOST: "rtl433.local"})
    options = build_device_options(entry, DEVICE, motion_clear_delay=45)
    assert options[CONF_DEVICES][DEVICE] == {DEVICE_MOTION_CLEAR_DELAY: 45}


def test_build_device_options_drops_a_device_whose_last_override_is_cleared():
    """A cleared delay must leave no trace, or the per-device map grows forever.

    An emptied sub-map left in place means a hub that has never kept an override
    still accumulates one entry per device it has ever opened a form for.
    """
    entry = _entry(
        data={CONF_HOST: "rtl433.local"},
        options={CONF_DEVICES: {DEVICE: {DEVICE_MOTION_CLEAR_DELAY: 45}}},
    )
    options = build_device_options(entry, DEVICE, motion_clear_delay=None)
    assert DEVICE not in options[CONF_DEVICES]


def test_build_device_options_keeps_a_devices_other_options_when_the_delay_clears():
    """Clearing one knob must not wipe the device's other per-device options.

    The sub-map is dropped only when it is genuinely empty; anything else stored
    alongside the delay keeps the device in the map.
    """
    entry = _entry(
        data={CONF_HOST: "rtl433.local"},
        options={
            CONF_DEVICES: {
                DEVICE: {DEVICE_MOTION_CLEAR_DELAY: 45, "future_knob": "keep me"}
            }
        },
    )
    options = build_device_options(entry, DEVICE, motion_clear_delay=None)
    assert options[CONF_DEVICES][DEVICE] == {"future_knob": "keep me"}


def test_build_device_options_clears_cleanly_when_nothing_was_stored():
    """Saving an untouched form must not raise on a device with no options yet.

    Every device save submits the clear-delay field, so clearing a delay that
    was never set is the ordinary path.
    """
    entry = _entry(data={CONF_HOST: "rtl433.local"})
    options = build_device_options(entry, DEVICE, motion_clear_delay=None)
    assert options[CONF_DEVICES] == {}


def test_build_device_options_leaves_other_devices_and_hub_options_alone():
    """This dict replaces ``entry.options``, so anything it drops is really dropped.

    The hub's own availability timeout and every other device's delay have to
    survive one device's save.
    """
    entry = _entry(
        data={CONF_HOST: "rtl433.local"},
        options={
            CONF_AVAILABILITY_TIMEOUT: 300,
            CONF_DEVICES: {OTHER_DEVICE: {DEVICE_MOTION_CLEAR_DELAY: 20}},
        },
    )
    options = build_device_options(entry, DEVICE, motion_clear_delay=45)
    assert options[CONF_AVAILABILITY_TIMEOUT] == 300
    assert options[CONF_DEVICES][OTHER_DEVICE] == {DEVICE_MOTION_CLEAR_DELAY: 20}
    assert options is not entry.options


# ---------------------------------------------------------------------------
# build_receiver_options -- the hub-level save
# ---------------------------------------------------------------------------


def test_build_hub_options_drops_a_stored_timeout_when_the_hub_returns_to_defaults():
    """Returning to per-device-type defaults must actually remove the hub's value.

    A hub timeout left in options keeps masking the per-device-class defaults,
    so an event-driven device -- a doorbell that has not rung in ten minutes --
    still goes unavailable even though the user switched the override off.
    """
    entry = _entry(
        data={CONF_HOST: "rtl433.local"}, options={CONF_AVAILABILITY_TIMEOUT: 300}
    )
    options = build_receiver_options(entry, None, True)
    assert CONF_AVAILABILITY_TIMEOUT not in options
    assert options[CONF_MANAGE_SETTINGS] is True


def test_build_hub_options_clears_cleanly_when_no_timeout_was_stored():
    """A hub that never set a timeout must still be able to save its other setting.

    The hub form submits both keys every time, so "unset, and still unset" is
    the ordinary save on most hubs.
    """
    entry = _entry(data={CONF_HOST: "rtl433.local"})
    options = build_receiver_options(entry, None, False)
    assert CONF_AVAILABILITY_TIMEOUT not in options
    assert options[CONF_MANAGE_SETTINGS] is False


def test_build_hub_options_stores_zero_as_a_deliberate_never_expire():
    """``0`` is a choice -- never expire -- not an absent value.

    Deciding by truthiness rather than by ``None`` would turn "never expire"
    into "use the defaults", which is close to its opposite.
    """
    entry = _entry(data={CONF_HOST: "rtl433.local"})
    options = build_receiver_options(entry, 0, True)
    assert options[CONF_AVAILABILITY_TIMEOUT] == 0


def test_build_hub_options_keeps_the_per_device_map_and_does_not_alias_the_entry():
    """The hub form owns two keys; the per-device overrides live alongside them.

    This dict replaces ``entry.options`` wholesale, so a hub save that dropped
    the device sub-map would clear every per-device clear-delay at once.
    """
    entry = _entry(
        data={CONF_HOST: "rtl433.local"},
        options={CONF_DEVICES: {DEVICE: {DEVICE_MOTION_CLEAR_DELAY: 45}}},
    )
    options = build_receiver_options(entry, 600, True)
    assert options[CONF_DEVICES] == {DEVICE: {DEVICE_MOTION_CLEAR_DELAY: 45}}
    assert options[CONF_AVAILABILITY_TIMEOUT] == 600
    assert options is not entry.options
