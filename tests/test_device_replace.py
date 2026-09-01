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
    async_replace_device,
)
from homeassistant.helpers import device_registry as dr, entity_registry as er

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


async def _setup_hub(hass, hub_entry_builder, devices):
    """Set up a hub entry seeded with ``devices`` and return it."""
    hub = hub_entry_builder(availability_timeout=600, devices=devices)
    hub.add_to_hass(hass)
    assert await hass.config_entries.async_setup(hub.entry_id)
    await hass.async_block_till_done()
    return hub


def _rows(hass, hub, device_key) -> dict[str, tuple[str, str]]:
    """Map ``unique_id -> (entity_id, registry row id)`` for one nested device."""
    ent_reg = er.async_get(hass)
    prefix = f"{hub.entry_id}:{device_key}:"
    return {
        entry.unique_id: (entry.entity_id, entry.id)
        for entry in er.async_entries_for_config_entry(ent_reg, hub.entry_id)
        if entry.unique_id.startswith(prefix)
    }


def _row_ids(hass, hub) -> set[str]:
    """Every registry row id currently owned by the hub entry."""
    ent_reg = er.async_get(hass)
    return {
        entry.id for entry in er.async_entries_for_config_entry(ent_reg, hub.entry_id)
    }


# --------------------------------------------------------------------------- #
# Happy path: the survivors keep their rows, the duplicate loses its claim.    #
# --------------------------------------------------------------------------- #
async def test_replace_preserves_entity_rows_and_repoints_device(
    hass, hub_entry_builder, no_socket
):
    """Every survivor keeps its ``entity_id`` *and* its registry row id.

    This is the history-preservation guarantee. It is asserted against the
    realistic collision state — Home Assistant has already built a full set of
    entities and a device row for the new transmitter id — because that is what
    every real replace looks like, and freeing those duplicates is the step the
    rewrite depends on.
    """
    hub = await _setup_hub(
        hass, hub_entry_builder, {OLD_KEY: OLD_RECORD, NEW_KEY: NEW_RECORD}
    )
    dev_reg = dr.async_get(hass)

    before_old = _rows(hass, hub, OLD_KEY)
    before_new = _rows(hass, hub, NEW_KEY)
    # Sanity: the state under test really is the collision case.
    assert before_old
    assert before_new
    old_device_id = dev_reg.async_get_device(
        identifiers={(DOMAIN, f"{hub.entry_id}:{OLD_KEY}")}
    ).id
    duplicate_device_id = dev_reg.async_get_device(
        identifiers={(DOMAIN, f"{hub.entry_id}:{NEW_KEY}")}
    ).id
    assert old_device_id != duplicate_device_id

    await async_replace_device(hass, hub, OLD_KEY, NEW_KEY)
    await hass.async_block_till_done()

    after = _rows(hass, hub, NEW_KEY)

    # Each survivor moved from ':{OLD_KEY}:{suffix}' to ':{NEW_KEY}:{suffix}'
    # with the suffix byte-for-byte unchanged, and the *same* row carried it.
    old_prefix = f"{hub.entry_id}:{OLD_KEY}:"
    for old_unique_id, (entity_id, row_id) in before_old.items():
        suffix = old_unique_id[len(old_prefix) :]
        assert after[f"{hub.entry_id}:{NEW_KEY}:{suffix}"] == (entity_id, row_id)

    # Nothing is left behind on the old key.
    assert _rows(hass, hub, OLD_KEY) == {}

    # The duplicate rows that contested a survivor's unique_id are gone: the
    # survivor's row, not the throwaway, now holds each contested unique_id.
    # (Suffixes the survivor never had -- the replacement's extra ``battery_ok``
    # -- are not part of the rewrite; Home Assistant restores those rows itself
    # when the platforms rebuild, which is its behaviour to define, not ours.)
    survivor_unique_ids = {
        f"{hub.entry_id}:{NEW_KEY}:{uid[len(old_prefix) :]}" for uid in before_old
    }
    contested = survivor_unique_ids & set(before_new)
    assert contested, "expected the duplicate to have claimed a survivor's unique_id"
    live_row_ids = _row_ids(hass, hub)
    for unique_id in contested:
        assert before_new[unique_id][1] != after[unique_id][1]
        assert before_new[unique_id][1] not in live_row_ids

    # The union field the original never transmitted still gets an entity under
    # the new key, so the folded record is what the platforms rebuilt from.
    assert set(after) - survivor_unique_ids

    # The device row was re-pointed in place, not recreated: same row id, new
    # identifiers, and the serial number now reports the new transmitter id.
    new_device = dev_reg.async_get_device(
        identifiers={(DOMAIN, f"{hub.entry_id}:{NEW_KEY}")}
    )
    assert new_device.id == old_device_id
    assert new_device.serial_number == "9f3c"
    assert (
        dev_reg.async_get_device(identifiers={(DOMAIN, f"{hub.entry_id}:{OLD_KEY}")})
        is None
    )


# --------------------------------------------------------------------------- #
# The record fold.                                                             #
# --------------------------------------------------------------------------- #
async def test_replace_folds_settings_onto_new_key(hass, hub_entry_builder, no_socket):
    """The user's settings survive under the new key; ``fields`` is the union."""
    hub = await _setup_hub(
        hass, hub_entry_builder, {OLD_KEY: OLD_RECORD, NEW_KEY: NEW_RECORD}
    )

    await async_replace_device(hass, hub, OLD_KEY, NEW_KEY)
    await hass.async_block_till_done()

    devices = hub.data[CONF_DEVICES]
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
    hass, hub_entry_builder, no_socket
):
    """Adopting a key the devices map never registered transfers the record whole.

    This is the discovery-disabled case the docs recommend for urban areas: the
    coordinator hears the replacement but never registers it, so there is no
    record and no duplicate device to free. The old record must simply move.
    """
    hub = await _setup_hub(hass, hub_entry_builder, {OLD_KEY: OLD_RECORD})
    before_old = _rows(hass, hub, OLD_KEY)

    await async_replace_device(hass, hub, OLD_KEY, NEW_KEY)
    await hass.async_block_till_done()

    assert hub.data[CONF_DEVICES] == {NEW_KEY: dict(OLD_RECORD)}

    # The rows still moved in place even with nothing to free first.
    after = _rows(hass, hub, NEW_KEY)
    old_prefix = f"{hub.entry_id}:{OLD_KEY}:"
    for old_unique_id, row in before_old.items():
        suffix = old_unique_id[len(old_prefix) :]
        assert after[f"{hub.entry_id}:{NEW_KEY}:{suffix}"] == row


@pytest.mark.parametrize(
    ("new_record", "expected_model"),
    [
        pytest.param(NEW_RECORD, MODEL, id="new-record-supplies-model"),
        pytest.param(None, "", id="no-new-record-leaves-model-blank"),
    ],
)
async def test_replace_model_fallback_when_old_record_has_none(
    hass, hub_entry_builder, no_socket, new_record, expected_model
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
    hub = await _setup_hub(hass, hub_entry_builder, devices)

    await async_replace_device(hass, hub, OLD_KEY, NEW_KEY)
    await hass.async_block_till_done()

    assert hub.data[CONF_DEVICES][NEW_KEY][CONF_MODEL] == expected_model


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
    hass, hub_entry_builder, no_socket, old_key, new_key, message
):
    """Bad input raises ``DeviceReplaceError`` and leaves the devices map alone.

    The message is asserted because it is what the options flow surfaces and what
    lands in the log — a guard that raised the wrong reason would send a user
    hunting the wrong problem.
    """
    hub = await _setup_hub(hass, hub_entry_builder, {OLD_KEY: OLD_RECORD})
    before = dict(hub.data[CONF_DEVICES])

    with pytest.raises(DeviceReplaceError, match=re.escape(message)):
        await async_replace_device(hass, hub, old_key, new_key)

    assert hub.data[CONF_DEVICES] == before
    assert _rows(hass, hub, OLD_KEY)


async def test_replace_rejects_a_hub_with_no_devices_map(
    hass, hub_entry_builder, no_socket
):
    """A hub that never stored a devices map has nothing to replace.

    ``entry.data`` predates the key entirely on a hub that has only ever run with
    discovery off, so the lookup must tolerate its absence rather than blow up.
    """
    hub = await _setup_hub(hass, hub_entry_builder, None)
    assert CONF_DEVICES not in hub.data

    with pytest.raises(
        DeviceReplaceError, match=re.escape(f"Unknown device key: {OLD_KEY}")
    ):
        await async_replace_device(hass, hub, OLD_KEY, NEW_KEY)
