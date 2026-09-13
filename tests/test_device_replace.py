"""Tests for the in-place device re-key helper (``device_replace``).

The property these tests protect is the whole point of the feature: when a cheap
433 MHz sensor generates a new transmitter id after a battery swap, re-keying the
device onto the new id must carry the user's recorder history across. History is
keyed on ``entity_id``, and an ``entity_id`` only survives if the *registry row*
survives — so the central assertions compare the immutable registry row id
(``RegistryEntry.id``) before and after, not just the ``entity_id``: a recreated
row can coincidentally reclaim a freed ``entity_id`` and would still have
orphaned its history.

Everything here drives the real ``async_setup_entry`` (with the transport's
connect loop stubbed by the shared ``no_socket`` fixture) so the registry state
under test is the state Home Assistant actually builds, including the throwaway
device Home Assistant has already created for the new transmitter id.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from custom_components.rtl_433.const import (
    CALIBRATION_COMMODITY,
    CALIBRATION_SCALE,
    CALIBRATION_UNIT,
    COMMODITY_WATER,
    CONF_DEVICES,
    CONF_MODEL,
    DEVICE_CALIBRATION,
    DEVICE_EVENT_TYPES,
    DEVICE_FIELDS,
    DEVICE_MOTION_CLEAR_DELAY,
    DEVICE_TIMEOUT_OVERRIDE,
    DOMAIN,
)
from custom_components.rtl_433.device_replace import (
    DeviceReplaceError,
    _fold_devices,
    async_consolidate_location,
    async_replace_device,
)
from custom_components.rtl_433.receiver_settings import receiver_subentries
from homeassistant.const import CONF_HOST
from homeassistant.core import callback
from homeassistant.helpers import (
    device_registry as dr,
    entity_registry as er,
    issue_registry as ir,
)

from .conftest import receiver_id as first_receiver_id

MODEL = "Acurite-986"
OLD_KEY = f"{MODEL}-1a2b"
NEW_KEY = f"{MODEL}-9f3c"

# The user's deliberate per-device settings: everything the fold must carry
# across untouched, so a replace never silently resets a configured device.
OLD_RECORD: dict[str, Any] = {
    CONF_MODEL: MODEL,
    DEVICE_FIELDS: ["temperature_C"],
    DEVICE_TIMEOUT_OVERRIDE: 900,
    DEVICE_MOTION_CLEAR_DELAY: 45,
    DEVICE_EVENT_TYPES: {"cmd": ["open", "close"]},
    DEVICE_CALIBRATION: {
        CALIBRATION_COMMODITY: COMMODITY_WATER,
        CALIBRATION_UNIT: "L",
        CALIBRATION_SCALE: 0.1,
    },
}
# The replacement as Home Assistant discovered it: a deliberate superset of the
# original's fields, so the union in the fold is observable.
NEW_RECORD: dict[str, Any] = {
    CONF_MODEL: MODEL,
    DEVICE_FIELDS: ["temperature_C", "battery_ok"],
}


async def _setup_receiver(hass, receiver_entry_builder, devices):
    """Set up a receiver entry seeded with ``devices`` and return it."""
    receiver = receiver_entry_builder(availability_timeout=600, devices=devices)
    receiver.add_to_hass(hass)
    assert await hass.config_entries.async_setup(receiver.entry_id)
    await hass.async_block_till_done()
    return receiver


def _rows(hass, receiver, device_key) -> dict[str, tuple[str, str]]:
    """Map ``unique_id -> (entity_id, registry row id)`` for one nested device."""
    ent_reg = er.async_get(hass)
    prefix = f"{receiver.entry_id}:{device_key}:"
    return {
        entry.unique_id: (entry.entity_id, entry.id)
        for entry in er.async_entries_for_config_entry(ent_reg, receiver.entry_id)
        if entry.unique_id.startswith(prefix)
    }


def _row_ids(hass, receiver) -> set[str]:
    """Every registry row id currently owned by the receiver entry."""
    ent_reg = er.async_get(hass)
    return {
        entry.id
        for entry in er.async_entries_for_config_entry(ent_reg, receiver.entry_id)
    }


# --------------------------------------------------------------------------- #
# Happy path: the survivors keep their rows, the duplicate loses its claim.    #
# --------------------------------------------------------------------------- #
async def test_replace_preserves_entity_rows_and_repoints_device(
    hass, receiver_entry_builder, no_socket
):
    """Every survivor keeps its ``entity_id`` *and* its registry row id.

    This is the history-preservation guarantee. It is asserted against the
    realistic collision state — Home Assistant has already built a full set of
    entities and a device row for the new transmitter id — because that is what
    every real replace looks like, and freeing those duplicates is the step the
    rewrite depends on.
    """
    receiver = await _setup_receiver(
        hass, receiver_entry_builder, {OLD_KEY: OLD_RECORD, NEW_KEY: NEW_RECORD}
    )
    dev_reg = dr.async_get(hass)

    before_old = _rows(hass, receiver, OLD_KEY)
    before_new = _rows(hass, receiver, NEW_KEY)
    # Sanity: the state under test really is the collision case.
    assert before_old
    assert before_new
    old_device_id = dev_reg.async_get_device_by_identifier(
        (DOMAIN, f"{receiver.entry_id}:{OLD_KEY}"), receiver.entry_id
    ).id
    duplicate_device_id = dev_reg.async_get_device_by_identifier(
        (DOMAIN, f"{receiver.entry_id}:{NEW_KEY}"), receiver.entry_id
    ).id
    assert old_device_id != duplicate_device_id

    await async_replace_device(hass, receiver, OLD_KEY, NEW_KEY)
    await hass.async_block_till_done()

    after = _rows(hass, receiver, NEW_KEY)

    # Each survivor moved from ':{OLD_KEY}:{suffix}' to ':{NEW_KEY}:{suffix}'
    # with the suffix byte-for-byte unchanged, and the *same* row carried it.
    old_prefix = f"{receiver.entry_id}:{OLD_KEY}:"
    for old_unique_id, (entity_id, row_id) in before_old.items():
        suffix = old_unique_id[len(old_prefix) :]
        assert after[f"{receiver.entry_id}:{NEW_KEY}:{suffix}"] == (
            entity_id,
            row_id,
        )

    # Nothing is left behind on the old key.
    assert _rows(hass, receiver, OLD_KEY) == {}

    # The duplicate rows that contested a survivor's unique_id are gone: the
    # survivor's row, not the throwaway, now holds each contested unique_id.
    # (Suffixes the survivor never had -- the replacement's extra ``battery_ok``
    # -- are not part of the rewrite; Home Assistant restores those rows itself
    # when the platforms rebuild, which is its behaviour to define, not ours.)
    survivor_unique_ids = {
        f"{receiver.entry_id}:{NEW_KEY}:{uid[len(old_prefix) :]}" for uid in before_old
    }
    contested = survivor_unique_ids & set(before_new)
    assert contested, "expected the duplicate to have claimed a survivor's unique_id"
    live_row_ids = _row_ids(hass, receiver)
    for unique_id in contested:
        assert before_new[unique_id][1] != after[unique_id][1]
        assert before_new[unique_id][1] not in live_row_ids

    # The union field the original never transmitted still gets an entity under
    # the new key, so the folded record is what the platforms rebuilt from.
    assert set(after) - survivor_unique_ids

    # The device row was re-pointed in place, not recreated: same row id, new
    # identifiers, and the serial number now reports the new transmitter id.
    new_device = dev_reg.async_get_device_by_identifier(
        (DOMAIN, f"{receiver.entry_id}:{NEW_KEY}"), receiver.entry_id
    )
    assert new_device.id == old_device_id
    assert new_device.serial_number == "9f3c"
    assert (
        dev_reg.async_get_device_by_identifier(
            (DOMAIN, f"{receiver.entry_id}:{OLD_KEY}"), receiver.entry_id
        )
        is None
    )


# --------------------------------------------------------------------------- #
# The record fold.                                                             #
# --------------------------------------------------------------------------- #
async def test_replace_folds_settings_onto_new_key(
    hass, receiver_entry_builder, no_socket
):
    """The user's settings survive under the new key; ``fields`` is the union."""
    receiver = await _setup_receiver(
        hass, receiver_entry_builder, {OLD_KEY: OLD_RECORD, NEW_KEY: NEW_RECORD}
    )

    await async_replace_device(hass, receiver, OLD_KEY, NEW_KEY)
    await hass.async_block_till_done()

    devices = receiver.data[CONF_DEVICES]
    assert OLD_KEY not in devices
    record = devices[NEW_KEY]

    # Every deliberate setting carried across from the old record.
    assert record[DEVICE_TIMEOUT_OVERRIDE] == 900
    assert record[DEVICE_MOTION_CLEAR_DELAY] == 45
    assert record[DEVICE_EVENT_TYPES] == {"cmd": ["open", "close"]}
    assert record[DEVICE_CALIBRATION] == OLD_RECORD[DEVICE_CALIBRATION]
    assert record[CONF_MODEL] == MODEL

    # ``fields`` alone is unioned, and sorted.
    assert record[DEVICE_FIELDS] == ["battery_ok", "temperature_C"]


async def test_replace_adopts_new_key_with_no_record(
    hass, receiver_entry_builder, no_socket
):
    """Adopting a key the devices map never registered transfers the record whole.

    This is the discovery-disabled case the docs recommend for urban areas: the
    coordinator receives the replacement but never registers it, so there is no
    record and no duplicate device to free. The old record must simply move.
    """
    receiver = await _setup_receiver(
        hass, receiver_entry_builder, {OLD_KEY: OLD_RECORD}
    )
    before_old = _rows(hass, receiver, OLD_KEY)

    await async_replace_device(hass, receiver, OLD_KEY, NEW_KEY)
    await hass.async_block_till_done()

    assert receiver.data[CONF_DEVICES] == {NEW_KEY: dict(OLD_RECORD)}

    # The rows still moved in place even with nothing to free first.
    after = _rows(hass, receiver, NEW_KEY)
    old_prefix = f"{receiver.entry_id}:{OLD_KEY}:"
    for old_unique_id, row in before_old.items():
        suffix = old_unique_id[len(old_prefix) :]
        assert after[f"{receiver.entry_id}:{NEW_KEY}:{suffix}"] == row


@pytest.mark.parametrize(
    ("new_record", "expected_model"),
    [
        pytest.param(NEW_RECORD, MODEL, id="new-record-supplies-model"),
        pytest.param(None, "", id="no-new-record-leaves-model-blank"),
    ],
)
async def test_replace_model_fallback_when_old_record_has_none(
    hass, receiver_entry_builder, no_socket, new_record, expected_model
):
    """A model-less old record inherits the model the replacement was seen with.

    A v1-migrated record can carry an empty ``model``; the replacement's freshly
    decoded one is strictly better than nothing, so it is the one fallback the
    fold takes from the new record. With no new record to fall back to the field
    stays an empty string — never ``None``, which would break the label helpers
    that call ``str`` methods on it.
    """
    devices = {OLD_KEY: {CONF_MODEL: "", DEVICE_FIELDS: ["temperature_C"]}}
    if new_record is not None:
        devices[NEW_KEY] = new_record
    receiver = await _setup_receiver(hass, receiver_entry_builder, devices)

    await async_replace_device(hass, receiver, OLD_KEY, NEW_KEY)
    await hass.async_block_till_done()

    assert receiver.data[CONF_DEVICES][NEW_KEY][CONF_MODEL] == expected_model


# --------------------------------------------------------------------------- #
# Guards. A replace that quietly did nothing would look successful while        #
# leaving the user's history stranded, so each of these must raise.             #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("old_key", "new_key", "message"),
    [
        pytest.param(
            "Acurite-986-dead",
            NEW_KEY,
            "Unknown device key: Acurite-986-dead",
            id="unknown-old-key",
        ),
        pytest.param(
            OLD_KEY,
            OLD_KEY,
            "The new device key must differ from the old one",
            id="same-key",
        ),
        pytest.param(
            "",
            NEW_KEY,
            "Both the old and the new device key are required",
            id="empty-old-key",
        ),
        pytest.param(
            OLD_KEY,
            "",
            "Both the old and the new device key are required",
            id="empty-new-key",
        ),
    ],
)
async def test_replace_rejects_invalid_keys(
    hass, receiver_entry_builder, no_socket, old_key, new_key, message
):
    """Bad input raises ``DeviceReplaceError`` and leaves the devices map alone.

    The message is asserted because it is what the options flow surfaces and what
    lands in the log — a guard that raised the wrong reason would send a user
    hunting the wrong problem.
    """
    receiver = await _setup_receiver(
        hass, receiver_entry_builder, {OLD_KEY: OLD_RECORD}
    )
    before = dict(receiver.data[CONF_DEVICES])

    with pytest.raises(DeviceReplaceError, match=re.escape(message)):
        await async_replace_device(hass, receiver, old_key, new_key)

    assert receiver.data[CONF_DEVICES] == before
    assert _rows(hass, receiver, OLD_KEY)


async def test_replace_rejects_a_receiver_with_no_devices_map(
    hass, receiver_entry_builder, no_socket
):
    """A receiver that never stored a devices map has nothing to replace.

    ``entry.data`` carries no devices map at all on a receiver where the user has
    never added a device, so the lookup must tolerate its absence rather than
    blow up.
    """
    receiver = await _setup_receiver(hass, receiver_entry_builder, None)
    assert CONF_DEVICES not in receiver.data

    with pytest.raises(
        DeviceReplaceError, match=re.escape(f"Unknown device key: {OLD_KEY}")
    ):
        await async_replace_device(hass, receiver, OLD_KEY, NEW_KEY)


# --------------------------------------------------------------------------- #
# Consolidation: folding one location into another.                           #
# --------------------------------------------------------------------------- #
SHARED_KEY = f"{MODEL}-5e7d"
SOLO_KEY = f"{MODEL}-0c11"
# ``object_suffix`` tails, as the shipped library spells them: the unique_id
# carries the descriptor's suffix, not the raw rtl_433 field key.
TEMP_SUFFIX = "T"
HUMIDITY_SUFFIX = "H"


async def _two_locations(hass, receiver_entry_builder, *, shared_only: bool = False):
    """Set up two locations that both received ``SHARED_KEY``, target first.

    The target is created first so it is the location holding the earliest-added
    receiver — the one whose entity survives a collision. Unless ``shared_only``,
    the source also carries a device the target never received, so the test can see
    a clean move alongside the forced merge.
    """
    target = await _setup_receiver(
        hass,
        receiver_entry_builder,
        {
            SHARED_KEY: {
                CONF_MODEL: MODEL,
                DEVICE_FIELDS: ["temperature_C"],
                # The surviving location's own setting: the fold keeps the
                # record that still describes a live entity, which is this one.
                DEVICE_TIMEOUT_OVERRIDE: 900,
            }
        },
    )
    source_devices = {SHARED_KEY: {CONF_MODEL: MODEL, DEVICE_FIELDS: ["temperature_C"]}}
    if not shared_only:
        source_devices[SOLO_KEY] = {CONF_MODEL: MODEL, DEVICE_FIELDS: ["humidity"]}
    source = receiver_entry_builder(
        availability_timeout=600,
        host="garage.local",
        devices=source_devices,
    )
    source.add_to_hass(hass)
    assert await hass.config_entries.async_setup(source.entry_id)
    await hass.async_block_till_done()
    return source, target


async def test_consolidation_keeps_the_earliest_receiver_and_reports_the_loss(
    hass, receiver_entry_builder, no_socket
):
    """The earliest-added receiver's entity survives, with its row and its history.

    Both locations recorded the same physical sensor, and Home Assistant forbids
    two registry rows holding one ``unique_id`` — so one of the two histories has
    to go, which is the single lossy moment in the whole location model. The rule
    is deterministic (keep the earliest-added receiver, i.e. the location being
    merged *into*) and the loss is announced: the survivor keeps its ``entity_id``
    *and* its immutable registry row id, and a Repairs notice names the receiver
    whose copy was dropped.
    """
    source, target = await _two_locations(hass, receiver_entry_builder)
    ent_reg = er.async_get(hass)

    survivor_unique_id = f"{target.entry_id}:{SHARED_KEY}:{TEMP_SUFFIX}"
    survivor = ent_reg.async_get(
        ent_reg.async_get_entity_id("sensor", DOMAIN, survivor_unique_id)
    )
    doomed_unique_id = f"{source.entry_id}:{SHARED_KEY}:{TEMP_SUFFIX}"
    doomed_entity_id = ent_reg.async_get_entity_id("sensor", DOMAIN, doomed_unique_id)
    assert survivor is not None and doomed_entity_id is not None
    dropped_receiver_title = receiver_subentries(source)[0].title
    kept_receiver_title = receiver_subentries(target)[0].title

    dropped = await async_consolidate_location(hass, source, target)
    await hass.async_block_till_done()

    # The survivor is the same registry row it always was.
    kept = ent_reg.async_get(survivor.entity_id)
    assert kept is not None
    assert kept.id == survivor.id
    assert kept.unique_id == survivor_unique_id
    assert kept.config_entry_id == target.entry_id
    # The duplicate is gone, and it is the one that was reported.
    assert ent_reg.async_get(doomed_entity_id) is None
    assert dropped == [doomed_entity_id]

    issue_reg = ir.async_get(hass)
    issue = issue_reg.async_get_issue(
        DOMAIN, f"history_merged_on_consolidation_{target.entry_id}"
    )
    assert issue is not None
    assert issue.translation_placeholders["dropped"] == dropped_receiver_title
    assert issue.translation_placeholders["kept"] == kept_receiver_title
    assert issue.translation_placeholders["count"] == "1"
    assert doomed_entity_id in issue.translation_placeholders["entities"]
    # A notice, not a repair: there is nothing to undo, and it has to survive a
    # restart because the user has to see it once.
    assert issue.is_fixable is False
    assert issue.is_persistent is True
    assert issue.severity is ir.IssueSeverity.WARNING
    assert issue.translation_key == "history_merged_on_consolidation"


async def test_consolidation_moves_the_receiver_and_its_unshared_device(
    hass, receiver_entry_builder, no_socket
):
    """Everything that does not collide is re-keyed in place, not recreated.

    A device only the absorbed location received has no counterpart to lose to, so
    it moves wholesale: same registry row, same ``entity_id``, re-keyed onto the
    surviving location's scope. The receiver subentry moves with its id intact —
    that is what keeps its radio controls and its managed-settings store — and
    the emptied location is removed.
    """
    source, target = await _two_locations(hass, receiver_entry_builder)
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)

    moved_receiver_id = first_receiver_id(source)
    solo_unique_id = f"{source.entry_id}:{SOLO_KEY}:{HUMIDITY_SUFFIX}"
    solo = ent_reg.async_get(
        ent_reg.async_get_entity_id("sensor", DOMAIN, solo_unique_id)
    )
    assert solo is not None
    source_entry_id = source.entry_id

    assert await async_consolidate_location(hass, source, target) is not None
    await hass.async_block_till_done()

    # The unshared device's entity is the same row under the new scope.
    rekeyed = ent_reg.async_get(solo.entity_id)
    assert rekeyed is not None
    assert rekeyed.id == solo.id
    assert rekeyed.unique_id == f"{target.entry_id}:{SOLO_KEY}:{HUMIDITY_SUFFIX}"
    assert rekeyed.config_entry_id == target.entry_id
    # Its device row moved too, keeping its identity under the new location.
    assert (
        dev_reg.async_get_device_by_identifier(
            (DOMAIN, f"{target.entry_id}:{SOLO_KEY}"), target.entry_id
        )
        is not None
    )

    # The receiver moved as a subentry, keeping its id, and the location it came
    # from is gone.
    assert {sub.subentry_id for sub in receiver_subentries(target)} == {
        first_receiver_id(target),
        moved_receiver_id,
    }
    assert hass.config_entries.async_get_entry(source_entry_id) is None
    # Both device records are on the surviving location.
    assert set(target.data[CONF_DEVICES]) == {SHARED_KEY, SOLO_KEY}


async def test_consolidation_without_a_collision_raises_no_notice(
    hass, receiver_entry_builder, no_socket
):
    """Two locations with no sensor in common merge losslessly and silently.

    The Repairs notice exists to report a dropped history; raising one when
    nothing was dropped would train users to ignore it.
    """
    target = await _setup_receiver(
        hass,
        receiver_entry_builder,
        {SHARED_KEY: {CONF_MODEL: MODEL, DEVICE_FIELDS: ["temperature_C"]}},
    )
    source = receiver_entry_builder(
        availability_timeout=600,
        host="garage.local",
        devices={SOLO_KEY: {CONF_MODEL: MODEL, DEVICE_FIELDS: ["humidity"]}},
    )
    source.add_to_hass(hass)
    assert await hass.config_entries.async_setup(source.entry_id)
    await hass.async_block_till_done()

    assert await async_consolidate_location(hass, source, target) == []
    await hass.async_block_till_done()

    issue_reg = ir.async_get(hass)
    assert not [i for i in issue_reg.issues.values() if i.domain == DOMAIN]


async def test_consolidation_unions_the_ignore_list_with_ignored_winning(
    hass, receiver_entry_builder, no_socket
):
    """A sensor one location hid stays hidden after the merge.

    Adoption and ignoring are both statements about a *sensor*, so both lists
    union — and a key on both resolves to ignored, because a device the user
    explicitly dismissed must not be resurrected by a second receiver that
    happens to hear it.
    """
    source, target = await _two_locations(hass, receiver_entry_builder)
    hass.config_entries.async_update_entry(
        source, data={**source.data, "ignored_devices": [SOLO_KEY]}
    )

    await async_consolidate_location(hass, source, target)
    await hass.async_block_till_done()

    assert target.data["ignored_devices"] == [SOLO_KEY]


async def test_consolidation_rejects_a_self_merge_or_a_receiverless_location(
    hass, receiver_entry_builder, no_socket
):
    """The guards raise rather than silently no-opping.

    A consolidation that quietly did nothing would look successful while leaving
    the user with two locations and no explanation.
    """
    source, target = await _two_locations(hass, receiver_entry_builder)

    with pytest.raises(
        DeviceReplaceError,
        match=re.escape("A location cannot be consolidated into itself"),
    ):
        await async_consolidate_location(hass, target, target)

    empty = receiver_entry_builder(receivers=[])
    empty.add_to_hass(hass)
    for wrong in ((empty, target), (source, empty)):
        with pytest.raises(
            DeviceReplaceError, match=re.escape(f"{empty.title} holds no receiver")
        ):
            await async_consolidate_location(hass, *wrong)


def test_fold_devices_unions_fields_and_keeps_the_survivors_settings():
    """The surviving location's record wins; only ``fields`` and ``model`` merge.

    A device_key both locations adopted is one physical sensor, and after the
    merge only the target's entity still exists — so the target's record is the
    one that describes something live, and its deliberate settings (timeout
    override, calibration, clear delay) are what must survive. ``fields`` is the
    exception: a field only the absorbed receiver ever decoded is still a field
    the merged device can produce.
    """
    source = {
        SHARED_KEY: {
            CONF_MODEL: MODEL,
            DEVICE_FIELDS: ["humidity", "temperature_C"],
            DEVICE_TIMEOUT_OVERRIDE: 111,
        },
        SOLO_KEY: {CONF_MODEL: MODEL, DEVICE_FIELDS: ["humidity"]},
        "OnlyTarget-1": {CONF_MODEL: "", DEVICE_FIELDS: []},
    }
    target = {
        SHARED_KEY: {
            CONF_MODEL: MODEL,
            DEVICE_FIELDS: ["battery_ok", "temperature_C"],
            DEVICE_TIMEOUT_OVERRIDE: 900,
        },
        # No model of its own: it falls back to what the absorbed location knew.
        "OnlyTarget-1": {CONF_MODEL: "", DEVICE_FIELDS: ["rssi"]},
    }
    source["OnlyTarget-1"][CONF_MODEL] = "Acurite-606TX"

    merged = _fold_devices(source, target)

    assert set(merged) == {SHARED_KEY, SOLO_KEY, "OnlyTarget-1"}
    # The target's settings win, and the two field lists are unioned + sorted.
    assert merged[SHARED_KEY][DEVICE_TIMEOUT_OVERRIDE] == 900
    assert merged[SHARED_KEY][DEVICE_FIELDS] == [
        "battery_ok",
        "humidity",
        "temperature_C",
    ]
    # A device only the absorbed location had comes across untouched.
    assert merged[SOLO_KEY] == {CONF_MODEL: MODEL, DEVICE_FIELDS: ["humidity"]}
    # An empty model falls back to the absorbed location's.
    assert merged["OnlyTarget-1"][CONF_MODEL] == "Acurite-606TX"
    assert merged["OnlyTarget-1"][DEVICE_FIELDS] == ["rssi"]
    # Records are copied, never shared with either entry's stored mapping.
    merged[SHARED_KEY][DEVICE_FIELDS].append("tampered")
    assert target[SHARED_KEY][DEVICE_FIELDS] == ["battery_ok", "temperature_C"]
    assert source[SHARED_KEY][DEVICE_FIELDS] == ["humidity", "temperature_C"]


def test_fold_devices_keeps_a_model_the_target_already_knows():
    """A target record that has a model is not overwritten by the source's."""
    merged = _fold_devices(
        {SHARED_KEY: {CONF_MODEL: "Old-Model", DEVICE_FIELDS: []}},
        {SHARED_KEY: {CONF_MODEL: MODEL, DEVICE_FIELDS: []}},
    )
    assert merged[SHARED_KEY][CONF_MODEL] == MODEL


async def test_consolidation_moves_rows_instead_of_deleting_and_rebuilding_them(
    hass, receiver_entry_builder, no_socket
):
    """Nothing is removed but the forced duplicates, and every survivor is moved.

    The reload at the end of a consolidation rebuilds every entity, so a merge
    that *destroyed* rows and let the rebuild recreate them looks identical from
    the outside — right up until the user opens the history graph. This watches
    the registries' own removal events and the immutable row ids instead: exactly
    one entity row is removed (the duplicate the merge is allowed to drop) and
    exactly one device row (its device), and every other row comes out the far
    side as the same row, area and user-assigned name included.
    """
    source, target = await _two_locations(hass, receiver_entry_builder)
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)

    removed_entities: list[str] = []
    removed_devices: list[str] = []

    @callback
    def _entity_event(event):
        if event.data["action"] == "remove":
            removed_entities.append(event.data["entity_id"])

    @callback
    def _device_event(event):
        if event.data["action"] == "remove":
            removed_devices.append(event.data["device_id"])

    hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, _entity_event)
    hass.bus.async_listen(dr.EVENT_DEVICE_REGISTRY_UPDATED, _device_event)

    # A user-assigned name on the device that is about to be re-keyed: a
    # recreated row would come back nameless.
    solo_device = dev_reg.async_get_device_by_identifier(
        (DOMAIN, f"{source.entry_id}:{SOLO_KEY}"), source.entry_id
    )
    shared_source_device = dev_reg.async_get_device_by_identifier(
        (DOMAIN, f"{source.entry_id}:{SHARED_KEY}"), source.entry_id
    )
    # The absorbed location's own device row is a duplicate of the surviving
    # location's, so it goes too -- there is one location device per location.
    source_location_device = dev_reg.async_get_device_by_identifier(
        (DOMAIN, source.entry_id), source.entry_id
    )
    assert solo_device is not None and shared_source_device is not None
    assert source_location_device is not None
    dev_reg.async_update_device(solo_device.id, name_by_user="The shed")

    receiver_subentry_id = first_receiver_id(source)
    receiver_device = dev_reg.async_get_device_by_identifier(
        (DOMAIN, f"{source.entry_id}:receiver:{receiver_subentry_id}"), source.entry_id
    )
    assert receiver_device is not None
    # One of the moved receiver's own control entities, which is re-keyed by the
    # same prefix swap as the device fields.
    control_unique_id = (
        f"{source.entry_id}:receiver:{receiver_subentry_id}:center_frequency"
    )
    control = ent_reg.async_get(
        ent_reg.async_get_entity_id("number", DOMAIN, control_unique_id)
    )
    solo_entity = ent_reg.async_get(
        ent_reg.async_get_entity_id(
            "sensor", DOMAIN, f"{source.entry_id}:{SOLO_KEY}:{HUMIDITY_SUFFIX}"
        )
    )
    assert control is not None and solo_entity is not None
    doomed_entity_id = ent_reg.async_get_entity_id(
        "sensor", DOMAIN, f"{source.entry_id}:{SHARED_KEY}:{TEMP_SUFFIX}"
    )

    dropped = await async_consolidate_location(hass, source, target)
    await hass.async_block_till_done()

    # Exactly the forced duplicate, and its now-redundant device row.
    assert dropped == [doomed_entity_id]
    assert removed_entities == [doomed_entity_id]
    assert set(removed_devices) == {
        shared_source_device.id,
        source_location_device.id,
    }
    assert len(removed_devices) == 2

    # The absorbed location's device row is the same row under the new scope,
    # with the user's name on it.
    moved = dev_reg.async_get_device_by_identifier(
        (DOMAIN, f"{target.entry_id}:{SOLO_KEY}"), target.entry_id
    )
    assert moved is not None
    assert moved.id == solo_device.id
    assert moved.name_by_user == "The shed"
    assert moved.config_entry_id == target.entry_id
    assert moved.config_subentry_id is None

    # So is the receiver device, and it still belongs to the subentry that moved
    # with it -- that is what keeps its radio controls attached to it.
    moved_receiver = dev_reg.async_get_device_by_identifier(
        (DOMAIN, f"{target.entry_id}:receiver:{receiver_subentry_id}"), target.entry_id
    )
    assert moved_receiver is not None
    assert moved_receiver.id == receiver_device.id
    assert moved_receiver.config_subentry_id == receiver_subentry_id

    # The receiver's control entity is the same row, re-keyed, still owned by its
    # own subentry and still on its own device.
    moved_control = ent_reg.async_get(control.entity_id)
    assert moved_control is not None
    assert moved_control.id == control.id
    assert moved_control.unique_id == (
        f"{target.entry_id}:receiver:{receiver_subentry_id}:center_frequency"
    )
    assert moved_control.config_entry_id == target.entry_id
    assert moved_control.config_subentry_id == receiver_subentry_id
    assert moved_control.device_id == receiver_device.id

    # And the unshared device's entity sits on the moved device row.
    moved_solo = ent_reg.async_get(solo_entity.entity_id)
    assert moved_solo is not None
    assert moved_solo.id == solo_entity.id
    assert moved_solo.device_id == solo_device.id
    assert moved_solo.config_subentry_id is None

    # The moved subentry kept its title and its stable radio identity.
    moved_subentry = next(
        sub
        for sub in receiver_subentries(target)
        if sub.subentry_id == receiver_subentry_id
    )
    assert moved_subentry.title == "rtl_433 (garage.local)"
    assert moved_subentry.unique_id == "hub:garage.local:8433"
    assert dict(moved_subentry.data)[CONF_HOST] == "garage.local"


async def test_consolidation_folds_the_stored_state_of_both_locations(
    hass, receiver_entry_builder, no_socket
):
    """Devices, ignore list and user mappings all come across, target winning.

    The registry work is only half a consolidation: the surviving location also
    has to end up with the absorbed one's adopted devices and user mappings, or
    the rebuild after the reload would drop entities the rows were just re-keyed
    for.
    """
    source, target = await _two_locations(hass, receiver_entry_builder)
    hass.config_entries.async_update_entry(
        source,
        data={
            **source.data,
            "ignored_devices": ["Neighbour-1"],
            "user_mappings": {"only_source": {"platform": "sensor"}},
        },
    )
    hass.config_entries.async_update_entry(
        target,
        data={
            **target.data,
            "ignored_devices": ["Neighbour-2"],
            "user_mappings": {"only_target": {"platform": "sensor"}},
        },
    )

    await async_consolidate_location(hass, source, target)
    await hass.async_block_till_done()

    assert set(target.data[CONF_DEVICES]) == {SHARED_KEY, SOLO_KEY}
    assert target.data[CONF_DEVICES][SOLO_KEY][DEVICE_FIELDS] == ["humidity"]
    # The surviving location's own record for the shared sensor, not the
    # absorbed one's, is what came through.
    assert target.data[CONF_DEVICES][SHARED_KEY][DEVICE_TIMEOUT_OVERRIDE] == 900
    assert target.data["ignored_devices"] == ["Neighbour-1", "Neighbour-2"]
    assert set(target.data["user_mappings"]) == {"only_source", "only_target"}


def test_fold_devices_tolerates_records_with_no_fields_or_model():
    """A record missing ``fields`` or ``model`` folds to the empty values, not ``None``.

    Both keys are optional in a stored record — a device received once but never
    decoded has no fields, and one whose model was never learned has no model —
    so the fold has to read them with real empty defaults. ``None`` would
    propagate into the devices map and blow up the next platform build.
    """
    merged = _fold_devices({SOLO_KEY: {}}, {SOLO_KEY: {}})

    assert merged[SOLO_KEY][DEVICE_FIELDS] == []
    assert merged[SOLO_KEY][CONF_MODEL] == ""


async def test_consolidating_two_locations_that_have_adopted_nothing(
    hass, receiver_entry_builder, no_socket
):
    """Two brand-new locations merge cleanly with no stored state at all.

    A location that has adopted nothing carries no devices map, no ignore list
    and no user mappings — the keys are simply absent — which is exactly the
    state a user is in when they add a second server and immediately decide it
    belongs with the first. Every read of that state has to survive the key not
    being there.
    """
    target = receiver_entry_builder()
    target.add_to_hass(hass)
    assert await hass.config_entries.async_setup(target.entry_id)
    source = receiver_entry_builder(host="garage.local")
    source.add_to_hass(hass)
    assert await hass.config_entries.async_setup(source.entry_id)
    await hass.async_block_till_done()
    assert CONF_DEVICES not in source.data and CONF_DEVICES not in target.data

    assert await async_consolidate_location(hass, source, target) == []
    await hass.async_block_till_done()

    assert target.data[CONF_DEVICES] == {}
    assert target.data["ignored_devices"] == []
    assert target.data["user_mappings"] == {}
    assert len(receiver_subentries(target)) == 2
