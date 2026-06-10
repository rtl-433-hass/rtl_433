"""Mutation-floor tests for custom_components/rtl_433/sdr_settings.py.

These tests are written specifically to kill surviving mutmut mutants that the
existing test suite misses. They assert exact, low-level behaviour so that even
small mutations (wrong operator, wrong dict key, wrong comparison string) cause
at least one assertion to fail.

Groups:
- conversion_val_to_label — boundary at val=0 and out-of-range val=3
- _read_gain_db (via SdrSetting.read) — correct dict key, guard semantics,
  empty-string -> None, non-empty -> float
- _read_gain_auto (via SdrSetting.read) — correct dict key, None -> None,
  empty-string -> True, non-empty -> False
"""

from __future__ import annotations

import pytest

from custom_components.rtl_433.sdr_settings import (
    CONVERSION_MODES,
    KEY_GAIN_AUTO,
    KEY_GAIN_DB,
    SDR_SETTINGS_BY_KEY,
    conversion_val_to_label,
)

# --------------------------------------------------------------------------- #
# conversion_val_to_label: boundary tests                                      #
# --------------------------------------------------------------------------- #
# Mutant 1: `1 <= val` kills val=0 returning "native"
# Mutant 2: `0 < val` kills val=0 returning "native"
# Mutant 3: `<= len(CONVERSION_MODES)` allows index=3 (len=3) which would crash


class TestConversionValToLabel:
    """conversion_val_to_label: exact return values for every in-range index and
    None for the first out-of-range index."""

    def test_zero_returns_native(self):
        """val=0 -> 'native' (the first option; kills mutants that shift lower bound)."""
        result = conversion_val_to_label(0)
        assert result == "native"
        # Confirm it is the correct tuple element by index.
        assert result == CONVERSION_MODES[0]

    def test_one_returns_si(self):
        """val=1 -> 'si'."""
        assert conversion_val_to_label(1) == "si"

    def test_two_returns_customary(self):
        """val=2 -> 'customary'."""
        assert conversion_val_to_label(2) == "customary"

    def test_three_returns_none(self):
        """val=3 == len(CONVERSION_MODES) -> None (not an IndexError).

        Kills mutant_3 which uses ``<= len(…)`` and would attempt index 3,
        raising IndexError instead of returning None.
        """
        result = conversion_val_to_label(3)
        assert result is None

    def test_negative_returns_none(self):
        """val=-1 < 0 -> None."""
        assert conversion_val_to_label(-1) is None

    def test_large_out_of_range_returns_none(self):
        """val=100 -> None."""
        assert conversion_val_to_label(100) is None


# --------------------------------------------------------------------------- #
# _read_gain_db: exercised via SDR_SETTINGS_BY_KEY[KEY_GAIN_DB].read           #
# --------------------------------------------------------------------------- #
# Mutant 1-4: wrong dict key -> any dict lookup returns None -> None
# Mutant 5: `or` -> `and`: empty-string branch no longer triggers
# Mutant 6: `is not None` -> gain="32.8" falls through None-guard and returns
#            float on the mutated path *correctly*, but gain=None triggers float()
# Mutant 7: `!= ""` -> non-empty gain returns None (wrong)
# Mutant 8: `== "XXXX"` -> empty string "" no longer returns None
# Mutant 9: `float(None)` -> TypeError instead of the float value


class TestReadGainDb:
    """_read_gain_db via the registry SdrSetting.read callable."""

    @pytest.fixture(autouse=True)
    def _setting(self):
        self.read = SDR_SETTINGS_BY_KEY[KEY_GAIN_DB].read

    def test_gain_string_returns_float(self):
        """meta['gain']='32.8' -> 32.8 (kills key-mutation and float(None) mutants)."""
        result = self.read({"gain": "32.8"})
        assert result == pytest.approx(32.8)
        assert isinstance(result, float)

    def test_gain_integer_string_returns_float(self):
        """meta['gain']='40' -> 40.0."""
        result = self.read({"gain": "40"})
        assert result == pytest.approx(40.0)
        assert isinstance(result, float)

    def test_empty_string_returns_none(self):
        """meta['gain']='' -> None (auto-gain sentinel; kills mutmut_8 '==XXXX')."""
        assert self.read({"gain": ""}) is None

    def test_missing_key_returns_none(self):
        """meta with no 'gain' key -> None."""
        assert self.read({}) is None

    def test_none_value_returns_none(self):
        """meta['gain']=None -> None."""
        assert self.read({"gain": None}) is None

    def test_non_gain_key_is_ignored(self):
        """A meta that has only a different key returns None (kills wrong-key mutants)."""
        # These keys represent the mutated alternatives (None, 'XXgainXX', 'GAIN').
        assert self.read({"GAIN": "32.8"}) is None
        assert self.read({"XXgainXX": "32.8"}) is None

    def test_non_empty_string_not_suppressed_by_empty_check(self):
        """A non-empty gain string is NOT None (kills mutmut_7 '!= ""' flip)."""
        # mutmut_7: `gain != ""` would make "32.8" trigger `return None`.
        result = self.read({"gain": "32.8"})
        assert result is not None
        assert result == pytest.approx(32.8)

    def test_empty_string_suppressed_correctly(self):
        """Empty string IS None but a value string is NOT (kills mutmut_5 and/or)."""
        # mutmut_5: `or` -> `and` means `gain="" and gain is None` is always False
        # for a non-None gain="", so "" would fall through to float("") -> ValueError.
        assert self.read({"gain": ""}) is None
        assert self.read({"gain": "10.5"}) is not None

    def test_invalid_string_returns_none(self):
        """A non-numeric string is caught by the except clause -> None."""
        assert self.read({"gain": "auto"}) is None


# --------------------------------------------------------------------------- #
# _read_gain_auto: exercised via SDR_SETTINGS_BY_KEY[KEY_GAIN_AUTO].read       #
# --------------------------------------------------------------------------- #
# Mutant 1-4: wrong dict key -> always None
# Mutant 5: `is not None` -> non-None gain returns None (inverted guard)
# Mutant 6: `!= ""` -> auto gain returns False, numeric gain returns True (flipped)
# Mutant 7: `== "XXXX"` -> always returns False


class TestReadGainAuto:
    """_read_gain_auto via the registry SdrSetting.read callable."""

    @pytest.fixture(autouse=True)
    def _setting(self):
        self.read = SDR_SETTINGS_BY_KEY[KEY_GAIN_AUTO].read

    def test_missing_gain_key_returns_none(self):
        """meta with no 'gain' key -> None (no gain info at all)."""
        assert self.read({}) is None

    def test_none_gain_value_returns_none(self):
        """meta['gain']=None -> None (pre-connect / unavailable)."""
        assert self.read({"gain": None}) is None

    def test_empty_string_returns_true(self):
        """meta['gain']='' -> True (auto gain is active).

        Kills mutmut_6 ('!= ""' would return False) and mutmut_7 ('== "XXXX"'
        would return False).
        """
        result = self.read({"gain": ""})
        assert result is True

    def test_nonempty_gain_returns_false(self):
        """meta['gain']='32.8' -> False (manual gain, auto is off).

        Kills mutmut_5 ('is not None' would return None for non-None gain) and
        key-mutation mutants (wrong key -> None != False).
        """
        result = self.read({"gain": "32.8"})
        assert result is False

    def test_nonempty_gain_zero_string_returns_false(self):
        """meta['gain']='0' -> False (0 dB manual gain; non-empty string)."""
        assert self.read({"gain": "0"}) is False

    def test_non_gain_key_is_ignored(self):
        """A meta without the 'gain' key returns None (kills wrong-key mutants)."""
        assert self.read({"GAIN": ""}) is None
        assert self.read({"XXgainXX": ""}) is None

    def test_empty_vs_nonempty_are_distinct(self):
        """Empty -> True, non-empty -> False: the two branches are distinct.

        Collectively kills all operator/string mutations on the ``gain == ""``
        comparison.
        """
        assert self.read({"gain": ""}) is True
        assert self.read({"gain": "10"}) is False
        assert self.read({"gain": "32.8"}) is False
