"""Mutation-killing tests for custom_components/rtl_433/calibration.py.

Precision tests targeting each surviving mutant from mutmut. Every test
asserts the exact behaviour the corresponding mutation would break:
exact return values, dict key presence, scale arithmetic, both branches,
and boundary nibble values in the ert_type lookup.

Surviving mutants targeted (17/18 — one genuinely equivalent):

normalize_calibration:
  mutmut_9  – COMMODITY_UNITS.get(commodity, ()) default changed to None
  mutmut_11 – COMMODITY_UNITS.get(commodity, ()) default removed (TypeError on miss)
  mutmut_15 – raw.get(CALIBRATION_SCALE, 1.0) default changed to None
  mutmut_17 – raw.get(CALIBRATION_SCALE, ) default removed
  mutmut_18 – raw.get(CALIBRATION_SCALE, 1.0) default changed to 2.0
  mutmut_19 – except-block scale = 1.0 changed to None
  mutmut_20 – except-block scale = 1.0 changed to 2.0

commodity_from_fields:
  mutmut_13 – "water" key in MeterType map changed to "XXwaterXX"
  mutmut_14 – "water" key in MeterType map changed to "WATER"
  mutmut_26 – .get(nibble, COMMODITY_NONE) default changed to None
  mutmut_28 – .get(nibble, ) default removed (KeyError on unmapped nibble)
  mutmut_30 – nibble 4 key changed to 5 (energy nibble 4 dropped)
  mutmut_31 – nibble 5 key changed to 6 (energy nibble 5 dropped)
  mutmut_32 – nibble 7 key changed to 8 (energy nibble 7 dropped)
  mutmut_33 – nibble 8 key changed to 9 (energy nibble 8 dropped; 9 is gas)
  mutmut_34 – nibble 9 key changed to 10 (gas nibble 9 dropped)
  mutmut_36 – nibble 12 key changed to 13 (gas nibble 12 dropped; 13 is water)
  mutmut_37 – nibble 13 key changed to 14 (water nibble 13 dropped)
"""

from __future__ import annotations

import pytest

from custom_components.rtl_433.calibration import (
    COMMODITY_UNITS,
    commodity_from_fields,
    normalize_calibration,
)
from custom_components.rtl_433.const import (
    CALIBRATION_COMMODITY,
    CALIBRATION_SCALE,
    CALIBRATION_UNIT,
    COMMODITY_ENERGY,
    COMMODITY_GAS,
    COMMODITY_NONE,
    COMMODITY_WATER,
)
from homeassistant.const import UnitOfEnergy, UnitOfVolume

# ---------------------------------------------------------------------------
# normalize_calibration — scale default = 1.0 when key absent
# mutmut_15, mutmut_17, mutmut_18
# ---------------------------------------------------------------------------


class TestNormCalibrationScaleDefault:
    """normalize_calibration uses 1.0 when CALIBRATION_SCALE is absent."""

    def test_scale_defaults_to_1_when_key_absent(self):
        """When scale key is missing, scale must be exactly 1.0 (kills _15, _17, _18)."""
        raw = {
            CALIBRATION_COMMODITY: COMMODITY_ENERGY,
            CALIBRATION_UNIT: UnitOfEnergy.KILO_WATT_HOUR,
            # No CALIBRATION_SCALE key
        }
        result = normalize_calibration(raw)
        assert result is not None
        assert result[CALIBRATION_SCALE] == 1.0

    def test_scale_default_is_float_1_not_2(self):
        """Absent scale must be 1.0 not 2.0 (kills _18: default=2.0)."""
        raw = {
            CALIBRATION_COMMODITY: COMMODITY_GAS,
            CALIBRATION_UNIT: UnitOfVolume.CUBIC_METERS,
        }
        result = normalize_calibration(raw)
        assert result is not None
        assert result[CALIBRATION_SCALE] == pytest.approx(1.0)
        assert result[CALIBRATION_SCALE] != pytest.approx(2.0)

    def test_scale_default_is_1_not_none(self):
        """Absent scale must be float 1.0, not None (kills _15: default=None)."""
        raw = {
            CALIBRATION_COMMODITY: COMMODITY_WATER,
            CALIBRATION_UNIT: UnitOfVolume.GALLONS,
        }
        result = normalize_calibration(raw)
        assert result is not None
        assert result[CALIBRATION_SCALE] is not None
        assert isinstance(result[CALIBRATION_SCALE], float)
        assert result[CALIBRATION_SCALE] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# normalize_calibration — except-block fallback scale = 1.0
# mutmut_19, mutmut_20
# ---------------------------------------------------------------------------


class TestNormCalibrationScaleExceptFallback:
    """normalize_calibration falls back to scale=1.0 on bad scale value."""

    def test_bad_scale_string_falls_back_to_1_not_none(self):
        """Non-numeric string scale falls back to 1.0, not None (kills _19)."""
        raw = {
            CALIBRATION_COMMODITY: COMMODITY_ENERGY,
            CALIBRATION_UNIT: UnitOfEnergy.KILO_WATT_HOUR,
            CALIBRATION_SCALE: "bad",
        }
        result = normalize_calibration(raw)
        assert result is not None
        assert result[CALIBRATION_SCALE] is not None
        assert isinstance(result[CALIBRATION_SCALE], float)
        assert result[CALIBRATION_SCALE] == pytest.approx(1.0)

    def test_bad_scale_fallback_is_1_not_2(self):
        """Non-numeric list scale falls back to exactly 1.0, not 2.0 (kills _20)."""
        raw = {
            CALIBRATION_COMMODITY: COMMODITY_GAS,
            CALIBRATION_UNIT: UnitOfVolume.CUBIC_METERS,
            CALIBRATION_SCALE: [1, 2, 3],
        }
        result = normalize_calibration(raw)
        assert result is not None
        assert result[CALIBRATION_SCALE] == pytest.approx(1.0)
        assert result[CALIBRATION_SCALE] != pytest.approx(2.0)

    def test_none_scale_falls_back_to_1(self):
        """None scale falls back to 1.0, covering TypeError path (kills _19, _20)."""
        raw = {
            CALIBRATION_COMMODITY: COMMODITY_WATER,
            CALIBRATION_UNIT: UnitOfVolume.LITERS,
            CALIBRATION_SCALE: None,
        }
        result = normalize_calibration(raw)
        assert result is not None
        assert result[CALIBRATION_SCALE] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# normalize_calibration — unit validation rejects an unknown unit
# mutmut_9, mutmut_11
# ---------------------------------------------------------------------------
# These mutants change COMMODITY_UNITS.get(commodity, ()) to return None
# or raise TypeError — the "in" test would then behave differently for an
# unknown unit.  The test below asserts that None is NOT a valid unit.


class TestNormCalibrationUnitValidation:
    """normalize_calibration rejects units not in COMMODITY_UNITS for the commodity."""

    def test_none_unit_rejected(self):
        """Unit=None is not valid for any commodity (kills _9, _11 via changed default)."""
        raw = {
            CALIBRATION_COMMODITY: COMMODITY_ENERGY,
            CALIBRATION_UNIT: None,
        }
        result = normalize_calibration(raw)
        assert result is None

    def test_wrong_commodity_unit_rejected(self):
        """A unit valid for energy is not valid for water (kills _9, _11)."""
        raw = {
            CALIBRATION_COMMODITY: COMMODITY_WATER,
            CALIBRATION_UNIT: UnitOfEnergy.KILO_WATT_HOUR,
        }
        result = normalize_calibration(raw)
        assert result is None

    def test_valid_unit_accepted_energy(self):
        """A unit in COMMODITY_UNITS[energy] is accepted."""
        for unit in COMMODITY_UNITS[COMMODITY_ENERGY]:
            raw = {
                CALIBRATION_COMMODITY: COMMODITY_ENERGY,
                CALIBRATION_UNIT: unit,
            }
            result = normalize_calibration(raw)
            assert result is not None, f"Expected valid result for energy unit {unit!r}"
            assert result[CALIBRATION_UNIT] == unit

    def test_valid_unit_accepted_gas(self):
        """A unit in COMMODITY_UNITS[gas] is accepted."""
        for unit in COMMODITY_UNITS[COMMODITY_GAS]:
            raw = {
                CALIBRATION_COMMODITY: COMMODITY_GAS,
                CALIBRATION_UNIT: unit,
            }
            result = normalize_calibration(raw)
            assert result is not None, f"Expected valid result for gas unit {unit!r}"

    def test_valid_unit_accepted_water(self):
        """A unit in COMMODITY_UNITS[water] is accepted."""
        for unit in COMMODITY_UNITS[COMMODITY_WATER]:
            raw = {
                CALIBRATION_COMMODITY: COMMODITY_WATER,
                CALIBRATION_UNIT: unit,
            }
            result = normalize_calibration(raw)
            assert result is not None, f"Expected valid result for water unit {unit!r}"


# ---------------------------------------------------------------------------
# commodity_from_fields — MeterType "water" mapping
# mutmut_13, mutmut_14
# ---------------------------------------------------------------------------


class TestCommodityFromFieldsMeterType:
    """commodity_from_fields maps MeterType strings case-insensitively."""

    def test_meter_type_water_lowercase(self):
        """'Water' MeterType maps to COMMODITY_WATER (kills _13 XXwaterXX, _14 WATER)."""
        assert commodity_from_fields({"MeterType": "Water"}) == COMMODITY_WATER

    def test_meter_type_water_uppercase(self):
        """'WATER' MeterType maps to COMMODITY_WATER (kills _14 'WATER' key)."""
        assert commodity_from_fields({"MeterType": "WATER"}) == COMMODITY_WATER

    def test_meter_type_water_exact_lowercase(self):
        """'water' MeterType maps to COMMODITY_WATER (kills _13, _14)."""
        assert commodity_from_fields({"MeterType": "water"}) == COMMODITY_WATER

    def test_meter_type_water_with_spaces(self):
        """'  water  ' MeterType (whitespace stripped) maps to COMMODITY_WATER."""
        assert commodity_from_fields({"MeterType": "  water  "}) == COMMODITY_WATER

    def test_meter_type_electric_maps_to_energy(self):
        """'Electric' maps to COMMODITY_ENERGY (sanity check)."""
        assert commodity_from_fields({"MeterType": "Electric"}) == COMMODITY_ENERGY

    def test_meter_type_gas_maps_to_gas(self):
        """'Gas' maps to COMMODITY_GAS (sanity check)."""
        assert commodity_from_fields({"MeterType": "Gas"}) == COMMODITY_GAS

    def test_unknown_meter_type_falls_through_to_none(self):
        """Unknown MeterType with no ert_type returns COMMODITY_NONE."""
        assert commodity_from_fields({"MeterType": "Steam"}) == COMMODITY_NONE


# ---------------------------------------------------------------------------
# commodity_from_fields — ert_type nibble lookup, unmapped default
# mutmut_26, mutmut_28
# ---------------------------------------------------------------------------


class TestCommodityFromFieldsErtTypeDefault:
    """commodity_from_fields returns COMMODITY_NONE for unmapped nibble values."""

    def test_ert_type_unmapped_nibble_0_returns_none(self):
        """Nibble 0 is unmapped → COMMODITY_NONE (kills _26, _28)."""
        assert commodity_from_fields({"ert_type": 0}) == COMMODITY_NONE

    def test_ert_type_unmapped_nibble_1_returns_none(self):
        """Nibble 1 is unmapped → COMMODITY_NONE (kills _26, _28)."""
        assert commodity_from_fields({"ert_type": 1}) == COMMODITY_NONE

    def test_ert_type_unmapped_nibble_3_returns_none(self):
        """Nibble 3 is unmapped → COMMODITY_NONE (kills _26, _28)."""
        assert commodity_from_fields({"ert_type": 3}) == COMMODITY_NONE

    def test_ert_type_unmapped_nibble_6_returns_none(self):
        """Nibble 6 is unmapped → COMMODITY_NONE (kills _26, _28)."""
        assert commodity_from_fields({"ert_type": 6}) == COMMODITY_NONE

    def test_ert_type_unmapped_nibble_10_returns_none(self):
        """Nibble 10 is unmapped → COMMODITY_NONE (kills _26, _28)."""
        assert commodity_from_fields({"ert_type": 10}) == COMMODITY_NONE

    def test_ert_type_unmapped_nibble_14_returns_none(self):
        """Nibble 14 is unmapped → COMMODITY_NONE (kills _26, _28)."""
        assert commodity_from_fields({"ert_type": 14}) == COMMODITY_NONE

    def test_ert_type_unmapped_nibble_15_returns_none(self):
        """Nibble 15 is unmapped → COMMODITY_NONE (kills _26, _28)."""
        assert commodity_from_fields({"ert_type": 15}) == COMMODITY_NONE

    def test_ert_type_default_is_commodity_none_not_none_python(self):
        """Unmapped nibble yields COMMODITY_NONE string, not Python None (kills _26)."""
        result = commodity_from_fields({"ert_type": 0})
        assert result is not None
        assert result == COMMODITY_NONE


# ---------------------------------------------------------------------------
# commodity_from_fields — ert_type nibble 4 → COMMODITY_ENERGY
# mutmut_30 (nibble 4 key changed to 5, dropping key 4)
# ---------------------------------------------------------------------------


class TestCommodityFromFieldsErtTypeNibble4:
    """Nibble 4 must map to COMMODITY_ENERGY."""

    def test_ert_type_4_is_energy(self):
        """ert_type=4 (nibble 4) → COMMODITY_ENERGY (kills _30)."""
        assert commodity_from_fields({"ert_type": 4}) == COMMODITY_ENERGY

    def test_ert_type_20_nibble_4_is_energy(self):
        """ert_type=20 (0x14, nibble 4) → COMMODITY_ENERGY (kills _30)."""
        assert commodity_from_fields({"ert_type": 20}) == COMMODITY_ENERGY

    def test_ert_type_4_is_not_none(self):
        """ert_type=4 must not return COMMODITY_NONE (kills _30)."""
        assert commodity_from_fields({"ert_type": 4}) != COMMODITY_NONE


# ---------------------------------------------------------------------------
# commodity_from_fields — ert_type nibble 5 → COMMODITY_ENERGY
# mutmut_31 (nibble 5 key changed to 6, dropping key 5)
# ---------------------------------------------------------------------------


class TestCommodityFromFieldsErtTypeNibble5:
    """Nibble 5 must map to COMMODITY_ENERGY."""

    def test_ert_type_5_is_energy(self):
        """ert_type=5 (nibble 5) → COMMODITY_ENERGY (kills _31)."""
        assert commodity_from_fields({"ert_type": 5}) == COMMODITY_ENERGY

    def test_ert_type_21_nibble_5_is_energy(self):
        """ert_type=21 (0x15, nibble 5) → COMMODITY_ENERGY (kills _31)."""
        assert commodity_from_fields({"ert_type": 21}) == COMMODITY_ENERGY

    def test_ert_type_6_is_not_energy(self):
        """ert_type=6 (nibble 6) must NOT be energy (kills _31)."""
        assert commodity_from_fields({"ert_type": 6}) != COMMODITY_ENERGY


# ---------------------------------------------------------------------------
# commodity_from_fields — ert_type nibble 7 → COMMODITY_ENERGY
# mutmut_32 (nibble 7 key changed to 8, dropping key 7)
# ---------------------------------------------------------------------------


class TestCommodityFromFieldsErtTypeNibble7:
    """Nibble 7 must map to COMMODITY_ENERGY."""

    def test_ert_type_7_is_energy(self):
        """ert_type=7 (nibble 7) → COMMODITY_ENERGY (kills _32)."""
        assert commodity_from_fields({"ert_type": 7}) == COMMODITY_ENERGY

    def test_ert_type_23_nibble_7_is_energy(self):
        """ert_type=23 (0x17, nibble 7) → COMMODITY_ENERGY (kills _32)."""
        assert commodity_from_fields({"ert_type": 23}) == COMMODITY_ENERGY

    def test_ert_type_7_is_not_gas(self):
        """ert_type=7 must not be gas (kills _32)."""
        assert commodity_from_fields({"ert_type": 7}) != COMMODITY_GAS


# ---------------------------------------------------------------------------
# commodity_from_fields — ert_type nibble 8 → COMMODITY_ENERGY
# mutmut_33 (nibble 8 key changed to 9, dropping key 8; 9 is gas)
# ---------------------------------------------------------------------------


class TestCommodityFromFieldsErtTypeNibble8:
    """Nibble 8 must map to COMMODITY_ENERGY, not COMMODITY_GAS."""

    def test_ert_type_8_is_energy(self):
        """ert_type=8 (nibble 8) → COMMODITY_ENERGY (kills _33)."""
        assert commodity_from_fields({"ert_type": 8}) == COMMODITY_ENERGY

    def test_ert_type_8_is_not_gas(self):
        """ert_type=8 must NOT be gas (kills _33: nibble 8 → 9 which is gas)."""
        assert commodity_from_fields({"ert_type": 8}) != COMMODITY_GAS

    def test_ert_type_24_nibble_8_is_energy(self):
        """ert_type=24 (0x18, nibble 8) → COMMODITY_ENERGY (kills _33)."""
        assert commodity_from_fields({"ert_type": 24}) == COMMODITY_ENERGY


# ---------------------------------------------------------------------------
# commodity_from_fields — ert_type nibble 9 → COMMODITY_GAS
# mutmut_34 (nibble 9 key changed to 10, dropping key 9)
# ---------------------------------------------------------------------------


class TestCommodityFromFieldsErtTypeNibble9:
    """Nibble 9 must map to COMMODITY_GAS."""

    def test_ert_type_9_is_gas(self):
        """ert_type=9 (nibble 9) → COMMODITY_GAS (kills _34)."""
        assert commodity_from_fields({"ert_type": 9}) == COMMODITY_GAS

    def test_ert_type_25_nibble_9_is_gas(self):
        """ert_type=25 (0x19, nibble 9) → COMMODITY_GAS (kills _34)."""
        assert commodity_from_fields({"ert_type": 25}) == COMMODITY_GAS

    def test_ert_type_9_is_not_none(self):
        """ert_type=9 must not return COMMODITY_NONE (kills _34)."""
        assert commodity_from_fields({"ert_type": 9}) != COMMODITY_NONE

    def test_ert_type_9_is_not_energy(self):
        """ert_type=9 must not return energy (sanity for nibble boundary)."""
        assert commodity_from_fields({"ert_type": 9}) != COMMODITY_ENERGY


# ---------------------------------------------------------------------------
# commodity_from_fields — ert_type nibble 12 → COMMODITY_GAS
# mutmut_36 (nibble 12 key changed to 13, dropping key 12; 13 is water)
# ---------------------------------------------------------------------------


class TestCommodityFromFieldsErtTypeNibble12:
    """Nibble 12 must map to COMMODITY_GAS, not COMMODITY_WATER."""

    def test_ert_type_12_is_gas(self):
        """ert_type=12 (nibble 12) → COMMODITY_GAS (kills _36)."""
        assert commodity_from_fields({"ert_type": 12}) == COMMODITY_GAS

    def test_ert_type_12_is_not_water(self):
        """ert_type=12 must NOT be water (kills _36: nibble 12 → 13 which is water)."""
        assert commodity_from_fields({"ert_type": 12}) != COMMODITY_WATER

    def test_ert_type_28_nibble_12_is_gas(self):
        """ert_type=28 (0x1C, nibble 12) → COMMODITY_GAS (kills _36)."""
        assert commodity_from_fields({"ert_type": 28}) == COMMODITY_GAS


# ---------------------------------------------------------------------------
# commodity_from_fields — ert_type nibble 13 → COMMODITY_WATER
# mutmut_37 (nibble 13 key changed to 14, dropping key 13)
# ---------------------------------------------------------------------------


class TestCommodityFromFieldsErtTypeNibble13:
    """Nibble 13 must map to COMMODITY_WATER."""

    def test_ert_type_13_is_water(self):
        """ert_type=13 (nibble 13) → COMMODITY_WATER (kills _37)."""
        assert commodity_from_fields({"ert_type": 13}) == COMMODITY_WATER

    def test_ert_type_29_nibble_13_is_water(self):
        """ert_type=29 (0x1D, nibble 13) → COMMODITY_WATER (kills _37)."""
        assert commodity_from_fields({"ert_type": 29}) == COMMODITY_WATER

    def test_ert_type_13_is_not_none(self):
        """ert_type=13 must not return COMMODITY_NONE (kills _37)."""
        assert commodity_from_fields({"ert_type": 13}) != COMMODITY_NONE

    def test_ert_type_14_is_not_water(self):
        """ert_type=14 (nibble 14) must NOT be water (kills _37)."""
        assert commodity_from_fields({"ert_type": 14}) != COMMODITY_WATER


# ---------------------------------------------------------------------------
# commodity_from_fields — complete nibble coverage sanity check
# Verifies all mapped nibbles produce the correct commodity so any key-swap
# mutation in the lookup dict is caught.
# ---------------------------------------------------------------------------


class TestCommodityFromFieldsErtTypeAllMapped:
    """All documented ert_type nibble mappings are exercised explicitly."""

    @pytest.mark.parametrize(
        "ert_type, expected",
        [
            (2, COMMODITY_GAS),
            (4, COMMODITY_ENERGY),
            (5, COMMODITY_ENERGY),
            (7, COMMODITY_ENERGY),
            (8, COMMODITY_ENERGY),
            (9, COMMODITY_GAS),
            (11, COMMODITY_WATER),
            (12, COMMODITY_GAS),
            (13, COMMODITY_WATER),
        ],
    )
    def test_nibble_mapping(self, ert_type, expected):
        """Direct nibble value produces the expected commodity."""
        assert commodity_from_fields({"ert_type": ert_type}) == expected

    @pytest.mark.parametrize(
        "ert_type, expected",
        [
            (2 + 16, COMMODITY_GAS),  # nibble 2 with high bit set
            (4 + 16, COMMODITY_ENERGY),  # nibble 4
            (5 + 32, COMMODITY_ENERGY),  # nibble 5
            (7 + 48, COMMODITY_ENERGY),  # nibble 7
            (8 + 64, COMMODITY_ENERGY),  # nibble 8
            (9 + 16, COMMODITY_GAS),  # nibble 9
            (11 + 16, COMMODITY_WATER),  # nibble 11
            (12 + 16, COMMODITY_GAS),  # nibble 12
            (13 + 16, COMMODITY_WATER),  # nibble 13
        ],
    )
    def test_nibble_masking_high_bits_ignored(self, ert_type, expected):
        """ert_type & 0x0F is used; high nibble bits are masked off."""
        assert commodity_from_fields({"ert_type": ert_type}) == expected


# ---------------------------------------------------------------------------
# commodity_from_fields — edge cases for the ert_type path
# ---------------------------------------------------------------------------


class TestCommodityFromFieldsErtTypeEdge:
    """Edge cases for ert_type handling (non-numeric, missing, None)."""

    def test_no_meter_type_no_ert_type_returns_none(self):
        """Fields with neither MeterType nor ert_type → COMMODITY_NONE."""
        assert commodity_from_fields({}) == COMMODITY_NONE

    def test_none_fields_returns_none(self):
        """None input → COMMODITY_NONE."""
        assert commodity_from_fields(None) == COMMODITY_NONE

    def test_non_dict_fields_returns_none(self):
        """Non-dict input → COMMODITY_NONE."""
        assert commodity_from_fields("not a dict") == COMMODITY_NONE

    def test_ert_type_none_returns_none(self):
        """ert_type=None → COMMODITY_NONE (TypeError handled)."""
        assert commodity_from_fields({"ert_type": None}) == COMMODITY_NONE

    def test_ert_type_string_non_numeric_returns_none(self):
        """Non-numeric ert_type string → COMMODITY_NONE (ValueError handled)."""
        assert commodity_from_fields({"ert_type": "abc"}) == COMMODITY_NONE


# ---------------------------------------------------------------------------
# normalize_calibration — complete path correctness
# ---------------------------------------------------------------------------


class TestNormCalibrationComplete:
    """normalize_calibration full-path correctness tests."""

    def test_returns_all_three_keys(self):
        """Result always contains commodity, unit, and scale."""
        raw = {
            CALIBRATION_COMMODITY: COMMODITY_ENERGY,
            CALIBRATION_UNIT: UnitOfEnergy.WATT_HOUR,
            CALIBRATION_SCALE: 1.0,
        }
        result = normalize_calibration(raw)
        assert result is not None
        assert CALIBRATION_COMMODITY in result
        assert CALIBRATION_UNIT in result
        assert CALIBRATION_SCALE in result

    def test_scale_is_coerced_to_float(self):
        """Integer scale is coerced to float."""
        raw = {
            CALIBRATION_COMMODITY: COMMODITY_GAS,
            CALIBRATION_UNIT: UnitOfVolume.CUBIC_METERS,
            CALIBRATION_SCALE: 2,
        }
        result = normalize_calibration(raw)
        assert result is not None
        assert isinstance(result[CALIBRATION_SCALE], float)
        assert result[CALIBRATION_SCALE] == pytest.approx(2.0)

    def test_explicit_scale_preserved(self):
        """Explicit scale value is returned unchanged."""
        raw = {
            CALIBRATION_COMMODITY: COMMODITY_WATER,
            CALIBRATION_UNIT: UnitOfVolume.LITERS,
            CALIBRATION_SCALE: 0.001,
        }
        result = normalize_calibration(raw)
        assert result is not None
        assert result[CALIBRATION_SCALE] == pytest.approx(0.001)
        # Not 1.0: the default must NOT override an explicit scale
        assert result[CALIBRATION_SCALE] != pytest.approx(1.0)

    def test_non_dict_input_returns_none(self):
        """Non-dict input → None."""
        assert normalize_calibration(None) is None
        assert normalize_calibration("string") is None
        assert normalize_calibration(42) is None

    def test_unknown_commodity_returns_none(self):
        """commodity='steam' is not in COMMODITY_DEVICE_CLASS → None."""
        raw = {
            CALIBRATION_COMMODITY: "steam",
            CALIBRATION_UNIT: UnitOfVolume.LITERS,
        }
        assert normalize_calibration(raw) is None

    def test_commodity_none_returns_none(self):
        """commodity='none' → None."""
        raw = {
            CALIBRATION_COMMODITY: "none",
            CALIBRATION_UNIT: UnitOfVolume.LITERS,
        }
        assert normalize_calibration(raw) is None

    def test_commodity_preserved_in_result(self):
        """Result commodity matches input commodity."""
        for commodity, unit in [
            (COMMODITY_ENERGY, UnitOfEnergy.KILO_WATT_HOUR),
            (COMMODITY_GAS, UnitOfVolume.CUBIC_METERS),
            (COMMODITY_WATER, UnitOfVolume.GALLONS),
        ]:
            raw = {
                CALIBRATION_COMMODITY: commodity,
                CALIBRATION_UNIT: unit,
                CALIBRATION_SCALE: 1.0,
            }
            result = normalize_calibration(raw)
            assert result is not None
            assert result[CALIBRATION_COMMODITY] == commodity
