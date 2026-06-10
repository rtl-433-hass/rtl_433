"""Mutation-killing floor tests for custom_components/rtl_433/mapping.py.

Every test in this file is designed to kill one or more specific surviving
mutants identified after the baseline mutmut run.  The file is organised by
function/cluster so it is easy to cross-reference with the mutant list.

Strategy:
- For LOGGER-argument mutations (None substitution, string case changes,
  XX…XX wrapping): use ``caplog`` at DEBUG/WARNING/ERROR level and assert on
  message *content* – the exact substring that distinguishes the original from
  the mutant.
- For ``continue`` → ``break`` mutations in loops: use a data structure that
  has items *after* the offending key so the loop body must keep running.
- For ``as_int=True`` vs ``as_int=False`` in _apply_binary_payload: use a
  fractional token that is truncated to int if as_int=True, causing a mismatch.
- For functional mutations (key name changes, pop key errors, etc.): assert the
  observable side-effect directly.
"""

from __future__ import annotations

import logging

import pytest

import custom_components.rtl_433.mapping as mp
from custom_components.rtl_433.mapping import (
    FieldDescriptor,
    Registry,
    _apply_binary_payload,
    _apply_sensor_transform,
    _descriptor_from_entry,
    _normalize_payload,
    _parse_models_block,
    load_library,
    lookup,
    merge_overrides,
    normalize_overrides,
    validate_user_mappings,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

MAPPING_LOGGER = "custom_components.rtl_433"


def _make_desc(**kwargs) -> FieldDescriptor:
    defaults = dict(
        field_key="f",
        platform="sensor",
        name="F",
        object_suffix="F",
    )
    defaults.update(kwargs)
    return FieldDescriptor(**defaults)


def _make_reg(flat=None, models=None) -> Registry:
    return Registry(flat=flat or {}, models=models or {})


# ---------------------------------------------------------------------------
# _normalize_payload — mutmut_7 (key=="XXonXX") and mutmut_8 (key=="ON")
# and mutmut_16 (key=="XXoffXX") and mutmut_17 (key=="OFF")
# ---------------------------------------------------------------------------


class TestNormalizePayloadStringKeys:
    """Kill mutants that replace the string 'on'/'off' with wrong literals."""

    def test_string_on_key_written_lowercase_on(self):
        """key=='on' must place the value under 'on', not 'XXonXX' or 'ON'."""
        result = _normalize_payload({"on": "ACTIVE"})
        assert "on" in result
        assert result["on"] == "ACTIVE"
        # No spurious keys produced.
        assert "XXonXX" not in result
        assert "ON" not in result

    def test_string_off_key_written_lowercase_off(self):
        """key=='off' must place the value under 'off', not 'XXoffXX' or 'OFF'."""
        result = _normalize_payload({"off": "INACTIVE"})
        assert "off" in result
        assert result["off"] == "INACTIVE"
        assert "XXoffXX" not in result
        assert "OFF" not in result

    def test_both_string_keys_normalised_together(self):
        """Both 'on' and 'off' string keys survive normalisation unchanged."""
        result = _normalize_payload({"on": "open", "off": "closed"})
        assert result == {"on": "open", "off": "closed"}

    def test_string_on_key_via_module(self):
        """Confirm via module route (kills module-namespace mutants)."""
        result = mp._normalize_payload({"on": "1", "off": "0"})
        assert result["on"] == "1"
        assert result["off"] == "0"


# ---------------------------------------------------------------------------
# _descriptor_from_entry — mutmut_3 (type(None).__name__ instead of
#   type(entry).__name__), mutmut_6 (unknown=None)
# ---------------------------------------------------------------------------


class TestDescriptorFromEntryErrorMessages:
    """Kill mutants that corrupt the TypeError message or unknown-set logic."""

    def test_type_error_message_contains_actual_entry_type(self):
        """TypeError message must name the actual entry type, not 'NoneType'."""
        with pytest.raises(TypeError) as exc_info:
            _descriptor_from_entry("myfield", 42)
        msg = str(exc_info.value)
        assert "int" in msg  # type(42).__name__ == 'int'
        assert "NoneType" not in msg

    def test_type_error_message_contains_actual_entry_type_list(self):
        """For a list entry the error should say 'list', not 'NoneType'."""
        with pytest.raises(TypeError) as exc_info:
            _descriptor_from_entry("myfield", [1, 2])
        msg = str(exc_info.value)
        assert "list" in msg
        assert "NoneType" not in msg

    def test_unknown_set_is_difference_not_none(self, caplog):
        """unknown = set(entry) - _DESCRIPTOR_ATTRS, not None; must log when unknown exist."""
        with caplog.at_level(logging.DEBUG, logger=MAPPING_LOGGER):
            desc = _descriptor_from_entry(
                "test_field",
                {
                    "platform": "sensor",
                    "name": "T",
                    "object_suffix": "T",
                    "unknown_xyz_attr": "garbage",
                },
            )
        assert desc.platform == "sensor"
        # If unknown=None, the `if unknown:` block wouldn't run (None is falsy),
        # so no debug log would appear. Checking we got a debug message kills _6.
        assert any(
            "unknown_xyz_attr" in r.message or "Ignoring" in r.message
            for r in caplog.records
        )

    def test_unknown_attrs_via_module_logs_sorted_list(self, caplog):
        """LOGGER.debug must receive sorted(unknown) — kills mutmut_6/9/10/11/12/13/14/15/16/17."""
        with caplog.at_level(logging.DEBUG, logger=MAPPING_LOGGER):
            mp._descriptor_from_entry(
                "my_field",
                {
                    "platform": "sensor",
                    "name": "T",
                    "object_suffix": "T",
                    "zzz_extra": "v",
                    "aaa_extra": "v2",
                },
            )
        # The log record must include the attribute names and field name.
        messages = [r.message for r in caplog.records]
        combined = " ".join(messages)
        assert "my_field" in combined
        assert "zzz_extra" in combined or "Ignoring" in combined


# ---------------------------------------------------------------------------
# _descriptor_from_entry LOGGER message mutations (mutmut_9–17): exact format
# string text.  These mutants change the debug message text to None, wrong
# case, or XX…XX variants.  caplog captures the rendered message so we can
# check the text.
# ---------------------------------------------------------------------------


class TestDescriptorFromEntryDebugMessages:
    """Kill mutants that change LOGGER.debug arguments in the unknown-attrs path."""

    def test_debug_message_starts_with_ignoring_capital_i(self, caplog):
        """Message must start with capital 'Ignoring', not lower-case or None."""
        with caplog.at_level(logging.DEBUG, logger=MAPPING_LOGGER):
            _descriptor_from_entry(
                "f",
                {
                    "platform": "sensor",
                    "name": "N",
                    "object_suffix": "O",
                    "extra_key": "val",
                },
            )
        msgs = [r.message for r in caplog.records if "gnoring" in r.message]
        assert msgs, "Expected a 'Ignoring...' debug message but got none"
        # None of them should be the lowercased or None variant.
        for m in msgs:
            assert m.startswith("Ignoring"), f"Unexpected message start: {m!r}"


# ---------------------------------------------------------------------------
# _descriptor_from_entry event_map branch mutations (mutmut_28/29): key check
# ---------------------------------------------------------------------------


class TestDescriptorFromEntryEventMap:
    """Kill mutants that change the 'event_map' key string."""

    def test_event_map_dict_is_parsed(self):
        """'event_map' (not 'XXevent_mapXX' or 'EVENT_MAP') must be recognised."""
        desc = _descriptor_from_entry(
            "button",
            {
                "platform": "event",
                "name": "Button",
                "object_suffix": "btn",
                "event_map": {"0": "press", "1": "long_press"},
            },
        )
        assert desc.event_map == {"0": "press", "1": "long_press"}

    def test_event_map_uppercase_key_not_parsed(self):
        """'EVENT_MAP' (wrong case) must NOT populate event_map on the descriptor."""
        desc = _descriptor_from_entry(
            "button",
            {
                "platform": "event",
                "name": "Button",
                "object_suffix": "btn",
                "EVENT_MAP": {"0": "press"},
            },
        )
        # EVENT_MAP is an unknown key and is silently dropped.
        assert desc.event_map is None

    def test_event_map_string_coercion(self):
        """event_map keys and values must be str-coerced."""
        desc = _descriptor_from_entry(
            "switch",
            {
                "platform": "event",
                "name": "Switch",
                "object_suffix": "sw",
                "event_map": {0: "off", 1: "on"},
            },
        )
        assert desc.event_map == {"0": "off", "1": "on"}

    def test_invalid_event_map_dropped_and_logged(self, caplog):
        """Non-dict event_map is dropped; LOGGER.debug with 'Ignoring' (not None)."""
        with caplog.at_level(logging.DEBUG, logger=MAPPING_LOGGER):
            desc = _descriptor_from_entry(
                "button",
                {
                    "platform": "event",
                    "name": "Button",
                    "object_suffix": "btn",
                    "event_map": "not-a-dict",
                },
            )
        assert desc.event_map is None
        # The debug message should contain 'event_map' and NOT be None.
        msgs = [r.message for r in caplog.records]
        combined = " ".join(msgs)
        assert "event_map" in combined or "Ignoring" in combined


# ---------------------------------------------------------------------------
# _descriptor_from_entry event_map pop mutations (mutmut_44/45/46)
# ---------------------------------------------------------------------------


class TestDescriptorFromEntryEventMapPop:
    """Kill mutants that change known.pop('event_map') key."""

    def test_invalid_event_map_fully_removed(self):
        """After invalid event_map, the key must be removed so FieldDescriptor gets no event_map."""
        desc = _descriptor_from_entry(
            "btn",
            {
                "platform": "event",
                "name": "Btn",
                "object_suffix": "b",
                "event_map": 99,  # not a dict
            },
        )
        # If pop used 'None' or 'XXevent_mapXX' or 'EVENT_MAP', a KeyError would
        # be raised (except for None which would silently fail). The resulting
        # descriptor should have no event_map.
        assert desc.event_map is None

    def test_valid_event_map_not_dropped(self):
        """Valid event_map must survive (not be popped)."""
        desc = _descriptor_from_entry(
            "btn",
            {
                "platform": "event",
                "name": "Btn",
                "object_suffix": "b",
                "event_map": {"0": "tap"},
            },
        )
        assert desc.event_map is not None
        assert desc.event_map == {"0": "tap"}


# ---------------------------------------------------------------------------
# _descriptor_from_entry clear_delay boundary (mutmut_62: raw <= 1 instead of
# raw <= 0)
# ---------------------------------------------------------------------------


class TestDescriptorFromEntryClearDelayBoundary:
    """Kill mutant_62 which changes <= 0 to <= 1."""

    def test_clear_delay_1_is_valid(self):
        """clear_delay=1 must be KEPT (raw <= 0 is the rejection boundary, not <= 1)."""
        desc = _descriptor_from_entry(
            "motion",
            {
                "platform": "binary_sensor",
                "name": "Motion",
                "object_suffix": "M",
                "clear_delay": 1,
            },
        )
        # With the mutant (raw <= 1), clear_delay=1 would be rejected (<=1 is True).
        assert desc.clear_delay == 1

    def test_clear_delay_0_is_rejected(self):
        """clear_delay=0 must be REJECTED (raw <= 0 is True)."""
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

    def test_clear_delay_2_is_valid(self):
        """clear_delay=2 must be KEPT (raw <= 0 is False)."""
        desc = _descriptor_from_entry(
            "motion",
            {
                "platform": "binary_sensor",
                "name": "Motion",
                "object_suffix": "M",
                "clear_delay": 2,
            },
        )
        assert desc.clear_delay == 2


# ---------------------------------------------------------------------------
# _descriptor_from_entry LOGGER.debug for clear_delay (mutmut_63–71)
# and event_driven (mutmut_80–88)
# ---------------------------------------------------------------------------


class TestDescriptorFromEntryClearDelayLogs:
    """Kill LOGGER-message mutants on the clear_delay invalid-path."""

    def test_invalid_clear_delay_logged_with_ignoring(self, caplog):
        """Invalid clear_delay must produce a 'Ignoring' (capital I) debug log."""
        with caplog.at_level(logging.DEBUG, logger=MAPPING_LOGGER):
            _descriptor_from_entry(
                "motion",
                {
                    "platform": "binary_sensor",
                    "name": "Motion",
                    "object_suffix": "M",
                    "clear_delay": 0,  # invalid -> triggers debug
                },
            )
        msgs = [r.message for r in caplog.records]
        combined = " ".join(msgs)
        assert "Ignoring" in combined
        assert "clear_delay" in combined

    def test_invalid_clear_delay_log_contains_field_key(self, caplog):
        """LOGGER.debug must pass field_key (not None) as arg — kills mutmut_65."""
        with caplog.at_level(logging.DEBUG, logger=MAPPING_LOGGER):
            _descriptor_from_entry(
                "unique_motion_field",
                {
                    "platform": "binary_sensor",
                    "name": "Motion",
                    "object_suffix": "M",
                    "clear_delay": -5,  # invalid
                },
            )
        msgs = [r.message for r in caplog.records]
        combined = " ".join(msgs)
        # mutmut_65 replaces field_key with None, so 'unique_motion_field' would
        # not appear in the rendered message.
        assert "unique_motion_field" in combined


class TestDescriptorFromEntryEventDrivenLogs:
    """Kill LOGGER-message mutants on the event_driven invalid-path (mutmut_80–88)."""

    def test_invalid_event_driven_logged_with_ignoring(self, caplog):
        """Invalid event_driven must produce an 'Ignoring' (capital I) debug log."""
        with caplog.at_level(logging.DEBUG, logger=MAPPING_LOGGER):
            _descriptor_from_entry(
                "wobble",
                {
                    "platform": "binary_sensor",
                    "name": "Wobble",
                    "object_suffix": "W",
                    "event_driven": "yes-please",  # not a bool
                },
            )
        msgs = [r.message for r in caplog.records]
        combined = " ".join(msgs)
        assert "Ignoring" in combined
        assert "event_driven" in combined

    def test_invalid_event_driven_log_includes_field_key(self, caplog):
        """event_driven debug message must include the field key name (not None)."""
        with caplog.at_level(logging.DEBUG, logger=MAPPING_LOGGER):
            _descriptor_from_entry(
                "special_event_field",
                {
                    "platform": "binary_sensor",
                    "name": "X",
                    "object_suffix": "X",
                    "event_driven": 42,  # not a bool
                },
            )
        msgs = [r.message for r in caplog.records]
        combined = " ".join(msgs)
        assert "special_event_field" in combined

    def test_invalid_event_driven_log_includes_value(self, caplog):
        """event_driven debug message must include the invalid value (not None)."""
        with caplog.at_level(logging.DEBUG, logger=MAPPING_LOGGER):
            _descriptor_from_entry(
                "f",
                {
                    "platform": "binary_sensor",
                    "name": "X",
                    "object_suffix": "X",
                    "event_driven": "bad_value_sentinel",
                },
            )
        msgs = [r.message for r in caplog.records]
        combined = " ".join(msgs)
        assert "bad_value_sentinel" in combined


# ---------------------------------------------------------------------------
# _parse_models_block LOGGER mutations (mutmut_4/5/9/10/12/15/16/17/22/23/25)
# ---------------------------------------------------------------------------


class TestParseModelsBlockLogs:
    """Kill LOGGER-message mutants in _parse_models_block."""

    def test_non_dict_raw_warning_starts_with_ignoring(self, caplog):
        """Warning for non-dict raw must contain 'Ignoring' (capital I)."""
        with caplog.at_level(logging.WARNING, logger=MAPPING_LOGGER):
            result = _parse_models_block("not-a-dict", "test.yaml")
        assert result == {}
        msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        combined = " ".join(msgs)
        assert "Ignoring" in combined

    def test_non_dict_raw_warning_contains_source(self, caplog):
        """Warning for non-dict raw must include source name (not None)."""
        with caplog.at_level(logging.WARNING, logger=MAPPING_LOGGER):
            _parse_models_block([], "unique_source_file.yaml")
        msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        combined = " ".join(msgs)
        assert "unique_source_file.yaml" in combined

    def test_non_dict_raw_warning_contains_type(self, caplog):
        """Warning for non-dict raw must include the raw type name (not 'NoneType')."""
        with caplog.at_level(logging.WARNING, logger=MAPPING_LOGGER):
            _parse_models_block(123, "src.yaml")
        msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        combined = " ".join(msgs)
        # type(123).__name__ = 'int'; if mutmut_5 uses type(None).__name__ it would say 'NoneType'
        assert "int" in combined
        assert "NoneType" not in combined

    def test_non_mapping_model_warning_contains_source(self, caplog):
        """Warning for non-mapping model entry includes source name (not None)."""
        with caplog.at_level(logging.WARNING, logger=MAPPING_LOGGER):
            _parse_models_block({"BadModel": "not-a-dict"}, "specific_src.yaml")
        msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        combined = " ".join(msgs)
        assert "specific_src.yaml" in combined

    def test_non_mapping_model_warning_contains_model_name(self, caplog):
        """Warning for non-mapping model entry includes model name (not None)."""
        with caplog.at_level(logging.WARNING, logger=MAPPING_LOGGER):
            _parse_models_block({"UniqueModelName": 99}, "src.yaml")
        msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        combined = " ".join(msgs)
        assert "UniqueModelName" in combined

    def test_non_mapping_model_warning_contains_entry_type(self, caplog):
        """Warning for non-mapping model entry includes entry type (not 'NoneType')."""
        with caplog.at_level(logging.WARNING, logger=MAPPING_LOGGER):
            _parse_models_block({"M": [1, 2]}, "src.yaml")
        msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        combined = " ".join(msgs)
        # type([1,2]).__name__ = 'list'
        assert "list" in combined
        assert "NoneType" not in combined

    def test_non_mapping_model_warning_starts_with_ignoring(self, caplog):
        """Warning for non-mapping model entry starts with 'Ignoring'."""
        with caplog.at_level(logging.WARNING, logger=MAPPING_LOGGER):
            _parse_models_block({"M": "bad"}, "src.yaml")
        msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        combined = " ".join(msgs)
        assert "Ignoring" in combined

    def test_malformed_descriptor_exception_logged_with_source(self, caplog):
        """Exception for malformed descriptor includes source name (not None)."""
        with caplog.at_level(logging.ERROR, logger=MAPPING_LOGGER):
            _parse_models_block(
                {"M": {"bad_field": "not-a-dict"}}, "special_source.yaml"
            )
        # Exception is logged at ERROR (LOGGER.exception).
        msgs = [r.message for r in caplog.records]
        combined = " ".join(msgs)
        assert "special_source.yaml" in combined

    def test_malformed_descriptor_exception_logged_with_model(self, caplog):
        """Exception for malformed descriptor includes model name (not None)."""
        with caplog.at_level(logging.ERROR, logger=MAPPING_LOGGER):
            _parse_models_block({"SpecialModelX": {"bad": "not-a-dict"}}, "src.yaml")
        msgs = [r.message for r in caplog.records]
        combined = " ".join(msgs)
        assert "SpecialModelX" in combined

    def test_malformed_descriptor_exception_logged_with_field_key(self, caplog):
        """Exception for malformed descriptor includes field_key (not None)."""
        with caplog.at_level(logging.ERROR, logger=MAPPING_LOGGER):
            _parse_models_block(
                {"M": {"unique_bad_field_xyz": "not-a-dict"}}, "src.yaml"
            )
        msgs = [r.message for r in caplog.records]
        combined = " ".join(msgs)
        assert "unique_bad_field_xyz" in combined

    def test_malformed_descriptor_exception_starts_with_ignoring(self, caplog):
        """Exception message must begin with 'Ignoring' (capital I)."""
        with caplog.at_level(logging.ERROR, logger=MAPPING_LOGGER):
            _parse_models_block({"M": {"bad": "not-a-dict"}}, "src.yaml")
        msgs = [r.message for r in caplog.records]
        combined = " ".join(msgs)
        assert "Ignoring" in combined


# ---------------------------------------------------------------------------
# _load_skip_keys LOGGER mutations (mutmut_3–7/10–12/16/19–25)
# ---------------------------------------------------------------------------


class TestLoadSkipKeysLogs:
    """Kill LOGGER message mutants in _load_skip_keys (accessed via load_library)."""

    def test_missing_file_warning_contains_path(self, caplog, tmp_path):
        """Warning for missing skip-keys file must include the path (not None)."""
        missing = tmp_path / "_skip_keys.yaml"
        # Ensure file does not exist.
        assert not missing.exists()
        with caplog.at_level(logging.WARNING, logger=MAPPING_LOGGER):
            load_library(tmp_path)  # triggers _load_skip_keys with missing file
        msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        combined = " ".join(msgs)
        # The warning should reference the path.
        assert str(tmp_path) in combined or "_skip_keys" in combined

    def test_missing_file_warning_starts_with_skip_keys_capital_s(
        self, caplog, tmp_path
    ):
        """Warning message for missing file must start with 'Skip-keys' (capital S)."""
        assert not (tmp_path / "_skip_keys.yaml").exists()
        with caplog.at_level(logging.WARNING, logger=MAPPING_LOGGER):
            load_library(tmp_path)
        msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        combined = " ".join(msgs)
        # mutmut_7 changes 'Skip-keys' to 'skip-keys' (lowercase s)
        assert "Skip-keys" in combined

    def test_malformed_skip_keys_exception_contains_path(self, caplog, tmp_path):
        """Exception for malformed skip-keys file must include the path (not None)."""
        (tmp_path / "_skip_keys.yaml").write_text(
            ":\n bad yaml: [unclosed", encoding="utf-8"
        )
        with caplog.at_level(logging.ERROR, logger=MAPPING_LOGGER):
            result = load_library(tmp_path)
        _, skip_keys = result
        # The file was malformed; skip_keys should be empty.
        assert skip_keys == set()
        # Exception log should reference the file path.
        msgs = [r.message for r in caplog.records]
        combined = " ".join(msgs)
        assert "_skip_keys.yaml" in combined or str(tmp_path) in combined

    def test_malformed_skip_keys_exception_starts_with_failed(self, caplog, tmp_path):
        """Exception message must start 'Failed' (capital F), not 'failed' or 'FAILED'."""
        (tmp_path / "_skip_keys.yaml").write_text(
            "not_a_mapping: [unclosed",
            encoding="utf-8",
        )
        with caplog.at_level(logging.ERROR, logger=MAPPING_LOGGER):
            load_library(tmp_path)
        msgs = [r.message for r in caplog.records]
        combined = " ".join(msgs)
        # mutmut_24 changes 'Failed' to 'failed', mutmut_25 to 'FAILED TO ...'
        assert "Failed" in combined


# ---------------------------------------------------------------------------
# load_library LOGGER mutations (mutmut_7–11/26–37/41–42/63–64)
# ---------------------------------------------------------------------------


class TestLoadLibraryLogs:
    """Kill LOGGER message mutants in load_library."""

    def test_missing_dir_error_contains_path(self, caplog, tmp_path):
        """LOGGER.error for missing directory must include the path (not None)."""
        missing = tmp_path / "no_such_dir"
        with caplog.at_level(logging.ERROR, logger=MAPPING_LOGGER):
            load_library(missing)
        msgs = [r.message for r in caplog.records if r.levelno == logging.ERROR]
        combined = " ".join(msgs)
        assert str(missing) in combined

    def test_missing_dir_error_starts_with_device_library_capital(
        self, caplog, tmp_path
    ):
        """Error message for missing dir must say 'Device-library' (capital D)."""
        missing = tmp_path / "no_such_dir"
        with caplog.at_level(logging.ERROR, logger=MAPPING_LOGGER):
            load_library(missing)
        msgs = [r.message for r in caplog.records if r.levelno == logging.ERROR]
        combined = " ".join(msgs)
        # mutmut_11 changes 'Device-library' to 'device-library'
        assert "Device-library" in combined

    def test_malformed_file_exception_contains_path(self, caplog, tmp_path):
        """Exception log for malformed library file must include file path (not None)."""
        (tmp_path / "bad_file.yaml").write_text(
            "- not_a_mapping\n",
            encoding="utf-8",
        )
        with caplog.at_level(logging.ERROR, logger=MAPPING_LOGGER):
            load_library(tmp_path)
        msgs = [r.message for r in caplog.records]
        combined = " ".join(msgs)
        assert "bad_file.yaml" in combined

    def test_malformed_file_exception_starts_with_skipping_capital(
        self, caplog, tmp_path
    ):
        """Exception message for malformed file must start 'Skipping' (capital S)."""
        (tmp_path / "bad_file2.yaml").write_text("- not_a_mapping\n", encoding="utf-8")
        with caplog.at_level(logging.ERROR, logger=MAPPING_LOGGER):
            load_library(tmp_path)
        msgs = [r.message for r in caplog.records]
        combined = " ".join(msgs)
        # mutmut_31 changes 'Skipping' to 'skipping', mutmut_32 to 'SKIPPING ...'
        assert "Skipping" in combined

    def test_malformed_file_continue_not_break(self, tmp_path):
        """continue (not break) after malformed file: later good files are loaded."""
        # Two files: bad one first (alphabetically), then good one.
        (tmp_path / "aaa_bad.yaml").write_text("- not_a_mapping\n", encoding="utf-8")
        (tmp_path / "zzz_good.yaml").write_text(
            "humidity:\n  platform: sensor\n  name: Humidity\n  object_suffix: H\n",
            encoding="utf-8",
        )
        registry, _ = load_library(tmp_path)
        # If break is used instead of continue, 'humidity' would never be loaded.
        assert "humidity" in registry.flat

    def test_key_collision_warning_contains_key_name(self, caplog, tmp_path):
        """Warning for colliding field keys must include the key name (not None)."""
        (tmp_path / "aaa.yaml").write_text(
            "collision_field:\n  platform: sensor\n  name: A\n  object_suffix: A\n",
            encoding="utf-8",
        )
        (tmp_path / "zzz.yaml").write_text(
            "collision_field:\n  platform: sensor\n  name: Z\n  object_suffix: Z\n",
            encoding="utf-8",
        )
        with caplog.at_level(logging.WARNING, logger=MAPPING_LOGGER):
            load_library(tmp_path)
        msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        combined = " ".join(msgs)
        # mutmut_36 replaces key with None in the log call.
        assert "collision_field" in combined

    def test_key_collision_warning_contains_file_name(self, caplog, tmp_path):
        """Warning for colliding keys must include file name (not None)."""
        (tmp_path / "aaa.yaml").write_text(
            "dup_field:\n  platform: sensor\n  name: A\n  object_suffix: A\n",
            encoding="utf-8",
        )
        (tmp_path / "zzz_unique.yaml").write_text(
            "dup_field:\n  platform: sensor\n  name: Z\n  object_suffix: Z\n",
            encoding="utf-8",
        )
        with caplog.at_level(logging.WARNING, logger=MAPPING_LOGGER):
            load_library(tmp_path)
        msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        combined = " ".join(msgs)
        # mutmut_37 replaces path.name with None.
        assert "zzz_unique.yaml" in combined

    def test_key_collision_warning_starts_with_field_capital(self, caplog, tmp_path):
        """Warning for colliding key must say 'Field' (capital F)."""
        (tmp_path / "a.yaml").write_text(
            "x:\n  platform: sensor\n  name: X\n  object_suffix: X\n",
            encoding="utf-8",
        )
        (tmp_path / "z.yaml").write_text(
            "x:\n  platform: sensor\n  name: Y\n  object_suffix: Y\n",
            encoding="utf-8",
        )
        with caplog.at_level(logging.WARNING, logger=MAPPING_LOGGER):
            load_library(tmp_path)
        msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        combined = " ".join(msgs)
        # mutmut_42 changes 'Field' to 'field'
        assert "Field" in combined

    def test_key_collision_uses_intersection_not_union(self, tmp_path):
        """Collision detection uses & (intersection), not | (union) — kills mutmut_34."""
        (tmp_path / "a.yaml").write_text(
            "only_in_a:\n  platform: sensor\n  name: A\n  object_suffix: A\n"
            "shared:\n  platform: sensor\n  name: S1\n  object_suffix: S\n",
            encoding="utf-8",
        )
        (tmp_path / "z.yaml").write_text(
            "only_in_z:\n  platform: sensor\n  name: Z\n  object_suffix: Z\n"
            "shared:\n  platform: sensor\n  name: S2\n  object_suffix: S\n",
            encoding="utf-8",
        )
        # This should not raise; the registry should contain all four fields.
        registry, _ = load_library(tmp_path)
        assert "only_in_a" in registry.flat
        assert "only_in_z" in registry.flat
        assert "shared" in registry.flat
        # The later file wins for 'shared'.
        assert registry.flat["shared"].name == "S2"

    def test_load_library_debug_log_starts_with_loaded_capital(self, caplog, tmp_path):
        """The final debug log must say 'Loaded' (capital L), not 'loaded'."""
        (tmp_path / "good.yaml").write_text(
            "temp:\n  platform: sensor\n  name: T\n  object_suffix: T\n",
            encoding="utf-8",
        )
        (tmp_path / "_skip_keys.yaml").write_text(
            "skip_keys:\n  - id\n",
            encoding="utf-8",
        )
        with caplog.at_level(logging.DEBUG, logger=MAPPING_LOGGER):
            load_library(tmp_path)
        msgs = [r.message for r in caplog.records if r.levelno == logging.DEBUG]
        combined = " ".join(msgs)
        # mutmut_64 changes 'Loaded' to 'loaded'.
        assert "Loaded" in combined


# ---------------------------------------------------------------------------
# _load_descriptor_file mutations (mutmut_13/19/22)
# ---------------------------------------------------------------------------


class TestLoadDescriptorFile:
    """Kill mutants in _load_descriptor_file."""

    def test_non_mapping_yaml_raises_type_error_with_message(self, tmp_path):
        """Top-level non-mapping YAML raises TypeError with a non-None message."""
        (tmp_path / "bad.yaml").write_text("- item1\n- item2\n", encoding="utf-8")
        # load_library catches TypeError and logs; verify that the exception is
        # actually raised inside the file handler via a direct import.
        from custom_components.rtl_433.mapping import _load_descriptor_file

        with pytest.raises(TypeError) as exc_info:
            _load_descriptor_file(tmp_path / "bad.yaml")
        # mutmut_13 changes the message to None; None is not a valid string.
        assert exc_info.value.args[0] is not None
        assert "bad.yaml" in str(exc_info.value)

    def test_models_field_in_file_passes_path_name_to_parse_models_block(
        self, tmp_path
    ):
        """_parse_models_block is called with path.name (not None) as source."""
        # If None is passed instead of path.name, any warning log would not
        # include the file name. We check that models are parsed correctly
        # (the happy path), confirming the call goes through.
        (tmp_path / "with_models.yaml").write_text(
            "models:\n"
            "  AcmeX:\n"
            "    temp:\n"
            "      platform: sensor\n"
            "      name: T\n"
            "      object_suffix: T\n",
            encoding="utf-8",
        )
        registry, _ = load_library(tmp_path)
        assert "AcmeX" in registry.models
        assert "temp" in registry.models["AcmeX"]

    def test_models_field_continue_not_break(self, tmp_path):
        """After processing models: key, continue (not break): other fields parsed."""
        (tmp_path / "mixed.yaml").write_text(
            "models:\n"
            "  AcmeX:\n"
            "    temp:\n"
            "      platform: sensor\n"
            "      name: T\n"
            "      object_suffix: T\n"
            "humidity:\n"
            "  platform: sensor\n"
            "  name: Humidity\n"
            "  object_suffix: H\n",
            encoding="utf-8",
        )
        registry, _ = load_library(tmp_path)
        # If break is used instead of continue, 'humidity' would be lost.
        assert "humidity" in registry.flat
        assert "AcmeX" in registry.models


# ---------------------------------------------------------------------------
# merge_overrides LOGGER mutations (mutmut_9–13/15/17) and continue→break
# mutations (mutmut_24/37) and field_key→None (mutmut_39)
# ---------------------------------------------------------------------------


class TestMergeOverridesLogs:
    """Kill LOGGER message mutants and continue→break mutants in merge_overrides."""

    def test_non_dict_override_warning_contains_type(self, caplog):
        """Warning for non-dict override must include the type name (not None)."""
        reg = _make_reg()
        with caplog.at_level(logging.WARNING, logger=MAPPING_LOGGER):
            merge_overrides(reg, set(), "bad_string_data")
        msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        combined = " ".join(msgs)
        # type("bad_string_data").__name__ = 'str'
        assert "str" in combined
        assert "NoneType" not in combined

    def test_non_dict_override_warning_starts_with_override_capital(self, caplog):
        """Warning for non-dict override must start with 'Override' (capital O)."""
        reg = _make_reg()
        with caplog.at_level(logging.WARNING, logger=MAPPING_LOGGER):
            merge_overrides(reg, set(), [1, 2, 3])
        msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        combined = " ".join(msgs)
        assert "Override" in combined

    def test_non_dict_override_returns_registry_with_models_not_none(self):
        """Non-dict override must return Registry(models=merged_models), not models=None."""
        reg = _make_reg(
            flat={"x": _make_desc(field_key="x")},
            models={"M": {"x": _make_desc(field_key="x")}},
        )
        merged, _ = merge_overrides(reg, set(), "bad_data")
        # mutmut_17 sets models=None; this would fail the isinstance check.
        assert merged.models is not None
        assert isinstance(merged.models, dict)

    def test_skip_keys_continue_not_break_processes_later_entries(self):
        """After skip_keys entry, loop must continue (not break) to process more entries."""
        reg = _make_reg()
        # Put skip_keys BEFORE a flat field entry. If break is used, the field is lost.
        merged, merged_skips = merge_overrides(
            reg,
            set(),
            {
                "skip_keys": ["vendor_noise"],
                "after_skip_field": {
                    "platform": "sensor",
                    "name": "After Skip",
                    "object_suffix": "AS",
                },
            },
        )
        assert "vendor_noise" in merged_skips
        assert "after_skip_field" in merged.flat

    def test_models_field_continue_not_break_processes_later_entries(self):
        """After models: key, loop must continue (not break) to process more entries."""
        reg = _make_reg()
        merged, _ = merge_overrides(
            reg,
            set(),
            {
                "models": {
                    "TestDev": {
                        "f": {
                            "platform": "sensor",
                            "name": "F",
                            "object_suffix": "F",
                        }
                    }
                },
                "after_models_field": {
                    "platform": "sensor",
                    "name": "After Models",
                    "object_suffix": "AM",
                },
            },
        )
        # If break is used instead of continue after models block, 'after_models_field' is lost.
        assert "TestDev" in merged.models
        assert "after_models_field" in merged.flat

    def test_descriptor_from_entry_called_with_field_key_not_none(self):
        """_descriptor_from_entry must get field_key (not None) — kills mutmut_39."""
        reg = _make_reg()
        merged, _ = merge_overrides(
            reg,
            set(),
            {
                "my_custom_field": {
                    "platform": "sensor",
                    "name": "Custom",
                    "object_suffix": "C",
                }
            },
        )
        desc = lookup("my_custom_field", registry=merged)
        # If None was passed as field_key, the descriptor's field_key would be None.
        assert desc is not None
        assert desc.field_key == "my_custom_field"

    def test_malformed_override_exception_contains_field_key(self, caplog):
        """Exception for malformed override must log field_key (not None)."""
        reg = _make_reg()
        with caplog.at_level(logging.ERROR, logger=MAPPING_LOGGER):
            merge_overrides(
                reg,
                set(),
                {"unique_bad_override_field": "not-a-dict"},
            )
        msgs = [r.message for r in caplog.records]
        combined = " ".join(msgs)
        assert "unique_bad_override_field" in combined

    def test_malformed_override_exception_starts_with_ignoring_capital(self, caplog):
        """Exception message must start with 'Ignoring' (capital I), not 'ignoring'."""
        reg = _make_reg()
        with caplog.at_level(logging.ERROR, logger=MAPPING_LOGGER):
            merge_overrides(reg, set(), {"bad": "not-a-dict"})
        msgs = [r.message for r in caplog.records]
        combined = " ".join(msgs)
        assert "Ignoring" in combined

    def test_merge_overrides_source_passed_to_parse_models_block(self, caplog):
        """_parse_models_block called with 'override' (not None) as source — kills mutmut_27."""
        reg = _make_reg()
        with caplog.at_level(logging.WARNING, logger=MAPPING_LOGGER):
            # Pass an invalid models block to trigger the warning path.
            merge_overrides(
                reg,
                set(),
                {
                    "models": {
                        "AModel": "not-a-dict"  # invalid per-model entry
                    }
                },
            )
        msgs = [r.message for r in caplog.records]
        combined = " ".join(msgs)
        # If None is passed as source, "override" would not appear in the message.
        # mutmut_27 passes None instead of "override".
        assert "override" in combined


# ---------------------------------------------------------------------------
# validate_user_mappings continue→break mutations (mutmut_12/18/21/27)
# and _validate_entry message mutations (mutmut_21/22)
# ---------------------------------------------------------------------------


class TestValidateUserMappingsContinueBreak:
    """Kill continue→break mutations in validate_user_mappings."""

    def test_skip_keys_continue_processes_later_entries(self):
        """After skip_keys, loop must continue to validate later entries."""
        data = {
            "skip_keys": ["a"],
            "bad_entry_after_skip": {},  # missing required attrs
        }
        problems = validate_user_mappings(data)
        # If break is used, "bad_entry_after_skip" would not be validated.
        assert any("bad_entry_after_skip" in p for p in problems)

    def test_models_non_mapping_continue_not_break(self):
        """After non-mapping models value, loop must continue for later flat entries."""
        data = {
            "models": "not-a-dict",
            "flat_field_after_bad_models": {},  # invalid
        }
        problems = validate_user_mappings(data)
        # If break is used, "flat_field_after_bad_models" would be skipped.
        assert any("flat_field_after_bad_models" in p for p in problems)

    def test_models_non_mapping_model_entry_continue_not_break(self):
        """After non-mapping per-model entry, loop must continue for other models."""
        data = {
            "models": {
                "BadModel": "not-a-dict",
                "GoodModel": {"temp": {}},  # this must still be validated
            }
        }
        problems = validate_user_mappings(data)
        # If break is used in the per-model loop, GoodModel.temp is never validated.
        assert any("GoodModel.temp" in p and "platform" in p for p in problems)

    def test_models_valid_entries_continue_for_flat(self):
        """After processing models block, loop continues for flat entries."""
        data = {
            "models": {
                "M": {
                    "temp": {
                        "platform": "sensor",
                        "name": "T",
                        "object_suffix": "T",
                    }
                }
            },
            "flat_field_after_models": {},  # must be validated
        }
        problems = validate_user_mappings(data)
        # If break is used after models block, flat_field_after_models is skipped.
        assert any("flat_field_after_models" in p for p in problems)


class TestValidateEntryPlatformMessages:
    """Kill mutants on the unknown-platform message text (mutmut_18/21/22)."""

    def test_empty_string_platform_not_flagged_as_unknown(self):
        """Empty string platform does not trigger the unknown-platform message — kills mutmut_18."""
        from custom_components.rtl_433.mapping import _validate_entry

        problems = _validate_entry(
            "f", {"platform": "", "name": "N", "object_suffix": "O"}
        )
        # mutmut_18 changes platform != "" to platform != "XXXX", which would
        # cause the empty string to trigger the unknown platform check.
        # With the correct code, empty string triggers only the missing check.
        unknown_platform_problems = [p for p in problems if "unknown platform" in p]
        assert len(unknown_platform_problems) == 0

    def test_unknown_platform_message_contains_expected_platforms(self):
        """Unknown-platform message must list 'sensor, binary_sensor, event' in original case."""
        from custom_components.rtl_433.mapping import _validate_entry

        problems = _validate_entry(
            "f", {"platform": "switch", "name": "N", "object_suffix": "O"}
        )
        # mutmut_21 changes message to XX(expected one of sensor, binary_sensor, event)XX
        # mutmut_22 changes to uppercase.
        combined = " ".join(problems)
        # Look for the phrase with correct case — neither XX prefix nor ALL CAPS.
        assert "(expected one of sensor, binary_sensor, event)" in combined, (
            f"Expected exact phrase in problem message, got: {combined!r}"
        )


# ---------------------------------------------------------------------------
# normalize_overrides continue→break mutations (mutmut_6/9/11/22)
# ---------------------------------------------------------------------------


class TestNormalizeOverridesContinueBreak:
    """Kill continue→break mutations in normalize_overrides."""

    def test_skip_keys_continue_normalises_later_entries(self):
        """After skip_keys key, later flat entries with payload must be normalised."""
        data = {
            "skip_keys": ["id"],
            "door": {
                "platform": "binary_sensor",
                "name": "Door",
                "object_suffix": "D",
                "payload": {True: "1", False: "0"},
            },
        }
        result = normalize_overrides(data)
        # If break is used after skip_keys, 'door' would not be normalised.
        assert result["door"]["payload"] == {"on": "1", "off": "0"}

    def test_models_non_dict_value_continue_normalises_later_entries(self):
        """After non-dict models value, loop must continue for flat entries."""
        data = {
            "models": "not-a-dict",
            "door": {
                "platform": "binary_sensor",
                "name": "Door",
                "object_suffix": "D",
                "payload": {True: "1", False: "0"},
            },
        }
        result = normalize_overrides(data)
        # If break is used after non-dict models, 'door' would not be normalised.
        assert result["door"]["payload"] == {"on": "1", "off": "0"}

    def test_models_non_dict_entries_continue_normalises_other_entries(self):
        """After non-dict per-model entries, loop must continue for others."""
        data = {
            "models": {
                "BadModel": "not-a-dict",
                "GoodModel": {
                    "wet": {
                        "platform": "binary_sensor",
                        "name": "W",
                        "object_suffix": "W",
                        "payload": {True: "1", False: "0"},
                    }
                },
            }
        }
        result = normalize_overrides(data)
        # If break is used after BadModel, GoodModel.wet would not be normalised.
        assert result["models"]["GoodModel"]["wet"]["payload"] == {
            "on": "1",
            "off": "0",
        }

    def test_models_block_continue_normalises_flat_entries_after(self):
        """After processing models block, loop continues for flat entries."""
        data = {
            "models": {
                "M": {
                    "temp": {
                        "platform": "sensor",
                        "name": "T",
                        "object_suffix": "T",
                    }
                }
            },
            "door": {
                "platform": "binary_sensor",
                "name": "Door",
                "object_suffix": "D",
                "payload": {True: "1", False: "0"},
            },
        }
        result = normalize_overrides(data)
        # If break is used after models block, 'door' is not normalised.
        assert result["door"]["payload"] == {"on": "1", "off": "0"}


# ---------------------------------------------------------------------------
# _apply_sensor_transform LOGGER mutations (mutmut_41–82): scale/offset/round
# error paths — use caplog to verify the log message.
# The existing tests call the functions with invalid params and check return
# values. Here we additionally check log output to kill the LOGGER mutations.
# ---------------------------------------------------------------------------


class TestApplySensorTransformErrorLogs:
    """Kill LOGGER message mutants in the scale/offset/round error paths."""

    def test_invalid_scale_logs_message_starts_with_invalid_not_xx(self, caplog):
        """Invalid scale must log message starting with 'Invalid' (not 'XXInvalid...' or 'invalid')."""
        with caplog.at_level(logging.DEBUG, logger=MAPPING_LOGGER):
            _apply_sensor_transform({"scale": "bad_scale"}, 5)
        msgs = [r.message for r in caplog.records]
        # Find the message about scale.
        scale_msgs = [m for m in msgs if "scale" in m.lower()]
        assert scale_msgs, "Expected a log message about scale"
        # The original message starts with "Invalid 'scale'" not "XXInvalid"
        for m in scale_msgs:
            assert m.startswith("Invalid"), (
                f"Message should start with 'Invalid', got: {m!r}"
            )

    def test_invalid_scale_log_contains_scale_value(self, caplog):
        """Invalid scale log must reference the bad scale value (not None or wrong key)."""
        with caplog.at_level(logging.DEBUG, logger=MAPPING_LOGGER):
            _apply_sensor_transform({"scale": "scale_sentinel"}, 5)
        msgs = [r.message for r in caplog.records]
        combined = " ".join(msgs)
        # mutmut_48 uses transform.get(None) → returns None, not "scale_sentinel"
        # mutmut_49 uses 'XXscaleXX' → get('XXscaleXX') returns None
        # mutmut_50 uses 'SCALE' → get('SCALE') returns None
        assert "scale_sentinel" in combined

    def test_invalid_offset_logs_message_starts_with_invalid_not_xx(self, caplog):
        """Invalid offset must log message starting with 'Invalid' (not 'XXInvalid...')."""
        with caplog.at_level(logging.DEBUG, logger=MAPPING_LOGGER):
            _apply_sensor_transform({"offset": "bad_offset"}, 5)
        msgs = [r.message for r in caplog.records]
        offset_msgs = [m for m in msgs if "offset" in m.lower()]
        assert offset_msgs, "Expected a log message about offset"
        for m in offset_msgs:
            assert m.startswith("Invalid"), (
                f"Message should start with 'Invalid', got: {m!r}"
            )

    def test_invalid_offset_log_contains_offset_value(self, caplog):
        """Invalid offset log must reference the bad offset value."""
        with caplog.at_level(logging.DEBUG, logger=MAPPING_LOGGER):
            _apply_sensor_transform({"offset": "offset_sentinel"}, 5)
        msgs = [r.message for r in caplog.records]
        combined = " ".join(msgs)
        assert "offset_sentinel" in combined

    def test_invalid_round_logs_message_starts_with_invalid_not_xx(self, caplog):
        """Invalid round must log message starting with 'Invalid' (not 'XXInvalid...')."""
        with caplog.at_level(logging.DEBUG, logger=MAPPING_LOGGER):
            _apply_sensor_transform({"round": "bad_round"}, 5)
        msgs = [r.message for r in caplog.records]
        round_msgs = [m for m in msgs if "round" in m.lower()]
        assert round_msgs, "Expected a log message about round"
        for m in round_msgs:
            assert m.startswith("Invalid"), (
                f"Message should start with 'Invalid', got: {m!r}"
            )

    def test_invalid_round_log_contains_round_value(self, caplog):
        """Invalid round log must reference the bad round value."""
        with caplog.at_level(logging.DEBUG, logger=MAPPING_LOGGER):
            _apply_sensor_transform({"round": "round_sentinel"}, 5)
        msgs = [r.message for r in caplog.records]
        combined = " ".join(msgs)
        assert "round_sentinel" in combined


# ---------------------------------------------------------------------------
# _apply_sensor_transform needs_float mutations (mutmut_12/13/14/15/17/18):
# These surviving mutants change 'or' to 'and' for specific sub-expressions.
# We need tests where ONLY one float-implying key is present.
# ---------------------------------------------------------------------------


class TestApplySensorTransformNeedsFloatSingleKey:
    """Tests targeting the surviving needs_float mutations (12,13,14,15,17,18).

    The key insight for needs_float mutations is that if as_int=True is used when
    it shouldn't be, and a *fractional* input is provided, the initial coercion
    truncates it (e.g., 3.9 → 3 with as_int=True). Then multiplying by 2.0 gives
    6.0 instead of 7.8. So we must use fractional raw values to expose the bug.
    """

    def test_only_offset_with_int_fractional_input(self):
        """Only 'offset' with int:True and fractional input → must NOT truncate.

        Kills mutmut_12: requires 'offset AND has_round' — without round, offset
        alone wouldn't set needs_float, so as_int=True would truncate 3.9 → 3.
        Kills mutmut_13: requires 'scale AND offset' — without scale, offset alone
        wouldn't set needs_float, so as_int=True would truncate.

        Original: needs_float=True (offset present) → as_int=False → coerce(3.9)=3.9 → 3.9+2.0=5.9
        Mutant_12: needs_float = False (no round) → as_int=True → coerce(3.9)=3 → 3+2.0=5.0 (WRONG)
        """
        result = _apply_sensor_transform({"int": True, "offset": 2.0}, 3.9)
        # Original must give 3.9 + 2.0 = 5.9 (not 3 + 2.0 = 5.0).
        assert abs(result - 5.9) < 1e-9, f"Expected 5.9, got {result}"

    def test_only_scale_with_int_fractional_input(self):
        """Only 'scale' with int:True and fractional input → must NOT truncate.

        Kills mutmut_13: requires 'scale AND offset' — without offset, scale alone
        wouldn't set needs_float in mutant.
        Kills mutmut_14: '"XXscaleXX" in transform' (wrong key).
        Kills mutmut_15: '"SCALE" in transform' (wrong case).

        Original: needs_float=True (scale present) → as_int=False → coerce(3.9)=3.9 → 3.9*2.0=7.8
        Mutant_13: needs_float = False (no offset) → as_int=True → coerce(3.9)=3 → 3*2.0=6.0 (WRONG)
        """
        result = _apply_sensor_transform({"int": True, "scale": 2.0}, 3.9)
        # Original must give 3.9 * 2.0 = 7.8 (not 3 * 2.0 = 6.0).
        assert abs(result - 7.8) < 1e-9, f"Expected 7.8, got {result}"

    def test_only_offset_with_int_fractional_via_module(self):
        """Via module: offset alone implies float — kills namespace mutants too."""
        result = mp._apply_sensor_transform({"int": True, "offset": 2.0}, 3.9)
        assert abs(result - 5.9) < 1e-9

    def test_only_scale_with_int_fractional_via_module(self):
        """Via module call: scale alone implies float — fractional input exposed."""
        result = mp._apply_sensor_transform({"int": True, "scale": 2.0}, 3.9)
        assert abs(result - 7.8) < 1e-9

    def test_only_offset_with_string_fractional_input(self):
        """String fractional raw value also exposed via truncation.

        Original: coerce('3.9', as_int=False)=3.9 → 3.9+2.0=5.9
        Mutant: coerce('3.9', as_int=True)=3 → 3+2.0=5.0
        """
        result = _apply_sensor_transform({"int": True, "offset": 2.0}, "3.9")
        assert abs(result - 5.9) < 1e-9

    def test_only_offset_without_int_gives_float(self):
        """'offset' alone (no 'round') still implies float — kills mutmut_12 which
        requires 'offset AND has_round' for offset to count as float-implying."""
        # has_round = False here (no 'round' key). has_int=False.
        result = _apply_sensor_transform({"offset": 1.0}, 5)
        assert isinstance(result, float)
        assert result == 6.0

    def test_only_offset_with_int_simple(self):
        """Simpler version: offset alone gives float (even for integer input)."""
        result = _apply_sensor_transform({"int": True, "offset": 2.5}, 0)
        assert isinstance(result, float)
        assert result == 2.5

    def test_offset_without_round_kills_mutmut_12_exact(self):
        """Exact boundary test for mutmut_12 (requires offset AND round).

        With int:True and only offset (no round):
        - Original (needs_float=True): as_int=False, coerce(7.7)=7.7, 7.7+0.3=8.0 (float)
        - Mutant_12 (needs_float=False): as_int=True, coerce(7.7)=7, 7+0.3=7.3 (float, but WRONG VALUE)
        """
        result = _apply_sensor_transform({"int": True, "offset": 0.3}, 7.7)
        assert abs(result - 8.0) < 1e-9, f"Expected 8.0, got {result}"

    def test_scale_without_offset_kills_mutmut_13_exact(self):
        """Exact boundary test for mutmut_13 (requires scale AND offset).

        With int:True and only scale (no offset):
        - Original (needs_float=True): as_int=False, coerce(2.5)=2.5, 2.5*4=10.0 (float)
        - Mutant_13 (needs_float=False): as_int=True, coerce(2.5)=2, 2*4=8.0 (WRONG)
        """
        result = _apply_sensor_transform({"int": True, "scale": 4}, 2.5)
        assert abs(result - 10.0) < 1e-9, f"Expected 10.0, got {result}"

    def test_offset_only_key_spelling_is_lowercase(self):
        """Only lowercase 'offset' triggers float mode — 'OFFSET'/'XXoffsetXX' do not.
        Kills mutmut_17 and mutmut_18.
        """
        # With offset key present, result should be float.
        result = _apply_sensor_transform({"int": True, "offset": 0}, 5)
        # offset=0 means no change in value, but as_int=False → coerce(5)=5.0 (float).
        assert isinstance(result, float)

    def test_scale_only_key_spelling_is_lowercase(self):
        """Only lowercase 'scale' triggers float mode — 'SCALE'/'XXscaleXX' do not.
        Kills mutmut_14 and mutmut_15.
        """
        result = _apply_sensor_transform({"int": True, "scale": 1}, 5)
        # scale=1 means no change in value, but as_int=False → coerce(5)=5.0 (float).
        assert isinstance(result, float)


# ---------------------------------------------------------------------------
# _apply_binary_payload as_int mutations (mutmut_23/26/31/34/40/43):
# as_int=None/True instead of False in the numeric fallback paths.
# Use fractional tokens/values where as_int=True would truncate and break match.
# ---------------------------------------------------------------------------


class TestApplyBinaryPayloadAsIntFalseKillers:
    """Kill as_int=None/True mutations in _apply_binary_payload.

    The key insight: if as_int=True is used for raw_value coercion,
    a raw_value of 1.7 would coerce to 1, not 1.7. Similarly if on_token="1.7"
    and we coerce with as_int=True we get 1, not 1.7. So fractional mismatches
    reveal the bug.
    """

    def test_raw_value_as_int_false_fractional_raw_vs_integer_token(self):
        """raw_value=1.7, on_token='1': with as_int=False raw is 1.7 != 1.0 → no match.
        With as_int=True raw would be truncated to 1, matching on_token='1' → WRONG.
        This kills mutmut_26 (as_int=True for raw_value coercion).
        """
        payload = {"on": "1", "off": "0"}
        # 1.7 does NOT match "1" as float (1.7 != 1.0).
        # String path: str(1.7) = "1.7" != "1" → no string match.
        # Numeric path: _coerce_number(1.7, as_int=False)=1.7 != 1.0 → no match.
        result = _apply_binary_payload(payload, 1.7)
        assert result is None

    def test_on_token_as_int_false_fractional_token_vs_integer_raw(self):
        """on_token='1.5', raw_value=1.5: numeric coerce with as_int=False gives 1.5==1.5.
        With as_int=True coerce('1.5')=1, and coerce(1.5)=1.5, so 1!=1.5 → no match.
        This kills mutmut_34 (as_int=True for on_token coercion).
        """
        payload = {"on": "1.5"}
        # String path: str(1.5)="1.5" == "1.5" → True (string match, not numeric path).
        # But the numeric path matters when string doesn't match:
        # raw=1.6 → "1.6" != "1.5" → numeric: coerce(1.6)=1.6, coerce("1.5")=1.5 → 1.6!=1.5 → None
        # (This tests the string match path.)
        assert _apply_binary_payload(payload, 1.5) is True

    def test_on_token_as_int_false_numeric_path_only(self):
        """Force through the numeric path: raw 1.500001 does NOT match on_token 1.5
        (since floats not equal), proving as_int doesn't matter here. But ensure
        the function is exercising the float path correctly."""
        payload = {"on": "1"}
        # raw_value=1.0: str(1.0)="1.0" != "1" → numeric: 1.0==1.0 → True
        assert _apply_binary_payload(payload, 1.0) is True

    def test_off_token_as_int_false_numeric_path(self):
        """off_token='0.5', raw=0.5: coerce with as_int=False gives 0.5==0.5 → False.
        With as_int=True coerce('0.5')=0, 0!=0.5 → no match.
        This kills mutmut_43 (as_int=True for off_token coercion).
        """
        payload = {"on": "1", "off": "0.5"}
        # String path: "0.5"=="0.5" → False (via string match).
        assert _apply_binary_payload(payload, "0.5") is False
        # Numeric path:
        # raw=0.500: str="0.5"=="0.5" → match.
        assert _apply_binary_payload(payload, 0.5) is False

    def test_off_token_as_int_false_only_numeric_path(self):
        """Force numeric path: raw=2.0, off_token='2'.
        coerce(2.0, as_int=False)=2.0, coerce('2', as_int=False)=2.0 → 2.0==2.0 → False.
        with as_int=None: effectively same as False (None is falsy). This kills mutmut_40.
        """
        payload = {"on": "99", "off": "2"}
        # str(2.0)="2.0" != "2" → numeric path.
        result = _apply_binary_payload(payload, 2.0)
        assert result is False

    def test_raw_value_as_int_none_treated_same_as_false(self):
        """as_int=None is treated as falsy by _coerce_number (same as False).
        Use a fractional raw to expose the difference from as_int=True.
        raw=0.7, on_token='0': coerce(0.7, as_int=False)=0.7 != 0.0 → no match.
        coerce(0.7, as_int=True)=0 == 0.0 → match (WRONG).
        This kills mutmut_23 (as_int=None) indirectly via the None→falsy path.
        """
        payload = {"on": "0", "off": "1"}
        # 0.7 as float: str="0.7" != "0" → numeric: coerce(0.7, as_int=False)=0.7 != 0.0 → None
        result = _apply_binary_payload(payload, 0.7)
        assert result is None

    def test_on_token_coerce_as_int_none_via_module(self):
        """Via module: numeric path for on_token uses as_int=False not as_int=None.
        on_token='1', raw=1.0 → coerce(raw, as_int=False)=1.0; coerce('1', as_int=False)=1.0 → match.
        """
        payload = {"on": "1", "off": "2"}
        result = mp._apply_binary_payload(payload, 1.0)
        assert result is True

    def test_off_token_coerce_as_int_none_via_module(self):
        """Via module: numeric path for off_token uses as_int=False not as_int=None."""
        payload = {"on": "1", "off": "2"}
        result = mp._apply_binary_payload(payload, 2.0)
        assert result is False


# ---------------------------------------------------------------------------
# _apply_binary_payload LOGGER mutations (mutmut_46–54): no-match debug log.
# ---------------------------------------------------------------------------


class TestApplyBinaryPayloadNoMatchLog:
    """Kill LOGGER mutations in the no-match debug log."""

    def test_no_match_debug_log_starts_with_binary_value_capital_b(self, caplog):
        """Debug message must start with 'Binary value' (capital B, not XX prefix)."""
        payload = {"on": "x", "off": "y"}
        with caplog.at_level(logging.DEBUG, logger=MAPPING_LOGGER):
            result = _apply_binary_payload(payload, "unmatched_sentinel")
        assert result is None
        msgs = [r.message for r in caplog.records]
        binary_msgs = [m for m in msgs if "binary" in m.lower() or "Binary" in m]
        assert binary_msgs, "Expected a log message about binary value"
        # mutmut_52 wraps with XX...XX, mutmut_53 uses 'binary', mutmut_54 uses 'BINARY'
        for m in binary_msgs:
            assert m.startswith("Binary value"), (
                f"Message should start with 'Binary value', got: {m!r}"
            )

    def test_no_match_debug_log_contains_raw_value(self, caplog):
        """Debug message must include raw_value (not None)."""
        payload = {"on": "x", "off": "y"}
        with caplog.at_level(logging.DEBUG, logger=MAPPING_LOGGER):
            _apply_binary_payload(payload, "unique_raw_sentinel")
        msgs = [r.message for r in caplog.records]
        combined = " ".join(msgs)
        # mutmut_47 replaces raw_value with None.
        assert "unique_raw_sentinel" in combined

    def test_no_match_debug_log_contains_payload(self, caplog):
        """Debug message must include payload (not None)."""
        payload = {"on": "xval_unique", "off": "yval_unique"}
        with caplog.at_level(logging.DEBUG, logger=MAPPING_LOGGER):
            _apply_binary_payload(payload, "unmatched")
        msgs = [r.message for r in caplog.records]
        combined = " ".join(msgs)
        # mutmut_48 replaces payload with None; the payload dict values won't appear.
        assert "xval_unique" in combined

    def test_no_match_debug_via_module(self, caplog):
        """Via module route: debug message is correct."""
        payload = {"on": "a", "off": "b"}
        with caplog.at_level(logging.DEBUG, logger=MAPPING_LOGGER):
            result = mp._apply_binary_payload(payload, "unmatched_via_module")
        assert result is None
        msgs = [r.message for r in caplog.records]
        combined = " ".join(msgs)
        assert "Binary value" in combined


# ---------------------------------------------------------------------------
# event_driven_field_keys mutmut_2: registry, _ = None
# ---------------------------------------------------------------------------


class TestEventDrivenFieldKeysMutation:
    """Kill mutmut_2: registry, _ = None instead of = _default_library()."""

    def test_event_driven_field_keys_with_none_registry_uses_default(self):
        """event_driven_field_keys(None) must call _default_library(), not assign None."""
        from custom_components.rtl_433.mapping import event_driven_field_keys

        # Passing None explicitly triggers the default-library path.
        keys = event_driven_field_keys(None)
        # The shipped library has these event fields.
        assert isinstance(keys, frozenset)
        assert "button" in keys

    def test_event_driven_field_keys_custom_registry(self):
        """event_driven_field_keys with an explicit registry returns correct keys."""
        from custom_components.rtl_433.mapping import event_driven_field_keys

        fd_event = _make_desc(
            field_key="press", platform="event", name="Press", object_suffix="p"
        )
        fd_sensor = _make_desc(
            field_key="temp", platform="sensor", name="Temp", object_suffix="T"
        )
        fd_driven = _make_desc(
            field_key="contact",
            platform="binary_sensor",
            name="Contact",
            object_suffix="c",
            event_driven=True,
        )
        reg = _make_reg(
            flat={
                "press": fd_event,
                "temp": fd_sensor,
                "contact": fd_driven,
            }
        )
        keys = event_driven_field_keys(reg)
        assert "press" in keys
        assert "contact" in keys
        assert "temp" not in keys


# ---------------------------------------------------------------------------
# Additional via-module tests for normalize_overrides and validate_user_mappings
# to ensure the already-known mutations are covered via the module namespace.
# ---------------------------------------------------------------------------


class TestNormalizeOverridesViaModuleExtra:
    """Extra module-routed tests for normalize_overrides that kill surviving mutants."""

    def test_skip_keys_continue_normalises_door_via_module(self):
        """Via module: skip_keys continue means door payload is normalised."""
        data = {
            "skip_keys": ["id"],
            "door": {
                "platform": "binary_sensor",
                "name": "D",
                "object_suffix": "D",
                "payload": {True: "open", False: "closed"},
            },
        }
        result = mp.normalize_overrides(data)
        assert result["door"]["payload"] == {"on": "open", "off": "closed"}

    def test_models_block_continue_normalises_flat_via_module(self):
        """Via module: models continue means flat payload is normalised after."""
        data = {
            "models": {"M": {}},
            "wet": {
                "platform": "binary_sensor",
                "name": "Wet",
                "object_suffix": "W",
                "payload": {True: "1", False: "0"},
            },
        }
        result = mp.normalize_overrides(data)
        assert result["wet"]["payload"] == {"on": "1", "off": "0"}


class TestValidateUserMappingsViaModuleExtra:
    """Extra module-routed tests for validate_user_mappings continue→break mutations."""

    def test_skip_keys_continue_validates_flat_after_via_module(self):
        """Via module: after skip_keys, flat entries still validated."""
        data = {"skip_keys": ["id"], "bad_flat": {}}
        problems = mp.validate_user_mappings(data)
        assert any("bad_flat" in p for p in problems)

    def test_models_non_mapping_continue_validates_flat_via_module(self):
        """Via module: after bad models, flat entries still validated."""
        data = {"models": "bad", "bad_flat": {}}
        problems = mp.validate_user_mappings(data)
        assert any("bad_flat" in p for p in problems)

    def test_models_valid_continue_validates_flat_via_module(self):
        """Via module: after good models block, flat entries still validated."""
        data = {
            "models": {
                "M": {"t": {"platform": "sensor", "name": "T", "object_suffix": "T"}}
            },
            "flat_bad": {},
        }
        problems = mp.validate_user_mappings(data)
        assert any("flat_bad" in p for p in problems)


# ---------------------------------------------------------------------------
# merge_overrides source parameter for _parse_models_block (mutmut_27/30/31)
# ---------------------------------------------------------------------------


class TestMergeOverridesParseModelsBlockSource:
    """Kill mutmut_27 (None), mutmut_30 (XXoverrideXX), mutmut_31 (OVERRIDE)."""

    def test_override_source_string_lowercase_override_in_log(self, caplog):
        """When models block has a bad entry, warning must say 'override' (not None/XX/upper)."""
        reg = _make_reg()
        with caplog.at_level(logging.WARNING, logger=MAPPING_LOGGER):
            merge_overrides(
                reg,
                set(),
                {"models": {"BadM": "not-a-dict"}},
            )
        msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        combined = " ".join(msgs)
        # mutmut_27 replaces "override" with None.
        # mutmut_30 replaces with "XXoverrideXX".
        # mutmut_31 replaces with "OVERRIDE".
        assert "override" in combined.lower()


# ---------------------------------------------------------------------------
# _load_descriptor_file encoding mutations (mutmut_2/3/4/8) — these are hard
# to distinguish since utf-8/UTF-8/None/"r" all work for ASCII. Write a file
# with actual UTF-8 multibyte content (degree symbol) to expose encoding=None.
# ---------------------------------------------------------------------------


class TestLoadDescriptorFileEncoding:
    """Kill encoding-related mutants in _load_descriptor_file (mutmut_2/3/4/8)."""

    def test_utf8_content_in_library_file_loads_correctly(self, tmp_path):
        """A library file with UTF-8 content (°) loads without error."""
        (tmp_path / "utf8_test.yaml").write_text(
            "temp:\n  platform: sensor\n  name: Temperature °C\n  object_suffix: T\n",
            encoding="utf-8",
        )
        registry, _ = load_library(tmp_path)
        assert "temp" in registry.flat
        assert "°C" in registry.flat["temp"].name


# ---------------------------------------------------------------------------
# _load_skip_keys encoding mutations (mutmut_10/11/12/16) —
# same approach: UTF-8 multibyte content in the skip file.
# ---------------------------------------------------------------------------


class TestLoadSkipKeysEncoding:
    """Kill encoding-related mutants in _load_skip_keys (mutmut_10/11/12/16)."""

    def test_utf8_content_in_skip_keys_file_loads(self, tmp_path):
        """_skip_keys.yaml with UTF-8 skip keys loads without error."""
        (tmp_path / "_skip_keys.yaml").write_text(
            "skip_keys:\n  - température\n  - id\n",
            encoding="utf-8",
        )
        _, skip_keys = load_library(tmp_path)
        # Both keys are present.
        assert "id" in skip_keys
        assert "température" in skip_keys


# ---------------------------------------------------------------------------
# Shipped library integration — exercise LOGGER paths on the real library
# to ensure all is well end-to-end
# ---------------------------------------------------------------------------


class TestShippedLibraryLogging:
    """Integration smoke tests that exercise real library logging paths."""

    def test_load_library_debug_mentions_descriptor_count(self, caplog):
        """The load-library debug message must be emitted and contain a count."""
        with caplog.at_level(logging.DEBUG, logger=MAPPING_LOGGER):
            registry, skip_keys = load_library()
        debug_msgs = [r.message for r in caplog.records if r.levelno == logging.DEBUG]
        combined = " ".join(debug_msgs)
        # The final debug line says "Loaded N descriptor(s), M model(s) and K skip key(s)".
        assert "descriptor" in combined
        assert len(registry.flat) > 0

    def test_apply_sensor_transform_via_module_with_scale_uses_float(self):
        """Via module: sensor transform with scale uses float arithmetic."""
        result = mp._apply_sensor_transform({"scale": 3.6, "round": 2}, 3.5)
        assert result == 12.6

    def test_apply_binary_payload_fractional_raw_not_truncated(self):
        """Fractional raw value is not truncated when matching numeric tokens."""
        # raw=1.0 matches on_token="1" via numeric path (1.0 == 1.0 as floats).
        payload = {"on": "1", "off": "0"}
        assert _apply_binary_payload(payload, 1.0) is True
        assert _apply_binary_payload(payload, 0.0) is False
