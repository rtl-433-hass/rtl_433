"""Tests for the ``sdr_settings`` Home Assistant adapter mapping.

The wire-protocol half of the managed SDR controls lives in
:mod:`pyrtl_433.sdr` (``SdrCommand`` / ``SDR_COMMANDS_BY_KEY``); this repo's
``sdr_settings`` module is the thin adapter that re-attaches the Home Assistant
entity-description metadata the library drops and merges it with the library's
protocol contract to build the :class:`SdrSetting` records the coordinator and
control platforms consume.

These tests lock the adaptation this repo owns: every library command is
surfaced as a setting whose protocol callables/keys are taken *by reference* from
the library (so ``/cmd`` composition stays byte-identical), and each setting
carries the HA metadata the platforms need to build entities. They do not
re-test the library's own transforms (covered upstream).
"""

from __future__ import annotations

from pyrtl_433.sdr import SDR_COMMANDS_BY_KEY
import pytest

from custom_components.rtl_433.sdr_settings import (
    CONTROL_PLATFORMS,
    SDR_SETTINGS,
    SDR_SETTINGS_BY_KEY,
    _adapt,
)

# The protocol fields the adapter must copy verbatim off the library command.
_REFERENCED = ("read", "to_command", "capability", "available")
_SCALAR = ("key", "command", "arg_kind")


def test_every_library_command_is_surfaced_as_a_setting():
    """Each library command has exactly one adapter setting under the same key."""
    assert set(SDR_SETTINGS_BY_KEY) == set(SDR_COMMANDS_BY_KEY)
    assert len(SDR_SETTINGS) == len(SDR_COMMANDS_BY_KEY)
    # Indexed-by-key is consistent with the tuple.
    assert {s.key: s for s in SDR_SETTINGS} == SDR_SETTINGS_BY_KEY


@pytest.mark.parametrize("key", sorted(SDR_COMMANDS_BY_KEY))
def test_protocol_fields_are_taken_by_reference_from_the_library(key):
    """The protocol contract is the library's original, wired in unchanged.

    The scalar identity fields equal the command's, and the four callables are
    the *same objects* (by reference) so entity generation and ``/cmd`` argument
    composition are identical to the pre-extraction behaviour.
    """
    setting = SDR_SETTINGS_BY_KEY[key]
    command = SDR_COMMANDS_BY_KEY[key]

    for field in _SCALAR:
        assert getattr(setting, field) == getattr(command, field), field
    for field in _REFERENCED:
        # Identity, not just equality: the adapter must not substitute a wrapper.
        assert getattr(setting, field) is getattr(command, field), field


@pytest.mark.parametrize("key", sorted(SDR_COMMANDS_BY_KEY))
def test_each_setting_carries_ha_entity_metadata(key):
    """Every setting supplies the HA entity-description fields the library drops."""
    setting = SDR_SETTINGS_BY_KEY[key]
    assert setting.name  # non-empty entity name
    assert setting.object_suffix  # unique-id token
    assert setting.platform in CONTROL_PLATFORMS


def test_gain_pair_shares_the_gain_command_across_two_platforms():
    """The gain dB Number and the Auto-gain Switch both map to the ``gain`` command."""
    from custom_components.rtl_433.sdr_settings import KEY_GAIN_AUTO, KEY_GAIN_DB

    gain_db = SDR_SETTINGS_BY_KEY[KEY_GAIN_DB]
    gain_auto = SDR_SETTINGS_BY_KEY[KEY_GAIN_AUTO]
    assert gain_db.command == gain_auto.command == "gain"
    assert gain_db.platform == "number"
    assert gain_auto.platform == "switch"


# --------------------------------------------------------------------------- #
# The factory itself.                                                           #
# --------------------------------------------------------------------------- #
# The tests above read ``SDR_SETTINGS``, which ``_adapt`` builds once while this
# module is imported. That proves the registry is right, but it never exercises
# the factory: by the time a test runs, the tuple is long since built. So a
# change in ``_adapt`` is invisible to them unless it happens to be a change
# that also breaks the values -- and mutation testing, which swaps the function
# body at call time, finds nothing to swap at all.
#
# Calling ``_adapt`` directly closes that. It is also the more honest unit test:
# the contract is "mirror the library command, merge the HA metadata", and that
# is a property of the function, not of one particular registry.


def _minimal_metadata(**extra):
    """The three entity-description fields ``SdrSetting`` has no defaults for."""
    return {"name": "N", "object_suffix": "o", "platform": "number", **extra}


@pytest.mark.parametrize("key", sorted(SDR_COMMANDS_BY_KEY))
def test_adapt_mirrors_the_library_command_for(key):
    """Built fresh, a setting still carries the library's protocol unchanged."""
    command = SDR_COMMANDS_BY_KEY[key]
    built = _adapt(key, **_minimal_metadata())

    for field in _SCALAR:
        assert getattr(built, field) == getattr(command, field), field
    for field in _REFERENCED:
        # Identity, not equality: a wrapper would change /cmd composition.
        assert getattr(built, field) is getattr(command, field), field


def test_adapt_reads_the_command_named_by_its_key():
    """The lookup is by the argument, not by anything incidental about it.

    Two different keys must yield two different commands, which is what stops
    the factory from silently resolving every setting to the same entry.
    """
    first, second = sorted(SDR_COMMANDS_BY_KEY)[:2]
    assert _adapt(first, **_minimal_metadata()).key == first
    assert _adapt(second, **_minimal_metadata()).key == second
    assert (
        _adapt(first, **_minimal_metadata()).command
        == SDR_COMMANDS_BY_KEY[first].command
    )


def test_adapt_passes_ha_metadata_through_untouched():
    """The metadata half is the caller's; the factory adds and changes nothing."""
    key = sorted(SDR_COMMANDS_BY_KEY)[0]
    built = _adapt(
        key,
        name="Sample rate",
        object_suffix="sample_rate",
        platform="number",
        native_min=0,
        native_max=20_000_000,
        native_step=1,
    )
    assert built.name == "Sample rate"
    assert built.object_suffix == "sample_rate"
    assert built.platform == "number"
    assert built.native_min == 0
    assert built.native_max == 20_000_000
    assert built.native_step == 1
    # Anything the caller did not supply keeps the dataclass default.
    assert built.options is None
    assert built.device_class is None


def test_adapt_rejects_a_key_the_library_does_not_define():
    """A typo'd key must fail loudly here rather than build a setting whose
    protocol half is silently wrong."""
    with pytest.raises(KeyError):
        _adapt("not-a-real-command", **_minimal_metadata())
