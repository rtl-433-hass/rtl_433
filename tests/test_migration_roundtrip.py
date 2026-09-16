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

- **latest entry** (``version=3, minor_version=1``): migration is a no-op that
  preserves every entity and device unchanged;
- **legacy entry** (``version=1``): migration reaches ``version=3,
  minor_version=1`` monotonically (never downgrading) while re-homing — not
  destroying — the pre-existing registry objects;
- **minor 7 entry**: the retired ``discovery_enabled`` toggle is stripped from
  both ``data`` and ``options`` while every adopted device, per-device override
  and calibration survives the upgrade unchanged;
- **v2 (minor 8) entry**: the v2 → v3 conversion gives it the receiver subentry
  it now needs to load, re-keys the ``:hub:`` controls and the per-receiver link
  fields with every ``entity_id`` preserved, and leaves every other identity
  byte-identical; two separate v2 entries stay two separate locations.

Contract templates encoded here (see COMPATIBILITY_CONTRACT.md §2, §3):

- per-device field entity ``unique_id`` = ``f"{entry_id}:{device_key}:{suffix}"``
  (v2 and v3 alike — the union is receiver-agnostic, so this one never moved)
- per-receiver link field ``unique_id`` = v2
  ``f"{entry_id}:{device_key}:{suffix}"`` → v3
  ``f"{entry_id}:{device_key}:{receiver_id}:{suffix}"``
- receiver control entity ``unique_id`` = v2 ``f"{entry_id}:hub:{suffix}"`` → v3
  ``f"{entry_id}:receiver:{receiver_id}:{suffix}"``
- location device ``identifiers`` = ``{(DOMAIN, entry_id)}`` — byte-identical to
  the v2 receiver ("hub") device identifier
- per-device ``identifiers`` = ``{(DOMAIN, f"{entry_id}:{device_key}")}`` with
  ``via_device_id`` set to the location device's id
"""

from __future__ import annotations

from copy import deepcopy
from unittest.mock import patch

import pytest
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
    CONF_INITIAL_FREQUENCY,
    CONF_MANAGE_SETTINGS,
    CONF_MODEL,
    CONF_PATH,
    CONF_PORT,
    CONF_RECEIVER_ENTRY_ID,
    CONF_USER_MAPPINGS,
    DEVICE_CALIBRATION,
    DEVICE_FIELDS,
    DEVICE_MOTION_CLEAR_DELAY,
    DEVICE_TIMEOUT_OVERRIDE,
    DOMAIN,
    ENTRY_TYPE_DEVICE,
    ENTRY_TYPE_RECEIVER,
    receiver_identity,
)
from custom_components.rtl_433.migration import (
    _LAST_SEEN_OBJECT_SUFFIX,
    LEGACY_CONF_OBSERVED_FIELDS,
    _rehome_device_objects,
    async_migrate_entry,
)
from custom_components.rtl_433.receiver_settings import receiver_subentries
from homeassistant.helpers import (
    device_registry as dr,
    entity_registry as er,
    issue_registry as ir,
)

from .conftest import build_receiver_entry, receiver_id as first_receiver_id

# The current declared schema (COMPATIBILITY_CONTRACT.md §1).
CONTRACT_VERSION = 3
CONTRACT_MINOR_VERSION = 1

# The schema every pre-upgrade install is at: the last v2 minor version.
V2_VERSION = 2
V2_MINOR_VERSION = 8


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
    for device in dev_reg.devices:
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
    """A full-schema v3 location round-trips through migration untouched.

    Seeds the entity + device registries using the contract's exact templates,
    runs ``async_migrate_entry``, and asserts the entry stays at the latest
    version and every seeded entity/device identity is preserved byte-for-byte —
    no duplication, no orphaning. This is the every-restart path: Home Assistant
    calls the migration before every setup, so "already current" has to be a
    complete no-op.
    """
    device_a = "Acurite-Tower-1234"
    device_b = "LaCrosse-TX141-7"

    entry = build_receiver_entry(
        devices={
            device_a: {
                CONF_MODEL: "Acurite-Tower",
                DEVICE_FIELDS: ["temperature_C"],
            },
            device_b: {CONF_MODEL: "LaCrosse-TX141", DEVICE_FIELDS: ["humidity"]},
        },
        # A deliberately non-default (non-600) timeout: it must survive untouched.
        options={CONF_AVAILABILITY_TIMEOUT: 300},
        version=CONTRACT_VERSION,
        minor_version=CONTRACT_MINOR_VERSION,
    )
    entry.add_to_hass(hass)

    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    eid = entry.entry_id
    rid = first_receiver_id(entry)

    # --- Devices: location + one nested device per device_key (contract §3) ---
    location_device = dev_reg.async_get_or_create(
        config_entry_id=eid, identifiers={(DOMAIN, eid)}
    )
    for device_key in (device_a, device_b):
        dev_reg.async_get_or_create(
            config_entry_id=eid,
            identifiers={(DOMAIN, f"{eid}:{device_key}")},
            via_device_id=location_device.id,
        )

    # --- Entities: unioned per-device field entities (contract §2) ---
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
    # --- Entities: a per-receiver link field (four segments, contract §2) ---
    ent_reg.async_get_or_create(
        "sensor", DOMAIN, f"{eid}:{device_a}:{rid}:rssi", config_entry=entry
    )
    # --- Entities: receiver controls + connectivity (contract §2) ---
    ent_reg.async_get_or_create(
        "number",
        DOMAIN,
        f"{receiver_identity(eid, rid)}:frequency",
        config_entry=entry,
    )
    ent_reg.async_get_or_create(
        "binary_sensor",
        DOMAIN,
        f"{receiver_identity(eid, rid)}:connectivity",
        config_entry=entry,
    )

    before_entities = _entity_identity_set(ent_reg)
    before_devices = _device_identifier_map(dev_reg)
    assert len(before_entities) == 7
    assert len(before_devices) == 3  # location + two nested devices

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
    """A legacy v1 receiver (with a v1 child device entry) migrates to v3.

    Asserts the terminal schema is exactly ``version=3, minor_version=1``, that
    the version never decreases along the path (monotonic, non-downgrading), and
    that every pre-existing registry device/entity survives — re-homed onto the
    location and, for the one family whose template moved, re-keyed in place —
    never destroyed, never duplicated, and never given a fresh ``entity_id``.
    """
    receiver_id = "receiver-entry-legacy"
    child_id = "child-entry-legacy"
    device_key = "Acurite-Tower-9001"

    receiver = MockConfigEntry(
        domain=DOMAIN,
        title="rtl_433 (legacy.local)",
        version=1,
        entry_id=receiver_id,
        data={
            CONF_HOST: "legacy.local",
            CONF_PORT: 8433,
            CONF_PATH: "/ws",
            CONF_ENTRY_TYPE: ENTRY_TYPE_RECEIVER,
        },
    )
    child = MockConfigEntry(
        domain=DOMAIN,
        title="Acurite-Tower 9001",
        version=1,
        entry_id=child_id,
        data={
            CONF_ENTRY_TYPE: ENTRY_TYPE_DEVICE,
            CONF_RECEIVER_ENTRY_ID: receiver_id,
            CONF_DEVICE_KEY: device_key,
            CONF_MODEL: "Acurite-Tower",
        },
        options={LEGACY_CONF_OBSERVED_FIELDS: ["temperature_C", "humidity"]},
    )
    receiver.add_to_hass(hass)
    child.add_to_hass(hass)

    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)

    # Receiver device is owned by the receiver entry; nested device + its entities are owned
    # by the *child* entry, exactly as the 0.1.0 per-device model created them.
    receiver_device = dev_reg.async_get_or_create(
        config_entry_id=receiver_id, identifiers={(DOMAIN, receiver_id)}
    )
    nested = dev_reg.async_get_or_create(
        config_entry_id=child_id,
        identifiers={(DOMAIN, f"{receiver_id}:{device_key}")},
        via_device_id=receiver_device.id,
    )

    temp_ent = ent_reg.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{receiver_id}:{device_key}:temperature_C",
        config_entry=child,
    )
    hum_ent = ent_reg.async_get_or_create(
        "sensor", DOMAIN, f"{receiver_id}:{device_key}:humidity", config_entry=child
    )
    # A "Last seen" sensor: minor 3 disables it (identity preserved, not removed).
    last_seen_ent = ent_reg.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{receiver_id}:{device_key}:{_LAST_SEEN_OBJECT_SUFFIX}",
        config_entry=child,
    )

    before_entities = _entity_identity_set(ent_reg)
    before_devices = _device_identifier_map(dev_reg)
    assert len(before_entities) == 3
    assert len(before_devices) == 2  # receiver + nested device

    # Record every (version, minor_version) the migration writes, to prove the
    # schema is monotonic and never downgrades.
    seen_versions: list[tuple[int, int]] = [
        (receiver.version, receiver.minor_version or 1)
    ]
    original_update = hass.config_entries.async_update_entry

    def _record_update(target_entry, **kwargs):
        res = original_update(target_entry, **kwargs)
        if target_entry.entry_id == receiver_id:
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
        result = await async_migrate_entry(hass, receiver)

    # Terminal schema is exactly the frozen contract version.
    assert result is True
    assert receiver.version == CONTRACT_VERSION
    assert receiver.minor_version == CONTRACT_MINOR_VERSION

    # Monotonic, non-downgrading: each recorded (version, minor) >= its predecessor
    # and nothing ever dropped below the starting version 1.
    for earlier, later in zip(seen_versions, seen_versions[1:], strict=False):
        assert later >= earlier, f"schema downgraded: {earlier} -> {later}"
    assert all(v >= 1 for v, _ in seen_versions)

    after_entities = _entity_identity_set(ent_reg)
    after_devices = _device_identifier_map(dev_reg)

    # "Last seen" is a per-receiver link field now, so its unique_id gained a
    # receiver segment; everything else is byte-identical. Nothing was destroyed:
    # the after-set covers every before-identity except the one that was
    # deliberately re-keyed, and the re-keyed row is the same registry row.
    receiver_subentry_id = first_receiver_id(receiver)
    relocated = ("sensor", f"{receiver_id}:{device_key}:{_LAST_SEEN_OBJECT_SUFFIX}")
    assert before_entities - after_entities == {relocated}
    assert (
        "sensor",
        f"{receiver_id}:{device_key}:{receiver_subentry_id}:{_LAST_SEEN_OBJECT_SUFFIX}",
    ) in after_entities
    assert set(before_devices) <= set(after_devices)
    # No identifier split across two devices (no duplicated identity).
    assert len(after_devices) == len(set(after_devices))

    # The nested device kept its identifier tuple and was re-homed onto the receiver
    # (owned by the receiver entry now, no longer by the removed child).
    rehomed = dev_reg.async_get_device_by_identifier(
        (DOMAIN, f"{receiver_id}:{device_key}"), receiver_id
    )
    assert rehomed is not None
    assert rehomed.id == nested.id  # same physical device, not a duplicate
    assert rehomed.config_entry_id == receiver_id

    # Field entities survived as the *same registry rows* -- same registry id,
    # same entity_id -- and were re-homed onto the location.
    for ent in (temp_ent, hum_ent, last_seen_ent):
        moved = ent_reg.async_get(ent.entity_id)
        assert moved is not None
        assert moved.id == ent.id
        assert moved.config_entry_id == receiver_id

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

# The retired per-receiver toggle, spelled as a literal for the same reason the
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
    """Build (and register) a pre-upgrade receiver entry at version 2, minor 7."""
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
        # it into ``data`` and the receiver options step wrote it into ``options``.
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

    Discovery stopped being a toggle — every received device now waits for an
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
    # And the strip is narrow: the neighbouring sensor-side keys are untouched.
    assert entry.data[CONF_USER_MAPPINGS] == {}
    assert entry.options == {CONF_AVAILABILITY_TIMEOUT: 300}
    # The server-side keys are not gone, they moved: the v3 step hands them to
    # the receiver subentry that makes this entry a loadable location.
    assert CONF_HOST not in entry.data
    subentries = receiver_subentries(entry)
    assert len(subentries) == 1
    assert subentries[0].data[CONF_HOST] == "rtl433.local"
    assert subentries[0].data[CONF_PORT] == 8433
    assert subentries[0].data[CONF_PATH] == "/ws"


async def test_toggle_strip_is_idempotent_and_writes_nothing_when_absent(hass):
    """An entry with no toggle is bumped, not rewritten; a re-run does nothing.

    The strip compares before it writes, which matters twice over: an entry that
    never carried the key must not be rewritten (a needless write fires the
    update listener, and on a loaded receiver that is a chance to reload for nothing),
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
        # Two writes, both of them a version bump: the minor-8 one carries no
        # data / options rewrite because the toggle was never there, and the v3
        # one carries the connection keys leaving for the receiver subentry.
        assert updates == [
            {"version": 2, "minor_version": V2_MINOR_VERSION},
            {
                "data": {CONF_USER_MAPPINGS: {}, CONF_DEVICES: before},
                "unique_id": None,
                "version": CONTRACT_VERSION,
                "minor_version": CONTRACT_MINOR_VERSION,
            },
        ]

        # Re-running against the migrated entry writes nothing at all.
        updates.clear()
        assert await async_migrate_entry(hass, entry) is True
        assert updates == []

    assert entry.version == CONTRACT_VERSION
    assert entry.minor_version == CONTRACT_MINOR_VERSION
    assert entry.data[CONF_DEVICES] == before
    assert entry.options == {CONF_AVAILABILITY_TIMEOUT: 300}
    # A second receiver subentry was not minted by the re-run.
    assert len(receiver_subentries(entry)) == 1


# ===========================================================================
# v2 (minor 8) -> v3: the conversion every existing install goes through
# ===========================================================================

# One of each family a v2 install could have on its receiver ("hub") device: a
# radio control from each control platform, a noise diagnostic sensor, and the
# connectivity binary sensor. All four were keyed ``f"{entry_id}:hub:{suffix}"``,
# which a location holding two receivers would make collide -- hence the re-key.
_HUB_ENTITIES = (
    ("number", "center_frequency"),
    ("select", "hop_interval"),
    ("switch", "report_meta"),
    ("sensor", "noise_level"),
    ("binary_sensor", "connectivity"),
)

# The three link fields, whose template gained a receiver segment in v3.
_LINK_SUFFIXES = ("rssi", "snr", _LAST_SEEN_OBJECT_SUFFIX)

# The unioned device fields, whose template did not move at all.
_UNIONED = (("sensor", "temperature_C"), ("binary_sensor", "battery_ok"))


def _v2_entry(hass, *, entry_id=None, host="rtl433.local", device_key, minor=8):
    """Build (and register) a pre-upgrade v2 receiver entry at ``minor``."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=f"rtl_433 ({host})",
        version=V2_VERSION,
        minor_version=minor,
        unique_id=f"hub:{host}:8433",
        data={
            CONF_HOST: host,
            CONF_PORT: 8433,
            CONF_PATH: "/ws",
            "secure": True,
            CONF_MANAGE_SETTINGS: True,
            CONF_INITIAL_FREQUENCY: 433.92,
            CONF_USER_MAPPINGS: {},
            CONF_DEVICES: {
                device_key: {
                    CONF_MODEL: "Acurite-Tower",
                    DEVICE_FIELDS: ["temperature_C", "battery_ok", "rssi", "snr"],
                }
            },
        },
        **({"entry_id": entry_id} if entry_id else {}),
    )
    entry.add_to_hass(hass)
    return entry


def _seed_v2_registry(hass, entry, device_key):
    """Seed the registries exactly as a v2 install left them.

    Returns ``{unique_id: entity_id}`` for every seeded row, which is the pair the
    upgrade has to keep intact: the ``unique_id`` may be re-keyed, but the
    ``entity_id`` is what recorder history hangs off and must never move.
    """
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    eid = entry.entry_id

    hub_device = dev_reg.async_get_or_create(
        config_entry_id=eid, identifiers={(DOMAIN, eid)}
    )
    dev_reg.async_get_or_create(
        config_entry_id=eid,
        identifiers={(DOMAIN, f"{eid}:{device_key}")},
        via_device_id=hub_device.id,
    )

    seeded: dict[str, str] = {}
    # Deliberately first: a row both v3 sweeps must leave alone, registered ahead
    # of the rows they must rewrite. A sweep that stopped at its first skip
    # instead of continuing past it would then do nothing at all, where the
    # reverse order would hide the bug completely.
    for domain, suffix in _UNIONED:
        unique_id = f"{eid}:{device_key}:{suffix}"
        seeded[unique_id] = ent_reg.async_get_or_create(
            domain, DOMAIN, unique_id, config_entry=entry
        ).entity_id
    for domain, suffix in _HUB_ENTITIES:
        unique_id = f"{eid}:hub:{suffix}"
        seeded[unique_id] = ent_reg.async_get_or_create(
            domain, DOMAIN, unique_id, config_entry=entry, device_id=hub_device.id
        ).entity_id
    for suffix in _LINK_SUFFIXES:
        unique_id = f"{eid}:{device_key}:{suffix}"
        seeded[unique_id] = ent_reg.async_get_or_create(
            "sensor", DOMAIN, unique_id, config_entry=entry
        ).entity_id
    return seeded


async def test_v2_install_upgrades_with_every_entity_id_preserved(hass):
    """A v2 install becomes a location, re-keyed where it must be and nowhere else.

    This is the upgrade every current user runs. It asserts three things at once:
    the entry can load again (it has a receiver subentry), the two families whose
    template gained a receiver segment are re-keyed to the new form, and every
    other identity — the unioned device fields, both device-registry identifiers
    and the ``via_device`` link — is byte-identical to what v2 wrote. Through all
    of it not one ``entity_id`` moves, because a moved ``entity_id`` is a lost
    recorder history.
    """
    device_key = "Acurite-Tower-1234"
    entry = _v2_entry(hass, device_key=device_key)
    seeded = _seed_v2_registry(hass, entry, device_key)
    eid = entry.entry_id

    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    before_devices = _device_identifier_map(dev_reg)

    assert await async_migrate_entry(hass, entry) is True

    assert (entry.version, entry.minor_version) == (
        CONTRACT_VERSION,
        CONTRACT_MINOR_VERSION,
    )
    # The entry is a location holding exactly one receiver, which is what makes
    # it loadable at all: setup refuses a location with no receiver.
    subentries = receiver_subentries(entry)
    assert len(subentries) == 1
    receiver = subentries[0]
    rid = receiver.subentry_id
    # The server's identity moved to the receiver; the location has none.
    assert receiver.unique_id == "hub:rtl433.local:8433"
    assert receiver.title == entry.title
    assert entry.unique_id is None
    # Every key that describes the *server* moved, and none of them was left
    # behind on the location -- a stale copy would be a second source of truth
    # for where this receiver dials.
    assert dict(receiver.data) == {
        CONF_HOST: "rtl433.local",
        CONF_PORT: 8433,
        CONF_PATH: "/ws",
        "secure": True,
        CONF_MANAGE_SETTINGS: True,
        CONF_INITIAL_FREQUENCY: 433.92,
    }
    assert set(entry.data) == {CONF_USER_MAPPINGS, CONF_DEVICES}

    def entity_id_for(unique_id: str, domain: str) -> str:
        found = ent_reg.async_get_entity_id(domain, DOMAIN, unique_id)
        assert found is not None, f"no entity for {unique_id}"
        return found

    # 1. The receiver-owned families are re-keyed, entity_id untouched.
    for domain, suffix in _HUB_ENTITIES:
        old = f"{eid}:hub:{suffix}"
        new = f"{receiver_identity(eid, rid)}:{suffix}"
        assert ent_reg.async_get_entity_id(domain, DOMAIN, old) is None
        assert entity_id_for(new, domain) == seeded[old]

    # 2. The link fields gain the receiver segment, entity_id untouched.
    for suffix in _LINK_SUFFIXES:
        old = f"{eid}:{device_key}:{suffix}"
        new = f"{eid}:{device_key}:{rid}:{suffix}"
        assert ent_reg.async_get_entity_id("sensor", DOMAIN, old) is None
        assert entity_id_for(new, "sensor") == seeded[old]

    # 3. The unioned device fields are byte-identical — not re-keyed at all.
    for domain, suffix in _UNIONED:
        unchanged = f"{eid}:{device_key}:{suffix}"
        assert entity_id_for(unchanged, domain) == seeded[unchanged]

    # 4. Both device identifiers are byte-identical, still on the same rows: the
    # v2 hub device identifier *is* the v3 location device identifier. The
    # ``via_device`` link rides along unchanged, because it resolves that same
    # identifier.
    assert _device_identifier_map(dev_reg) == before_devices
    nested = dev_reg.async_get_device_by_identifier(
        (DOMAIN, f"{eid}:{device_key}"), eid
    )
    location = dev_reg.async_get_device_by_identifier((DOMAIN, eid), eid)
    assert nested is not None and location is not None
    assert nested.via_device_id == location.id

    # 5. The adopted devices map is untouched by the conversion.
    assert set(entry.data[CONF_DEVICES]) == {device_key}


async def test_two_v2_entries_become_two_locations_and_never_merge(hass):
    """Two v2 entries hearing one sensor stay two locations, with no history lost.

    The integration cannot know whether a user's two entries are two servers in
    one house or two houses, so the upgrade never folds them together: folding
    would force two recorder histories onto one ``unique_id`` and silently
    discard one. Each entry keeps its ``entry_id``, which is what keeps both
    copies of every device-scoped identity valid and distinct.
    """
    device_key = "Acurite-Tower-1234"
    attic = _v2_entry(
        hass, entry_id="atticlocation01", host="attic.local", device_key=device_key
    )
    garage = _v2_entry(
        hass, entry_id="garagelocation1", host="garage.local", device_key=device_key
    )
    seeded = {
        entry.entry_id: _seed_v2_registry(hass, entry, device_key)
        for entry in (attic, garage)
    }

    ent_reg = er.async_get(hass)
    for entry in (attic, garage):
        assert await async_migrate_entry(hass, entry) is True

    # Still two entries, each its own location with its own single receiver.
    assert {e.entry_id for e in hass.config_entries.async_entries(DOMAIN)} == {
        attic.entry_id,
        garage.entry_id,
    }
    assert [len(receiver_subentries(e)) for e in (attic, garage)] == [1, 1]

    # Every seeded row survives, under its own location, with its entity_id
    # intact -- nothing was dropped as a "duplicate" of the other location's.
    for entry in (attic, garage):
        for entity_id in seeded[entry.entry_id].values():
            row = ent_reg.async_get(entity_id)
            assert row is not None, f"{entity_id} was lost to a merge"
            assert row.config_entry_id == entry.entry_id

    # And no Repairs notice: nothing was merged, so nothing was dropped.
    issue_reg = ir.async_get(hass)
    assert not [issue for issue in issue_reg.issues.values() if issue.domain == DOMAIN]


@pytest.mark.parametrize("minor", [1, 2, 3, 4, 5, 6, 7, 8])
async def test_every_v2_minor_version_converges_on_the_same_v3_state(hass, minor):
    """Entering the ladder at any v2 minor version reaches the identical v3 state.

    A user upgrading from an old release enters at a low minor version and walks
    every step; one upgrading from the last release enters at 8 and walks one.
    Both must land on the same schema, the same subentry topology and the same
    re-keyed identities — otherwise the state an install ends up in depends on
    when it was last updated.
    """
    device_key = "Acurite-Tower-1234"
    entry = _v2_entry(hass, device_key=device_key, minor=minor)
    seeded = _seed_v2_registry(hass, entry, device_key)
    eid = entry.entry_id

    with patch(
        "custom_components.rtl_433.migration._read_legacy_overrides", return_value={}
    ):
        assert await async_migrate_entry(hass, entry) is True

    assert (entry.version, entry.minor_version) == (
        CONTRACT_VERSION,
        CONTRACT_MINOR_VERSION,
    )
    rid = first_receiver_id(entry)
    ent_reg = er.async_get(hass)

    # The same identity set, whichever rung the entry started on.
    assert _entity_identity_set(ent_reg) == (
        {
            (domain, f"{receiver_identity(eid, rid)}:{suffix}")
            for domain, suffix in _HUB_ENTITIES
        }
        | {
            ("sensor", f"{eid}:{device_key}:{rid}:{suffix}")
            for suffix in _LINK_SUFFIXES
        }
        | {(domain, f"{eid}:{device_key}:{suffix}") for domain, suffix in _UNIONED}
    )
    # And the same rows: no entity_id moved on any path through the ladder.
    for unique_id, entity_id in seeded.items():
        assert ent_reg.async_get(entity_id) is not None, f"{unique_id} lost"
    assert entry.data[CONF_USER_MAPPINGS] == {}


async def test_the_v3_step_is_idempotent_over_the_registry(hass):
    """Re-running the migration re-keys nothing and mints no second receiver.

    Home Assistant runs the migration before every setup, so "already migrated"
    is the common case, not an edge one. A second pass must not find its own
    output and re-key it again — which is what the ``receiver`` marker and the
    four-segment link ids buy.
    """
    device_key = "Acurite-Tower-1234"
    entry = _v2_entry(hass, device_key=device_key)
    _seed_v2_registry(hass, entry, device_key)

    assert await async_migrate_entry(hass, entry) is True
    ent_reg = er.async_get(hass)
    after_first = _entity_identity_set(ent_reg)
    receiver_after_first = first_receiver_id(entry)

    assert await async_migrate_entry(hass, entry) is True

    assert _entity_identity_set(ent_reg) == after_first
    assert len(receiver_subentries(entry)) == 1
    assert first_receiver_id(entry) == receiver_after_first
    assert (entry.version, entry.minor_version) == (
        CONTRACT_VERSION,
        CONTRACT_MINOR_VERSION,
    )


async def test_a_device_literally_keyed_hub_is_not_mistaken_for_the_controls(
    hass, caplog
):
    """``hub`` was never a reserved device_key, so its fields must not be re-keyed.

    A v2 device whose ``device_key`` is literally ``hub`` mints
    ``f"{entry_id}:hub:{suffix}"`` — indistinguishable from a receiver control.
    Re-keying those would move a user's sensor entities onto the receiver device
    under a receiver-scoped id, so the sweep stands down instead, using the
    adopted-devices map to tell the two apart.
    """
    entry = _v2_entry(hass, device_key="hub")
    ent_reg = er.async_get(hass)
    unique_id = f"{entry.entry_id}:hub:temperature_C"
    entity_id = ent_reg.async_get_or_create(
        "sensor", DOMAIN, unique_id, config_entry=entry
    ).entity_id

    assert await async_migrate_entry(hass, entry) is True

    assert ent_reg.async_get_entity_id("sensor", DOMAIN, unique_id) == entity_id
    # And it says so, because standing down silently would leave a user with
    # receiver controls that never got re-keyed and no hint why.
    assert (
        f"Not re-keying the receiver controls of {entry.title}: it has adopted a "
        "device keyed 'hub', whose entities are indistinguishable from the v2 "
        "receiver controls"
    ) in caplog.text


async def test_rehome_moves_the_same_rows_rather_than_recreating_them(hass):
    """``_rehome_device_objects`` must move entities before devices.

    Moving a device with ``new_config_entry_id`` makes Home Assistant delete
    every entity still pointing at the old config entry, so re-homing the device
    first destroys exactly the rows the entity pass exists to move. The bug used
    to hide behind a successful setup recreating the rows under the same
    unique_ids — which looks identical unless you check the registry row *id*,
    which is what this asserts.
    """
    receiver = MockConfigEntry(
        domain=DOMAIN, title="receiver", version=2, data={CONF_HOST: "h"}
    )
    child = MockConfigEntry(
        domain=DOMAIN, title="child", version=1, data={CONF_HOST: "h"}
    )
    receiver.add_to_hass(hass)
    child.add_to_hass(hass)

    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    nested = dev_reg.async_get_or_create(
        config_entry_id=child.entry_id,
        identifiers={(DOMAIN, f"{receiver.entry_id}:Acurite-Tower-1")},
    )
    seeded = ent_reg.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{receiver.entry_id}:Acurite-Tower-1:temperature_C",
        config_entry=child,
        device_id=nested.id,
    )

    _rehome_device_objects(hass, child, receiver.entry_id)

    moved = ent_reg.async_get(seeded.entity_id)
    assert moved is not None, "the entity was destroyed by the device move"
    # The *same* registry row, not a recreated one: the immutable row id and the
    # entity_id both survive, which is what recorder history is keyed on.
    assert moved.id == seeded.id
    assert moved.entity_id == seeded.entity_id
    assert moved.config_entry_id == receiver.entry_id
    assert dev_reg.async_get(nested.id).config_entry_id == receiver.entry_id
