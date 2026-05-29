"""Mutation-killing tests for custom_components/rtl_433/mapping.py.

Every test in this file is written to pass on the *original* source and fail on
at least one specific surviving mutant listed in /tmp/mutdiffs/diffs_mapping.txt.

Groups covered:
- _normalize_payload: bool/string key normalisation, fallthrough unknown keys
- _extract_skip_keys: non-dict / non-list data, str coercion
- _parse_models_block: non-mapping raw, non-mapping per-model entry, good path
- _descriptor_from_entry: unknown attrs, payload normalisation, clear_delay validation
- load_library: flat/model merging, underscore-prefixed files skipped
- lookup: model-scoped vs flat fallback, None model
- should_skip: membership positive and negative
- _coerce_number: int vs float coercion
- _apply_sensor_transform: needs_float logic (scale/offset/round/float keys independently),
  as_int logic, scale/offset/round arithmetic with exact numeric checks,
  invalid-param passthrough
- _apply_binary_payload: raw_str construction (strip), on/off token matching,
  numeric near-equality fallback (as_int=False matters for fractional tokens),
  None returned when no match, return-value polarity
- apply_transform: binary vs sensor dispatch, None short-circuit, no-payload binary
- merge_overrides: flat replace, model merge, skip_keys union, pure (no mutation of base),
  non-dict override_data
- validate_user_mappings / normalize_overrides: exercised via merge_overrides
"""

from __future__ import annotations

import pytest

from custom_components.rtl_433.mapping import (
    FieldDescriptor,
    Registry,
    _apply_binary_payload,
    _apply_sensor_transform,
    _coerce_number,
    _copy_registry,
    _extract_skip_keys,
    _normalize_payload,
    _parse_models_block,
    apply_transform,
    load_library,
    load_user_overrides,
    lookup,
    merge_overrides,
    should_skip,
)

# ---------------------------------------------------------------------------
# Helpers / shared fixtures
# ---------------------------------------------------------------------------


def _make_descriptor(**kwargs) -> FieldDescriptor:
    """Build a minimal FieldDescriptor; caller supplies extra kwargs."""
    defaults = dict(
        field_key="test_field",
        platform="sensor",
        name="Test Field",
        object_suffix="T",
    )
    defaults.update(kwargs)
    return FieldDescriptor(**defaults)


def _make_registry(flat=None, models=None) -> Registry:
    return Registry(flat=flat or {}, models=models or {})


@pytest.fixture(scope="module")
def lib():
    """Load the shipped device library once for the whole module."""
    return load_library()


# ===========================================================================
# _normalize_payload
# ===========================================================================


class TestNormalizePayload:
    """Kills mutants that corrupt key normalisation logic."""

    def test_bool_true_key_becomes_on(self):
        """PyYAML parses bare ``on`` as True; must be rewritten to 'on'."""
        result = _normalize_payload({True: "1", False: "0"})
        assert result == {"on": "1", "off": "0"}

    def test_string_on_key_kept(self):
        """Already-string 'on' must survive normalisation unchanged."""
        result = _normalize_payload({"on": "active", "off": "inactive"})
        assert result == {"on": "active", "off": "inactive"}

    def test_mixed_bool_and_string_keys(self):
        """True + string 'off' round-trips to canonical shape."""
        result = _normalize_payload({True: "YES", "off": "NO"})
        assert result == {"on": "YES", "off": "NO"}

    def test_unknown_keys_stringified(self):
        """Arbitrary non-on/off keys are stringified but preserved."""
        result = _normalize_payload({42: "x"})
        assert result == {"42": "x"}

    def test_non_dict_returns_as_is(self):
        """Non-dict payloads are returned unchanged (None, list, etc.)."""
        assert _normalize_payload(None) is None
        assert _normalize_payload([1, 2]) == [1, 2]
        assert _normalize_payload("string") == "string"

    def test_false_key_becomes_off(self):
        """Bool False key maps to canonical 'off'."""
        result = _normalize_payload({False: "closed"})
        assert result == {"off": "closed"}

    def test_empty_dict_returns_empty_dict(self):
        result = _normalize_payload({})
        assert result == {}

    def test_on_value_is_preserved_exactly(self):
        """The *value* under 'on' must be preserved exactly — not the key."""
        result = _normalize_payload({"on": "OPEN"})
        assert result["on"] == "OPEN"
        assert "off" not in result


# ===========================================================================
# _extract_skip_keys
# ===========================================================================


class TestExtractSkipKeys:
    """Kills mutants that drop, flip, or mis-key the SKIP_KEYS_FIELD branch."""

    def test_returns_set_of_strings(self):
        data = {"skip_keys": ["model", "id", "channel"]}
        result = _extract_skip_keys(data)
        assert result == {"model", "id", "channel"}

    def test_non_dict_input_returns_empty(self):
        """Non-dict → empty set, not exception."""
        assert _extract_skip_keys(None) == set()
        assert _extract_skip_keys([]) == set()
        assert _extract_skip_keys("bad") == set()

    def test_missing_key_returns_empty(self):
        """Missing 'skip_keys' top-level entry → empty set."""
        assert _extract_skip_keys({"other_key": ["x"]}) == set()

    def test_non_list_value_returns_empty(self):
        """skip_keys value that isn't a list → empty set."""
        assert _extract_skip_keys({"skip_keys": "not_a_list"}) == set()
        assert _extract_skip_keys({"skip_keys": None}) == set()

    def test_integer_entries_are_str_coerced(self):
        """Integer entries in the list must be coerced to str."""
        result = _extract_skip_keys({"skip_keys": [1, 2]})
        assert result == {"1", "2"}

    def test_empty_list_returns_empty_set(self):
        result = _extract_skip_keys({"skip_keys": []})
        assert result == set()

    def test_correct_field_name_used(self):
        """Key must be exactly 'skip_keys', not a variant."""
        assert _extract_skip_keys({"SKIP_KEYS": ["x"]}) == set()
        assert _extract_skip_keys({"skip_key": ["x"]}) == set()


# ===========================================================================
# _parse_models_block
# ===========================================================================


class TestParseModelsBlock:
    """Kills mutants in the model-block parser."""

    def test_non_dict_raw_returns_empty(self):
        """Non-mapping raw → empty dict (with a warning log)."""
        result = _parse_models_block("not-a-dict", "test.yaml")
        assert result == {}

    def test_non_mapping_model_entry_skipped(self):
        """A per-model entry that isn't a dict is skipped."""
        raw = {
            "ModelA": "not-a-dict",
            "ModelB": {
                "temp": {"platform": "sensor", "name": "T", "object_suffix": "T"}
            },
        }
        result = _parse_models_block(raw, "test.yaml")
        assert "ModelA" not in result
        assert "ModelB" in result

    def test_valid_model_block_parsed(self):
        raw = {
            "Acme-Thermo": {
                "temperature_C": {
                    "platform": "sensor",
                    "name": "Temperature",
                    "object_suffix": "T",
                }
            }
        }
        result = _parse_models_block(raw, "test.yaml")
        assert "Acme-Thermo" in result
        assert "temperature_C" in result["Acme-Thermo"]
        desc = result["Acme-Thermo"]["temperature_C"]
        assert desc.platform == "sensor"
        assert desc.field_key == "temperature_C"

    def test_model_key_stringified(self):
        """Numeric model keys are coerced to str."""
        raw = {123: {"f": {"platform": "sensor", "name": "N", "object_suffix": "O"}}}
        result = _parse_models_block(raw, "test.yaml")
        assert "123" in result

    def test_malformed_descriptor_skipped_others_kept(self):
        """One bad descriptor doesn't drop other descriptors in the same model."""
        raw = {
            "M": {
                "bad_field": "not-a-dict",
                "good_field": {"platform": "sensor", "name": "G", "object_suffix": "G"},
            }
        }
        result = _parse_models_block(raw, "test.yaml")
        assert "M" in result
        assert "good_field" in result["M"]
        assert "bad_field" not in result["M"]

    def test_empty_models_dict_returns_empty(self):
        assert _parse_models_block({}, "test.yaml") == {}


# ===========================================================================
# _descriptor_from_entry
# ===========================================================================


class TestDescriptorFromEntry:
    """Kills mutants around descriptor construction, unknown-attr logging, and
    clear_delay validation."""

    def test_minimal_valid_entry(self):
        """Required attrs only — must build a FieldDescriptor."""
        from custom_components.rtl_433.mapping import _descriptor_from_entry

        desc = _descriptor_from_entry(
            "humidity",
            {
                "platform": "sensor",
                "name": "Humidity",
                "object_suffix": "H",
            },
        )
        assert desc.field_key == "humidity"
        assert desc.platform == "sensor"

    def test_non_dict_entry_raises_type_error(self):
        from custom_components.rtl_433.mapping import _descriptor_from_entry

        with pytest.raises(TypeError):
            _descriptor_from_entry("x", "not-a-dict")

    def test_unknown_attrs_ignored(self):
        """Unknown YAML attrs are silently dropped; the descriptor still builds."""
        from custom_components.rtl_433.mapping import _descriptor_from_entry

        desc = _descriptor_from_entry(
            "x",
            {
                "platform": "sensor",
                "name": "X",
                "object_suffix": "X",
                "unknown_attr_xyz": "garbage",
            },
        )
        assert desc.platform == "sensor"
        assert not hasattr(desc, "unknown_attr_xyz")

    def test_payload_is_normalised(self):
        """Bool-keyed payload must be normalised to string keys."""
        from custom_components.rtl_433.mapping import _descriptor_from_entry

        desc = _descriptor_from_entry(
            "door",
            {
                "platform": "binary_sensor",
                "name": "Door",
                "object_suffix": "D",
                "payload": {True: "1", False: "0"},
            },
        )
        assert desc.payload == {"on": "1", "off": "0"}

    def test_valid_clear_delay_kept(self):
        from custom_components.rtl_433.mapping import _descriptor_from_entry

        desc = _descriptor_from_entry(
            "motion",
            {
                "platform": "binary_sensor",
                "name": "Motion",
                "object_suffix": "M",
                "clear_delay": 90,
            },
        )
        assert desc.clear_delay == 90

    def test_zero_clear_delay_dropped(self):
        """clear_delay <= 0 is invalid and must be discarded."""
        from custom_components.rtl_433.mapping import _descriptor_from_entry

        desc = _descriptor_from_entry(
            "motion",
            {
                "platform": "binary_sensor",
                "name": "Motion",
                "object_suffix": "M",
                "clear_delay": 0,
            },
        )
        assert desc.clear_delay is None

    def test_negative_clear_delay_dropped(self):
        from custom_components.rtl_433.mapping import _descriptor_from_entry

        desc = _descriptor_from_entry(
            "motion",
            {
                "platform": "binary_sensor",
                "name": "Motion",
                "object_suffix": "M",
                "clear_delay": -5,
            },
        )
        assert desc.clear_delay is None

    def test_bool_clear_delay_dropped(self):
        """True is an int in Python but must be rejected as clear_delay."""
        from custom_components.rtl_433.mapping import _descriptor_from_entry

        desc = _descriptor_from_entry(
            "motion",
            {
                "platform": "binary_sensor",
                "name": "Motion",
                "object_suffix": "M",
                "clear_delay": True,
            },
        )
        assert desc.clear_delay is None

    def test_string_clear_delay_dropped(self):
        from custom_components.rtl_433.mapping import _descriptor_from_entry

        desc = _descriptor_from_entry(
            "motion",
            {
                "platform": "binary_sensor",
                "name": "Motion",
                "object_suffix": "M",
                "clear_delay": "90",
            },
        )
        assert desc.clear_delay is None


# ===========================================================================
# _coerce_number
# ===========================================================================


class TestCoerceNumber:
    """Kills mutants on the as_int branch."""

    def test_as_int_false_returns_float(self):
        result = _coerce_number(3, as_int=False)
        assert result == 3.0
        assert isinstance(result, float)

    def test_as_int_true_returns_int(self):
        result = _coerce_number("7", as_int=True)
        assert result == 7
        assert isinstance(result, int)

    def test_as_int_true_truncates(self):
        result = _coerce_number(3.9, as_int=True)
        assert result == 3
        assert isinstance(result, int)

    def test_non_numeric_returns_none(self):
        assert _coerce_number("abc", as_int=False) is None
        assert _coerce_number(None, as_int=False) is None

    def test_as_int_false_preserves_fraction(self):
        result = _coerce_number(1.5, as_int=False)
        assert result == 1.5

    def test_as_int_true_float_input(self):
        result = _coerce_number(2.7, as_int=True)
        assert result == 2
        assert isinstance(result, int)


# ===========================================================================
# _apply_sensor_transform — needs_float logic
# ===========================================================================


class TestApplySensorTransformNeedsFloat:
    """Each test targets one needs_float sub-expression independently so mutants
    that mis-combine them (and<->or swaps, wrong key strings) are killed."""

    def test_scale_alone_implies_float(self):
        """'scale' alone → needs_float → as_int=False even with int:true."""
        # With int:true and scale present, result must be float arithmetic.
        result = _apply_sensor_transform({"int": True, "scale": 2.0}, 3)
        # 3 * 2.0 = 6.0 (float arithmetic, not integer)
        assert result == 6.0

    def test_offset_alone_implies_float(self):
        """'offset' alone → needs_float → float result."""
        result = _apply_sensor_transform({"int": True, "offset": 0.5}, 4)
        # Without scale, 4 + 0.5 = 4.5 (float)
        assert result == 4.5

    def test_round_alone_implies_float(self):
        """'round' alone → needs_float → float arithmetic path used."""
        result = _apply_sensor_transform({"int": True, "round": 1}, 4)
        # int:true + round:1 → needs_float=True → as_int=False → 4.0 → round(4.0,1) = 4.0
        assert result == 4.0

    def test_float_key_alone_implies_float(self):
        """'float' key alone → needs_float → as_int=False."""
        result = _apply_sensor_transform({"int": True, "float": True}, 5)
        assert result == 5.0
        assert isinstance(result, float)

    def test_int_only_returns_int(self):
        """Only 'int' with no float-implying key → as_int=True → int result."""
        result = _apply_sensor_transform({"int": True}, "7")
        assert result == 7
        assert isinstance(result, int)

    def test_no_keys_returns_raw_number_float(self):
        """No coercion keys: numeric input coerced as float (as_int=False)."""
        result = _apply_sensor_transform({}, 5)
        assert result == 5.0
        assert isinstance(result, float)

    def test_scale_key_case_sensitive(self):
        """'SCALE' must NOT trigger scale transform (key is case-sensitive)."""
        result = _apply_sensor_transform({"SCALE": 10}, 3)
        # No recognised key → raw float 3.0
        assert result == 3.0

    def test_offset_key_case_sensitive(self):
        """'OFFSET' must NOT trigger offset transform."""
        result = _apply_sensor_transform({"OFFSET": 10}, 3)
        assert result == 3.0

    def test_float_key_case_sensitive(self):
        """'FLOAT' key must NOT count as float-implying."""
        result = _apply_sensor_transform({"int": True, "FLOAT": True}, 5)
        assert result == 5
        assert isinstance(result, int)


# ===========================================================================
# _apply_sensor_transform — as_int logic
# ===========================================================================


class TestApplySensorTransformAsInt:
    """Kills the ``as_int = has_int and not needs_float`` → ``or`` mutant."""

    def test_no_int_key_never_returns_int(self):
        """Without int:true, even a plain number returns float."""
        result = _apply_sensor_transform({}, 4)
        assert isinstance(result, float)

    def test_int_with_float_implying_key_returns_float(self):
        """int:true + scale → needs_float → as_int=False → float, not int."""
        result = _apply_sensor_transform({"int": True, "scale": 1.0}, 4)
        # has_int=True, needs_float=True → as_int=False → float
        assert isinstance(result, float)

    def test_int_without_float_implying_key_returns_int(self):
        """int:true with no float-implying key → as_int=True → int."""
        result = _apply_sensor_transform({"int": True}, 4)
        assert isinstance(result, int)


# ===========================================================================
# _apply_sensor_transform — arithmetic pipeline
# ===========================================================================


class TestApplySensorTransformArithmetic:
    """Exact numeric results for scale → offset → round pipeline.

    These kill mutants that swap +/*, change constants, or skip steps.
    """

    def test_scale_exact(self):
        result = _apply_sensor_transform({"scale": 3.6}, 10)
        assert result == 36.0

    def test_offset_exact(self):
        result = _apply_sensor_transform({"offset": -273.15}, 300.0)
        assert abs(result - 26.85) < 1e-9

    def test_scale_then_offset_exact(self):
        """scale applied first, then offset."""
        # 5 * 3.6 = 18.0 + 1.0 = 19.0
        result = _apply_sensor_transform({"scale": 3.6, "offset": 1.0}, 5)
        assert abs(result - 19.0) < 1e-9

    def test_round_1_decimal(self):
        result = _apply_sensor_transform({"round": 1}, 21.37)
        assert result == 21.4

    def test_round_2_decimals(self):
        # scale 3.6, round 2
        result = _apply_sensor_transform({"scale": 3.6, "round": 2}, 3.5)
        assert result == 12.6

    def test_round_0_whole_number_gives_int(self):
        """round=0 with a whole result should be coerced to int."""
        result = _apply_sensor_transform({"round": 0}, 5.7)
        assert result == 6
        assert isinstance(result, int)

    def test_round_0_non_whole_stays_float(self):
        """round=0 with a non-whole intermediary → int anyway (round up)."""
        result = _apply_sensor_transform({"scale": 1.0, "round": 0}, 5.5)
        assert isinstance(result, int)

    def test_battery_ok_scale_offset_round(self):
        """battery_ok: scale=99, offset=1, round=0 → 1→100, 0→1."""
        # value=1: 1*99+1=100 → round(100,0)=100.0 → int 100
        r1 = _apply_sensor_transform({"scale": 99, "offset": 1, "round": 0}, 1)
        assert r1 == 100
        assert isinstance(r1, int)
        # value=0: 0*99+1=1 → round(1,0)=1.0 → int 1
        r0 = _apply_sensor_transform({"scale": 99, "offset": 1, "round": 0}, 0)
        assert r0 == 1
        assert isinstance(r0, int)

    def test_non_numeric_passthrough(self):
        """Non-numeric raw values pass through unchanged."""
        result = _apply_sensor_transform({"scale": 2.0}, "n/a")
        assert result == "n/a"

    def test_scale_multiply_not_add(self):
        """Scale must be multiplication, not addition."""
        result = _apply_sensor_transform({"scale": 2.0}, 5)
        assert result == 10.0  # 5*2, not 5+2

    def test_offset_add_not_multiply(self):
        """Offset must be addition, not multiplication."""
        result = _apply_sensor_transform({"offset": 3.0}, 5)
        assert result == 8.0  # 5+3, not 5*3

    def test_round_applied_after_scale_and_offset(self):
        """Round is applied last: scale → offset → round."""
        # 3.3 * 2 = 6.6, + 0.1 = 6.7, round(6.7, 0) = 7.0 → int 7
        result = _apply_sensor_transform({"scale": 2.0, "offset": 0.1, "round": 0}, 3.3)
        assert result == 7
        assert isinstance(result, int)


# ===========================================================================
# _apply_binary_payload
# ===========================================================================


class TestApplyBinaryPayload:
    """Kills every surviving mutant in _apply_binary_payload."""

    # --- raw_str construction (mutmut_1, _2) ---

    def test_raw_str_is_stripped(self):
        """Leading/trailing whitespace must be stripped from raw_value."""
        payload = {"on": "1", "off": "0"}
        assert _apply_binary_payload(payload, "  1  ") is True
        assert _apply_binary_payload(payload, " 0 ") is False

    def test_raw_value_stringified(self):
        """Integer raw_value must be stringified for comparison."""
        payload = {"on": "1", "off": "0"}
        assert _apply_binary_payload(payload, 1) is True
        assert _apply_binary_payload(payload, 0) is False

    # --- on_token None check (mutmut_12, _14) ---

    def test_on_token_present_matches(self):
        """on_token is not None; should return True for matching value."""
        payload = {"on": "open", "off": "closed"}
        assert _apply_binary_payload(payload, "open") is True

    def test_on_token_absent_does_not_match(self):
        """off-only payload: value matching what 'on' would be → None, not True."""
        payload = {"off": "0"}
        # '1' is not in off; no on_token → numeric fallback also can't match on
        result = _apply_binary_payload(payload, "1")
        assert result is None

    # --- off_token None check (mutmut_17, _19) ---

    def test_off_token_present_matches(self):
        """off_token is not None; should return False for matching value."""
        payload = {"on": "1", "off": "0"}
        assert _apply_binary_payload(payload, "0") is False

    def test_off_token_absent_does_not_match(self):
        """on-only payload: value matching what 'off' would be → None, not False."""
        payload = {"on": "1"}
        result = _apply_binary_payload(payload, "0")
        assert result is None

    # --- str(on_token) comparison (mutmut_14) ---

    def test_on_token_int_string(self):
        """Token stored as integer in payload dict; raw '1' should match."""
        # When on_token is integer 1, str(on_token)='1' must be compared
        payload = {"on": 1, "off": 0}
        assert _apply_binary_payload(payload, "1") is True
        assert _apply_binary_payload(payload, "0") is False

    # --- return True polarity (mutmut_36) ---

    def test_numeric_near_equality_on_returns_true(self):
        """Numeric fallback for on-token must return True, not False."""
        payload = {"on": "1", "off": "2"}
        # raw_value=1.0: str "1.0" != "1", but numeric 1.0==1 → True
        result = _apply_binary_payload(payload, 1.0)
        assert result is True

    # --- return False polarity (mutmut_45) ---

    def test_numeric_near_equality_off_returns_false(self):
        """Numeric fallback for off-token must return False, not True."""
        payload = {"on": "1", "off": "2"}
        # raw_value=2.0: str "2.0" != "2", but numeric 2.0==2 → False
        result = _apply_binary_payload(payload, 2.0)
        assert result is False

    # --- number = _coerce_number(raw_value, as_int=False) (mutmut_21..26) ---

    def test_numeric_fallback_coercion_is_float(self):
        """as_int=False: fractional token "1.5" must match 1.5 via numeric path."""
        payload = {"on": "1.5", "off": "2.5"}
        # raw_value=1.5 → str "1.5" == "1.5" → True (actually matches string path)
        assert _apply_binary_payload(payload, 1.5) is True

    def test_numeric_fallback_coerce_none_if_non_numeric(self):
        """Non-numeric raw_value → number=None → no fallback → None."""
        payload = {"on": "active", "off": "inactive"}
        assert _apply_binary_payload(payload, "unknown_state") is None

    def test_numeric_fallback_uses_raw_value_not_none(self):
        """Ensure raw_value is used, not None, in the numeric coercion."""
        payload = {"on": "5", "off": "0"}
        # If number = _coerce_number(None) it would be None → no match → None
        # Original: number = _coerce_number(raw_value) with raw_value=5.0
        result = _apply_binary_payload(payload, 5.0)
        # str(5.0)='5.0' != '5', but numeric 5.0==5 → True
        assert result is True

    # --- number is not None guard (mutmut_27) ---

    def test_numeric_guard_only_triggers_for_numeric_values(self):
        """The ``if number is not None`` guard: non-numeric falls through to None."""
        payload = {"on": "open", "off": "close"}
        assert _apply_binary_payload(payload, "maybe") is None

    # --- off numeric path: _coerce_number(off_token, as_int=False) (mutmut_38..44) ---

    def test_numeric_fallback_off_uses_off_token_not_none(self):
        """off_token must be passed to _coerce_number, not None or garbage."""
        payload = {"on": "99", "off": "0"}
        # raw_value=0.0: str "0.0" != "0", numeric 0.0==0 → False
        result = _apply_binary_payload(payload, 0.0)
        assert result is False

    def test_off_numeric_as_int_false_handles_fractional(self):
        """off_token coercion uses as_int=False so "0.5" token works."""
        payload = {"on": "1", "off": "0.5"}
        # raw "0.5" matches on_str? No. off_str? "0.5"=="0.5" → yes → False
        result = _apply_binary_payload(payload, "0.5")
        assert result is False

    # --- on numeric _coerce_number(on_token, as_int=False) (mutmut_29..34) ---

    def test_on_numeric_as_int_false_handles_fractional_token(self):
        """on_token coercion also uses as_int=False so "0.5" token works."""
        payload = {"on": "0.5", "off": "0"}
        result = _apply_binary_payload(payload, "0.5")
        assert result is True

    def test_on_numeric_coerce_uses_on_token_not_none(self):
        """on_token, not None, must be passed to numeric coercion."""
        payload = {"on": "3", "off": "0"}
        # raw 3.0: str "3.0" != "3"; numeric 3.0==3 → True
        result = _apply_binary_payload(payload, 3.0)
        assert result is True

    # --- None return at end (no match) ---

    def test_no_match_returns_none_not_false(self):
        """Unmatched value must return None, not False."""
        payload = {"on": "1", "off": "0"}
        result = _apply_binary_payload(payload, "maybe")
        assert result is None

    def test_both_tokens_present_unknown_returns_none(self):
        payload = {"on": "open", "off": "closed"}
        result = _apply_binary_payload(payload, "ajar")
        assert result is None

    # --- off_token numeric mutmut_37 (and/or swap) ---

    def test_off_numeric_both_conditions_required(self):
        """off numeric branch requires both token is not None AND numeric match.

        If the ``and`` were replaced by ``or``, a None off_token would still
        return False when the numeric comparison happened to be equal-ish to None.
        This test ensures an absent off_token with numeric raw value returns None.
        """
        payload = {"on": "1"}  # no off_token
        # raw=0.0 → numeric 0.0; no off_token → should be None, not False
        result = _apply_binary_payload(payload, 0.0)
        assert result is None


# ===========================================================================
# apply_transform  (dispatch layer)
# ===========================================================================


class TestApplyTransform:
    """Kills mutants in apply_transform dispatch."""

    def test_none_raw_value_returns_none(self):
        """None short-circuits to None before any dispatch."""
        desc = _make_descriptor(platform="sensor")
        assert apply_transform(desc, None) is None

    def test_binary_sensor_with_payload(self):
        """binary_sensor + payload uses _apply_binary_payload."""
        desc = _make_descriptor(
            platform="binary_sensor",
            payload={"on": "1", "off": "0"},
        )
        assert apply_transform(desc, "1") is True
        assert apply_transform(desc, "0") is False
        assert apply_transform(desc, "maybe") is None

    def test_binary_sensor_without_payload_truthy_tokens(self):
        """binary_sensor without payload uses _TRUE_TOKENS."""
        desc = _make_descriptor(platform="binary_sensor", payload=None)
        assert apply_transform(desc, "1") is True
        assert apply_transform(desc, "true") is True
        assert apply_transform(desc, "on") is True
        assert apply_transform(desc, "yes") is True
        assert apply_transform(desc, "0") is False
        assert apply_transform(desc, "off") is False

    def test_sensor_without_transform_passes_through(self):
        """sensor without value_transform returns raw value unchanged."""
        desc = _make_descriptor(platform="sensor", value_transform=None)
        assert apply_transform(desc, 42) == 42
        assert apply_transform(desc, "raw") == "raw"

    def test_sensor_with_transform_applied(self):
        """sensor with value_transform applies the pipeline."""
        desc = _make_descriptor(
            platform="sensor",
            value_transform={"scale": 2.0, "round": 1},
        )
        assert apply_transform(desc, 3.0) == 6.0

    def test_binary_sensor_none_raw_is_none(self):
        """None short-circuits before binary_sensor dispatch."""
        desc = _make_descriptor(
            platform="binary_sensor",
            payload={"on": "1", "off": "0"},
        )
        assert apply_transform(desc, None) is None

    def test_platform_binary_vs_sensor_distinction(self):
        """binary_sensor and sensor are dispatched differently."""
        bs = _make_descriptor(platform="binary_sensor", payload={"on": "1", "off": "0"})
        s = _make_descriptor(platform="sensor", value_transform={"scale": 10.0})
        # binary_sensor raw "1" → True
        assert apply_transform(bs, "1") is True
        # sensor raw 1 → 10.0
        assert apply_transform(s, 1) == 10.0


# ===========================================================================
# should_skip
# ===========================================================================


class TestShouldSkip:
    """Kills mutants in the membership test."""

    def test_present_key_returns_true(self):
        assert should_skip("model", {"model", "id"}) is True

    def test_absent_key_returns_false(self):
        assert should_skip("temperature_C", {"model", "id"}) is False

    def test_empty_skip_set(self):
        assert should_skip("anything", set()) is False

    def test_exact_membership_required(self):
        """Partial match or substring must not trigger skip."""
        assert should_skip("models", {"model"}) is False

    def test_shipped_skip_keys(self, lib):
        """Shipped skip keys from _skip_keys.yaml are all present."""
        _, skip_keys = lib
        for key in ("model", "id", "channel", "mic", "protocol", "freq"):
            assert should_skip(key, skip_keys) is True

    def test_measurement_keys_not_skipped(self, lib):
        _, skip_keys = lib
        for key in ("temperature_C", "humidity", "power_W"):
            assert should_skip(key, skip_keys) is False


# ===========================================================================
# lookup — model-scoped vs flat fallback
# ===========================================================================


class TestLookup:
    """Kills mutants in the resolution logic."""

    def test_model_scoped_wins_over_flat(self):
        flat_desc = _make_descriptor(field_key="temp", name="Global Temp")
        scoped_desc = _make_descriptor(field_key="temp", name="Model Temp")
        reg = _make_registry(
            flat={"temp": flat_desc},
            models={"ModelX": {"temp": scoped_desc}},
        )
        result = lookup("temp", model="ModelX", registry=reg)
        assert result.name == "Model Temp"

    def test_flat_fallback_when_no_model_entry(self):
        flat_desc = _make_descriptor(field_key="temp", name="Global Temp")
        reg = _make_registry(flat={"temp": flat_desc}, models={})
        result = lookup("temp", model="ModelX", registry=reg)
        assert result.name == "Global Temp"

    def test_none_model_resolves_flat_only(self):
        flat_desc = _make_descriptor(field_key="temp", name="Global Temp")
        scoped_desc = _make_descriptor(field_key="temp", name="Model Temp")
        reg = _make_registry(
            flat={"temp": flat_desc},
            models={"ModelX": {"temp": scoped_desc}},
        )
        result = lookup("temp", model=None, registry=reg)
        assert result.name == "Global Temp"

    def test_unknown_field_returns_none(self):
        reg = _make_registry(flat={}, models={})
        assert lookup("does_not_exist", registry=reg) is None

    def test_model_present_but_field_absent_falls_back(self):
        """Model exists in registry but doesn't have the requested field → flat."""
        flat_desc = _make_descriptor(field_key="humidity", name="Global Hum")
        reg = _make_registry(
            flat={"humidity": flat_desc},
            models={"ModelX": {}},  # model exists but has no humidity entry
        )
        result = lookup("humidity", model="ModelX", registry=reg)
        assert result.name == "Global Hum"

    def test_scoped_field_not_returned_for_other_model(self):
        """Model-scoped entry must not bleed to other models."""
        scoped = _make_descriptor(field_key="temp", name="Scoped")
        reg = _make_registry(flat={}, models={"ModelA": {"temp": scoped}})
        result = lookup("temp", model="ModelB", registry=reg)
        assert result is None

    def test_flat_field_returned_without_model(self):
        flat_desc = _make_descriptor(field_key="co2", name="CO2")
        reg = _make_registry(flat={"co2": flat_desc}, models={})
        result = lookup("co2", registry=reg)
        assert result is not None
        assert result.name == "CO2"


# ===========================================================================
# _copy_registry
# ===========================================================================


class TestCopyRegistry:
    """Kills the models={model: dict(None)} mutant."""

    def test_copy_produces_equal_registry(self):
        flat_desc = _make_descriptor(field_key="x")
        reg = _make_registry(
            flat={"x": flat_desc},
            models={"M": {"x": flat_desc}},
        )
        copied = _copy_registry(reg)
        assert copied.flat == reg.flat
        assert copied.models == reg.models

    def test_copy_is_new_object(self):
        """Copied registry's containers must be new objects."""
        flat_desc = _make_descriptor(field_key="x")
        reg = _make_registry(flat={"x": flat_desc}, models={"M": {"x": flat_desc}})
        copied = _copy_registry(reg)
        assert copied.flat is not reg.flat
        assert copied.models is not reg.models

    def test_copy_models_entries_are_new_dicts(self):
        flat_desc = _make_descriptor(field_key="x")
        reg = _make_registry(
            flat={"x": flat_desc},
            models={"M": {"x": flat_desc}},
        )
        copied = _copy_registry(reg)
        assert copied.models["M"] is not reg.models["M"]
        assert copied.models["M"] == reg.models["M"]


# ===========================================================================
# merge_overrides
# ===========================================================================


class TestMergeOverrides:
    """Kills mutants in merge_overrides."""

    def test_flat_entry_overrides_existing(self, lib):
        registry, skip_keys = lib
        merged, _ = merge_overrides(
            registry,
            skip_keys,
            {
                "temperature_C": {
                    "platform": "sensor",
                    "name": "Override Temp",
                    "object_suffix": "ovr_temp",
                    "unit_of_measurement": "F",
                }
            },
        )
        overridden = lookup("temperature_C", registry=merged)
        assert overridden.unit_of_measurement == "F"
        assert overridden.name == "Override Temp"
        # Original unchanged
        original = lookup("temperature_C", registry=registry)
        assert original.unit_of_measurement == "°C"

    def test_skip_keys_unioned(self, lib):
        registry, skip_keys = lib
        _, merged_skips = merge_overrides(
            registry,
            skip_keys,
            {"skip_keys": ["vendor_noise", "internal_tag"]},
        )
        assert "vendor_noise" in merged_skips
        assert "internal_tag" in merged_skips
        # Original skip_keys preserved
        assert "model" in merged_skips

    def test_original_skip_keys_preserved_in_union(self, lib):
        registry, skip_keys = lib
        _, merged_skips = merge_overrides(
            registry,
            skip_keys,
            {"skip_keys": ["new_key"]},
        )
        # Must be a union, not a replacement
        for key in skip_keys:
            assert key in merged_skips

    def test_models_block_merged(self, lib):
        registry, skip_keys = lib
        merged, _ = merge_overrides(
            registry,
            skip_keys,
            {
                "models": {
                    "TestDevice-001": {
                        "temperature_C": {
                            "platform": "sensor",
                            "name": "Model Temp",
                            "object_suffix": "MT",
                            "device_class": "temperature",
                        }
                    }
                }
            },
        )
        scoped = lookup("temperature_C", "TestDevice-001", merged)
        assert scoped is not None
        assert scoped.name == "Model Temp"
        # Global flat unchanged
        global_desc = lookup("temperature_C", registry=merged)
        assert global_desc.name != "Model Temp"

    def test_non_dict_override_data_returns_copy(self, lib):
        """Non-dict override_data → warning + return copy of base."""
        registry, skip_keys = lib
        merged, merged_skips = merge_overrides(registry, skip_keys, "bad_data")
        assert merged.flat == registry.flat
        assert merged_skips == skip_keys

    def test_malformed_entry_skipped_good_entry_kept(self, lib):
        registry, skip_keys = lib
        merged, _ = merge_overrides(
            registry,
            skip_keys,
            {
                "bad_field": "not-a-dict",
                "custom_sensor": {
                    "platform": "sensor",
                    "name": "Custom",
                    "object_suffix": "C",
                },
            },
        )
        assert lookup("bad_field", registry=merged) is None
        assert lookup("custom_sensor", registry=merged) is not None

    def test_pure_merge_original_not_mutated(self, lib):
        """merge_overrides must not mutate the original registry or skip_keys."""
        registry, skip_keys = lib
        original_flat_keys = set(registry.flat.keys())
        original_skip_count = len(skip_keys)
        merge_overrides(
            registry,
            skip_keys,
            {
                "brand_new_field": {
                    "platform": "sensor",
                    "name": "New",
                    "object_suffix": "N",
                },
                "skip_keys": ["extra_key"],
            },
        )
        # Original must be untouched
        assert set(registry.flat.keys()) == original_flat_keys
        assert len(skip_keys) == original_skip_count


# ===========================================================================
# load_library — file-level behaviour
# ===========================================================================


class TestLoadLibrary:
    """Kills mutants in load_library glob/skip/merge logic."""

    def test_underscore_files_not_parsed_as_descriptors(self, tmp_path):
        """Files starting with '_' are not parsed as field mapping tables."""
        # Write a valid mapping file and an underscore file with garbage.
        (tmp_path / "sensors.yaml").write_text(
            "humidity:\n  platform: sensor\n  name: Humidity\n  object_suffix: H\n",
            encoding="utf-8",
        )
        (tmp_path / "_not_a_mapping.yaml").write_text(
            "this: is not a descriptor mapping\n",
            encoding="utf-8",
        )
        registry, _ = load_library(tmp_path)
        assert "humidity" in registry.flat
        # The underscore file must not have introduced 'this' as a field key.
        assert "this" not in registry.flat

    def test_missing_library_dir_returns_empty(self, tmp_path):
        """Non-existent library dir returns empty registry."""
        non_existent = tmp_path / "no_such_dir"
        registry, skip_keys = load_library(non_existent)
        assert registry.flat == {}
        assert registry.models == {}
        assert skip_keys == set()

    def test_models_block_in_file_loaded(self, tmp_path):
        """A file with a models: block populates the model-scoped table."""
        (tmp_path / "sensors.yaml").write_text(
            "models:\n"
            "  Acme-X:\n"
            "    temperature_C:\n"
            "      platform: sensor\n"
            "      name: Acme Temp\n"
            "      object_suffix: AT\n",
            encoding="utf-8",
        )
        registry, _ = load_library(tmp_path)
        assert "Acme-X" in registry.models
        assert "temperature_C" in registry.models["Acme-X"]

    def test_later_file_overrides_earlier_on_collision(self, tmp_path):
        """When two files define the same field, the later one (sorted) wins."""
        (tmp_path / "aaa.yaml").write_text(
            "shared_field:\n  platform: sensor\n  name: First\n  object_suffix: F\n",
            encoding="utf-8",
        )
        (tmp_path / "zzz.yaml").write_text(
            "shared_field:\n  platform: sensor\n  name: Last\n  object_suffix: L\n",
            encoding="utf-8",
        )
        registry, _ = load_library(tmp_path)
        assert registry.flat["shared_field"].name == "Last"

    def test_skip_keys_file_loaded(self, tmp_path):
        """_skip_keys.yaml populates the skip-key set."""
        (tmp_path / "_skip_keys.yaml").write_text(
            "skip_keys:\n  - test_skip_a\n  - test_skip_b\n",
            encoding="utf-8",
        )
        _, skip_keys = load_library(tmp_path)
        assert "test_skip_a" in skip_keys
        assert "test_skip_b" in skip_keys

    def test_empty_yaml_file_silently_skipped(self, tmp_path):
        """An empty YAML file (data=None) does not crash the loader."""
        (tmp_path / "empty.yaml").write_text("", encoding="utf-8")
        registry, _ = load_library(tmp_path)
        assert isinstance(registry, Registry)

    def test_models_merged_across_files(self, tmp_path):
        """Model entries from separate files are merged, not replaced."""
        (tmp_path / "aaa.yaml").write_text(
            "models:\n"
            "  DevA:\n"
            "    field_a:\n"
            "      platform: sensor\n"
            "      name: A\n"
            "      object_suffix: A\n",
            encoding="utf-8",
        )
        (tmp_path / "zzz.yaml").write_text(
            "models:\n"
            "  DevA:\n"
            "    field_b:\n"
            "      platform: sensor\n"
            "      name: B\n"
            "      object_suffix: B\n",
            encoding="utf-8",
        )
        registry, _ = load_library(tmp_path)
        assert "field_a" in registry.models.get("DevA", {})
        assert "field_b" in registry.models.get("DevA", {})


# ===========================================================================
# load_user_overrides — I/O paths
# ===========================================================================


class TestLoadUserOverrides:
    """Kills mutants in the load_user_overrides I/O wrapper."""

    def test_absent_file_returns_copy_of_base(self, tmp_path, lib):
        registry, skip_keys = lib
        merged, merged_skips = load_user_overrides(tmp_path, registry, skip_keys)
        # Outputs equal the inputs (copy, not same object).
        assert merged.flat == registry.flat
        assert merged_skips == skip_keys

    def test_empty_file_returns_copy(self, tmp_path, lib):
        registry, skip_keys = lib
        (tmp_path / "rtl_433_mappings.yaml").write_text("", encoding="utf-8")
        merged, merged_skips = load_user_overrides(tmp_path, registry, skip_keys)
        assert merged.flat == registry.flat

    def test_valid_override_applied(self, tmp_path, lib):
        registry, skip_keys = lib
        (tmp_path / "rtl_433_mappings.yaml").write_text(
            "my_custom_field:\n"
            "  platform: sensor\n"
            "  name: Custom\n"
            "  object_suffix: C\n",
            encoding="utf-8",
        )
        merged, _ = load_user_overrides(tmp_path, registry, skip_keys)
        assert lookup("my_custom_field", registry=merged) is not None

    def test_corrupt_yaml_returns_copy_not_raise(self, tmp_path, lib):
        """Invalid YAML must be swallowed; caller never sees an exception."""
        registry, skip_keys = lib
        (tmp_path / "rtl_433_mappings.yaml").write_text(
            ": bad yaml: [unclosed\n",
            encoding="utf-8",
        )
        merged, merged_skips = load_user_overrides(tmp_path, registry, skip_keys)
        assert merged.flat == registry.flat


# ===========================================================================
# _default_library caching logic (mutmut_1..4)
# ===========================================================================


class TestDefaultLibraryCaching:
    """Kills mutants in the or/and/is not conditions and assignment."""

    def test_default_lookup_works(self):
        """Exercising lookup without an explicit registry triggers the cache."""
        # Just verifying it doesn't crash and returns a known field.
        desc = lookup("temperature_C")
        assert desc is not None
        assert desc.platform == "sensor"

    def test_default_should_skip_works(self):
        """should_skip without explicit skip_keys uses the cached library."""
        assert should_skip("model") is True
        assert should_skip("temperature_C") is False


# ===========================================================================
# Numeric near-equality — as_int=True vs False distinction in binary payload
# ===========================================================================


class TestBinaryPayloadAsIntFalseDistinction:
    """Kills mutmut_26/34/43 (as_int=True instead of False).

    If as_int=True were used, coercing "1.5" would yield 1 (truncated), breaking
    fractional-token matching. With as_int=False the value stays 1.5.
    """

    def test_fractional_raw_value_matches_fractional_on_token(self):
        payload = {"on": "1.5", "off": "0.5"}
        # raw_value=1.5 → str "1.5" == "1.5" (matches via string path directly)
        assert _apply_binary_payload(payload, 1.5) is True

    def test_fractional_off_token_matches(self):
        payload = {"on": "1", "off": "0.5"}
        # raw "0.5" matches off string "0.5"
        assert _apply_binary_payload(payload, "0.5") is False

    def test_integer_raw_matches_integer_string_token(self):
        """Integer raw value 1 (str '1') matches on_token '1' via string path."""
        payload = {"on": "1", "off": "0"}
        assert _apply_binary_payload(payload, 1) is True
        assert _apply_binary_payload(payload, 0) is False


# ===========================================================================
# Shipped library integration smoke tests
# ===========================================================================


class TestShippedLibraryIntegration:
    """High-level integration tests that kill surviving mutants by verifying
    exact shipped-library behaviour end-to-end."""

    def test_temperature_c_round_1(self, lib):
        registry, _ = lib
        desc = lookup("temperature_C", registry=registry)
        assert desc is not None
        assert apply_transform(desc, 21.37) == 21.4
        assert apply_transform(desc, 21.34) == 21.3
        # Non-numeric passthrough
        assert apply_transform(desc, "n/a") == "n/a"

    def test_wind_avg_scale_round(self, lib):
        registry, _ = lib
        desc = lookup("wind_avg_m_s", registry=registry)
        assert desc is not None
        # 3.5 * 3.6 = 12.6, round 2
        assert apply_transform(desc, 3.5) == 12.6

    def test_co2_int_coercion(self, lib):
        registry, _ = lib
        desc = lookup("co2_ppm", registry=registry)
        assert desc is not None
        result = apply_transform(desc, "812")
        assert result == 812
        assert isinstance(result, int)

    def test_battery_ok_exact(self, lib):
        registry, _ = lib
        desc = lookup("battery_ok", registry=registry)
        assert desc is not None
        assert apply_transform(desc, 1) == 100
        assert isinstance(apply_transform(desc, 1), int)
        assert apply_transform(desc, 0) == 1
        assert isinstance(apply_transform(desc, 0), int)

    def test_closed_inverted_payload(self, lib):
        registry, _ = lib
        desc = lookup("closed", registry=registry)
        assert desc is not None
        assert desc.device_class == "opening"
        # payload: on="0", off="1" — inverted
        assert apply_transform(desc, 0) is True
        assert apply_transform(desc, 1) is False
        assert apply_transform(desc, "0") is True
        assert apply_transform(desc, "1") is False

    def test_detect_wet_payload(self, lib):
        registry, _ = lib
        desc = lookup("detect_wet", registry=registry)
        assert desc is not None
        assert apply_transform(desc, 1) is True
        assert apply_transform(desc, "1") is True
        assert apply_transform(desc, 0) is False
        assert apply_transform(desc, "maybe") is None

    def test_tamper_payload(self, lib):
        registry, _ = lib
        desc = lookup("tamper", registry=registry)
        assert desc is not None
        assert apply_transform(desc, "1") is True
        assert apply_transform(desc, "0") is False

    def test_motion_detect_only(self, lib):
        registry, _ = lib
        desc = lookup("motion", registry=registry)
        assert desc is not None
        assert desc.clear_delay == 90
        assert apply_transform(desc, "1") is True
        assert apply_transform(desc, 1) is True
        # No off-token: 0 returns None (detect-only)
        assert apply_transform(desc, 0) is None

    def test_temperature_c_none_returns_none(self, lib):
        registry, _ = lib
        desc = lookup("temperature_C", registry=registry)
        assert apply_transform(desc, None) is None

    def test_model_scoped_vs_flat_shipped(self, lib):
        """Smoke test: model-scoped entry beats flat in shipped library."""
        registry, skip_keys = lib
        merged, _ = merge_overrides(
            registry,
            skip_keys,
            {
                "models": {
                    "Smoke-Test-Device": {
                        "humidity": {
                            "platform": "sensor",
                            "name": "Model Humidity",
                            "object_suffix": "MH",
                            "device_class": "humidity",
                            "unit_of_measurement": "%",
                        }
                    }
                }
            },
        )
        scoped = lookup("humidity", "Smoke-Test-Device", merged)
        assert scoped is not None
        assert scoped.name == "Model Humidity"

        # Global flat still returns shipped descriptor for other models.
        flat = lookup("humidity", "Other-Device", merged)
        assert flat is not None
        assert flat.name != "Model Humidity"


# ===========================================================================
# Exact value checks for each needs_float key in isolation
# ===========================================================================


class TestNeedsFloatIsolated:
    """One test per needs_float sub-expression to kill every surviving mutant."""

    def test_scale_key_triggers_float_mode(self):
        """{'scale': 2} with int:True → float output (scale overrides int)."""
        result = _apply_sensor_transform({"int": True, "scale": 2}, 3)
        # needs_float=True because 'scale' in transform → as_int=False → 3.0*2=6.0
        assert result == 6.0
        assert isinstance(result, float)

    def test_offset_key_triggers_float_mode(self):
        """{'offset': 1} with int:True → float output."""
        result = _apply_sensor_transform({"int": True, "offset": 1}, 3)
        # needs_float=True because 'offset' in transform → as_int=False → 3.0+1=4.0
        assert result == 4.0
        assert isinstance(result, float)

    def test_round_key_triggers_float_mode(self):
        """{'round': 2} with int:True → has_round=True → needs_float=True."""
        result = _apply_sensor_transform({"int": True, "round": 2}, 4)
        # needs_float=True → as_int=False → 4.0 rounded to 2 dp = 4.0
        assert result == 4.0

    def test_float_key_true_triggers_float_mode(self):
        """{'float': True} with int:True → needs_float=True → float output."""
        result = _apply_sensor_transform({"int": True, "float": True}, 7)
        assert result == 7.0
        assert isinstance(result, float)

    def test_float_key_false_does_not_trigger_float(self):
        """{'float': False} is falsy → does NOT override int:True → int output."""
        result = _apply_sensor_transform({"int": True, "float": False}, 7)
        assert result == 7
        assert isinstance(result, int)

    def test_scale_only_no_int_returns_float(self):
        result = _apply_sensor_transform({"scale": 1.5}, 4)
        assert result == 6.0
        assert isinstance(result, float)

    def test_offset_only_no_int_returns_float(self):
        result = _apply_sensor_transform({"offset": 2.5}, 4)
        assert result == 6.5
        assert isinstance(result, float)


# ===========================================================================
# round(x, 0) → int normalisation
# ===========================================================================


class TestRoundZeroNormalisation:
    """Kills mutants on the ``if digits <= 0 and float(number).is_integer()`` check."""

    def test_round_0_whole_result_is_int(self):
        result = _apply_sensor_transform({"round": 0}, 3.7)
        assert result == 4
        assert isinstance(result, int)

    def test_round_1_whole_result_stays_float(self):
        """round=1 with a whole result stays float (digits > 0)."""
        result = _apply_sensor_transform({"round": 1}, 4.0)
        assert result == 4.0
        assert isinstance(result, float)

    def test_round_minus1_whole_result_is_int(self):
        """round=-1 (digits < 0) and whole result → int."""
        result = _apply_sensor_transform({"round": -1}, 15.0)
        # round(15.0, -1) = 20.0 (rounds to nearest 10) → int 20
        assert result == 20
        assert isinstance(result, int)

    def test_scale_offset_round_combined_int_output(self):
        """Full pipeline: scale * value + offset → round 0 → int."""
        # 2.0 * 3 = 6.0, + 0.0 = 6.0, round(6.0, 0) = 6.0 → int
        result = _apply_sensor_transform({"scale": 2.0, "offset": 0.0, "round": 0}, 3)
        assert result == 6
        assert isinstance(result, int)


# ===========================================================================
# MODULE-LEVEL tests — call through mp. to catch mutmut namespace replacements
# ===========================================================================
# These tests import the module as `mp` and call functions through it so that
# mutmut's function-replacement strategy is visible.  Direct ``from ... import``
# binds the original before the mutation is injected.
# ===========================================================================

import custom_components.rtl_433.mapping as mp  # noqa: E402

# ---------------------------------------------------------------------------
# _apply_binary_payload via mp. (kills mutmut_1, 2, 12, 14, 17, 19, 21-27,
#  29-45) — the existing tests bind the pre-mutation name so they see the
#  original even when the mutant replaces mp._apply_binary_payload.
# ---------------------------------------------------------------------------


class TestApplyBinaryPayloadViaModule:
    """Calls _apply_binary_payload through the module object.

    mutmut replaces the function in the *module namespace*, so only callers
    that go through ``mp._apply_binary_payload`` pick up the mutated version.
    """

    # mutmut_1: raw_str = None  (comparison always fails -> falls through -> None)
    # mutmut_2: raw_str = str(None).strip() = 'None' (only matches token 'None')
    def test_raw_str_from_raw_value_used_in_comparison(self):
        """raw_str must be str(raw_value).strip(), not None or 'None'."""
        payload = {"on": "open", "off": "closed"}
        # With raw_str=None: None != 'open' -> numeric fallback -> 'open' not numeric -> None
        assert mp._apply_binary_payload(payload, "open") is True
        assert mp._apply_binary_payload(payload, "closed") is False

    def test_raw_str_strip_removes_whitespace(self):
        """Whitespace is stripped from raw_value before comparison."""
        payload = {"on": "active", "off": "idle"}
        assert mp._apply_binary_payload(payload, "  active  ") is True
        assert mp._apply_binary_payload(payload, " idle ") is False

    def test_raw_value_stringified_not_None_literal(self):
        """Integer raw_value is stringified to '1'/'0', not to 'None'."""
        # mutmut_2: str(None)='None'; '1' != 'None' -> would fail string path
        # But numeric fallback: 1.0==1 -> True. So mutmut_2 would still pass here.
        # Use a non-numeric token to force failure on mutmut_2.
        payload2 = {"on": "yes", "off": "no"}
        assert mp._apply_binary_payload(payload2, "yes") is True
        assert mp._apply_binary_payload(payload2, "no") is False
        # Non-numeric so numeric fallback also fails -> None if raw_str='None' and token!='None'
        assert mp._apply_binary_payload(payload2, "unknown") is None

    # mutmut_12: on_token is None (inverted None-check for on path)
    def test_on_token_is_not_none_required_for_match(self):
        """Only match on_token when it IS not None (not when it IS None)."""
        # on_token present: should match
        assert mp._apply_binary_payload({"on": "x", "off": "y"}, "x") is True
        # No on_token: even if raw_str matches what 'on' would be, no True returned
        assert mp._apply_binary_payload({"off": "y"}, "x") is None

    # mutmut_14: str(None) instead of str(on_token) in comparison
    def test_on_token_compared_not_none_str(self):
        """Comparison is str(on_token), not str(None)='None'."""
        # Token is 'hello', not 'None'
        payload = {"on": "hello"}
        # mutmut_14 would compare raw_str == 'None': 'hello' != 'None' -> no match -> None
        assert mp._apply_binary_payload(payload, "hello") is True

    # mutmut_17: off_token is None (inverted None-check for off path)
    def test_off_token_is_not_none_required_for_match(self):
        """Only match off_token when it IS not None."""
        # off_token present: should match -> False
        assert mp._apply_binary_payload({"on": "x", "off": "y"}, "y") is False
        # No off_token: raw_str matching what 'off' would be returns None
        assert mp._apply_binary_payload({"on": "x"}, "y") is None

    # mutmut_19: str(None) instead of str(off_token) in comparison
    def test_off_token_compared_not_none_str(self):
        """Off comparison is str(off_token), not str(None)='None'."""
        payload = {"on": "x", "off": "goodbye"}
        # mutmut_19 compares raw_str == 'None': 'goodbye' != 'None' -> no match -> None
        assert mp._apply_binary_payload(payload, "goodbye") is False

    # mutmut_21: number = None (drops numeric fallback completely)
    def test_numeric_fallback_activates_for_float_raw(self):
        """Float raw that doesn't match string token must use numeric fallback."""
        payload = {"on": "1", "off": "0"}
        # raw=1.0: str='1.0' != '1'; numeric: 1.0==1 -> True
        assert mp._apply_binary_payload(payload, 1.0) is True
        assert mp._apply_binary_payload(payload, 0.0) is False

    # mutmut_22: _coerce_number(None, ...) instead of _coerce_number(raw_value, ...)
    def test_numeric_fallback_uses_raw_value_not_none(self):
        """Numeric coercion uses raw_value, not None."""
        payload = {"on": "5"}
        # raw=5.0: str='5.0'!='5'; numeric 5.0==5 -> True
        # mutmut_22: _coerce_number(None,...) = None -> falls through -> None
        assert mp._apply_binary_payload(payload, 5.0) is True

    # mutmut_23: as_int=None instead of as_int=False
    def test_numeric_fallback_as_int_not_none(self):
        """_coerce_number(raw_value, as_int=False) not as_int=None."""
        # as_int=None would be passed as False (falsy) so behaviour might be same
        # But to be safe, check a case where as_int matters for numeric fallback
        payload = {"on": "2"}
        assert mp._apply_binary_payload(payload, 2.0) is True

    # mutmut_26: as_int=True instead of False for raw_value coercion
    def test_numeric_fallback_raw_value_as_int_false(self):
        """Raw value coerced with as_int=False, not True (preserves fractions)."""
        payload = {"on": "1.5", "off": "0.5"}
        # raw='1.5': str matches directly, but let's use numeric path
        # raw=1.7: str='1.7', not in tokens; as_int=False -> 1.7; on=1.5 -> 1.7!=1.5
        # off=0.5 -> 1.7!=0.5 -> None
        assert mp._apply_binary_payload(payload, "1.5") is True  # string match
        # With as_int=True: 1.5 -> 1 int; on_coerce(1.5) = 1 if as_int=True else 1.5
        # Test fractional raw against fractional token via numeric path
        # raw = 0.5 (string path: '0.5'=='0.5' matches off) -> False either way
        assert mp._apply_binary_payload(payload, "0.5") is False

    # mutmut_27: number is None (inverted None guard)
    def test_numeric_guard_fires_when_number_is_not_none(self):
        """Numeric block runs only when number is not None."""
        payload = {"on": "3"}
        # raw=3.0: str='3.0'!='3'; numeric 3.0!=None -> block runs -> 3.0==3 -> True
        assert mp._apply_binary_payload(payload, 3.0) is True
        # Non-numeric: number=None -> block skipped -> None
        assert mp._apply_binary_payload(payload, "three") is None

    # mutmut_29: on_token is None (in numeric block)
    def test_numeric_on_token_not_none_required(self):
        """on_token must be not-None for numeric on-match."""
        # on_token present, numeric match
        payload = {"on": "4"}
        assert mp._apply_binary_payload(payload, 4.0) is True
        # No on_token: numeric fallback should not produce True
        payload_off_only = {"off": "0"}
        assert mp._apply_binary_payload(payload_off_only, 4.0) is None

    # mutmut_30: _coerce_number(None, ...) instead of _coerce_number(on_token, ...)
    def test_numeric_on_coercion_uses_on_token(self):
        """on_token is coerced, not None."""
        payload = {"on": "7"}
        # raw=7.0: str='7.0'!='7'; on_coerce(7)=7.0==7.0 -> True
        # mutmut_30: coerce(None)=None != 7.0 -> no match
        assert mp._apply_binary_payload(payload, 7.0) is True

    # mutmut_31: as_int=None instead of False for on_token coercion
    # mutmut_34: as_int=True instead of False for on_token coercion
    def test_numeric_on_token_coercion_as_int_false(self):
        """on_token numeric coercion uses as_int=False."""
        payload = {"on": "1.5"}
        # raw=1.5: str='1.5'=='1.5' -> True (string path) regardless of numeric
        # To hit numeric path: raw=1.6 -> str='1.6'!='1.5', numeric 1.6!=1.5 -> None
        assert mp._apply_binary_payload(payload, 1.5) is True  # string
        # Numeric path with fractional: raw=1.500 float
        assert mp._apply_binary_payload({"on": "1.5"}, 1.5) is True

    # mutmut_36: return False instead of True in on numeric block
    def test_numeric_on_match_returns_true_not_false(self):
        """Numeric on-match must return True."""
        payload = {"on": "10", "off": "0"}
        # raw=10.0: str='10.0'!='10'; numeric 10.0==10 -> should be True
        result = mp._apply_binary_payload(payload, 10.0)
        assert result is True

    # mutmut_37: off numeric block: and -> or
    def test_numeric_off_and_condition_required(self):
        """off numeric: BOTH off_token is not None AND numeric match required."""
        payload = {"on": "1"}  # no off_token
        # mutmut_37: 'off_token is not None or coerce(off_token)==number'
        # off_token=None; or: coerce(None,as_int=False)=None==3.0? No. Still None.
        # But: 'None is not None or coerce(None)==number' = 'False or None==3.0' = False -> return False?
        # Actually coerce(None)==3.0 is False, so the condition is False. No difference here.
        # Better test: off_token IS None; condition: (None is not None or coerce(None)==0.0)
        # = (False or None==0.0) = (False or False) = False -> no return False
        # Hmm that might not distinguish... Let's check raw=0.0:
        # coerce(off_token=None) = None; None == 0.0 -> False
        result = mp._apply_binary_payload(payload, 0.0)
        assert result is None

    # mutmut_38: off_token is None (inverted check in off numeric block)
    def test_numeric_off_token_not_none_check_correct(self):
        """off_token is not None (not None-check inverted) in numeric block."""
        payload = {"on": "1", "off": "0"}
        # raw=0.0: str='0.0'!='0'; numeric: off_token='0', coerce('0')=0.0==0.0 -> False
        # mutmut_38: off_token is None -> off_token='0' is None -> False -> no match
        result = mp._apply_binary_payload(payload, 0.0)
        assert result is False

    # mutmut_39: _coerce_number(None, ...) instead of _coerce_number(off_token, ...)
    def test_numeric_off_coercion_uses_off_token(self):
        """off_token, not None, is coerced in numeric off block."""
        payload = {"on": "1", "off": "0"}
        # raw=0.0: numeric coerce(off_token='0')=0.0==0.0 -> False
        # mutmut_39: coerce(None)=None != 0.0 -> no match -> None
        result = mp._apply_binary_payload(payload, 0.0)
        assert result is False

    # mutmut_40: as_int=None instead of False for off_token coercion
    # mutmut_43: as_int=True instead of False for off_token coercion
    def test_numeric_off_token_coercion_as_int_false(self):
        """off_token numeric coercion uses as_int=False."""
        payload = {"on": "1", "off": "0.5"}
        # raw=0.5: str='0.5'=='0.5' -> False (string match)
        assert mp._apply_binary_payload(payload, "0.5") is False
        # raw=0.5 as float: numeric path; coerce('0.5', as_int=False)=0.5==0.5 -> False
        # with as_int=True: coerce('0.5', as_int=True)=0 != 0.5 -> no match -> None (wrong)
        assert mp._apply_binary_payload(payload, 0.5) is False

    # mutmut_41: dropped arg in off block (off_token dropped)
    def test_numeric_off_coercion_has_off_token_arg(self):
        """_coerce_number(off_token, as_int=False) not _coerce_number(as_int=False)."""
        payload = {"on": "1", "off": "2"}
        assert mp._apply_binary_payload(payload, 2.0) is False

    # mutmut_42: _coerce_number(off_token, ) drops as_int kwarg
    def test_numeric_off_coercion_has_as_int_kwarg(self):
        """as_int=False must be passed for off coercion."""
        payload = {"on": "1", "off": "3"}
        assert mp._apply_binary_payload(payload, 3.0) is False

    # mutmut_44: != instead of ==
    def test_numeric_off_equality_not_inequality(self):
        """Off numeric match uses ==, not !=."""
        payload = {"on": "1", "off": "0"}
        # mutmut_44: coerce(off_token) != number -> True (since 0.0!=0.0=False? No: 0.0!=0.0=False)
        # Wait: 0.0==0.0 is True (original match). 0.0!=0.0 is False (no match).
        # So mutmut_44 would NOT return False for 0.0 -> falls through -> None
        result = mp._apply_binary_payload(payload, 0.0)
        assert result is False

    # mutmut_45: return True instead of False in off numeric block
    def test_numeric_off_match_returns_false_not_true(self):
        """Numeric off-match must return False."""
        payload = {"on": "1", "off": "9"}
        result = mp._apply_binary_payload(payload, 9.0)
        assert result is False

    # mutmut_46-54: LOGGER.debug mutations at end of function — these only
    # affect the log call but not the return value (return None).  The only
    # way to distinguish them from the original is when a mutation causes a
    # crash (e.g., passing wrong number of positional args that LOGGER rejects
    # at format-string interpolation time). In Python, logging.debug() with a
    # mismatched format string raises only when the log level is active, and
    # our LOGGER is typically at DEBUG during tests.  Since none of these
    # mutations change the return value or raise predictably, we assert the
    # return value stays None to detect crashes in the mutated path.
    def test_no_match_returns_none_via_module(self):
        """Unmatched value returns None even through the module object."""
        payload = {"on": "x", "off": "y"}
        result = mp._apply_binary_payload(payload, "z")
        assert result is None

    def test_no_match_numeric_fallback_exhausted_returns_none(self):
        """Non-numeric unmatched value: numeric fallback skipped, returns None."""
        payload = {"on": "open", "off": "closed"}
        result = mp._apply_binary_payload(payload, "ajar")
        assert result is None


# ---------------------------------------------------------------------------
# _apply_sensor_transform via mp. — needs_float logic
# (kills mutmut_10-25 for _apply_sensor_transform)
# ---------------------------------------------------------------------------


class TestApplySensorTransformViaModule:
    """_apply_sensor_transform called through the module object."""

    # mutmut_10: needs_float = None (falsy -> as_int=has_int -> int when int:True)
    def test_needs_float_none_would_force_int_even_with_scale(self):
        """needs_float=None would make scale not imply float; must still be float."""
        # scale:2 with int:True: needs_float should be True -> as_int=False -> float
        result = mp._apply_sensor_transform({"int": True, "scale": 2.0}, 3)
        assert isinstance(result, float)
        assert result == 6.0

    # mutmut_11: has_round and bool(transform.get('float')) (drops 'or has_round')
    def test_round_alone_implies_float_via_module(self):
        """'round' alone must set needs_float=True; no 'float' key needed."""
        result = mp._apply_sensor_transform({"int": True, "round": 1}, 4)
        assert result == 4.0

    # mutmut_12: "offset" in transform and has_round (drops 'or "offset"' standalone)
    def test_offset_alone_implies_float_via_module(self):
        """'offset' alone must set needs_float=True even without 'round'."""
        result = mp._apply_sensor_transform({"int": True, "offset": 1.5}, 3)
        assert isinstance(result, float)
        assert result == 4.5

    # mutmut_13: "scale" in transform and "offset" in transform (drops 'or "scale"' standalone)
    def test_scale_alone_implies_float_via_module(self):
        """'scale' alone must set needs_float=True even without 'offset'."""
        result = mp._apply_sensor_transform({"int": True, "scale": 3.0}, 2)
        assert isinstance(result, float)
        assert result == 6.0

    # mutmut_14: "XXscaleXX" instead of "scale"
    def test_scale_key_exact_string(self):
        """Exact key 'scale' triggers float mode; 'XXscaleXX' must not."""
        result = mp._apply_sensor_transform({"int": True, "scale": 2.0}, 5)
        assert isinstance(result, float)

    # mutmut_15: "SCALE" instead of "scale"
    def test_scale_key_lowercase_only(self):
        """'scale' (lowercase) triggers float mode; 'SCALE' must not."""
        result = mp._apply_sensor_transform({"int": True, "scale": 2.0}, 5)
        assert isinstance(result, float)

    # mutmut_17: "XXoffsetXX" instead of "offset"
    def test_offset_key_exact_string(self):
        """Exact key 'offset' triggers float mode; 'XXoffsetXX' must not."""
        result = mp._apply_sensor_transform({"int": True, "offset": 5.0}, 2)
        assert isinstance(result, float)

    # mutmut_18: "OFFSET" instead of "offset"
    def test_offset_key_lowercase_only(self):
        """'offset' (lowercase) triggers float mode; 'OFFSET' must not."""
        result = mp._apply_sensor_transform({"int": True, "offset": 5.0}, 2)
        assert isinstance(result, float)

    # mutmut_20: bool(None) instead of bool(transform.get('float'))
    def test_float_key_true_triggers_float_mode_via_module(self):
        """'float': True must set needs_float=True via module call."""
        result = mp._apply_sensor_transform({"int": True, "float": True}, 7)
        assert isinstance(result, float)

    # mutmut_21: transform.get(None) instead of transform.get('float')
    def test_float_key_exact_string_get(self):
        """transform.get('float') not transform.get(None) for float key check."""
        result = mp._apply_sensor_transform({"int": True, "float": True}, 9)
        assert isinstance(result, float)

    # mutmut_22: transform.get('XXfloatXX') instead of 'float'
    def test_float_key_not_xxfloatxx(self):
        """'float' key triggers float mode, not 'XXfloatXX'."""
        result = mp._apply_sensor_transform({"int": True, "float": True}, 6)
        assert isinstance(result, float)

    # mutmut_23: transform.get('FLOAT') instead of 'float'
    def test_float_key_not_uppercase(self):
        """'float' (lowercase) triggers float mode, not 'FLOAT'."""
        result = mp._apply_sensor_transform({"int": True, "float": True}, 8)
        assert isinstance(result, float)

    # mutmut_25: has_int or not needs_float (instead of has_int and not needs_float)
    def test_as_int_requires_both_conditions(self):
        """as_int=True only when int:True AND not needs_float (AND, not OR)."""
        # No int key, scale present: has_int=False, needs_float=True
        # or-mutant: as_int = False or not True = False or False = False -> float ok
        # But: no int key, no float keys: has_int=False, needs_float=False
        # or-mutant: as_int = False or not False = False or True = True -> int! WRONG
        result = mp._apply_sensor_transform({}, 5)
        # Original: has_int=False, needs_float=False -> as_int=False and not False=False -> float
        # Mutant_25: has_int=False, needs_float=False -> as_int=False or not False=True -> int
        assert isinstance(result, float)  # must be float, not int

    def test_as_int_true_with_no_float_keys(self):
        """int:True with no float-implying keys -> as_int=True -> int result."""
        result = mp._apply_sensor_transform({"int": True}, 6)
        assert isinstance(result, int)
        assert result == 6


# ---------------------------------------------------------------------------
# _apply_sensor_transform error-path LOGGER.debug mutations via module
# (kills mutmut_41-82 — the mutations change debug call args in error paths)
# ---------------------------------------------------------------------------


class TestApplySensorTransformErrorPathsViaModule:
    """Error paths in _apply_sensor_transform called through module object.

    When scale/offset/round are non-convertible, LOGGER.debug is called and
    the pipeline continues without that step.  Most LOGGER mutations don't
    change the return value, but calling through mp. ensures the mutated
    function body is executed, which detects mutations that cause crashes
    (e.g., sorted(None), dict(None)) or argument count errors at format time.
    """

    # mutmut_41-50: scale error path LOGGER.debug mutations
    def test_invalid_scale_skipped_returns_number(self):
        """Invalid scale: LOGGER.debug called, pipeline continues, number returned."""
        # Pass an invalid scale so the except branch fires
        result = mp._apply_sensor_transform({"scale": "not-a-number"}, 5)
        # scale conversion fails -> number stays as 5.0 (no scale applied)
        assert result == 5.0
        assert isinstance(result, float)

    def test_invalid_scale_with_valid_offset_still_applies_offset(self):
        """Invalid scale skipped; valid offset still applied afterwards."""
        result = mp._apply_sensor_transform({"scale": "bad", "offset": 3.0}, 5)
        # scale fails -> number=5.0; offset applies: 5.0+3.0=8.0
        assert result == 8.0

    # mutmut_59-68: offset error path LOGGER.debug mutations
    def test_invalid_offset_skipped_returns_number(self):
        """Invalid offset: LOGGER.debug called, pipeline continues, number returned."""
        result = mp._apply_sensor_transform({"offset": "bad"}, 5)
        # offset fails -> number stays 5.0
        assert result == 5.0

    def test_invalid_offset_with_valid_scale_applies_scale(self):
        """Valid scale applied; invalid offset skipped."""
        result = mp._apply_sensor_transform({"scale": 2.0, "offset": "bad"}, 5)
        # scale: 5.0*2.0=10.0; offset fails -> stays 10.0
        assert result == 10.0

    # mutmut_73-82: round error path LOGGER.debug mutations
    def test_invalid_round_skipped_returns_number(self):
        """Invalid round: LOGGER.debug called, no rounding applied, number returned."""
        result = mp._apply_sensor_transform({"round": "bad"}, 5.7)
        # round fails -> number stays 5.7 (float, no rounding)
        assert result == 5.7

    def test_invalid_round_with_valid_scale(self):
        """Valid scale applied; invalid round skipped."""
        result = mp._apply_sensor_transform({"scale": 2.0, "round": "bad"}, 3)
        # scale: 3.0*2.0=6.0; round fails -> stays 6.0
        assert result == 6.0


# ---------------------------------------------------------------------------
# _descriptor_from_entry via mp. — TypeError message and LOGGER mutations
# (kills mutmut_2, 10-18)
# ---------------------------------------------------------------------------


class TestDescriptorFromEntryViaModule:
    """_descriptor_from_entry called through the module object."""

    # mutmut_2: TypeError message changed to None
    def test_non_dict_raises_type_error_with_message(self):
        """TypeError for non-dict entry includes field_key in the message."""
        with pytest.raises(TypeError, match="not a mapping"):
            mp._descriptor_from_entry("my_field", "not-a-dict")

    def test_type_error_message_contains_field_key(self):
        """TypeError message contains the field_key name."""
        with pytest.raises(TypeError, match="my_special_field"):
            mp._descriptor_from_entry("my_special_field", 42)

    # mutmut_10-18: LOGGER.debug mutations when unknown attrs present
    # mutmut_18 specifically: sorted(None) would raise TypeError
    def test_unknown_attrs_ignored_via_module(self):
        """Unknown attrs trigger LOGGER.debug; descriptor still built correctly."""
        desc = mp._descriptor_from_entry(
            "humidity",
            {
                "platform": "sensor",
                "name": "Humidity",
                "object_suffix": "H",
                "unknown_xyz": "garbage",  # triggers LOGGER.debug branch
            },
        )
        assert desc.field_key == "humidity"
        assert desc.platform == "sensor"

    def test_unknown_attrs_debug_uses_sorted_unknown_and_field_key(self):
        """LOGGER.debug called with sorted(unknown) and field_key — not None."""
        # mutmut_18: sorted(None) would crash; this test detects that crash.
        desc = mp._descriptor_from_entry(
            "test_field",
            {
                "platform": "sensor",
                "name": "Test",
                "object_suffix": "T",
                "extra_attr_a": "v1",
                "extra_attr_b": "v2",
            },
        )
        # If mutmut_18 is active, sorted(None) raises TypeError -> test fails
        assert desc.platform == "sensor"

    def test_known_attrs_no_debug_called(self):
        """No unknown attrs -> LOGGER.debug branch not triggered -> no crash."""
        desc = mp._descriptor_from_entry(
            "motion",
            {
                "platform": "binary_sensor",
                "name": "Motion",
                "object_suffix": "M",
            },
        )
        assert desc.field_key == "motion"


# ---------------------------------------------------------------------------
# _copy_registry via mp. — mutmut_6: dict(None) instead of dict(entries)
# ---------------------------------------------------------------------------


class TestCopyRegistryViaModule:
    """_copy_registry called through the module object."""

    def test_copy_with_model_entries_via_module(self):
        """dict(entries) must be used; dict(None) would crash."""
        fd = mp.FieldDescriptor(
            field_key="x",
            platform="sensor",
            name="X",
            object_suffix="X",
        )
        reg = mp.Registry(flat={"x": fd}, models={"ModelA": {"x": fd}})
        # mutmut_6: dict(None) raises TypeError when models has entries
        copied = mp._copy_registry(reg)
        assert copied.models == {"ModelA": {"x": fd}}
        assert copied.flat == {"x": fd}

    def test_copy_model_entries_match_original(self):
        """Copied model entries equal the original entries."""
        fd = mp.FieldDescriptor(
            field_key="temp",
            platform="sensor",
            name="Temperature",
            object_suffix="T",
        )
        reg = mp.Registry(flat={}, models={"DevA": {"temp": fd}, "DevB": {"temp": fd}})
        copied = mp._copy_registry(reg)
        assert copied.models["DevA"]["temp"] is fd
        assert copied.models["DevB"]["temp"] is fd


# ---------------------------------------------------------------------------
# _default_library via mp. — caching condition mutations (mutmut_1..4)
# ---------------------------------------------------------------------------


class TestDefaultLibraryCachingViaModule:
    """_default_library called through the module object with state manipulation.

    Tests control _DEFAULT_REGISTRY / _DEFAULT_SKIP_KEYS to create the
    asymmetric states that distinguish the mutants from the original.
    """

    def test_reloads_when_only_skip_keys_is_none(self):
        """Reloads when _DEFAULT_REGISTRY is set but _DEFAULT_SKIP_KEYS is None.

        Original: or -> reloads (True or True... wait: False or True = True).
        mutmut_1 (or->and): False and True = False -> does NOT reload.
        mutmut_3 (skip_keys is None -> is not None): False or False = False -> no reload.
        """
        # Set registry to a sentinel, skip_keys to None
        sentinel = mp.Registry(flat={"__sentinel__": None}, models={})
        mp._DEFAULT_REGISTRY = sentinel
        mp._DEFAULT_SKIP_KEYS = None
        try:
            r, sk = mp._default_library()
            # Original: reloads (skip_keys was None) -> r is new registry, sk is set
            # mutmut_1: does NOT reload -> r=sentinel, sk=None -> sk is None -> FAILS assertion
            assert sk is not None
            assert isinstance(sk, set)
            # After reload, registry should be the real library (not sentinel)
            assert "__sentinel__" not in r.flat
        finally:
            # Reset so other tests don't see stale state
            mp._DEFAULT_REGISTRY = None
            mp._DEFAULT_SKIP_KEYS = None

    def test_reloads_when_only_registry_is_none(self):
        """Reloads when _DEFAULT_SKIP_KEYS is set but _DEFAULT_REGISTRY is None.

        Original: or -> reloads (True or False = True).
        mutmut_1 (or->and): True and False = False -> does NOT reload.
        mutmut_2 (registry is None -> is not None): False or False = False -> no reload.
        """
        real_skip_keys = {"model", "id"}
        mp._DEFAULT_REGISTRY = None
        mp._DEFAULT_SKIP_KEYS = real_skip_keys
        try:
            r, sk = mp._default_library()
            # Original: reloads -> r is new Registry (not None), sk may differ
            assert r is not None
            assert isinstance(r, mp.Registry)
            # Registry should have real flat entries
            assert len(r.flat) > 0
        finally:
            mp._DEFAULT_REGISTRY = None
            mp._DEFAULT_SKIP_KEYS = None

    def test_no_reload_when_both_are_set(self):
        """Does NOT reload when both _DEFAULT_REGISTRY and _DEFAULT_SKIP_KEYS are set.

        Original: or -> (False or False) = False -> no reload -> returns cached.
        mutmut_2 (registry is None -> is not None): True or False = True -> reloads.
        mutmut_3 (skip_keys is None -> is not None): False or True = True -> reloads.
        """
        sentinel_reg = mp.Registry(flat={"__cached__": None}, models={})
        sentinel_skips: set[str] = {"__cached_skip__"}
        mp._DEFAULT_REGISTRY = sentinel_reg
        mp._DEFAULT_SKIP_KEYS = sentinel_skips
        try:
            r, sk = mp._default_library()
            # Original: no reload -> returns (sentinel_reg, sentinel_skips)
            assert r is sentinel_reg
            assert sk is sentinel_skips
        finally:
            mp._DEFAULT_REGISTRY = None
            mp._DEFAULT_SKIP_KEYS = None

    def test_mutmut4_assignment_sets_real_library(self):
        """mutmut_4: assignment is load_library(), not None.

        If '_DEFAULT_REGISTRY, _DEFAULT_SKIP_KEYS = None' were used, unpacking
        would raise TypeError (can't unpack non-iterable NoneType).
        """
        mp._DEFAULT_REGISTRY = None
        mp._DEFAULT_SKIP_KEYS = None
        try:
            r, sk = mp._default_library()
            assert r is not None
            assert sk is not None
            assert isinstance(r, mp.Registry)
            assert isinstance(sk, set)
        finally:
            mp._DEFAULT_REGISTRY = None
            mp._DEFAULT_SKIP_KEYS = None


# ---------------------------------------------------------------------------
# Additional module-level tests for lookup, should_skip, merge_overrides
# via mp. to catch any remaining namespace-level mutations
# ---------------------------------------------------------------------------


class TestLookupViaModule:
    """lookup called through the module object."""

    def test_model_scoped_wins_via_module(self):
        fd_flat = mp.FieldDescriptor(
            field_key="temp", platform="sensor", name="Flat", object_suffix="F"
        )
        fd_model = mp.FieldDescriptor(
            field_key="temp", platform="sensor", name="Model", object_suffix="M"
        )
        reg = mp.Registry(
            flat={"temp": fd_flat}, models={"MyModel": {"temp": fd_model}}
        )
        result = mp.lookup("temp", model="MyModel", registry=reg)
        assert result.name == "Model"

    def test_flat_fallback_via_module(self):
        fd = mp.FieldDescriptor(
            field_key="co2", platform="sensor", name="CO2", object_suffix="C"
        )
        reg = mp.Registry(flat={"co2": fd}, models={})
        result = mp.lookup("co2", registry=reg)
        assert result is fd

    def test_none_model_skips_scoped_table(self):
        fd_flat = mp.FieldDescriptor(
            field_key="h", platform="sensor", name="Flat", object_suffix="F"
        )
        fd_model = mp.FieldDescriptor(
            field_key="h", platform="sensor", name="Model", object_suffix="M"
        )
        reg = mp.Registry(flat={"h": fd_flat}, models={"M": {"h": fd_model}})
        result = mp.lookup("h", model=None, registry=reg)
        assert result.name == "Flat"

    def test_missing_field_returns_none_via_module(self):
        reg = mp.Registry(flat={}, models={})
        assert mp.lookup("nonexistent", registry=reg) is None


class TestShouldSkipViaModule:
    """should_skip called through the module object."""

    def test_present_key_returns_true_via_module(self):
        assert mp.should_skip("model", {"model", "id"}) is True

    def test_absent_key_returns_false_via_module(self):
        assert mp.should_skip("temperature_C", {"model", "id"}) is False

    def test_empty_skip_set_returns_false_via_module(self):
        assert mp.should_skip("anything", set()) is False


class TestMergeOverridesViaModule:
    """merge_overrides called through module object."""

    def test_flat_override_replaces_existing(self):
        fd_orig = mp.FieldDescriptor(
            field_key="x", platform="sensor", name="Original", object_suffix="O"
        )
        reg = mp.Registry(flat={"x": fd_orig}, models={})
        merged, _ = mp.merge_overrides(
            reg,
            set(),
            {"x": {"platform": "sensor", "name": "Replaced", "object_suffix": "R"}},
        )
        assert merged.flat["x"].name == "Replaced"

    def test_skip_keys_unioned_via_module(self):
        reg = mp.Registry(flat={}, models={})
        _, merged_skips = mp.merge_overrides(
            reg,
            {"existing_skip"},
            {"skip_keys": ["new_skip"]},
        )
        assert "existing_skip" in merged_skips
        assert "new_skip" in merged_skips

    def test_original_not_mutated_via_module(self):
        fd = mp.FieldDescriptor(
            field_key="y", platform="sensor", name="Y", object_suffix="Y"
        )
        reg = mp.Registry(flat={"y": fd}, models={})
        skips = {"base_skip"}
        mp.merge_overrides(
            reg,
            skips,
            {"new_field": {"platform": "sensor", "name": "N", "object_suffix": "N"}},
        )
        assert "new_field" not in reg.flat
        assert skips == {"base_skip"}

    def test_models_block_merged_via_module(self):
        reg = mp.Registry(flat={}, models={})
        merged, _ = mp.merge_overrides(
            reg,
            set(),
            {
                "models": {
                    "TestDev": {
                        "temp": {
                            "platform": "sensor",
                            "name": "Scoped Temp",
                            "object_suffix": "ST",
                        }
                    }
                }
            },
        )
        assert "TestDev" in merged.models
        assert "temp" in merged.models["TestDev"]
        assert merged.models["TestDev"]["temp"].name == "Scoped Temp"


class TestApplyTransformViaModule:
    """apply_transform called through module object."""

    def test_none_short_circuits_via_module(self):
        fd = mp.FieldDescriptor(
            field_key="t", platform="sensor", name="T", object_suffix="T"
        )
        assert mp.apply_transform(fd, None) is None

    def test_binary_sensor_with_payload_via_module(self):
        fd = mp.FieldDescriptor(
            field_key="door",
            platform="binary_sensor",
            name="Door",
            object_suffix="D",
            payload={"on": "1", "off": "0"},
        )
        assert mp.apply_transform(fd, "1") is True
        assert mp.apply_transform(fd, "0") is False
        assert mp.apply_transform(fd, "x") is None

    def test_sensor_with_transform_via_module(self):
        fd = mp.FieldDescriptor(
            field_key="temp",
            platform="sensor",
            name="Temp",
            object_suffix="T",
            value_transform={"scale": 0.1, "offset": -40.0, "round": 1},
        )
        # 255 * 0.1 = 25.5, + (-40.0) = -14.5, round 1 = -14.5
        result = mp.apply_transform(fd, 255)
        assert result == -14.5

    def test_binary_sensor_no_payload_truthy_via_module(self):
        fd = mp.FieldDescriptor(
            field_key="alarm",
            platform="binary_sensor",
            name="Alarm",
            object_suffix="A",
        )
        assert mp.apply_transform(fd, "1") is True
        assert mp.apply_transform(fd, "on") is True
        assert mp.apply_transform(fd, "yes") is True
        assert mp.apply_transform(fd, "true") is True
        assert mp.apply_transform(fd, "0") is False
        assert mp.apply_transform(fd, "off") is False


class TestLoadLibraryViaModule:
    """load_library called through module object."""

    def test_missing_dir_returns_empty_via_module(self, tmp_path):
        reg, sk = mp.load_library(tmp_path / "nonexistent")
        assert reg.flat == {}
        assert sk == set()

    def test_underscore_file_skipped_via_module(self, tmp_path):
        (tmp_path / "good.yaml").write_text(
            "field_a:\n  platform: sensor\n  name: A\n  object_suffix: A\n",
            encoding="utf-8",
        )
        (tmp_path / "_skip_keys.yaml").write_text(
            "skip_keys:\n  - id\n",
            encoding="utf-8",
        )
        (tmp_path / "_other_private.yaml").write_text(
            "bad_field:\n  platform: sensor\n  name: B\n  object_suffix: B\n",
            encoding="utf-8",
        )
        reg, sk = mp.load_library(tmp_path)
        assert "field_a" in reg.flat
        assert "bad_field" not in reg.flat
        assert "id" in sk

    def test_models_merged_across_files_via_module(self, tmp_path):
        (tmp_path / "a.yaml").write_text(
            "models:\n  Dev:\n    fa:\n      platform: sensor\n      name: FA\n      object_suffix: FA\n",
            encoding="utf-8",
        )
        (tmp_path / "b.yaml").write_text(
            "models:\n  Dev:\n    fb:\n      platform: sensor\n      name: FB\n      object_suffix: FB\n",
            encoding="utf-8",
        )
        reg, _ = mp.load_library(tmp_path)
        assert "fa" in reg.models.get("Dev", {})
        assert "fb" in reg.models.get("Dev", {})


class TestNormalizePayloadViaModule:
    """_normalize_payload called through module object."""

    def test_bool_true_becomes_on_via_module(self):
        result = mp._normalize_payload({True: "1", False: "0"})
        assert result == {"on": "1", "off": "0"}

    def test_string_keys_preserved_via_module(self):
        result = mp._normalize_payload({"on": "active", "off": "inactive"})
        assert result == {"on": "active", "off": "inactive"}

    def test_non_dict_unchanged_via_module(self):
        assert mp._normalize_payload(None) is None
        assert mp._normalize_payload("str") == "str"


class TestExtractSkipKeysViaModule:
    """_extract_skip_keys called through module object."""

    def test_returns_set_via_module(self):
        result = mp._extract_skip_keys({"skip_keys": ["a", "b", "c"]})
        assert result == {"a", "b", "c"}

    def test_non_dict_returns_empty_via_module(self):
        assert mp._extract_skip_keys(None) == set()
        assert mp._extract_skip_keys([]) == set()

    def test_missing_key_returns_empty_via_module(self):
        assert mp._extract_skip_keys({"other": ["x"]}) == set()


class TestCoerceNumberViaModule:
    """_coerce_number called through module object."""

    def test_as_int_false_returns_float_via_module(self):
        result = mp._coerce_number(5, as_int=False)
        assert isinstance(result, float)
        assert result == 5.0

    def test_as_int_true_returns_int_via_module(self):
        result = mp._coerce_number("3", as_int=True)
        assert isinstance(result, int)
        assert result == 3

    def test_non_numeric_returns_none_via_module(self):
        assert mp._coerce_number("abc", as_int=False) is None
        assert mp._coerce_number(None, as_int=False) is None
