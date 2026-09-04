"""Config-entry round-trip migration tests guarding the compatibility contract.

These tests exercise the *identity-preservation* guarantee of the migration
ladder documented in ``COMPATIBILITY_CONTRACT.md`` — the ABI shared by the full
HACS build and the future minimal Home Assistant Core build. Both builds read and
write the same config entries, entity registry, and device registry, so migration
must never duplicate or orphan a registry object, and must never downgrade an
entry.

Unlike ``test_mut_migration_floor.py`` (which asserts each migration *step* in
isolation), these tests seed a full registry snapshot using the contract's exact
``unique_id`` and device ``identifiers`` templates, run ``async_migrate_entry``
end-to-end, and compare the before/after identity sets:

- **latest entry** (``version=2, minor_version=8``): migration is a no-op that
  preserves every entity and device unchanged;
- **legacy entry** (``version=1``): migration reaches ``version=2,
  minor_version=8`` monotonically (never downgrading) while re-homing — not
  destroying — the pre-existing registry objects;
- **minor 7 entry**: the retired ``discovery_enabled`` toggle is stripped from
  both ``data`` and ``options`` while every adopted device, per-device override
  and calibration survives the upgrade unchanged.

Contract templates encoded here (see COMPATIBILITY_CONTRACT.md §2, §3):

- per-device field entity ``unique_id`` = ``f"{entry_id}:{device_key}:{suffix}"``
- hub control entity ``unique_id`` = ``f"{entry_id}:hub:{suffix}"``
- hub device ``identifiers`` = ``{(DOMAIN, entry_id)}``
- per-device ``identifiers`` = ``{(DOMAIN, f"{entry_id}:{device_key}")}`` with
  ``via_device=(DOMAIN, entry_id)``
"""

from __future__ import annotations

from copy import deepcopy
from unittest.mock import patch

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.rtl_433.const import (
    CALIBRATION_COMMODITY,
    CALIBRATION_SCALE,
    CALIBRATION_UNIT,
    COMMODITY_WATER,
    CONF_AVAILABILITY_TIMEOUT,
    CONF_DEVICE_KEY,
    CONF_DEVICES,
    CONF_ENTRY_TYPE,
    CONF_HOST,
    CONF_HUB_ENTRY_ID,
    CONF_MODEL,
    CONF_PATH,
    CONF_PORT,
    CONF_USER_MAPPINGS,
    DEVICE_CALIBRATION,
    DEVICE_FIELDS,
    DEVICE_MOTION_CLEAR_DELAY,
    DEVICE_TIMEOUT_OVERRIDE,
    DOMAIN,
    ENTRY_TYPE_DEVICE,
    ENTRY_TYPE_HUB,
)
from custom_components.rtl_433.migration import (
    _LAST_SEEN_OBJECT_SUFFIX,
    LEGACY_CONF_OBSERVED_FIELDS,
    async_migrate_entry,
)
from homeassistant.helpers import device_registry as dr, entity_registry as er

# The current declared schema (COMPATIBILITY_CONTRACT.md §1).
CONTRACT_VERSION = 2
CONTRACT_MINOR_VERSION = 8


# ---------------------------------------------------------------------------
# Registry snapshot helpers
# ---------------------------------------------------------------------------


def _entity_identity_set(ent_reg: er.EntityRegistry) -> set[tuple[str, str]]:
    """Return the set of ``(platform_domain, unique_id)`` for rtl_433 entities.

    The pair is the entity's stable identity: two entries can never share it, so a
    growth in this set that is a strict superset (no member dropped) proves nothing
    was orphaned and nothing was re-created under a second unique_id.
    """
    return {
        (entry.domain, entry.unique_id)
        for entry in ent_reg.entities.values()
        if entry.platform == DOMAIN
    }


def _device_identifier_map(dev_reg: dr.DeviceRegistry) -> dict[tuple[str, str], str]:
    """Map each rtl_433 device identifier tuple → the device_id carrying it.

    A dict (not a set) so we can assert that no identifier is duplicated across
    two device_ids after migration (which would split one physical device in two).
    """
    result: dict[tuple[str, str], str] = {}
    for device in dev_reg.devices.values():
        for ident in device.identifiers:
            if ident[0] == DOMAIN:
                # A registry-level invariant already forbids the same identifier on
                # two devices; capturing device_id lets the test assert it anyway.
                result[ident] = device.id
    return result


# ===========================================================================
# Round-trip: an already-latest entry is a no-op that preserves identity
# ===========================================================================


async def test_latest_entry_roundtrip_preserves_registry_identity(hass):
    """A full-schema v2/m7 entry round-trips through migration untouched.

    Seeds the entity + device registries using the contract's exact templates,
    runs ``async_migrate_entry``, and asserts the entry stays at the latest
    version and every seeded entity/device identity is preserved byte-for-byte —
    no duplication, no orphaning.
    """
    device_a = "Acurite-Tower-1234"
    device_b = "LaCrosse-TX141-7"

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="rtl_433 (rtl433.local)",
        version=CONTRACT_VERSION,
        minor_version=CONTRACT_MINOR_VERSION,
        data={
            CONF_HOST: "rtl433.local",
            CONF_PORT: 8433,
            CONF_PATH: "/ws",
            CONF_USER_MAPPINGS: {},
            CONF_DEVICES: {
                device_a: {
                    CONF_MODEL: "Acurite-Tower",
                    DEVICE_FIELDS: ["temperature_C"],
                },
                device_b: {CONF_MODEL: "LaCrosse-TX141", DEVICE_FIELDS: ["humidity"]},
            },
        },
        # A deliberately non-default (non-600) timeout: it must survive untouched.
        options={CONF_AVAILABILITY_TIMEOUT: 300},
    )
    entry.add_to_hass(hass)

    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    eid = entry.entry_id

    # --- Devices: hub + one nested device per device_key (contract §3) ---
    dev_reg.async_get_or_create(config_entry_id=eid, identifiers={(DOMAIN, eid)})
    for device_key in (device_a, device_b):
        dev_reg.async_get_or_create(
            config_entry_id=eid,
            identifiers={(DOMAIN, f"{eid}:{device_key}")},
            via_device=(DOMAIN, eid),
        )

    # --- Entities: per-device field entities (contract §2) ---
    ent_reg.async_get_or_create(
        "sensor", DOMAIN, f"{eid}:{device_a}:temperature_C", config_entry=entry
    )
    ent_reg.async_get_or_create(
        "sensor", DOMAIN, f"{eid}:{device_b}:humidity", config_entry=entry
    )
    ent_reg.async_get_or_create(
        "binary_sensor", DOMAIN, f"{eid}:{device_a}:battery_ok", config_entry=entry
    )
    ent_reg.async_get_or_create(
        "event", DOMAIN, f"{eid}:{device_b}:button", config_entry=entry
    )
    # --- Entities: hub controls + connectivity (contract §2) ---
    ent_reg.async_get_or_create(
        "number", DOMAIN, f"{eid}:hub:frequency", config_entry=entry
    )
    ent_reg.async_get_or_create(
        "binary_sensor", DOMAIN, f"{eid}:hub:connectivity", config_entry=entry
    )

    before_entities = _entity_identity_set(ent_reg)
    before_devices = _device_identifier_map(dev_reg)
    assert len(before_entities) == 6
    assert len(before_devices) == 3  # hub + two nested devices

    result = await async_migrate_entry(hass, entry)

    # Latest-version entry: migration succeeds and does not move the version.
    assert result is True
    assert entry.version == CONTRACT_VERSION
    assert entry.minor_version == CONTRACT_MINOR_VERSION

    after_entities = _entity_identity_set(ent_reg)
    after_devices = _device_identifier_map(dev_reg)

    # No-op: identical identity sets, no duplication, no orphaning.
    assert after_entities == before_entities
    assert set(after_devices) == set(before_devices)
    # No identifier moved to a different device_id.
    assert after_devices == before_devices
    # The custom option is preserved (600 would be stripped; 300 must not be).
    assert entry.options.get(CONF_AVAILABILITY_TIMEOUT) == 300


# ===========================================================================
# Round-trip: a legacy (v1) entry migrates up to 2/7 monotonically without loss
# ===========================================================================


async def test_v1_entry_migrates_to_latest_without_downgrade_or_registry_loss(hass):
    """A legacy v1 hub (with a v1 child device entry) migrates to v2/m7.

    Asserts the terminal schema is exactly ``version=2, minor_version=8``, that
    the version never decreases along the path (monotonic, non-downgrading), and
    that every pre-existing registry device/entity survives — re-homed onto the
    hub, never destroyed or duplicated.
    """
    hub_id = "hub-entry-legacy"
    child_id = "child-entry-legacy"
    device_key = "Acurite-Tower-9001"

    hub = MockConfigEntry(
        domain=DOMAIN,
        title="rtl_433 (legacy.local)",
        version=1,
        entry_id=hub_id,
        data={
            CONF_HOST: "legacy.local",
            CONF_PORT: 8433,
            CONF_PATH: "/ws",
            CONF_ENTRY_TYPE: ENTRY_TYPE_HUB,
        },
    )
    child = MockConfigEntry(
        domain=DOMAIN,
        title="Acurite-Tower 9001",
        version=1,
        entry_id=child_id,
        data={
            CONF_ENTRY_TYPE: ENTRY_TYPE_DEVICE,
            CONF_HUB_ENTRY_ID: hub_id,
            CONF_DEVICE_KEY: device_key,
            CONF_MODEL: "Acurite-Tower",
        },
        options={LEGACY_CONF_OBSERVED_FIELDS: ["temperature_C", "humidity"]},
    )
    hub.add_to_hass(hass)
    child.add_to_hass(hass)

    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)

    # Hub device is owned by the hub entry; nested device + its entities are owned
    # by the *child* entry, exactly as the 0.1.0 per-device model created them.
    dev_reg.async_get_or_create(config_entry_id=hub_id, identifiers={(DOMAIN, hub_id)})
    nested = dev_reg.async_get_or_create(
        config_entry_id=child_id,
        identifiers={(DOMAIN, f"{hub_id}:{device_key}")},
        via_device=(DOMAIN, hub_id),
    )

    temp_ent = ent_reg.async_get_or_create(
        "sensor", DOMAIN, f"{hub_id}:{device_key}:temperature_C", config_entry=child
    )
    hum_ent = ent_reg.async_get_or_create(
        "sensor", DOMAIN, f"{hub_id}:{device_key}:humidity", config_entry=child
    )
    # A "Last seen" sensor: minor 3 disables it (identity preserved, not removed).
    last_seen_ent = ent_reg.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{hub_id}:{device_key}:{_LAST_SEEN_OBJECT_SUFFIX}",
        config_entry=child,
    )

    before_entities = _entity_identity_set(ent_reg)
    before_devices = _device_identifier_map(dev_reg)
    assert len(before_entities) == 3
    assert len(before_devices) == 2  # hub + nested device

    # Record every (version, minor_version) the migration writes, to prove the
    # schema is monotonic and never downgrades.
    seen_versions: list[tuple[int, int]] = [(hub.version, hub.minor_version or 1)]
    original_update = hass.config_entries.async_update_entry

    def _record_update(target_entry, **kwargs):
        res = original_update(target_entry, **kwargs)
        if target_entry.entry_id == hub_id:
            seen_versions.append(
                (target_entry.version, target_entry.minor_version or 1)
            )
        return res

    with (
        patch(
            "custom_components.rtl_433.migration._read_legacy_overrides",
            return_value={},
        ),
        patch.object(
            hass.config_entries, "async_update_entry", side_effect=_record_update
        ),
    ):
        result = await async_migrate_entry(hass, hub)

    # Terminal schema is exactly the frozen contract version.
    assert result is True
    assert hub.version == CONTRACT_VERSION
    assert hub.minor_version == CONTRACT_MINOR_VERSION

    # Monotonic, non-downgrading: each recorded (version, minor) >= its predecessor
    # and nothing ever dropped below the starting version 1.
    for earlier, later in zip(seen_versions, seen_versions[1:], strict=False):
        assert later >= earlier, f"schema downgraded: {earlier} -> {later}"
    assert all(v >= 1 for v, _ in seen_versions)

    after_entities = _entity_identity_set(ent_reg)
    after_devices = _device_identifier_map(dev_reg)

    # No pre-existing identity destroyed: the after-set is a superset of before.
    assert before_entities <= after_entities
    assert set(before_devices) <= set(after_devices)
    # No identifier split across two devices (no duplicated identity).
    assert len(after_devices) == len(set(after_devices))

    # The nested device kept its identifier tuple and was re-homed onto the hub
    # (owned by the hub entry now, no longer by the removed child).
    rehomed = dev_reg.async_get_device(identifiers={(DOMAIN, f"{hub_id}:{device_key}")})
    assert rehomed is not None
    assert rehomed.id == nested.id  # same physical device, not a duplicate
    assert hub_id in rehomed.config_entries
    assert child_id not in rehomed.config_entries

    # Field entities survived and were re-homed onto the hub.
    for ent in (temp_ent, hum_ent, last_seen_ent):
        moved = ent_reg.async_get(ent.entity_id)
        assert moved is not None
        assert moved.config_entry_id == hub_id

    # The "Last seen" sensor is disabled by the integration (minor 3), not deleted —
    # its identity is intact even though it is now disabled-by-default.
    assert (
        ent_reg.async_get(last_seen_ent.entity_id).disabled_by
        is er.RegistryEntryDisabler.INTEGRATION
    )

    # The legacy child config entry was consolidated away.
    assert hass.config_entries.async_get_entry(child_id) is None


# ===========================================================================
# Minor 7 -> 8: the retired discovery toggle is stripped, devices are not
# ===========================================================================

# The retired per-hub toggle, spelled as a literal for the same reason the
# migration does: the constant is gone, but entries written by older versions
# still carry the string, and this test is what proves they stop carrying it.
RETIRED_DISCOVERY_KEY = "discovery_enabled"


def _upgraded_devices_map() -> dict[str, dict]:
    """Three adopted devices carrying the settings a real upgrade would hold.

    Deliberately not a minimal map: a timeout override, a motion clear delay and
    a utility-meter calibration between them, because those are the per-device
    settings a careless migration would flatten, and the equality assertion they
    feed is the one that stands between an upgrade and every current user's
    configuration.
    """
    return {
        "Acurite-Tower-1234": {
            CONF_MODEL: "Acurite-Tower",
            DEVICE_FIELDS: ["humidity", "temperature_C"],
            DEVICE_TIMEOUT_OVERRIDE: 1800,
        },
        "GenericDoor-X1-88": {
            CONF_MODEL: "GenericDoor-X1",
            DEVICE_FIELDS: ["closed"],
            DEVICE_MOTION_CLEAR_DELAY: 45,
        },
        "WaterMeter-3000-77": {
            CONF_MODEL: "WaterMeter-3000",
            DEVICE_FIELDS: ["consumption_data"],
            DEVICE_CALIBRATION: {
                CALIBRATION_COMMODITY: COMMODITY_WATER,
                CALIBRATION_UNIT: "L",
                CALIBRATION_SCALE: 0.1,
            },
        },
    }


def _minor_7_entry(hass, *, devices, with_toggle: bool) -> MockConfigEntry:
    """Build (and register) a pre-upgrade hub entry at version 2, minor 7."""
    data = {
        CONF_HOST: "rtl433.local",
        CONF_PORT: 8433,
        CONF_PATH: "/ws",
        CONF_USER_MAPPINGS: {},
        CONF_DEVICES: devices,
    }
    options = {CONF_AVAILABILITY_TIMEOUT: 300}
    if with_toggle:
        # A real upgrade carries the key in *both* mappings: the add flows wrote
        # it into ``data`` and the hub options step wrote it into ``options``.
        data[RETIRED_DISCOVERY_KEY] = True
        options[RETIRED_DISCOVERY_KEY] = False
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="rtl_433 (rtl433.local)",
        version=2,
        minor_version=7,
        data=data,
        options=options,
    )
    entry.add_to_hass(hass)
    return entry


async def test_minor_7_upgrade_strips_the_toggle_and_preserves_every_device(hass):
    """The toggle goes from ``data`` and ``options``; nothing else moves.

    Discovery stopped being a toggle — every heard device now waits for an
    explicit add — so a value left behind would show up in diagnostics and
    config-entry exports as though it still meant something. The removal has to
    be exactly that narrow, though: the devices map is every current user's
    adopted devices, per-device overrides and calibrations, and the equality
    assertion below is what proves the upgrade does not disturb it.
    """
    devices = _upgraded_devices_map()
    before = deepcopy(devices)
    entry = _minor_7_entry(hass, devices=devices, with_toggle=True)

    assert await async_migrate_entry(hass, entry) is True

    assert entry.version == CONTRACT_VERSION
    assert entry.minor_version == CONTRACT_MINOR_VERSION
    assert RETIRED_DISCOVERY_KEY not in entry.data
    assert RETIRED_DISCOVERY_KEY not in entry.options
    # Every adopted device, override and calibration survives byte-for-byte.
    assert entry.data[CONF_DEVICES] == before
    # And the strip is narrow: the neighbouring keys are untouched.
    assert entry.data[CONF_HOST] == "rtl433.local"
    assert entry.data[CONF_USER_MAPPINGS] == {}
    assert entry.options == {CONF_AVAILABILITY_TIMEOUT: 300}


async def test_toggle_strip_is_idempotent_and_writes_nothing_when_absent(hass):
    """An entry with no toggle is bumped, not rewritten; a re-run does nothing.

    The strip compares before it writes, which matters twice over: an entry that
    never carried the key must not be rewritten (a needless write fires the
    update listener, and on a loaded hub that is a chance to reload for nothing),
    and re-running migration against an already-migrated entry — which happens on
    every restart — must be a complete no-op.
    """
    devices = _upgraded_devices_map()
    before = deepcopy(devices)
    entry = _minor_7_entry(hass, devices=devices, with_toggle=False)

    updates: list[dict] = []
    original = hass.config_entries.async_update_entry

    def _capture(target_entry, **kwargs):
        updates.append(kwargs)
        return original(target_entry, **kwargs)

    with patch.object(hass.config_entries, "async_update_entry", side_effect=_capture):
        assert await async_migrate_entry(hass, entry) is True
        # The only write is the version bump: no data / options rewrite.
        assert updates == [{"version": 2, "minor_version": CONTRACT_MINOR_VERSION}]

        # Re-running against the migrated entry writes nothing at all.
        updates.clear()
        assert await async_migrate_entry(hass, entry) is True
        assert updates == []

    assert entry.version == CONTRACT_VERSION
    assert entry.minor_version == CONTRACT_MINOR_VERSION
    assert entry.data[CONF_DEVICES] == before
    assert entry.options == {CONF_AVAILABILITY_TIMEOUT: 300}
