"""Tests for the device-library loader and value transforms.

These exercise the integration's own mapping semantics: descriptor resolution,
the coerce -> scale -> offset -> round transform pipeline, binary payload
mapping (including the inverted ``closed`` contact and the ``battery_ok``
percentage), skip-key exclusion, and the user-override merge.
"""

from __future__ import annotations

import json

import pytest
import yaml

from custom_components.rtl_433.mapping import (
    apply_transform,
    event_driven_field_keys,
    load_library,
    lookup,
    merge_overrides,
    normalize_overrides,
    should_skip,
    validate_user_mappings,
)


@pytest.fixture(scope="module")
def library():
    """Load the shipped device library once for the module."""
    return load_library()


def test_lookup_resolves_representative_descriptors(library):
    """A handful of representative fields resolve to the expected descriptors."""
    registry, _ = library

    temp = lookup("temperature_C", registry=registry)
    assert temp is not None
    assert temp.platform == "sensor"
    assert temp.device_class == "temperature"
    assert temp.unit_of_measurement == "°C"
    assert temp.state_class == "measurement"
    assert temp.object_suffix == "T"

    power = lookup("power_W", registry=registry)
    assert power is not None
    assert power.device_class == "power"
    assert power.unit_of_measurement == "W"

    energy = lookup("energy_kWh", registry=registry)
    assert energy is not None
    assert energy.state_class == "total_increasing"

    # A field that has no mapping returns None.
    assert lookup("totally_unknown_field", registry=registry) is None


def test_device_class_fields_have_no_explicit_name(library):
    """Fields whose name is just the device class ship with ``name=None``.

    A null/omitted ``name`` lets Home Assistant derive (and translate) the
    entity name from ``device_class``. Fields whose label differs from the
    device class keep an explicit name.
    """
    registry, _ = library

    # Auto-named: name omitted so HA names them from device_class.
    for field_key in ("temperature_C", "humidity", "power_W", "wind_avg_m_s"):
        assert lookup(field_key, registry=registry).name is None, field_key

    # Explicit name retained where it carries more than the device class.
    assert lookup("ad_raw", registry=registry).name == "AD raw"
    assert lookup("rssi", registry=registry).name == "RSSI"
    # The gust variants are unified to one label regardless of source field.
    for field_key in ("gust_speed_km_h", "wind_max_km_h", "wind_max_m_s"):
        assert lookup(field_key, registry=registry).name == "Gust speed", field_key


def test_wh51_soil_ad_and_boost_resolve_as_diagnostics(library):
    """The WH51 `ad_raw` and `boost` fields map to integer diagnostic sensors."""
    registry, _ = library

    ad_raw = lookup("ad_raw", registry=registry)
    assert ad_raw is not None
    assert ad_raw.platform == "sensor"
    assert ad_raw.entity_category == "diagnostic"
    assert ad_raw.object_suffix == "ad_raw"
    # Raw ADC count is coerced to a plain int (no unit / device_class).
    assert ad_raw.unit_of_measurement is None
    assert apply_transform(ad_raw, "278") == 278

    boost = lookup("boost", registry=registry)
    assert boost is not None
    assert boost.platform == "sensor"
    assert boost.entity_category == "diagnostic"
    assert boost.object_suffix == "boost"
    assert apply_transform(boost, "7") == 7


def test_event_fields_resolve_to_event_platform(library):
    """The shipped event fields resolve to ``event`` descriptors.

    Each carries the expected ``EventDeviceClass`` value (a plain string on the
    descriptor) and keeps its declared object_suffix. ``motion`` is intentionally
    NOT here: it is now a ``binary_sensor`` (see
    ``test_motion_resolves_to_binary_sensor``).
    """
    registry, _ = library

    expected = {
        "button": "button",
        "secret_knock": "doorbell",
    }
    for field_key, device_class in expected.items():
        descriptor = lookup(field_key, registry=registry)
        assert descriptor is not None, field_key
        assert descriptor.platform == "event", field_key
        assert descriptor.device_class == device_class, field_key
        # Event descriptors stringify the value directly: no transform/payload.
        assert descriptor.value_transform is None, field_key
        assert descriptor.payload is None, field_key
        # The object_suffix is the field key for all three shipped examples.
        assert descriptor.object_suffix == field_key, field_key

    # The doorbell ``secret_knock`` descriptor maps its raw values to the
    # standardized Home Assistant doorbell types so the entity advertises
    # ``ring`` (and stays compliant with the doorbell standard). The plain
    # ``button`` field carries no map (its values stringify verbatim).
    secret_knock = lookup("secret_knock", registry=registry)
    assert secret_knock.event_map == {"0": "ring", "1": "secret_knock"}
    assert lookup("button", registry=registry).event_map is None


def test_event_fields_not_in_skip_set(library):
    """None of the event field keys is excluded by the skip-key set."""
    _, skip_keys = library
    for field_key in ("button", "secret_knock"):
        assert should_skip(field_key, skip_keys) is False, field_key


def test_motion_resolves_to_binary_sensor(library):
    """``motion`` is a detect-only occupancy binary_sensor with a clear delay.

    It moved off the ``event`` platform: it resolves to a ``binary_sensor``
    descriptor with ``device_class == "occupancy"``, only an ``on`` token
    (raw "1"), and a 90s ``clear_delay`` driving the synthesized auto-off.
    """
    registry, skip_keys = library

    motion = lookup("motion", registry=registry)
    assert motion is not None
    assert motion.platform == "binary_sensor"
    assert motion.device_class == "occupancy"
    assert motion.clear_delay == 90
    assert motion.payload == {"on": "1"}
    # Detect-only: raw "1" is on; anything else is unknown (not off).
    assert apply_transform(motion, "1") is True
    assert apply_transform(motion, 1) is True
    assert apply_transform(motion, 0) is None
    # Not skipped.
    assert should_skip("motion", skip_keys) is False


def test_existing_fields_keep_original_platform(library):
    """Adding the event mappings did not change existing descriptors' platforms."""
    registry, _ = library

    # A representative sensor and binary_sensor field keep their platform.
    assert lookup("temperature_C", registry=registry).platform == "sensor"
    assert lookup("tamper", registry=registry).platform == "binary_sensor"


def test_event_driven_flag_parsed_from_library(library):
    """The ``event_driven`` flag is parsed onto the relevant binary descriptors."""
    registry, _ = library

    # Event-driven state fields carry the flag; diagnostic bits do not.
    assert lookup("motion", registry=registry).event_driven is True
    assert lookup("contact_open", registry=registry).event_driven is True
    assert lookup("closed", registry=registry).event_driven is True
    assert lookup("tamper", registry=registry).event_driven is False
    assert lookup("temperature_C", registry=registry).event_driven is False


def test_event_driven_field_keys_derivation(library):
    """The derived set covers event-platform + flagged fields, excludes diagnostics."""
    registry, _ = library

    keys = event_driven_field_keys(registry)

    # platform: event fields auto-qualify (no flag needed).
    assert {"button", "secret_knock"} <= keys
    # event_driven: true binary_sensors are included.
    assert {"motion", "contact_open", "reed_open", "closed", "alarm"} <= keys
    # Diagnostic bits and periodic measurements are excluded.
    assert keys.isdisjoint({"tamper", "battery_ok", "temperature_C", "humidity"})


def test_event_driven_field_keys_includes_model_scoped(library):
    """Model-scoped event-driven descriptors are picked up alongside the flat set."""
    registry, skip_keys = library

    merged, _ = merge_overrides(
        registry,
        skip_keys,
        {
            "models": {
                "Acme-9000": {
                    "blip": {
                        "platform": "binary_sensor",
                        "device_class": "motion",
                        "name": "Blip",
                        "object_suffix": "blip",
                        "payload": {"on": "1", "off": "0"},
                        "event_driven": True,
                    }
                }
            }
        },
    )

    assert "blip" in event_driven_field_keys(merged)


def test_invalid_event_driven_value_is_ignored():
    """A non-bool ``event_driven`` is dropped, defaulting the field to periodic."""
    registry, _ = load_library()
    merged, _ = merge_overrides(
        registry,
        set(),
        {
            "wobble": {
                "platform": "binary_sensor",
                "device_class": "motion",
                "name": "Wobble",
                "object_suffix": "wobble",
                "payload": {"on": "1", "off": "0"},
                "event_driven": "yes-please",  # not a bool -> ignored
            }
        },
    )

    assert lookup("wobble", registry=merged).event_driven is False
    assert "wobble" not in event_driven_field_keys(merged)


def test_sensor_transform_pipeline(library):
    """``apply_transform`` rounds, scales, offsets, and coerces as configured."""
    registry, _ = library

    # round: 1
    temp = lookup("temperature_C", registry=registry)
    assert apply_transform(temp, 21.37) == 21.4

    # scale: 3.6, round: 2 -> m/s converted to km/h.
    wind = lookup("wind_avg_m_s", registry=registry)
    assert apply_transform(wind, 3.5) == 12.6

    # int: true forces integer coercion.
    co2 = lookup("co2_ppm", registry=registry)
    assert apply_transform(co2, "812") == 812
    assert isinstance(apply_transform(co2, "812"), int)

    # battery_ok is a *percentage sensor*: scale 99, offset 1, round 0.
    battery = lookup("battery_ok", registry=registry)
    assert battery.platform == "sensor"
    assert apply_transform(battery, 1) == 100
    assert apply_transform(battery, 0) == 1

    # Non-numeric values pass through untouched.
    assert apply_transform(temp, "n/a") == "n/a"
    # None short-circuits to None.
    assert apply_transform(temp, None) is None


def test_binary_payload_mapping(library):
    """Binary descriptors map raw values to bool, honoring inverted payloads."""
    registry, _ = library

    # detect_wet: on == "1".
    wet = lookup("detect_wet", registry=registry)
    assert wet.platform == "binary_sensor"
    assert apply_transform(wet, 1) is True
    assert apply_transform(wet, "1") is True
    assert apply_transform(wet, 0) is False

    # closed: inverted -> on == "0" (0 means the contact is open).
    closed = lookup("closed", registry=registry)
    assert closed.device_class == "opening"
    assert apply_transform(closed, 0) is True
    assert apply_transform(closed, 1) is False

    # A value matching neither token yields None (unknown), not a crash.
    assert apply_transform(wet, "maybe") is None


def test_should_skip_excludes_skip_keys(library):
    """Identity / transport keys are skipped; measurement keys are not."""
    _, skip_keys = library

    # Identity / transport keys from the shipped _skip_keys.yaml.
    for key in ("model", "id", "channel", "subtype", "mic", "protocol", "freq1"):
        assert should_skip(key, skip_keys) is True

    # Measurement keys (and ``time``, which the library maps to a timestamp
    # sensor, and ``freq``, mapped to a diagnostic Frequency sensor) must not be
    # skipped.
    for key in ("temperature_C", "humidity", "power_W", "time", "freq"):
        assert should_skip(key, skip_keys) is False


def test_user_override_merges_and_adds(library):
    """A user-override object overrides an existing field and adds a new one."""
    registry, skip_keys = library

    overrides = {
        # Override the existing temperature_C descriptor.
        "temperature_C": {
            "platform": "sensor",
            "device_class": "temperature",
            "unit_of_measurement": "K",
            "state_class": "measurement",
            "name": "Kelvin Temp",
            "object_suffix": "K",
        },
        # Add a brand-new field not in the shipped library.
        "vendor_special_field": {
            "platform": "sensor",
            "name": "Special",
            "object_suffix": "SPC",
        },
        # Extend the skip-key set.
        "skip_keys": ["vendor_noise"],
    }

    merged, merged_skips = merge_overrides(registry, skip_keys, overrides)

    # Override replaced the shipped descriptor.
    overridden = lookup("temperature_C", registry=merged)
    assert overridden.unit_of_measurement == "K"
    assert overridden.name == "Kelvin Temp"

    # New field is present.
    added = lookup("vendor_special_field", registry=merged)
    assert added is not None
    assert added.object_suffix == "SPC"

    # Skip set was unioned with the override.
    assert "vendor_noise" in merged_skips

    # The base registry is untouched (pure merge).
    assert lookup("temperature_C", registry=registry).unit_of_measurement == "°C"


def test_user_override_absent_is_noop(library):
    """An empty override object means inputs come back unchanged."""
    registry, skip_keys = library
    merged, merged_skips = merge_overrides(registry, skip_keys, {})
    assert merged == registry
    assert merged_skips == skip_keys


def test_merge_overrides_ignores_malformed_entry(library):
    """A malformed override entry is dropped without taking down the merge."""
    registry, skip_keys = library
    merged, _ = merge_overrides(
        registry,
        skip_keys,
        {
            "bad_entry": "not-a-mapping",
            "good_entry": {
                "platform": "sensor",
                "name": "Good",
                "object_suffix": "G",
            },
        },
    )
    assert lookup("bad_entry", registry=merged) is None
    assert lookup("good_entry", registry=merged) is not None


# --------------------------------------------------------------------------- #
# Component A — model-scoped lookup + specificity-first precedence.            #
# --------------------------------------------------------------------------- #
# An illustrative, non-real model string (the shipped library intentionally
# carries no speculative real-meter consumption mapping, so tests synthesize one
# via merge_overrides).
ILLUSTRATIVE_MODEL = "Illustrative-Meter-Example"

# A model-scoped consumption_data descriptor for the illustrative model: a real
# device_class + a convertible base unit + total_increasing + a scale, i.e. the
# Energy-dashboard-eligible shape Component A enables.
_MODEL_CONSUMPTION = {
    "platform": "sensor",
    "device_class": "energy",
    "unit_of_measurement": "kWh",
    "state_class": "total_increasing",
    "name": "Illustrative Consumption",
    "object_suffix": "consumption",
    "value_transform": {"scale": 0.01},
}


def test_model_scoped_lookup_resolves_then_falls_back_to_global(library):
    """A model-scoped entry wins for its model; other models keep the global one.

    Merging a synthetic ``models:`` block for an illustrative model overrides the
    *global* unitless ``consumption_data`` descriptor only for that model; any
    other model still resolves the shipped global descriptor, and a non-meter
    field (``temperature_C``) is unaffected for the matching model.
    """
    registry, skip_keys = library
    merged, _ = merge_overrides(
        registry,
        skip_keys,
        {"models": {ILLUSTRATIVE_MODEL: {"consumption_data": _MODEL_CONSUMPTION}}},
    )

    # Matching model -> the model-scoped descriptor (Energy-dashboard-eligible).
    scoped = lookup("consumption_data", ILLUSTRATIVE_MODEL, merged)
    assert scoped is not None
    assert scoped.device_class == "energy"
    assert scoped.unit_of_measurement == "kWh"
    assert scoped.state_class == "total_increasing"
    assert scoped.value_transform == {"scale": 0.01}

    # A different model -> the shipped global (unitless) descriptor.
    other = lookup("consumption_data", "Some-Other-Model", merged)
    assert other is not None
    assert other.device_class is None
    assert other.unit_of_measurement is None
    assert other.value_transform == {"int": True}

    # No model context -> the global descriptor too.
    assert lookup("consumption_data", registry=merged).device_class is None

    # A non-meter field is unaffected for the matching model (no regression): it
    # still resolves the shipped global temperature descriptor.
    temp = lookup("temperature_C", ILLUSTRATIVE_MODEL, merged)
    assert temp is not None
    assert temp.device_class == "temperature"
    assert temp.unit_of_measurement == "°C"


def test_precedence_specificity_first(library):
    """Specificity-first: user-model > shipped-model > user-global > shipped-global.

    The decisive case is **shipped-model > user-global**: a shipped model-scoped
    entry must outrank a user-override *global* entry for a matching model.
    """
    registry, skip_keys = library
    field = "consumption_data"

    # Build a base registry that already carries a *shipped-equivalent*
    # model-scoped entry for (ILLUSTRATIVE_MODEL, field).
    shipped_model = dict(_MODEL_CONSUMPTION, name="Shipped Model")
    base, base_skips = merge_overrides(
        registry,
        skip_keys,
        {"models": {ILLUSTRATIVE_MODEL: {field: shipped_model}}},
    )

    # User override carries BOTH a user model-scoped entry and a user global entry
    # for the same field.
    user_model = dict(_MODEL_CONSUMPTION, name="User Model", unit_of_measurement="Wh")
    user_global = {
        "platform": "sensor",
        "device_class": "gas",
        "unit_of_measurement": "m³",
        "state_class": "total_increasing",
        "name": "User Global",
        "object_suffix": "consumption",
    }
    with_user, _ = merge_overrides(
        base,
        base_skips,
        {field: user_global, "models": {ILLUSTRATIVE_MODEL: {field: user_model}}},
    )

    # user-model > shipped-model.
    assert lookup(field, ILLUSTRATIVE_MODEL, with_user).name == "User Model"

    # Drop the user model entry: the *shipped* model entry still wins over the
    # user *global* entry for the matching model (the decisive specificity-first
    # case).
    shipped_model_only, _ = merge_overrides(base, base_skips, {field: user_global})
    decided = lookup(field, ILLUSTRATIVE_MODEL, shipped_model_only)
    assert decided.name == "Shipped Model"
    assert decided.device_class == "energy"

    # A non-matching model falls through to the user global entry.
    non_matching = lookup(field, "Some-Other-Model", shipped_model_only)
    assert non_matching.name == "User Global"
    assert non_matching.device_class == "gas"


# --------------------------------------------------------------------------- #
# validate_user_mappings — the UI accept/reject contract.                      #
# --------------------------------------------------------------------------- #
def test_validate_user_mappings_accepts_valid_object():
    """A well-formed override object (flat + skip_keys) returns no problems."""
    data = {
        "temperature_C": {
            "platform": "sensor",
            "name": "Kelvin Temp",
            "object_suffix": "K",
            "unit_of_measurement": "K",
        },
        "skip_keys": ["vendor_noise"],
    }
    assert validate_user_mappings(data) == []
    # ``None`` (an empty mapping) is also valid.
    assert validate_user_mappings(None) == []


def test_validate_user_mappings_flags_missing_platform():
    """An entry missing the required ``platform`` names that field in a problem."""
    problems = validate_user_mappings(
        {"vendor_field": {"name": "X", "object_suffix": "X"}}
    )
    assert any("vendor_field" in p and "platform" in p for p in problems)


def test_validate_user_mappings_flags_bad_skip_keys_type():
    """A non-list ``skip_keys`` produces a problem naming the ``skip_keys`` field."""
    problems = validate_user_mappings({"skip_keys": "not-a-list"})
    assert any(p.startswith("skip_keys") for p in problems)


def test_validate_user_mappings_flags_unknown_platform():
    """An unsupported ``platform`` value is rejected with a field-naming problem."""
    problems = validate_user_mappings(
        {
            "vendor_field": {
                "platform": "switch",
                "name": "X",
                "object_suffix": "X",
            }
        }
    )
    assert any("vendor_field" in p and "switch" in p for p in problems)


def test_validate_user_mappings_rejects_non_mapping_top_level():
    """A non-mapping top level yields a single self-contained problem."""
    problems = validate_user_mappings(["not", "a", "mapping"])
    assert len(problems) == 1
    assert "object" in problems[0]


# --------------------------------------------------------------------------- #
# normalize_overrides — JSON-safe, payload-canonical round-trip.               #
# --------------------------------------------------------------------------- #
def test_normalize_overrides_canonicalizes_bare_on_off_and_round_trips(library):
    """A binary payload with bare ``on``/``off`` becomes string keys and resolves.

    Building the input via ``yaml.safe_load`` genuinely yields ``True``/``False``
    dict keys (YAML 1.1 parses unquoted ``on``/``off`` as booleans). After
    ``normalize_overrides`` the keys are the canonical strings ``"on"``/``"off"``,
    so the result is ``json.dumps``-able (no non-string keys), and feeding it
    through ``merge_overrides`` + ``apply_transform`` resolves the binary value.
    """
    registry, skip_keys = library

    parsed = yaml.safe_load(
        "battery_low:\n"
        "  platform: binary_sensor\n"
        "  device_class: battery\n"
        "  name: Battery Low\n"
        "  object_suffix: BL\n"
        "  payload:\n"
        "    on: '0'\n"
        "    off: '1'\n"
    )
    # Sanity: PyYAML really did give us boolean keys we need to canonicalise.
    assert set(parsed["battery_low"]["payload"]) == {True, False}

    normalized = normalize_overrides(parsed)

    # Boolean keys are gone; canonical string keys remain.
    payload = normalized["battery_low"]["payload"]
    assert set(payload) == {"on", "off"}
    assert payload == {"on": "0", "off": "1"}
    # JSON-serialisable (would raise if any non-string keys survived).
    assert json.loads(json.dumps(normalized)) == normalized

    # The original input is untouched (pure).
    assert set(parsed["battery_low"]["payload"]) == {True, False}

    # Merge the normalized override and confirm the binary value resolves: on=='0'.
    merged, _ = merge_overrides(registry, skip_keys, normalized)
    descriptor = lookup("battery_low", registry=merged)
    assert descriptor is not None
    assert descriptor.platform == "binary_sensor"
    assert apply_transform(descriptor, "0") is True
    assert apply_transform(descriptor, "1") is False


def test_normalize_overrides_returns_empty_for_non_mapping():
    """A non-mapping input normalizes to an empty dict (never raises)."""
    assert normalize_overrides("nope") == {}
    assert normalize_overrides(None) == {}


# --------------------------------------------------------------------------- #
# Per-entry isolation — two override dicts produce independent registries.     #
# --------------------------------------------------------------------------- #
def test_per_override_isolation_does_not_cross_contaminate(library):
    """Two distinct override sets yield independent merged registries.

    This is the pure-merge core of the per-hub library wiring: hub A overrides
    temperature to Kelvin, hub B overrides it to Fahrenheit and adds a private
    field. Each merged registry reflects only its own override, the other's
    private field is absent, and the shared base registry is untouched.
    """
    registry, skip_keys = library

    overrides_a = {
        "temperature_C": {
            "platform": "sensor",
            "name": "A Kelvin",
            "object_suffix": "K",
            "unit_of_measurement": "K",
        }
    }
    overrides_b = {
        "temperature_C": {
            "platform": "sensor",
            "name": "B Fahrenheit",
            "object_suffix": "F",
            "unit_of_measurement": "°F",
        },
        "hub_b_only_field": {
            "platform": "sensor",
            "name": "B Only",
            "object_suffix": "BO",
        },
    }

    merged_a, _ = merge_overrides(registry, skip_keys, overrides_a)
    merged_b, _ = merge_overrides(registry, skip_keys, overrides_b)

    # Each hub sees only its own temperature override.
    assert lookup("temperature_C", registry=merged_a).unit_of_measurement == "K"
    assert lookup("temperature_C", registry=merged_b).unit_of_measurement == "°F"

    # Hub B's private field does not leak into hub A.
    assert lookup("hub_b_only_field", registry=merged_a) is None
    assert lookup("hub_b_only_field", registry=merged_b) is not None

    # The shared shipped base is untouched by either merge.
    assert lookup("temperature_C", registry=registry).unit_of_measurement == "°C"


def test_existing_themed_file_loads_identically(library, tmp_path):
    """Regression: an existing themed file loads identically via the loader.

    Re-loading the shipped library from a copy of the existing themed
    ``power_electrical.yaml`` (which carries no ``models:`` block) yields the same
    flat descriptors as the packaged load, with an empty model-scoped table —
    proving the additive ``models:`` parsing did not regress flat parsing.
    """
    from pathlib import Path
    import shutil

    registry, _ = library

    src = (
        Path(__file__).resolve().parents[1]
        / "custom_components"
        / "rtl_433"
        / "device_library"
    )
    shutil.copy(src / "power_electrical.yaml", tmp_path / "power_electrical.yaml")

    reloaded, _ = load_library(tmp_path)
    # The themed file carries no models: block.
    assert reloaded.models == {}
    # Every descriptor it defines matches the packaged registry exactly.
    for field_key in (
        "power_W",
        "energy_kWh",
        "consumption",
        "consumption_data",
        "ext_power",
    ):
        assert reloaded.flat[field_key] == registry.flat[field_key]
