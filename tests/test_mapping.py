"""Tests for the device-library loader and value transforms.

These exercise the integration's own mapping semantics: descriptor resolution,
the coerce -> scale -> offset -> round transform pipeline, binary payload
mapping (including the inverted ``closed`` contact and the ``battery_ok``
percentage), skip-key exclusion, and the user-override merge.
"""

from __future__ import annotations

import pytest

from custom_components.rtl_433.mapping import (
    apply_transform,
    load_library,
    load_user_overrides,
    lookup,
    merge_overrides,
    should_skip,
)


@pytest.fixture(scope="module")
def library():
    """Load the shipped device library once for the module."""
    return load_library()


def test_lookup_resolves_representative_descriptors(library):
    """A handful of representative fields resolve to the expected descriptors."""
    registry, _ = library

    temp = lookup("temperature_C", registry)
    assert temp is not None
    assert temp.platform == "sensor"
    assert temp.device_class == "temperature"
    assert temp.unit_of_measurement == "°C"
    assert temp.state_class == "measurement"
    assert temp.object_suffix == "T"

    power = lookup("power_W", registry)
    assert power is not None
    assert power.device_class == "power"
    assert power.unit_of_measurement == "W"

    energy = lookup("energy_kWh", registry)
    assert energy is not None
    assert energy.state_class == "total_increasing"

    # A field that has no mapping returns None.
    assert lookup("totally_unknown_field", registry) is None


def test_event_fields_resolve_to_event_platform(library):
    """The three shipped event fields resolve to ``event`` descriptors.

    Each carries the expected ``EventDeviceClass`` value (a plain string on the
    descriptor) and keeps its declared object_suffix.
    """
    registry, _ = library

    expected = {
        "button": "button",
        "motion": "motion",
        "secret_knock": "doorbell",
    }
    for field_key, device_class in expected.items():
        descriptor = lookup(field_key, registry)
        assert descriptor is not None, field_key
        assert descriptor.platform == "event", field_key
        assert descriptor.device_class == device_class, field_key
        # Event descriptors stringify the value directly: no transform/payload.
        assert descriptor.value_transform is None, field_key
        assert descriptor.payload is None, field_key
        # The object_suffix is the field key for all three shipped examples.
        assert descriptor.object_suffix == field_key, field_key


def test_event_fields_not_in_skip_set(library):
    """None of the three event field keys is excluded by the skip-key set."""
    _, skip_keys = library
    for field_key in ("button", "motion", "secret_knock"):
        assert should_skip(field_key, skip_keys) is False, field_key


def test_existing_fields_keep_original_platform(library):
    """Adding the event mappings did not change existing descriptors' platforms."""
    registry, _ = library

    # A representative sensor and binary_sensor field keep their platform.
    assert lookup("temperature_C", registry).platform == "sensor"
    assert lookup("tamper", registry).platform == "binary_sensor"


def test_sensor_transform_pipeline(library):
    """``apply_transform`` rounds, scales, offsets, and coerces as configured."""
    registry, _ = library

    # round: 1
    temp = lookup("temperature_C", registry)
    assert apply_transform(temp, 21.37) == 21.4

    # scale: 3.6, round: 2 -> m/s converted to km/h.
    wind = lookup("wind_avg_m_s", registry)
    assert apply_transform(wind, 3.5) == 12.6

    # int: true forces integer coercion.
    co2 = lookup("co2_ppm", registry)
    assert apply_transform(co2, "812") == 812
    assert isinstance(apply_transform(co2, "812"), int)

    # battery_ok is a *percentage sensor*: scale 99, offset 1, round 0.
    battery = lookup("battery_ok", registry)
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
    wet = lookup("detect_wet", registry)
    assert wet.platform == "binary_sensor"
    assert apply_transform(wet, 1) is True
    assert apply_transform(wet, "1") is True
    assert apply_transform(wet, 0) is False

    # closed: inverted -> on == "0" (0 means the contact is open).
    closed = lookup("closed", registry)
    assert closed.device_class == "opening"
    assert apply_transform(closed, 0) is True
    assert apply_transform(closed, 1) is False

    # A value matching neither token yields None (unknown), not a crash.
    assert apply_transform(wet, "maybe") is None


def test_should_skip_excludes_skip_keys(library):
    """Identity / transport keys are skipped; measurement keys are not."""
    _, skip_keys = library

    # Identity / transport keys from the shipped _skip_keys.yaml.
    for key in ("model", "id", "channel", "subtype", "mic", "protocol", "freq"):
        assert should_skip(key, skip_keys) is True

    # Measurement keys (and ``time``, which the library maps to a timestamp
    # sensor rather than skipping) must not be skipped.
    for key in ("temperature_C", "humidity", "power_W", "time"):
        assert should_skip(key, skip_keys) is False


def test_user_override_merges_and_adds(tmp_path, library):
    """A user-override YAML overrides an existing field and adds a new one."""
    registry, skip_keys = library

    override_file = tmp_path / "rtl_433_mappings.yaml"
    override_file.write_text(
        "\n".join(
            [
                # Override the existing temperature_C descriptor.
                "temperature_C:",
                "  platform: sensor",
                "  device_class: temperature",
                "  unit_of_measurement: K",
                "  state_class: measurement",
                "  name: Kelvin Temp",
                "  object_suffix: K",
                # Add a brand-new field not in the shipped library.
                "vendor_special_field:",
                "  platform: sensor",
                "  name: Special",
                "  object_suffix: SPC",
                # Extend the skip-key set.
                "skip_keys:",
                "  - vendor_noise",
            ]
        ),
        encoding="utf-8",
    )

    merged, merged_skips = load_user_overrides(tmp_path, registry, skip_keys)

    # Override replaced the shipped descriptor.
    overridden = lookup("temperature_C", merged)
    assert overridden.unit_of_measurement == "K"
    assert overridden.name == "Kelvin Temp"

    # New field is present.
    added = lookup("vendor_special_field", merged)
    assert added is not None
    assert added.object_suffix == "SPC"

    # Skip set was unioned with the override.
    assert "vendor_noise" in merged_skips

    # The base registry is untouched (pure merge).
    assert lookup("temperature_C", registry).unit_of_measurement == "°C"


def test_user_override_absent_is_noop(tmp_path, library):
    """No override file means inputs come back unchanged."""
    registry, skip_keys = library
    merged, merged_skips = load_user_overrides(tmp_path, registry, skip_keys)
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
    assert lookup("bad_entry", merged) is None
    assert lookup("good_entry", merged) is not None
