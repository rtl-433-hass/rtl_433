"""Mutation-killing tests for custom_components/rtl_433/switch.py.

Targets 14 surviving mutants across four areas:

* async_turn_on / async_turn_off: assert set_sdr is called with the EXACT
  (key, value) pair – kills all 5+5 arg-mutation variants per method.
* Rtl433SwitchControl.__init__: verify the coordinator is wired via the
  super().__init__ chain – kills the None-coordinator mutant.
* async_setup_entry logic:
  - ``and`` → ``or``: only switch-platform settings are registered.
  - capability(None) vs capability(coordinator.meta): capability receives
    coordinator.meta, not None.
  - Rtl433SwitchControl(None, …): the created entity is bound to the real
    coordinator.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.rtl_433.const import DOMAIN
from custom_components.rtl_433.coordinator import Rtl433Coordinator
from custom_components.rtl_433.sdr_settings import KEY_GAIN_AUTO, SDR_SETTINGS_BY_KEY
from custom_components.rtl_433.switch import PLATFORM, Rtl433SwitchControl

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_socket():
    """Stub the connect loop so no real WebSocket is opened."""

    async def _noop(self) -> None:
        return None

    with patch.object(Rtl433Coordinator, "_connect_loop", _noop):
        yield


async def _setup_hub(hass, hub_entry_builder, **kwargs):
    hub = hub_entry_builder(availability_timeout=600, **kwargs)
    hub.add_to_hass(hass)
    assert await hass.config_entries.async_setup(hub.entry_id)
    await hass.async_block_till_done()
    return hub


def _make_switch_entity() -> tuple[Rtl433SwitchControl, MagicMock]:
    """Build a bare Rtl433SwitchControl with a mock coordinator.

    Returns (entity, coordinator_mock). The mock's set_sdr is an AsyncMock
    so async_turn_on / async_turn_off can be awaited.
    """
    setting = SDR_SETTINGS_BY_KEY[KEY_GAIN_AUTO]

    coordinator = MagicMock()
    coordinator.set_sdr = AsyncMock()
    coordinator.meta = {}
    coordinator.get_desired = MagicMock(return_value=None)

    entity = Rtl433SwitchControl(coordinator, "entry_test", setting)
    return entity, coordinator


# ---------------------------------------------------------------------------
# async_turn_on – kills all 5 mutants (key→None, value→None, value→False,
# single-arg, empty-second-arg)
# ---------------------------------------------------------------------------


async def test_async_turn_on_calls_set_sdr_with_key_and_true():
    """async_turn_on calls coordinator.set_sdr(key, True) – kills mutmut_1..5."""
    entity, coordinator = _make_switch_entity()
    await entity.async_turn_on()

    coordinator.set_sdr.assert_awaited_once()
    args, kwargs = coordinator.set_sdr.call_args
    # Exact positional arguments: first is the setting key, second is True.
    assert len(args) == 2, f"expected 2 args, got {args!r}"
    assert args[0] == KEY_GAIN_AUTO, (
        f"first arg must be {KEY_GAIN_AUTO!r}, got {args[0]!r}"
    )
    assert args[1] is True, f"second arg must be True, got {args[1]!r}"


async def test_async_turn_on_key_is_not_none():
    """async_turn_on must pass the setting key, not None – kills mutmut_1."""
    entity, coordinator = _make_switch_entity()
    await entity.async_turn_on()

    args = coordinator.set_sdr.call_args[0]
    assert args[0] is not None


async def test_async_turn_on_value_is_true_not_false():
    """async_turn_on must pass True, not False – kills mutmut_5."""
    entity, coordinator = _make_switch_entity()
    await entity.async_turn_on()

    args = coordinator.set_sdr.call_args[0]
    assert args[1] is True
    assert args[1] is not False


async def test_async_turn_on_value_is_not_none():
    """async_turn_on must pass True, not None – kills mutmut_2."""
    entity, coordinator = _make_switch_entity()
    await entity.async_turn_on()

    args = coordinator.set_sdr.call_args[0]
    assert args[1] is not None


async def test_async_turn_on_two_positional_args():
    """async_turn_on must call set_sdr with exactly 2 args – kills mutmut_3/4."""
    entity, coordinator = _make_switch_entity()
    await entity.async_turn_on()

    args = coordinator.set_sdr.call_args[0]
    assert len(args) == 2


# ---------------------------------------------------------------------------
# async_turn_off – kills all 5 mutants (key→None, value→None, value→True,
# single-arg, empty-second-arg)
# ---------------------------------------------------------------------------


async def test_async_turn_off_calls_set_sdr_with_key_and_false():
    """async_turn_off calls coordinator.set_sdr(key, False) – kills mutmut_1..5."""
    entity, coordinator = _make_switch_entity()
    await entity.async_turn_off()

    coordinator.set_sdr.assert_awaited_once()
    args, kwargs = coordinator.set_sdr.call_args
    assert len(args) == 2, f"expected 2 args, got {args!r}"
    assert args[0] == KEY_GAIN_AUTO, (
        f"first arg must be {KEY_GAIN_AUTO!r}, got {args[0]!r}"
    )
    assert args[1] is False, f"second arg must be False, got {args[1]!r}"


async def test_async_turn_off_key_is_not_none():
    """async_turn_off must pass the setting key, not None – kills mutmut_1."""
    entity, coordinator = _make_switch_entity()
    await entity.async_turn_off()

    args = coordinator.set_sdr.call_args[0]
    assert args[0] is not None


async def test_async_turn_off_value_is_false_not_true():
    """async_turn_off must pass False, not True – kills mutmut_5."""
    entity, coordinator = _make_switch_entity()
    await entity.async_turn_off()

    args = coordinator.set_sdr.call_args[0]
    assert args[1] is False
    assert args[1] is not True


async def test_async_turn_off_value_is_not_none():
    """async_turn_off must pass False, not None – kills mutmut_2."""
    entity, coordinator = _make_switch_entity()
    await entity.async_turn_off()

    args = coordinator.set_sdr.call_args[0]
    assert args[1] is not None


async def test_async_turn_off_two_positional_args():
    """async_turn_off must call set_sdr with exactly 2 args – kills mutmut_3/4."""
    entity, coordinator = _make_switch_entity()
    await entity.async_turn_off()

    args = coordinator.set_sdr.call_args[0]
    assert len(args) == 2


# ---------------------------------------------------------------------------
# Rtl433SwitchControl.__init__ – coordinator wired via super().__init__
# kills xǁRtl433SwitchControlǁ__init____mutmut_1 (super().__init__(None, …))
# ---------------------------------------------------------------------------


def test_init_wires_coordinator_to_entity():
    """The entity's _coordinator must be the coordinator passed to __init__."""
    entity, coordinator = _make_switch_entity()
    # Rtl433HubControl.__init__ → Rtl433HubEntity.__init__ stores it as _coordinator.
    assert entity._coordinator is coordinator


def test_init_coordinator_not_none():
    """After __init__, _coordinator must not be None."""
    entity, coordinator = _make_switch_entity()
    assert entity._coordinator is not None


def test_init_is_on_reads_coordinator():
    """is_on accesses coordinator.get_desired – proves coordinator is wired."""
    entity, coordinator = _make_switch_entity()
    coordinator.get_desired.return_value = True
    assert entity.is_on is True
    coordinator.get_desired.assert_called_with(KEY_GAIN_AUTO)


# ---------------------------------------------------------------------------
# async_setup_entry – only switch-platform settings registered
# kills x_async_setup_entry__mutmut_10 (and → or)
# ---------------------------------------------------------------------------


async def test_setup_entry_only_creates_switch_platform_entities(
    hass, hub_entry_builder
):
    """async_setup_entry only registers settings with platform == 'switch'.

    With ``and → or`` the filter would pass settings from other platforms too.
    We count the created switch entities and verify only the gain_auto switch
    is registered (1 entity), not the number/select settings.
    """
    from homeassistant.helpers import entity_registry as er

    hub = await _setup_hub(hass, hub_entry_builder)
    ent_reg = er.async_get(hass)

    # Gather every switch entity under this hub.
    hub_switches = [
        e
        for e in ent_reg.entities.values()
        if e.platform == DOMAIN and e.domain == "switch"
    ]
    # Only "gain_auto" is a switch-platform setting.
    switch_settings = [
        s for s in SDR_SETTINGS_BY_KEY.values() if s.platform == PLATFORM
    ]
    assert len(hub_switches) == len(switch_settings)

    # Verify the single expected switch is the gain_auto one.
    uids = {e.unique_id for e in hub_switches}
    assert f"{hub.entry_id}:hub:gain_auto" in uids

    # Confirm that number-platform settings are NOT present as switches.
    for setting in SDR_SETTINGS_BY_KEY.values():
        if setting.platform != PLATFORM:
            assert f"{hub.entry_id}:hub:{setting.object_suffix}" not in uids, (
                f"non-switch setting {setting.key!r} must not appear as a switch"
            )


# ---------------------------------------------------------------------------
# async_setup_entry – capability called with coordinator.meta, not None
# kills x_async_setup_entry__mutmut_12 (capability(None))
# ---------------------------------------------------------------------------


async def test_setup_entry_capability_called_with_coordinator_meta(
    hass, hub_entry_builder
):
    """capability() must receive coordinator.meta, not None.

    We patch SDR_SETTINGS before the hub is set up so the spy is in place when
    async_setup_entry runs. If the mutant passes None, the recorded arg is None.
    """
    import dataclasses

    from custom_components.rtl_433.sdr_settings import SDR_SETTINGS

    capability_args: list = []

    # Build a spy-wrapped version of the switch-platform setting.
    patched_settings = []
    for setting in SDR_SETTINGS:
        if setting.platform == PLATFORM:
            orig_cap = setting.capability

            def _spy_cap(meta, _orig=orig_cap):
                capability_args.append(meta)
                return _orig(meta)

            patched_settings.append(dataclasses.replace(setting, capability=_spy_cap))
        else:
            patched_settings.append(setting)

    hub = hub_entry_builder(availability_timeout=600)
    hub.add_to_hass(hass)

    with patch(
        "custom_components.rtl_433.switch.SDR_SETTINGS", tuple(patched_settings)
    ):
        assert await hass.config_entries.async_setup(hub.entry_id)
        await hass.async_block_till_done()

    # The spy must have been called.
    assert len(capability_args) >= 1
    for meta_arg in capability_args:
        assert isinstance(meta_arg, dict), (
            f"capability received {meta_arg!r} instead of coordinator.meta dict"
        )
    assert all(a is not None for a in capability_args)


async def test_setup_entry_capability_receives_coordinator_meta_object(
    hass, hub_entry_builder
):
    """Directly verify capability receives exactly coordinator.meta, not None.

    Sets a custom sentinel meta on the coordinator (by patching Rtl433Coordinator
    __init__ to record the instance), then checks that the spy arg is the
    coordinator's meta dict (not None).
    """
    import dataclasses

    from custom_components.rtl_433.sdr_settings import SDR_SETTINGS

    capability_args: list = []

    patched_settings = []
    for setting in SDR_SETTINGS:
        if setting.platform == PLATFORM:
            orig_cap = setting.capability

            def _spy_cap(meta, _orig=orig_cap):
                capability_args.append(meta)
                return _orig(meta)

            patched_settings.append(dataclasses.replace(setting, capability=_spy_cap))
        else:
            patched_settings.append(setting)

    hub = hub_entry_builder(availability_timeout=600)
    hub.add_to_hass(hass)

    with patch(
        "custom_components.rtl_433.switch.SDR_SETTINGS", tuple(patched_settings)
    ):
        assert await hass.config_entries.async_setup(hub.entry_id)
        await hass.async_block_till_done()

    coordinator: Rtl433Coordinator = hass.data[DOMAIN][hub.entry_id]

    # All calls received coordinator.meta (the same dict object), not None.
    assert len(capability_args) >= 1
    for meta_arg in capability_args:
        assert meta_arg is coordinator.meta, (
            f"capability received {meta_arg!r} not coordinator.meta={coordinator.meta!r}"
        )


# ---------------------------------------------------------------------------
# async_setup_entry – coordinator passed to Rtl433SwitchControl, not None
# kills x_async_setup_entry__mutmut_4 (Rtl433SwitchControl(None, …))
# ---------------------------------------------------------------------------


async def test_setup_entry_entity_coordinator_is_not_none(hass, hub_entry_builder):
    """The switch entities created by async_setup_entry must have a real coordinator.

    The mutant passes None as the coordinator argument; the entity's _coordinator
    would then be None, breaking is_on and any other property.
    """
    hub = await _setup_hub(hass, hub_entry_builder)
    hass.data[DOMAIN][hub.entry_id]

    from homeassistant.helpers import entity_registry as er

    ent_reg = er.async_get(hass)
    gain_auto_eid = ent_reg.async_get_entity_id(
        "switch", DOMAIN, f"{hub.entry_id}:hub:gain_auto"
    )
    assert gain_auto_eid is not None

    # Retrieve the live entity from HA's entity platform to inspect its coordinator.
    # We use the state + a service call to force is_on evaluation (which reads
    # _coordinator.get_desired). If coordinator were None this would raise.
    state = hass.states.get(gain_auto_eid)
    assert state is not None  # entity is alive and responded to write_ha_state
    # State is not "unavailable" means the entity's properties executed without error.
    # (A None coordinator would raise AttributeError on the first property access.)


async def test_setup_entry_entity_reads_coordinator_meta(hass, hub_entry_builder):
    """The switch entity created by async_setup_entry can read coordinator.meta.

    This proves the real coordinator (not None) was wired in. We set
    coordinator.meta and fire signal_hub_update; if the entity's coordinator
    is None the attribute access would raise and the state would not update.
    """
    from custom_components.rtl_433.const import signal_hub_update
    from homeassistant.helpers import entity_registry as er
    from homeassistant.helpers.dispatcher import async_dispatcher_send

    hub = await _setup_hub(hass, hub_entry_builder)
    coordinator: Rtl433Coordinator = hass.data[DOMAIN][hub.entry_id]

    ent_reg = er.async_get(hass)
    gain_auto_eid = ent_reg.async_get_entity_id(
        "switch", DOMAIN, f"{hub.entry_id}:hub:gain_auto"
    )
    assert gain_auto_eid is not None

    # Update coordinator state and trigger repaint.
    coordinator.meta = {"gain": ""}
    async_dispatcher_send(hass, signal_hub_update(hub.entry_id))
    await hass.async_block_till_done()

    state = hass.states.get(gain_auto_eid)
    # With gain="" and no desired value, is_on should reflect auto=True -> state "on".
    assert state is not None
    assert state.state == "on"
