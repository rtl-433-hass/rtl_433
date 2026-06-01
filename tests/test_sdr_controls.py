"""Tests for the managed SDR controls (Plan 6).

These cover the seven acceptance areas of the hub-SDR-controls work:

1. Write path / command mapping — each control's ``set_sdr`` write emits the
   exact ``/cmd`` (``val`` strings / verbatim ``arg``) and records desired state.
2. Adoption — normal (populate + mark managed), hop-mode (center frequency left
   unmanaged), and ``/cmd``-down (adopt nothing, issue nothing, never raise).
3. Reconnect enforcement — every managed field is replayed on each of two
   connects, with gain emitted exactly once.
4. Store persistence — desired state survives a recreate/reload from the Store.
5. Toggle gating + suppression — managed: controls present with
   ``EntityCategory.CONFIG`` and the five folded sensors absent; unmanaged: no
   controls and no ``/cmd`` issued (the sensor matrix lives in test_lifecycle).
6. Reload-on-toggle — flipping ``CONF_MANAGE_SETTINGS`` reloads, whereas a
   timeout / discovery-only options change is applied live (no reload).
7. Failure isolation + serialization — a ``/cmd`` failure (write or enforcement)
   retains the desired value and never disturbs the event stream; all ``/cmd``
   issuance is serialized through the single lock.

The coordinator-level scenarios drive the public API directly (no real socket).
The integration scenarios reuse the ``test_lifecycle`` ``_no_socket`` /
``_setup_hub`` harness, so adoption/enforcement is invoked explicitly where a
scenario needs it.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from custom_components.rtl_433.const import (
    CONF_AVAILABILITY_TIMEOUT,
    CONF_DISCOVERY_ENABLED,
    CONF_MANAGE_SETTINGS,
    DOMAIN,
    sdr_store_key,
    signal_hub_update,
)
from custom_components.rtl_433.coordinator import Rtl433Coordinator
from custom_components.rtl_433.sdr_settings import (
    KEY_CENTER_FREQUENCY,
    KEY_CONVERSION_MODE,
    KEY_GAIN_AUTO,
    KEY_GAIN_DB,
    KEY_HOP_INTERVAL,
    KEY_PPM_ERROR,
    KEY_SAMPLE_RATE,
    SDR_SETTINGS_BY_KEY,
)
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.entity import EntityCategory

DISPATCH = "custom_components.rtl_433.coordinator.base.async_dispatcher_send"
_CMD_URL = "http://rtl433.local:8433/cmd"

# A single-frequency meta -> the normal-adoption case (center frequency adopted).
_META_SINGLE = {
    "center_frequency": 433920000,
    "samp_rate": 250000,
    "conversion_mode": 1,
    "frequencies": [433920000],
    "hop_times": [600],
    "hop_interval": 600,
    "gain": "32.8",
    "ppm_error": 2,
}
# A two-frequency meta -> the hop-mode case (center frequency left unmanaged).
_META_HOPPING = {**_META_SINGLE, "frequencies": [433920000, 868000000]}


def _run(hass, coro):
    """Drive an async coordinator method to completion on the hass loop."""
    return hass.loop.run_until_complete(coro)


def _queries(aioclient_mock) -> list[dict[str, str]]:
    """Return each recorded request's query params as a plain dict."""
    return [dict(call[1].query) for call in aioclient_mock.mock_calls]


def _setter_queries(aioclient_mock) -> list[dict[str, str]]:
    """Recorded queries for setter commands only (drop the get_* read-backs)."""
    return [q for q in _queries(aioclient_mock) if not q["cmd"].startswith("get_")]


def _mock_setters(aioclient_mock) -> None:
    """Register OK stubs for every setter + the read-back getters.

    Each setter has a distinct ``cmd`` so a registered ``{"cmd": ...}`` matcher
    routes the request even with extra ``val`` / ``arg`` components present.
    """
    for cmd in (
        "center_frequency",
        "sample_rate",
        "ppm_error",
        "convert",
        "hop_interval",
        "gain",
    ):
        aioclient_mock.get(_CMD_URL, params={"cmd": cmd}, json={"result": "Ok"})
    # The setter path reconciles via _refresh_meta -> register its getters too.
    aioclient_mock.get(_CMD_URL, params={"cmd": "get_meta"}, json={"result": {}})
    aioclient_mock.get(_CMD_URL, params={"cmd": "get_gain"}, json={"result": "32.8"})
    aioclient_mock.get(_CMD_URL, params={"cmd": "get_ppm_error"}, json={"result": 2})


@pytest.fixture
def coordinator(hass, hub_entry_builder):
    """A coordinator wired to a managed hub entry (no socket opened)."""
    entry = hub_entry_builder(availability_timeout=600)
    entry.add_to_hass(hass)
    return Rtl433Coordinator(
        hass,
        entry,
        host="rtl433.local",
        manage_settings=True,
        skip_keys={"model", "id", "channel", "subtype", "time", "mic"},
    )


# --------------------------------------------------------------------------- #
# 1. Write path / command mapping (incl. gain auto on/off).                    #
# --------------------------------------------------------------------------- #
def test_write_path_command_mapping(hass, coordinator, aioclient_mock):
    """Each control write emits the exact /cmd and records desired state."""
    _mock_setters(aioclient_mock)
    coordinator.connected = True

    cases = [
        (
            "center_frequency",
            868.0,
            {"cmd": "center_frequency", "val": "868000000"},
        ),
        (KEY_SAMPLE_RATE, 1024000, {"cmd": "sample_rate", "val": "1024000"}),
        (KEY_PPM_ERROR, -3, {"cmd": "ppm_error", "val": "-3"}),
        # Conversion mode is stored as the integer ``convert`` val (what the
        # Select entity writes via conversion_label_to_val); 1 == "si".
        (KEY_CONVERSION_MODE, 1, {"cmd": "convert", "val": "1"}),
        (KEY_HOP_INTERVAL, 30, {"cmd": "hop_interval", "val": "30"}),
    ]
    with patch(DISPATCH):
        for field, value, expected in cases:
            aioclient_mock.mock_calls.clear()
            _run(hass, coordinator.set_sdr(field, value))
            setter = _setter_queries(aioclient_mock)
            assert setter, field
            assert setter[0] == expected, field
            assert coordinator.get_desired(field) == value
            assert coordinator.is_managed(field)


def test_write_path_gain_auto_on_sends_empty_arg(hass, coordinator, aioclient_mock):
    """Auto gain on sends gain with an explicit EMPTY arg (the auto sentinel)."""
    _mock_setters(aioclient_mock)
    coordinator.connected = True

    with patch(DISPATCH):
        _run(hass, coordinator.set_sdr(KEY_GAIN_AUTO, True))

    gain_calls = [q for q in _setter_queries(aioclient_mock) if q["cmd"] == "gain"]
    assert len(gain_calls) == 1
    # arg is present and empty; val is never set on the gain command.
    assert gain_calls[0] == {"cmd": "gain", "arg": ""}
    assert coordinator.get_desired(KEY_GAIN_AUTO) is True


def test_write_path_gain_db_when_auto_off_sends_db_arg(
    hass, coordinator, aioclient_mock
):
    """Auto gain off + a dB write sends gain with the formatted dB string arg."""
    _mock_setters(aioclient_mock)
    coordinator.connected = True

    with patch(DISPATCH):
        # Auto off first, then the dB value: the composed arg is the dB string.
        _run(hass, coordinator.set_sdr(KEY_GAIN_AUTO, False))
        aioclient_mock.mock_calls.clear()
        _run(hass, coordinator.set_sdr(KEY_GAIN_DB, 32.8))

    gain_calls = [q for q in _setter_queries(aioclient_mock) if q["cmd"] == "gain"]
    assert len(gain_calls) == 1
    assert gain_calls[0] == {"cmd": "gain", "arg": "32.8"}
    assert coordinator.get_desired(KEY_GAIN_DB) == 32.8
    assert coordinator.get_desired(KEY_GAIN_AUTO) is False

    # A clean integer dB trims the trailing .0 (gain_command_arg uses %g).
    aioclient_mock.mock_calls.clear()
    with patch(DISPATCH):
        _run(hass, coordinator.set_sdr(KEY_GAIN_DB, 40.0))
    gain_calls = [q for q in _setter_queries(aioclient_mock) if q["cmd"] == "gain"]
    assert gain_calls[0] == {"cmd": "gain", "arg": "40"}


# --------------------------------------------------------------------------- #
# 2. Adoption: normal, hop-mode, /cmd-down.                                    #
# --------------------------------------------------------------------------- #
def test_adoption_normal_populates_and_marks_managed(hass, coordinator):
    """A single-frequency meta is adopted: all fields populated + managed."""
    coordinator.meta = dict(_META_SINGLE)
    _run(hass, coordinator._adopt_from_server())

    # Center frequency IS adopted when not hopping (meta Hz -> desired MHz).
    assert coordinator.get_desired("center_frequency") == 433.92
    assert coordinator.is_managed("center_frequency")
    assert coordinator.get_desired(KEY_SAMPLE_RATE) == 250000
    assert coordinator.get_desired(KEY_PPM_ERROR) == 2
    assert coordinator.get_desired(KEY_HOP_INTERVAL) == 600
    # conversion_mode is read as its integer convert val (1 == "si").
    assert coordinator.get_desired(KEY_CONVERSION_MODE) == 1
    # gain "32.8" -> auto False + the parsed dB float.
    assert coordinator.get_desired(KEY_GAIN_AUTO) is False
    assert coordinator.get_desired(KEY_GAIN_DB) == 32.8
    assert coordinator.is_managed(KEY_GAIN_DB)


def test_adoption_hop_mode_skips_center_frequency(hass, coordinator):
    """A multi-frequency meta leaves center frequency unmanaged (hop guard)."""
    coordinator.meta = dict(_META_HOPPING)
    _run(hass, coordinator._adopt_from_server())

    assert coordinator.get_desired("center_frequency") is None
    assert not coordinator.is_managed("center_frequency")
    # Everything else is still adopted.
    assert coordinator.is_managed(KEY_SAMPLE_RATE)
    assert coordinator.is_managed(KEY_HOP_INTERVAL)
    assert coordinator.is_managed(KEY_GAIN_AUTO)


async def _seed_initial_frequency(coordinator):
    """Run the first-connect seeding the connect loop performs (adopt + layer).

    Mirrors the ``if not self._desired:`` branch of ``_connect_loop``: adopt the
    server's settings, then layer the setup ``initial_center_frequency`` over the
    adopted value, marking it managed and persisting. Only runs while empty.
    """
    if not coordinator._desired:
        await coordinator._adopt_from_server()
        if coordinator.initial_center_frequency is not None:
            coordinator._desired[KEY_CENTER_FREQUENCY] = (
                coordinator.initial_center_frequency
            )
            coordinator._managed.add(KEY_CENTER_FREQUENCY)
            await coordinator._persist_desired()


async def test_initial_frequency_seeds_over_adoption_on_first_connect(
    hass, hub_entry_builder, hass_storage
):
    """An ``initial_center_frequency`` overrides adoption + is enforced as Hz."""
    entry = hub_entry_builder(availability_timeout=600)
    entry.add_to_hass(hass)
    coordinator = Rtl433Coordinator(
        hass,
        entry,
        host="rtl433.local",
        manage_settings=True,
        initial_center_frequency=915.0,
    )
    coordinator.meta = dict(_META_SINGLE)  # would adopt 433.92 absent the override
    assert coordinator._desired == {}

    await _seed_initial_frequency(coordinator)

    # The setup choice (MHz) wins over the adopted value and is managed.
    assert coordinator.get_desired(KEY_CENTER_FREQUENCY) == 915.0
    assert coordinator.is_managed(KEY_CENTER_FREQUENCY)
    # Enforcement maps the MHz desired value to an integer-Hz command.
    assert coordinator._command_args(KEY_CENTER_FREQUENCY) == (
        "center_frequency",
        915000000,
        None,
    )
    # The seed was persisted, so a reload would not re-seed.
    assert sdr_store_key(entry.entry_id) in hass_storage


async def test_initial_frequency_not_seeded_when_desired_nonempty(
    hass, hub_entry_builder, hass_storage
):
    """With desired state already present, the setup frequency is NOT re-applied."""
    entry = hub_entry_builder(availability_timeout=600)
    entry.add_to_hass(hass)
    coordinator = Rtl433Coordinator(
        hass,
        entry,
        host="rtl433.local",
        manage_settings=True,
        initial_center_frequency=915.0,
    )
    # A prior session already established a desired center frequency.
    coordinator._desired = {KEY_CENTER_FREQUENCY: 433.92}
    coordinator._managed = {KEY_CENTER_FREQUENCY}
    coordinator.meta = dict(_META_SINGLE)

    await _seed_initial_frequency(coordinator)

    # The existing value survives; the setup seed did not overwrite it.
    assert coordinator.get_desired(KEY_CENTER_FREQUENCY) == 433.92


def test_adoption_cmd_down_adopts_nothing(hass, coordinator, aioclient_mock):
    """An empty meta (/cmd hidden) adopts nothing, issues nothing, never raises."""
    # The full connect path: getters all 500 -> meta stays empty.
    aioclient_mock.get(_CMD_URL, status=500)
    coordinator.connected = True

    with patch(DISPATCH):
        _run(hass, coordinator._refresh_meta())  # populates nothing
        # Adoption against the empty meta must be a quiet no-op.
        _run(hass, coordinator._adopt_from_server())
        # Enforcement over an empty managed-set issues nothing.
        _run(hass, coordinator._enforce_all())

    assert coordinator.meta == {}
    assert coordinator.get_desired("center_frequency") is None
    assert not coordinator.is_managed(KEY_GAIN_AUTO)
    # No setter command was issued (only the failed getters were attempted).
    assert _setter_queries(aioclient_mock) == []


# --------------------------------------------------------------------------- #
# 3. Reconnect enforcement replays all managed fields on each connect.         #
# --------------------------------------------------------------------------- #
def test_enforcement_replays_all_managed_on_each_connect(
    hass, coordinator, aioclient_mock
):
    """Two successive enforce passes each replay every managed field once."""
    _mock_setters(aioclient_mock)
    coordinator.connected = True
    # Seed desired state directly (as adoption / the Store would).
    coordinator._desired = {
        "center_frequency": 433.92,
        KEY_SAMPLE_RATE: 250000,
        KEY_PPM_ERROR: 2,
        KEY_CONVERSION_MODE: 1,
        KEY_HOP_INTERVAL: 600,
        KEY_GAIN_DB: 32.8,
        KEY_GAIN_AUTO: False,
    }
    coordinator._managed = set(coordinator._desired)

    expected = {
        "center_frequency": {"cmd": "center_frequency", "val": "433920000"},
        "sample_rate": {"cmd": "sample_rate", "val": "250000"},
        "ppm_error": {"cmd": "ppm_error", "val": "2"},
        "convert": {"cmd": "convert", "val": "1"},
        "hop_interval": {"cmd": "hop_interval", "val": "600"},
        "gain": {"cmd": "gain", "arg": "32.8"},
    }

    for _connect in range(2):
        aioclient_mock.mock_calls.clear()
        with patch(DISPATCH):
            _run(hass, coordinator._enforce_all())
        by_cmd = {q["cmd"]: q for q in _setter_queries(aioclient_mock)}
        # Every managed field replayed; gain emitted exactly once.
        assert by_cmd == expected
        gain_count = sum(
            1 for q in _setter_queries(aioclient_mock) if q["cmd"] == "gain"
        )
        assert gain_count == 1


# --------------------------------------------------------------------------- #
# 4. Store persistence across reload / recreate.                              #
# --------------------------------------------------------------------------- #
async def test_store_persistence_across_recreate(hass, hub_entry_builder, hass_storage):
    """A written desired state is restored from the Store on a fresh coordinator."""
    entry = hub_entry_builder(availability_timeout=600)
    entry.add_to_hass(hass)

    first = Rtl433Coordinator(hass, entry, host="rtl433.local", manage_settings=True)
    first._desired = {"center_frequency": 868.0, KEY_GAIN_AUTO: True}
    first._managed = {"center_frequency", KEY_GAIN_AUTO}
    await first._persist_desired()

    # The payload landed in the per-hub Store key.
    assert sdr_store_key(entry.entry_id) in hass_storage

    # A brand-new coordinator (as a reload would build) loads from the Store —
    # NOT by re-adopting (its meta is empty, so adoption would yield nothing).
    second = Rtl433Coordinator(hass, entry, host="rtl433.local", manage_settings=True)
    assert second.get_desired("center_frequency") is None  # not loaded yet
    await second.async_load_desired_state()
    assert second.get_desired("center_frequency") == 868.0
    assert second.get_desired(KEY_GAIN_AUTO) is True
    assert second.is_managed("center_frequency")
    assert second.is_managed(KEY_GAIN_AUTO)


async def test_store_wiped_when_management_off(hass, hub_entry_builder, hass_storage):
    """Loading with management off removes the Store and clears desired state."""
    entry = hub_entry_builder(availability_timeout=600)
    entry.add_to_hass(hass)

    managed = Rtl433Coordinator(hass, entry, host="rtl433.local", manage_settings=True)
    managed._desired = {"center_frequency": 868.0}
    managed._managed = {"center_frequency"}
    await managed._persist_desired()
    assert sdr_store_key(entry.entry_id) in hass_storage

    off = Rtl433Coordinator(hass, entry, host="rtl433.local", manage_settings=False)
    await off.async_load_desired_state()
    assert off.get_desired("center_frequency") is None
    assert off._managed == set()
    assert sdr_store_key(entry.entry_id) not in hass_storage


def _seed_store(hass_storage, entry_id, *, version, values, managed):
    """Pre-seed the per-hub SDR Store with an on-disk payload at ``version``."""
    hass_storage[sdr_store_key(entry_id)] = {
        "version": version,
        "data": {"values": values, "managed": managed},
    }


async def test_store_migration_v1_hz_to_mhz(hass, hub_entry_builder, hass_storage):
    """A version-1 payload's center frequency is migrated Hz -> MHz on load."""
    entry = hub_entry_builder(availability_timeout=600)
    entry.add_to_hass(hass)
    _seed_store(
        hass_storage,
        entry.entry_id,
        version=1,
        values={"center_frequency": 433920000},
        managed=["center_frequency"],
    )

    coordinator = Rtl433Coordinator(
        hass, entry, host="rtl433.local", manage_settings=True
    )
    await coordinator.async_load_desired_state()

    assert coordinator.get_desired("center_frequency") == 433.92
    assert coordinator.is_managed("center_frequency")


async def test_store_migration_v2_value_unchanged(
    hass, hub_entry_builder, hass_storage
):
    """An already-current (version-2) MHz value is loaded as-is (no re-division)."""
    entry = hub_entry_builder(availability_timeout=600)
    entry.add_to_hass(hass)
    _seed_store(
        hass_storage,
        entry.entry_id,
        version=2,
        values={"center_frequency": 433.92},
        managed=["center_frequency"],
    )

    coordinator = Rtl433Coordinator(
        hass, entry, host="rtl433.local", manage_settings=True
    )
    await coordinator.async_load_desired_state()

    assert coordinator.get_desired("center_frequency") == 433.92


async def test_store_migration_v1_without_center_frequency(
    hass, hub_entry_builder, hass_storage
):
    """A version-1 payload lacking center_frequency migrates other fields untouched."""
    entry = hub_entry_builder(availability_timeout=600)
    entry.add_to_hass(hass)
    _seed_store(
        hass_storage,
        entry.entry_id,
        version=1,
        values={KEY_SAMPLE_RATE: 250000, KEY_GAIN_AUTO: True},
        managed=[KEY_SAMPLE_RATE, KEY_GAIN_AUTO],
    )

    coordinator = Rtl433Coordinator(
        hass, entry, host="rtl433.local", manage_settings=True
    )
    await coordinator.async_load_desired_state()

    assert coordinator.get_desired("center_frequency") is None
    assert coordinator.get_desired(KEY_SAMPLE_RATE) == 250000
    assert coordinator.get_desired(KEY_GAIN_AUTO) is True


# --------------------------------------------------------------------------- #
# Center-frequency registry setting: MHz read / Hz to_command round-trip.      #
# --------------------------------------------------------------------------- #
def test_center_frequency_setting_mhz_round_trip():
    """``read`` converts meta Hz -> MHz; ``to_command`` converts MHz -> integer Hz."""
    cf = SDR_SETTINGS_BY_KEY[KEY_CENTER_FREQUENCY]

    assert cf.read({"center_frequency": 915_000_000}) == 915.0
    assert cf.read({"center_frequency": 433_920_000}) == 433.92

    for mhz, hz in ((915.0, 915_000_000), (433.92, 433_920_000), (868.3, 868_300_000)):
        sent = cf.to_command(mhz)
        assert sent == hz
        assert isinstance(sent, int)


# --------------------------------------------------------------------------- #
# 5. Toggle gating + suppression (integration).                               #
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _no_socket():
    """Stub the connect loop so integration setups never open a real socket."""

    async def _noop(self) -> None:
        return None

    with patch.object(Rtl433Coordinator, "_connect_loop", _noop):
        yield


async def _setup_hub(hass, hub_entry_builder, **kwargs):
    """Set up a single hub entry and return it."""
    hub = hub_entry_builder(availability_timeout=600, **kwargs)
    hub.add_to_hass(hass)
    assert await hass.config_entries.async_setup(hub.entry_id)
    await hass.async_block_till_done()
    return hub


# (entry_suffix, platform) for the seven control entities.
_CONTROLS = (
    ("center_frequency", "number"),
    ("sample_rate", "number"),
    ("ppm_error", "number"),
    ("gain", "number"),
    ("hop_interval", "number"),
    ("conversion_mode", "select"),
    ("gain_auto", "switch"),
)


async def test_controls_present_and_config_category_when_managed(
    hass, hub_entry_builder
):
    """Managed mode registers every control with EntityCategory.CONFIG."""
    hub = await _setup_hub(hass, hub_entry_builder)  # managed by default
    ent_reg = er.async_get(hass)

    for suffix, platform in _CONTROLS:
        eid = ent_reg.async_get_entity_id(
            platform, DOMAIN, f"{hub.entry_id}:hub:{suffix}"
        )
        assert eid is not None, (platform, suffix)
        assert ent_reg.async_get(eid).entity_category is EntityCategory.CONFIG


async def test_center_and_hop_availability_track_frequencies(hass, hub_entry_builder):
    """Center frequency and hop interval availability track the hop mode.

    Single frequency: center frequency is controllable, hop interval is inert
    (unavailable). Hopping (>1 frequency): hop interval applies, while pinning a
    single center frequency would break hopping, so that control is hidden.
    """
    hub = await _setup_hub(hass, hub_entry_builder)  # managed by default
    coordinator = hass.data[DOMAIN][hub.entry_id]
    ent_reg = er.async_get(hass)

    def num_state(suffix):
        eid = ent_reg.async_get_entity_id(
            "number", DOMAIN, f"{hub.entry_id}:hub:{suffix}"
        )
        assert eid is not None, suffix
        return hass.states.get(eid).state

    # Single frequency -> center frequency available, hop interval hidden.
    coordinator.meta = dict(_META_SINGLE)
    async_dispatcher_send(hass, signal_hub_update(hub.entry_id))
    await hass.async_block_till_done()
    assert num_state("center_frequency") != STATE_UNAVAILABLE
    assert num_state("hop_interval") == STATE_UNAVAILABLE

    # Hopping -> hop interval available, center frequency hidden.
    coordinator.meta = dict(_META_HOPPING)
    async_dispatcher_send(hass, signal_hub_update(hub.entry_id))
    await hass.async_block_till_done()
    assert num_state("hop_interval") != STATE_UNAVAILABLE
    assert num_state("center_frequency") == STATE_UNAVAILABLE


async def test_select_entity_writes_convert_int_command(
    hass, hub_entry_builder, aioclient_mock
):
    """Selecting via the Select entity sends ``convert val=<int>`` and repaints.

    Regression guard: the desired value, the registry's ``to_command``, and the
    Select entity must all agree that ``conversion_mode`` is the integer
    ``convert`` val — a label/int mismatch previously raised when an option was
    selected through the real entity.
    """
    _mock_setters(aioclient_mock)
    hub = await _setup_hub(hass, hub_entry_builder)  # managed by default
    coordinator = hass.data[DOMAIN][hub.entry_id]
    coordinator.connected = True

    eid = er.async_get(hass).async_get_entity_id(
        "select", DOMAIN, f"{hub.entry_id}:hub:conversion_mode"
    )
    assert eid is not None

    aioclient_mock.mock_calls.clear()
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": eid, "option": "customary"},
        blocking=True,
    )
    await hass.async_block_till_done()

    convert = [q for q in _setter_queries(aioclient_mock) if q["cmd"] == "convert"]
    assert convert == [{"cmd": "convert", "val": "2"}]
    assert coordinator.get_desired(KEY_CONVERSION_MODE) == 2
    assert hass.states.get(eid).state == "customary"


async def test_no_controls_and_no_cmd_when_unmanaged(
    hass, hub_entry_builder, aioclient_mock
):
    """Unmanaged mode registers no controls and issues no /cmd at setup."""
    hub = await _setup_hub(
        hass, hub_entry_builder, options={CONF_MANAGE_SETTINGS: False}
    )
    ent_reg = er.async_get(hass)

    for suffix, platform in _CONTROLS:
        assert (
            ent_reg.async_get_entity_id(
                platform, DOMAIN, f"{hub.entry_id}:hub:{suffix}"
            )
            is None
        ), (platform, suffix)

    # The stubbed connect loop never runs, but to be explicit: no setter /cmd was
    # issued during setup (the coordinator left the server untouched).
    assert _setter_queries(aioclient_mock) == []


# --------------------------------------------------------------------------- #
# 6. Reload-on-toggle vs live-apply.                                          #
# --------------------------------------------------------------------------- #
async def test_toggle_manage_settings_triggers_reload(hass, hub_entry_builder):
    """Flipping CONF_MANAGE_SETTINGS in options reloads the entry."""
    hub = await _setup_hub(hass, hub_entry_builder)  # managed -> True

    with patch.object(
        hass.config_entries, "async_reload", wraps=hass.config_entries.async_reload
    ) as reload:
        hass.config_entries.async_update_entry(
            hub, options={CONF_MANAGE_SETTINGS: False}
        )
        await hass.async_block_till_done()

    reload.assert_called_once_with(hub.entry_id)


async def test_timeout_and_discovery_change_applied_live_no_reload(
    hass, hub_entry_builder
):
    """A timeout / discovery-only options change applies live without reload."""
    hub = await _setup_hub(hass, hub_entry_builder, discovery_enabled=True)
    coordinator = hass.data[DOMAIN][hub.entry_id]

    with patch.object(hass.config_entries, "async_reload") as reload:
        hass.config_entries.async_update_entry(
            hub,
            options={
                CONF_AVAILABILITY_TIMEOUT: 123,
                CONF_DISCOVERY_ENABLED: False,
                # manage_settings unchanged -> no reload.
                CONF_MANAGE_SETTINGS: True,
            },
        )
        await hass.async_block_till_done()

    reload.assert_not_called()
    # The live values were pushed straight into the running coordinator.
    assert coordinator.availability_timeout == 123
    assert coordinator.discovery_enabled is False


# --------------------------------------------------------------------------- #
# 7. Failure isolation + serialization.                                       #
# --------------------------------------------------------------------------- #
def test_write_failure_keeps_desired_value(hass, coordinator, aioclient_mock):
    """A failing setter /cmd during a write leaves the desired value intact."""
    # The setter 500s; the read-back getters still succeed (empty meta).
    aioclient_mock.get(_CMD_URL, params={"cmd": "center_frequency"}, status=500)
    aioclient_mock.get(_CMD_URL, params={"cmd": "get_meta"}, json={"result": {}})
    aioclient_mock.get(_CMD_URL, params={"cmd": "get_gain"}, json={"result": "32.8"})
    aioclient_mock.get(_CMD_URL, params={"cmd": "get_ppm_error"}, json={"result": 2})
    coordinator.connected = True

    with patch(DISPATCH):
        # Must not raise even though the send fails.
        _run(hass, coordinator.set_sdr("center_frequency", 868.0))

    # The desired value + managed flag survive the failed send.
    assert coordinator.get_desired("center_frequency") == 868.0
    assert coordinator.is_managed("center_frequency")


def test_enforcement_failure_keeps_desired_and_event_stream_works(
    hass, coordinator, aioclient_mock
):
    """A failed enforcement keeps desired state; a normal event still processes."""
    aioclient_mock.get(_CMD_URL, status=500)  # every /cmd fails
    coordinator.connected = True
    coordinator._desired = {"center_frequency": 868.0, KEY_GAIN_AUTO: True}
    coordinator._managed = {"center_frequency", KEY_GAIN_AUTO}

    with patch(DISPATCH):
        # Enforcement swallows the failures and never raises.
        _run(hass, coordinator._enforce_all())

    # Desired state is retained despite every send failing.
    assert coordinator.get_desired("center_frequency") == 868.0
    assert coordinator.is_managed(KEY_GAIN_AUTO)

    # A normal device event fed afterwards is still processed (stream undisturbed).
    with patch(DISPATCH) as dispatch:
        coordinator._handle_text_frame(
            '{"model": "Acurite-606TX", "id": 42, "temperature_C": 21.4}'
        )
    assert "Acurite-606TX-42" in coordinator.devices
    dispatch.assert_called_once()


async def test_cmd_issuance_serialized_through_lock(hass, hub_entry_builder):
    """A write and an enforcement replay cannot interleave their /cmd sends.

    ``_send_cmd`` holds ``_cmd_lock`` around the whole HTTP request. We patch the
    session ``get`` to record entry/exit ordering with an await in between; if two
    concurrent ``_send_cmd`` calls interleaved, an entry would appear before the
    prior call's exit. The lock must serialize them.
    """
    entry = hub_entry_builder(availability_timeout=600)
    entry.add_to_hass(hass)
    coordinator = Rtl433Coordinator(hass, entry, host="rtl433.local")
    coordinator.connected = True

    order: list[str] = []

    class _FakeResp:
        async def __aenter__(self):
            # A real suspension point inside the request: without the lock the
            # second _send_cmd would enter here before the first exited.
            await asyncio.sleep(0)
            return self

        async def __aexit__(self, *exc):
            return False

        def raise_for_status(self):
            return None

    def _fake_get(url, params=None, timeout=None):
        cmd = params["cmd"]
        order.append(f"enter:{cmd}")
        return _FakeResp()

    async def _send(cmd: str) -> None:
        await coordinator._send_cmd(cmd, val=1)
        order.append(f"exit:{cmd}")

    session = type("S", (), {"get": staticmethod(_fake_get)})()
    with patch(
        "custom_components.rtl_433.coordinator.base.async_get_clientsession",
        return_value=session,
    ):
        await asyncio.gather(_send("a"), _send("b"))

    # Each command's enter is immediately followed by its own exit: the lock
    # prevented the second enter from landing between the first enter and exit.
    assert order in (
        ["enter:a", "exit:a", "enter:b", "exit:b"],
        ["enter:b", "exit:b", "enter:a", "exit:a"],
    )
    assert coordinator._cmd_lock.locked() is False
