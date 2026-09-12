"""Mutation tests for the rtl_433 number control platform (``number.py``).

These tests construct ``Rtl433NumberControl`` from the real SDR settings registry
and call ``async_setup_entry`` through the live module object, so mutmut's
namespace-level function replacements are observable. Every assertion pins an
exact value the corresponding mutation would change.
"""

from __future__ import annotations

from custom_components.rtl_433.const import (
    DOMAIN,
    SUBENTRY_TYPE_RECEIVER,
    receiver_identity,
)
import custom_components.rtl_433.number as number_mod
from custom_components.rtl_433.number import PLATFORM, NumberMode, Rtl433NumberControl
from custom_components.rtl_433.sdr_settings import (
    KEY_CENTER_FREQUENCY,
    KEY_GAIN_DB,
    KEY_HOP_INTERVAL,
    KEY_PPM_ERROR,
    SDR_SETTINGS,
    SDR_SETTINGS_BY_KEY,
)

# The location entry and the receiver subentry these fakes stand in for. Two
# distinct ids, because the control's unique_id has to carry both.
_ENTRY_ID = "locationE"
_RECEIVER_ID = "receiverE"


class _RecordingCoordinator:
    """Coordinator stand-in that records set_sdr calls and serves meta/desired."""

    def __init__(self, *, manage_settings: bool = True, meta: dict | None = None):
        self.manage_settings = manage_settings
        self.meta = {} if meta is None else meta
        self.calls: list[tuple] = []
        self._desired: dict = {}
        # A control's identity now comes off the coordinator: the receiver it
        # belongs to is the coordinator's subentry, so there is no separate id
        # for a caller to pass (or to pass wrongly).
        self.receiver_id = _RECEIVER_ID
        self.receiver_identity = receiver_identity(_ENTRY_ID, _RECEIVER_ID)

    def get_desired(self, key):
        return self._desired.get(key)

    async def set_sdr(self, key, value):
        self.calls.append((key, value))


def _setting(key):
    return SDR_SETTINGS_BY_KEY[key]


def _build(key):
    return Rtl433NumberControl(_RecordingCoordinator(), _setting(key))


# --- __init__ attribute propagation (kills "= None" mutants 7-13) --------------


def test_init_native_min_max_step_unit_exact():
    s = _setting(KEY_CENTER_FREQUENCY)
    ent = _build(KEY_CENTER_FREQUENCY)
    assert ent._attr_native_min_value == s.native_min
    assert ent._attr_native_max_value == s.native_max
    assert ent._attr_native_step == s.native_step
    assert ent._attr_native_unit_of_measurement == s.native_unit
    # None-mutants would null these; the real values are non-None here.
    assert ent._attr_native_min_value is not None
    assert ent._attr_native_max_value is not None
    assert ent._attr_native_step is not None
    assert ent._attr_native_unit_of_measurement is not None


def test_init_gain_step_is_fractional():
    # gain step is 0.1 — a "= None" mutant on step is killed by exact equality.
    ent = _build(KEY_GAIN_DB)
    assert ent._attr_native_step == _setting(KEY_GAIN_DB).native_step == 0.1


def test_init_device_class_exact_for_center_frequency():
    ent = _build(KEY_CENTER_FREQUENCY)
    assert ent._attr_device_class == _setting(KEY_CENTER_FREQUENCY).device_class
    assert ent._attr_device_class is not None


def test_init_mode_falls_back_to_box():
    # All real number settings have mode=None, so "setting.mode or NumberMode.BOX"
    # must yield BOX. Both the "= None" mutant and the "setting.mode and BOX"
    # mutant produce None here and are killed by exact equality.
    ent = _build(KEY_PPM_ERROR)
    assert ent._attr_mode == NumberMode.BOX
    assert ent._attr_mode is not None


# --- super().__init__ wiring (kills arg mutants 1-6) ---------------------------


def test_init_wires_coordinator_and_setting_and_receiver_id():
    coord = _RecordingCoordinator()
    ent = Rtl433NumberControl(coord, _setting(KEY_PPM_ERROR))
    # coordinator wired (super arg 1 -> None mutant)
    assert ent._coordinator is coord
    # setting wired (super arg 2 -> None / dropped mutant)
    assert ent._setting is _setting(KEY_PPM_ERROR)
    # The receiver's identity flows into unique_id from the coordinator, so the
    # control names its location *and* its receiver -- two receivers in one
    # location would otherwise mint the same id for their own gain control.
    assert _RECEIVER_ID in ent.unique_id
    assert ent.unique_id == (
        f"{receiver_identity(_ENTRY_ID, _RECEIVER_ID)}:"
        f"{_setting(KEY_PPM_ERROR).object_suffix}"
    )


# --- async_set_native_value (kills set_sdr arg mutants 1-4) --------------------


async def test_set_native_value_passes_exact_key_and_value():
    coord = _RecordingCoordinator()
    ent = Rtl433NumberControl(coord, _setting(KEY_HOP_INTERVAL))
    await ent.async_set_native_value(42.0)
    # Kills set_sdr(None, value), set_sdr(key, None), set_sdr(value),
    # and the dropped-second-arg variant.
    assert coord.calls == [(KEY_HOP_INTERVAL, 42.0)]


async def test_set_native_value_negative_value_exact():
    coord = _RecordingCoordinator()
    ent = Rtl433NumberControl(coord, _setting(KEY_PPM_ERROR))
    await ent.async_set_native_value(-7.0)
    assert coord.calls == [(KEY_PPM_ERROR, -7.0)]
    assert coord.calls[0][0] == KEY_PPM_ERROR
    assert coord.calls[0][1] == -7.0


# --- native_value optimistic/confirmed (kills get_desired/read mutants) --------


def test_native_value_prefers_desired_including_zero():
    coord = _RecordingCoordinator()
    ent = Rtl433NumberControl(coord, _setting(KEY_PPM_ERROR))
    coord._desired[KEY_PPM_ERROR] = 0  # falsy but not None -> must be returned
    assert ent.native_value == 0


def test_native_value_falls_back_to_meta_when_no_desired():
    coord = _RecordingCoordinator(meta={KEY_PPM_ERROR: 12})
    ent = Rtl433NumberControl(coord, _setting(KEY_PPM_ERROR))
    # No desired set -> reads the setting from coordinator.meta.
    assert ent.native_value == 12


# --- async_setup_entry (kills setup mutants 1-12) -----------------------------


def _number_setting_count(meta):
    return sum(1 for s in SDR_SETTINGS if s.platform == PLATFORM and s.capability(meta))


class _FakeSubentry:
    subentry_id = _RECEIVER_ID
    subentry_type = SUBENTRY_TYPE_RECEIVER


class _FakeEntry:
    entry_id = _ENTRY_ID
    subentries = {_RECEIVER_ID: _FakeSubentry()}


def _make_hass(coordinator):
    class _Hass:
        # Coordinators are stored per receiver, keyed by the subentry id: a
        # location with two servers runs two of them.
        data = {DOMAIN: {_RECEIVER_ID: coordinator}}

    return _Hass()


def _collect(created):
    """An ``async_add_entities`` stand-in that tolerates ``config_subentry_id``."""

    def _add(entities, *args, **kwargs):
        created.extend(entities)

    return _add


async def test_setup_entry_creates_only_number_controls():
    coord = _RecordingCoordinator(meta={})
    created: list = []
    await number_mod.async_setup_entry(
        _make_hass(coord), _FakeEntry(), _collect(created)
    )
    created = list(created)
    # Every created entity is a number control (kills == -> != and and -> or).
    assert created, "expected at least one number control"
    assert all(isinstance(e, Rtl433NumberControl) for e in created)
    assert all(e._setting.platform == PLATFORM for e in created)
    # Exact count of number-platform settings whose capability gate passes.
    assert len(created) == _number_setting_count({})


async def test_setup_entry_entities_wired_to_real_coordinator_and_receiver_id():
    coord = _RecordingCoordinator(meta={})
    created: list = []
    await number_mod.async_setup_entry(
        _make_hass(coord), _FakeEntry(), _collect(created)
    )
    created = list(created)
    # coordinator=None mutant (construction arg) and hass.data->None mutant.
    assert all(e._coordinator is coord for e in created)
    # The receiver's identity flows into each unique_id.
    prefix = f"{receiver_identity(_ENTRY_ID, _RECEIVER_ID)}:"
    assert all(e.unique_id.startswith(prefix) for e in created)
    # setting positional arg present (setting->None / dropped mutant).
    assert all(e._setting is not None for e in created)


async def test_setup_entry_noop_when_not_managing():
    coord = _RecordingCoordinator(manage_settings=False)
    created: list = []
    await number_mod.async_setup_entry(
        _make_hass(coord), _FakeEntry(), _collect(created)
    )
    # Guard "if not manage_settings: return" -> inverted mutant would create entities.
    assert list(created) == []
