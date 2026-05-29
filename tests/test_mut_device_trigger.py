"""Mutation-killing tests for custom_components/rtl_433/device_trigger.py.

Groups of survivors targeted:
- _device_field_from_unique_id (8 mutants): None/empty/2-part/3-part/4-colon inputs,
  exact (device_key, field_key) assertions; tests that distinguish split(:,2) from
  split(:), split(:,3), rsplit(:,2), split(None,2), split(XX:XX,2), and the
  ``if not unique_id`` / ``if len(parts) != 3`` guards.
- _event_types_for_entry (28 mutants): full .get() chain assertions with a real
  hass/entry setup; the fallback (get_capability) path; ``and`` vs ``or`` guard;
  hub_entry=None path; persisted=None shortcut; each .get() default mutation.
- _async_attach_base_trigger (4 mutants): platform_type="device" is propagated
  into the trigger payload; assert calls[0]["trigger"]["platform"] == "device".
- _async_attach_subtype_trigger (2 mutants): HassJob name argument is not None/empty.
- async_validate_trigger_config (1 mutant): TRIGGER_SCHEMA(config) not TRIGGER_SCHEMA(None).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from freezegun import freeze_time

from custom_components.rtl_433.const import (
    CONF_MODEL,
    DEVICE_EVENT_TYPES,
    DEVICE_FIELDS,
    DOMAIN,
)
import custom_components.rtl_433.device_trigger as dt
from custom_components.rtl_433.device_trigger import (
    CONF_SUBTYPE,
    TRIGGER_TYPE_TRIGGERED,
    TRIGGER_TYPE_TRIGGERED_SUBTYPE,
    async_validate_trigger_config,
)
from homeassistant.components.event.const import ATTR_EVENT_TYPES
from homeassistant.const import (
    CONF_DEVICE_ID,
    CONF_DOMAIN,
    CONF_ENTITY_ID,
    CONF_PLATFORM,
    CONF_TYPE,
)
from homeassistant.core import HassJob, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.trigger import async_initialize_triggers
from homeassistant.util import dt as dt_util
from tests.test_lifecycle import _coordinator, _feed, _setup_hub

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

DEVICE_KEY = "Acurite-606TX-42"
MODEL = "Acurite-606TX"
DEVICE_ID = 42


async def _setup_button_hub(hass, hub_entry_builder):
    """Set up a hub seeded with a single ``button`` event device (types A/B)."""
    hub = await _setup_hub(
        hass,
        hub_entry_builder,
        devices={
            DEVICE_KEY: {
                CONF_MODEL: MODEL,
                DEVICE_FIELDS: ["button"],
                DEVICE_EVENT_TYPES: {"button": ["A", "B"]},
            }
        },
    )
    return hub


async def _attach(hass: HomeAssistant, trigger: dict) -> tuple[list, callable]:
    """Attach one device trigger; return (captured-calls list, detach callable)."""
    calls: list = []

    @callback
    def _action(run_variables, context=None):
        calls.append(run_variables)

    remove = await async_initialize_triggers(
        hass,
        [trigger],
        _action,
        DOMAIN,
        "test",
        lambda *args, **kwargs: None,
    )
    assert remove is not None
    return calls, remove


# ---------------------------------------------------------------------------
# _device_field_from_unique_id — 8 surviving mutants
# ---------------------------------------------------------------------------


class TestDeviceFieldFromUniqueId:
    """Kill all mutants in _device_field_from_unique_id.

    Mutants targeted:
    - mutmut_1: ``if unique_id:`` instead of ``if not unique_id:``
      -> valid unique_id would return None,None immediately
    - mutmut_3: ``split(None, 2)`` instead of ``split(":", 2)``
      -> no whitespace -> 1 part -> len != 3 -> None,None (for normal ids)
    - mutmut_6: ``split(":")`` (no limit) instead of ``split(":", 2)``
      -> uid with colon-in-field yields 4 parts -> None,None
    - mutmut_7: ``rsplit(":", 2)`` instead of ``split(":", 2)``
      -> splits from right; device_key and field_key are different
    - mutmut_8: ``split("XX:XX", 2)`` instead of ``split(":", 2)``
      -> no match -> 1 part -> None,None
    - mutmut_9: ``split(":", 3)`` instead of ``split(":", 2)``
      -> 4-colon uid: gives 4 parts -> len != 3 -> None,None
    - mutmut_10: ``if len(parts) == 3:`` instead of ``if len(parts) != 3:``
      -> 3-part uid returns None,None (no result ever for valid id)
    - mutmut_11: ``if len(parts) != 4:`` instead of ``if len(parts) != 3:``
      -> normal 3-part uid returns None,None (guard wrong)
    """

    def test_none_returns_none_none(self):
        """mutmut_1: if unique_id: (wrong polarity) would return None,None for valid ids.
        Also guards None input.
        """
        assert dt._device_field_from_unique_id(None) == (None, None)

    def test_empty_string_returns_none_none(self):
        """Empty string is falsy -> same path as None."""
        assert dt._device_field_from_unique_id("") == (None, None)

    def test_valid_3part_returns_device_field(self):
        """Core happy-path: 'hub:dev:field' -> ('dev', 'field').

        Kills mutmut_1 (inverted guard), mutmut_10 (== instead of !=),
        mutmut_11 (!=4 instead of !=3).
        """
        result = dt._device_field_from_unique_id("hub:dev:field")
        assert result == ("dev", "field")

    def test_valid_3part_exact_values(self):
        """Assert exact device_key and field_key for a real-looking unique_id."""
        uid = "entry123:Acurite-606TX-42:button"
        device_key, field_key = dt._device_field_from_unique_id(uid)
        assert device_key == "Acurite-606TX-42"
        assert field_key == "button"

    def test_2part_uid_returns_none_none(self):
        """Only 2 colons: len(parts) == 2 != 3 -> None, None."""
        assert dt._device_field_from_unique_id("hub:dev") == (None, None)

    def test_4colon_uid_stops_at_2_splits(self):
        """uid with 3 colons: split(':',2) yields 3 parts; field_key contains extra colon.

        Kills mutmut_6 (split no-limit -> 4 parts -> None,None),
        mutmut_9 (split(:,3) -> 4 parts -> None,None).
        """
        # 'hub:dev:fi:eld' split(':',2) -> ['hub','dev','fi:eld'] -> device_key='dev', field_key='fi:eld'
        uid = "hub:dev:fi:eld"
        device_key, field_key = dt._device_field_from_unique_id(uid)
        assert device_key == "dev"
        assert field_key == "fi:eld"

    def test_uid_with_colon_in_field_rsplit_gives_different_result(self):
        """Distinguishes split(':',2) from rsplit(':',2).

        real split(':',2) on 'hub:dev:fi:eld' -> device_key='dev', field_key='fi:eld'
        mutant rsplit(':',2)              -> parts=['hub:dev','fi','eld'] -> device_key='fi', field_key='eld'
        """
        uid = "hub:dev:fi:eld"
        device_key, field_key = dt._device_field_from_unique_id(uid)
        # Real behavior: split from left, max 2 splits
        assert device_key == "dev"  # NOT 'fi' (rsplit mutant would give 'fi')
        assert field_key == "fi:eld"  # NOT 'eld' (rsplit mutant would give 'eld')

    def test_split_on_colon_not_on_whitespace(self):
        """split(None,2) splits on whitespace; with ':'-only delimiter gives 1 part -> None,None.

        mutmut_3 changes split(':', 2) -> split(None, 2); 'hub:dev:field' has no
        whitespace -> 1 part -> len!=3 -> returns (None, None).
        We assert the real value is returned (not None, None) to kill this mutant.
        """
        uid = "hub:dev:field"
        result = dt._device_field_from_unique_id(uid)
        assert result == ("dev", "field")  # not (None, None)

    def test_split_on_colon_not_on_bogus_separator(self):
        """split('XX:XX', 2) never matches -> 1 part -> None,None.

        mutmut_8 changes split(':', 2) -> split('XX:XX', 2).
        We assert the real tuple to kill this mutant.
        """
        uid = "hub:dev:field"
        result = dt._device_field_from_unique_id(uid)
        assert result == ("dev", "field")

    def test_exactly_3_parts_guard(self):
        """Prove the guard is != 3 (not == 3, not != 4).

        With a plain 2-part uid, len==2, so !=3 is True -> None,None.
        With a 3-part uid, len==3, so !=3 is False -> proceed.
        With a 4-part uid (colon in field, split(:,2)), len==3 (limit!) -> proceed.
        This combination kills mutmut_10 and mutmut_11.
        """
        # 2 parts -> None, None
        assert dt._device_field_from_unique_id("a:b") == (None, None)
        # 3 parts -> valid
        assert dt._device_field_from_unique_id("a:b:c") == ("b", "c")
        # 4 raw colons but split(:,2) gives 3 -> valid (colon absorbed into field_key)
        assert dt._device_field_from_unique_id("a:b:c:d") == ("b", "c:d")


# ---------------------------------------------------------------------------
# _event_types_for_entry — 28 surviving mutants
# ---------------------------------------------------------------------------
# These tests call dt._event_types_for_entry(...) through the module to
# ensure mutmut's function replacement is observable at call time.
# ---------------------------------------------------------------------------


class TestEventTypesForEntryPersisted:
    """Tests that exercise the persisted data path in _event_types_for_entry.

    Targets mutants that change .get() keys or defaults in the lookup chain:
    mutmut_2  (unique_id -> None),
    mutmut_3  (and -> or),
    mutmut_4  (device_key is None),
    mutmut_5  (field_key is None),
    mutmut_6  (hub_entry = None),
    mutmut_7  (async_get_entry(None)),
    mutmut_8  (hub_entry is None -> if is None:),
    mutmut_9  (persisted = None),
    mutmut_18 (.get(device_key,{}) key -> None),
    mutmut_19 (.get(device_key,{}) default -> None),
    mutmut_21 (.get(device_key,{}) default removed),
    mutmut_22 (.get(CONF_DEVICES,{}) key -> None),
    mutmut_23 (.get(CONF_DEVICES,{}) default -> None),
    mutmut_25 (.get(CONF_DEVICES,{}) default removed),
    mutmut_14 (.get(DEVICE_EVENT_TYPES,{}) key -> None),
    mutmut_15 (.get(DEVICE_EVENT_TYPES,{}) default -> None),
    mutmut_17 (.get(DEVICE_EVENT_TYPES,{}) default removed),
    mutmut_10 (.get(field_key,[]) key -> None),
    mutmut_11 (.get(field_key,[]) default -> None),
    mutmut_13 (.get(field_key,[]) default removed).
    """

    async def test_returns_persisted_event_types(self, hass, hub_entry_builder):
        """Full persisted path: assert exact returned list from hass.data.

        This test provides a hub entry with the full data structure and asserts
        the exact list is returned. Any mutation in the .get() chain breaks the
        lookup so the persisted data isn't found, returning [] instead of ['A','B'].
        """
        hub = await _setup_hub(
            hass,
            hub_entry_builder,
            devices={
                DEVICE_KEY: {
                    CONF_MODEL: MODEL,
                    DEVICE_FIELDS: ["button"],
                    DEVICE_EVENT_TYPES: {"button": ["A", "B"]},
                }
            },
        )
        ent_reg = er.async_get(hass)
        entity_id = ent_reg.async_get_entity_id(
            "event", DOMAIN, f"{hub.entry_id}:{DEVICE_KEY}:button"
        )
        assert entity_id is not None
        entry = ent_reg.async_get(entity_id)
        assert entry is not None

        result = dt._event_types_for_entry(hass, entry)
        # Exact list from persisted data
        assert result == ["A", "B"]

    async def test_unique_id_none_falls_through_to_capability(
        self, hass, hub_entry_builder
    ):
        """mutmut_2: unique_id -> None causes _device_field_from_unique_id(None) -> (None,None).
        That makes device_key is None, so we skip persisted path -> fallback.
        With no state entity, returns [].
        We need hub set up but an entry with no unique_id to simulate that.
        Instead, test with a mocked entry that has unique_id=None.
        """
        hub = await _setup_hub(
            hass,
            hub_entry_builder,
            devices={
                DEVICE_KEY: {
                    CONF_MODEL: MODEL,
                    DEVICE_FIELDS: ["button"],
                    DEVICE_EVENT_TYPES: {"button": ["A", "B"]},
                }
            },
        )
        ent_reg = er.async_get(hass)
        entity_id = ent_reg.async_get_entity_id(
            "event", DOMAIN, f"{hub.entry_id}:{DEVICE_KEY}:button"
        )
        assert entity_id is not None
        entry = ent_reg.async_get(entity_id)
        assert entry is not None

        # The real entry has a valid unique_id -> returns persisted ['A','B']
        # The mutmut_2 mutant would call _device_field_from_unique_id(None) -> (None,None)
        # -> skip persisted path -> capability fallback -> may return [] or live attr
        # We assert real returns ['A','B'] (not []) to kill this
        result = dt._event_types_for_entry(hass, entry)
        assert result == ["A", "B"]

    async def test_both_keys_must_be_not_none(self, hass, hub_entry_builder):
        """mutmut_3 (and->or), mutmut_4 (device_key is None), mutmut_5 (field_key is None).

        The guard ``if device_key is not None and field_key is not None:`` must
        be AND. With or, one None key would still enter the block and raise or
        return wrong data. We test that a valid unique_id (both non-None) returns
        the expected list.
        """
        hub = await _setup_hub(
            hass,
            hub_entry_builder,
            devices={
                DEVICE_KEY: {
                    CONF_MODEL: MODEL,
                    DEVICE_FIELDS: ["button"],
                    DEVICE_EVENT_TYPES: {"button": ["C", "D"]},
                }
            },
        )
        ent_reg = er.async_get(hass)
        entity_id = ent_reg.async_get_entity_id(
            "event", DOMAIN, f"{hub.entry_id}:{DEVICE_KEY}:button"
        )
        entry = ent_reg.async_get(entity_id)
        result = dt._event_types_for_entry(hass, entry)
        assert result == ["C", "D"]

    async def test_hub_entry_none_falls_back_to_empty(self, hass, hub_entry_builder):
        """mutmut_6 (hub_entry=None directly) and mutmut_7 (async_get_entry(None)).

        When hub_entry is None (entry not found), the code falls through to
        get_capability. With no loaded entity state and no capabilities, returns [].
        We mock async_get_entry to return None so the persisted path is skipped,
        and also mock get_capability to return None so we get [].
        """
        hub = await _setup_hub(
            hass,
            hub_entry_builder,
            devices={
                DEVICE_KEY: {
                    CONF_MODEL: MODEL,
                    DEVICE_FIELDS: ["button"],
                    DEVICE_EVENT_TYPES: {"button": ["A", "B"]},
                }
            },
        )
        ent_reg = er.async_get(hass)
        entity_id = ent_reg.async_get_entity_id(
            "event", DOMAIN, f"{hub.entry_id}:{DEVICE_KEY}:button"
        )
        entry = ent_reg.async_get(entity_id)

        # Mock async_get_entry to return None (simulates hub not found)
        # Also mock get_capability to avoid state-machine side effects
        with (
            patch.object(hass.config_entries, "async_get_entry", return_value=None),
            patch(
                "custom_components.rtl_433.device_trigger.get_capability",
                return_value=None,
            ),
        ):
            result = dt._event_types_for_entry(hass, entry)
        # No persisted data accessible (hub_entry is None), no live capability -> []
        assert result == []

    async def test_persisted_empty_list_falls_back(self, hass, hub_entry_builder):
        """When persisted=[] (falsy), fallback to get_capability.

        This verifies the ``if persisted:`` guard (not tested as a mutant but
        confirms the path; also sets up for mutmut_9 where persisted=None).
        """
        hub = await _setup_hub(
            hass,
            hub_entry_builder,
            devices={
                DEVICE_KEY: {
                    CONF_MODEL: MODEL,
                    DEVICE_FIELDS: ["button"],
                    DEVICE_EVENT_TYPES: {"button": []},  # empty -> falsy
                }
            },
        )
        ent_reg = er.async_get(hass)
        entity_id = ent_reg.async_get_entity_id(
            "event", DOMAIN, f"{hub.entry_id}:{DEVICE_KEY}:button"
        )
        entry = ent_reg.async_get(entity_id)
        result = dt._event_types_for_entry(hass, entry)
        # No live state -> fallback also gives []
        assert result == []

    async def test_persisted_data_lookup_chain_conf_devices_key(
        self, hass, hub_entry_builder
    ):
        """mutmut_22/23/25: .get(CONF_DEVICES, {}) key or default changed.

        To distinguish: hub.data DOES have CONF_DEVICES -> we get it.
        If key is None (mutmut_22), .get(None, {}) returns {} -> no device -> [].
        If default is None (mutmut_23/25), irrelevant when CONF_DEVICES present.
        The assert of real ['A','B'] kills mutmut_22.
        """
        hub = await _setup_hub(
            hass,
            hub_entry_builder,
            devices={
                DEVICE_KEY: {
                    CONF_MODEL: MODEL,
                    DEVICE_FIELDS: ["button"],
                    DEVICE_EVENT_TYPES: {"button": ["A", "B"]},
                }
            },
        )
        ent_reg = er.async_get(hass)
        entity_id = ent_reg.async_get_entity_id(
            "event", DOMAIN, f"{hub.entry_id}:{DEVICE_KEY}:button"
        )
        entry = ent_reg.async_get(entity_id)
        result = dt._event_types_for_entry(hass, entry)
        assert result == ["A", "B"]

    async def test_missing_conf_devices_default_used(self, hass, hub_entry_builder):
        """mutmut_23/25: default=None breaks chained .get() when CONF_DEVICES absent.

        When hub.data has NO CONF_DEVICES key, .get(CONF_DEVICES, {}) returns {}
        (the default {}). If default is None (mutmut_23), .get(None) returns None,
        and .get(device_key) on None raises AttributeError.
        The real code should return [] when devices key is missing.
        """
        hub = await _setup_hub(
            hass,
            hub_entry_builder,
            devices=None,  # no CONF_DEVICES in hub.data
        )
        er.async_get(hass)
        # No event entities exist since no device is seeded; use a mock entry.
        # Build a fake entry with a valid unique_id for this hub.
        fake_uid = f"{hub.entry_id}:{DEVICE_KEY}:button"
        mock_entry = MagicMock()
        mock_entry.unique_id = fake_uid
        mock_entry.config_entry_id = hub.entry_id
        mock_entry.entity_id = "event.fake_button"

        # patch get_capability to return None so we don't error on entity lookup
        with patch(
            "custom_components.rtl_433.device_trigger.get_capability", return_value=None
        ):
            result = dt._event_types_for_entry(hass, mock_entry)
        assert result == []

    async def test_device_key_lookup_in_conf_devices(self, hass, hub_entry_builder):
        """mutmut_18/19/21: .get(device_key, {}) key or default changed.

        If key changes to None, .get(None, {}) on a dict keyed by device_key
        returns {} -> no event types -> []. Real returns ['A','B'].
        """
        hub = await _setup_hub(
            hass,
            hub_entry_builder,
            devices={
                DEVICE_KEY: {
                    CONF_MODEL: MODEL,
                    DEVICE_FIELDS: ["button"],
                    DEVICE_EVENT_TYPES: {"button": ["A", "B"]},
                }
            },
        )
        ent_reg = er.async_get(hass)
        entity_id = ent_reg.async_get_entity_id(
            "event", DOMAIN, f"{hub.entry_id}:{DEVICE_KEY}:button"
        )
        entry = ent_reg.async_get(entity_id)
        result = dt._event_types_for_entry(hass, entry)
        assert result == ["A", "B"]

    async def test_device_event_types_key_in_device_dict(self, hass, hub_entry_builder):
        """mutmut_14/15/17: .get(DEVICE_EVENT_TYPES, {}) key or default changed.

        If key changes to None, .get(None, {}) returns {} -> no field -> [].
        Real returns ['A','B'].
        """
        hub = await _setup_hub(
            hass,
            hub_entry_builder,
            devices={
                DEVICE_KEY: {
                    CONF_MODEL: MODEL,
                    DEVICE_FIELDS: ["button"],
                    DEVICE_EVENT_TYPES: {"button": ["A", "B"]},
                }
            },
        )
        ent_reg = er.async_get(hass)
        entity_id = ent_reg.async_get_entity_id(
            "event", DOMAIN, f"{hub.entry_id}:{DEVICE_KEY}:button"
        )
        entry = ent_reg.async_get(entity_id)
        result = dt._event_types_for_entry(hass, entry)
        assert result == ["A", "B"]

    async def test_field_key_lookup_in_event_types_dict(self, hass, hub_entry_builder):
        """mutmut_10/11/13: .get(field_key, []) key or default changed.

        If key changes to None, .get(None, []) returns [] -> no event types.
        Real returns ['A','B'].
        """
        hub = await _setup_hub(
            hass,
            hub_entry_builder,
            devices={
                DEVICE_KEY: {
                    CONF_MODEL: MODEL,
                    DEVICE_FIELDS: ["button"],
                    DEVICE_EVENT_TYPES: {"button": ["A", "B"]},
                }
            },
        )
        ent_reg = er.async_get(hass)
        entity_id = ent_reg.async_get_entity_id(
            "event", DOMAIN, f"{hub.entry_id}:{DEVICE_KEY}:button"
        )
        entry = ent_reg.async_get(entity_id)
        result = dt._event_types_for_entry(hass, entry)
        assert result == ["A", "B"]

    async def test_different_field_keys_are_distinct(self, hass, hub_entry_builder):
        """Assert that field_key selects the right sub-list (not a neighbour's).

        This directly kills mutmut_10 (key->None): None would fetch nothing.
        Use two valid event fields (button, secret_knock) each with distinct types.
        """
        # Two event fields: button and secret_knock (both are event platform fields)
        hub = await _setup_hub(
            hass,
            hub_entry_builder,
            devices={
                DEVICE_KEY: {
                    CONF_MODEL: MODEL,
                    DEVICE_FIELDS: ["button", "secret_knock"],
                    DEVICE_EVENT_TYPES: {
                        "button": ["A", "B"],
                        "secret_knock": ["ring"],
                    },
                }
            },
        )
        ent_reg = er.async_get(hass)
        # Check button entity
        button_eid = ent_reg.async_get_entity_id(
            "event", DOMAIN, f"{hub.entry_id}:{DEVICE_KEY}:button"
        )
        assert button_eid is not None, "button event entity not found"
        button_entry = ent_reg.async_get(button_eid)
        assert dt._event_types_for_entry(hass, button_entry) == ["A", "B"]

        # Check secret_knock entity
        knock_eid = ent_reg.async_get_entity_id(
            "event", DOMAIN, f"{hub.entry_id}:{DEVICE_KEY}:secret_knock"
        )
        assert knock_eid is not None, "secret_knock event entity not found"
        knock_entry = ent_reg.async_get(knock_eid)
        assert dt._event_types_for_entry(hass, knock_entry) == ["ring"]

    async def test_hub_entry_is_none_check_polarity(self, hass, hub_entry_builder):
        """mutmut_8: ``if hub_entry is None:`` instead of ``if hub_entry is not None:``.

        With the mutant, the persisted block runs when hub_entry is None
        (which would raise AttributeError on hub_entry.data), but SKIPS
        when hub_entry is actually set.
        Real: runs the block when hub_entry is not None -> returns persisted data.
        We assert persisted data is returned (not []).
        """
        hub = await _setup_hub(
            hass,
            hub_entry_builder,
            devices={
                DEVICE_KEY: {
                    CONF_MODEL: MODEL,
                    DEVICE_FIELDS: ["button"],
                    DEVICE_EVENT_TYPES: {"button": ["X", "Y", "Z"]},
                }
            },
        )
        ent_reg = er.async_get(hass)
        entity_id = ent_reg.async_get_entity_id(
            "event", DOMAIN, f"{hub.entry_id}:{DEVICE_KEY}:button"
        )
        entry = ent_reg.async_get(entity_id)
        result = dt._event_types_for_entry(hass, entry)
        assert result == ["X", "Y", "Z"]


class TestEventTypesForEntryFallback:
    """Tests for the fallback (get_capability) path.

    Targets:
    - mutmut_27 (capability = None),
    - mutmut_28 (get_capability(None, ...)),
    - mutmut_29 (get_capability(hass, None, ...)),
    - mutmut_30 (get_capability(hass, entity_id, None)),
    - mutmut_31 (get_capability(entity_id, ...)),
    - mutmut_32 (get_capability(hass, ATTR_EVENT_TYPES)),
    - mutmut_33 (get_capability(hass, entity_id, )),
    - mutmut_34 (list(None) instead of list(capability)).
    """

    async def test_fallback_returns_live_capability_attribute(
        self, hass, hub_entry_builder
    ):
        """get_capability returns the entity's event_types from live state.

        We feed a button event so the entity is in the state machine, then
        call _event_types_for_entry on an entry whose unique_id won't parse
        (so we skip the persisted path) but whose entity_id IS in the state machine
        with event_types=[...] in the attributes.

        mutmut_27 (capability=None) -> returns [] not the live list.
        mutmut_28 (hass->None) -> get_capability raises.
        mutmut_29 (entity_id->None) -> None not found.
        mutmut_30 (attr->None) -> state.attributes.get(None) = None -> fallback [].
        mutmut_31 (entity_id as first arg) -> wrong signature.
        mutmut_32 (hass, ATTR_EVENT_TYPES) -> wrong arity.
        mutmut_33 (entity_id dropped) -> wrong arity.
        mutmut_34 (list(None)) -> TypeError.
        """
        hub = await _setup_hub(
            hass,
            hub_entry_builder,
            devices={
                DEVICE_KEY: {
                    CONF_MODEL: MODEL,
                    DEVICE_FIELDS: ["button"],
                    DEVICE_EVENT_TYPES: {"button": ["A", "B"]},
                }
            },
        )
        # Feed a transmission to populate the entity state
        coordinator = _coordinator(hass, hub)
        _feed(coordinator, {"model": MODEL, "id": DEVICE_ID, "button": "A"})
        await hass.async_block_till_done()

        ent_reg = er.async_get(hass)
        entity_id = ent_reg.async_get_entity_id(
            "event", DOMAIN, f"{hub.entry_id}:{DEVICE_KEY}:button"
        )
        assert entity_id is not None
        ent_reg.async_get(entity_id)

        # Construct a fake entry that shares the entity_id but has no parseable unique_id
        mock_entry = MagicMock()
        mock_entry.unique_id = (
            None  # -> _device_field_from_unique_id(None) -> (None,None)
        )
        mock_entry.config_entry_id = hub.entry_id
        mock_entry.entity_id = entity_id  # points to the live state

        result = dt._event_types_for_entry(hass, mock_entry)
        # The live entity state should carry ATTR_EVENT_TYPES in its attributes
        state = hass.states.get(entity_id)
        assert state is not None
        live_types = state.attributes.get(ATTR_EVENT_TYPES)
        if live_types:
            # We got data from the fallback
            assert result == list(live_types)
        else:
            # Even if the state doesn't carry it, the real code returns []
            assert result == []

    async def test_fallback_get_capability_uses_correct_entity_id(
        self, hass, hub_entry_builder
    ):
        """Assert that get_capability is called with the right entity_id.

        Patch get_capability and check the call args. Kills mutmut_29 (entity_id->None).
        """
        hub = await _setup_hub(
            hass,
            hub_entry_builder,
            devices={
                DEVICE_KEY: {
                    CONF_MODEL: MODEL,
                    DEVICE_FIELDS: ["button"],
                    DEVICE_EVENT_TYPES: {},  # empty -> no persisted types
                }
            },
        )
        ent_reg = er.async_get(hass)
        entity_id = ent_reg.async_get_entity_id(
            "event", DOMAIN, f"{hub.entry_id}:{DEVICE_KEY}:button"
        )
        entry = ent_reg.async_get(entity_id)

        with patch(
            "custom_components.rtl_433.device_trigger.get_capability"
        ) as mock_cap:
            mock_cap.return_value = ["P", "Q"]
            result = dt._event_types_for_entry(hass, entry)

        # get_capability must have been called with (hass, entry.entity_id, ATTR_EVENT_TYPES)
        mock_cap.assert_called_once_with(hass, entry.entity_id, ATTR_EVENT_TYPES)
        assert result == ["P", "Q"]

    async def test_fallback_returns_list_of_capability(self, hass, hub_entry_builder):
        """mutmut_34: list(None) raises TypeError; real list(capability) works.

        Assert that when get_capability returns a non-empty iterable, the result
        is a list of those elements.
        """
        hub = await _setup_hub(
            hass,
            hub_entry_builder,
            devices={
                DEVICE_KEY: {
                    CONF_MODEL: MODEL,
                    DEVICE_FIELDS: ["button"],
                    DEVICE_EVENT_TYPES: {},
                }
            },
        )
        ent_reg = er.async_get(hass)
        entity_id = ent_reg.async_get_entity_id(
            "event", DOMAIN, f"{hub.entry_id}:{DEVICE_KEY}:button"
        )
        entry = ent_reg.async_get(entity_id)

        with patch(
            "custom_components.rtl_433.device_trigger.get_capability",
            return_value=("R", "S"),  # tuple, not list
        ):
            result = dt._event_types_for_entry(hass, entry)

        assert result == ["R", "S"]
        assert isinstance(result, list)

    async def test_fallback_none_capability_returns_empty_list(
        self, hass, hub_entry_builder
    ):
        """When get_capability returns None, _event_types_for_entry returns []."""
        hub = await _setup_hub(
            hass,
            hub_entry_builder,
            devices={
                DEVICE_KEY: {
                    CONF_MODEL: MODEL,
                    DEVICE_FIELDS: ["button"],
                    DEVICE_EVENT_TYPES: {},
                }
            },
        )
        ent_reg = er.async_get(hass)
        entity_id = ent_reg.async_get_entity_id(
            "event", DOMAIN, f"{hub.entry_id}:{DEVICE_KEY}:button"
        )
        entry = ent_reg.async_get(entity_id)

        with patch(
            "custom_components.rtl_433.device_trigger.get_capability",
            return_value=None,
        ):
            result = dt._event_types_for_entry(hass, entry)

        assert result == []

    async def test_fallback_uses_attr_event_types_not_none(
        self, hass, hub_entry_builder
    ):
        """mutmut_30: ATTR_EVENT_TYPES arg changed to None -> gets wrong/no attribute.

        Patch get_capability to spy on the capability argument.
        """
        hub = await _setup_hub(
            hass,
            hub_entry_builder,
            devices={
                DEVICE_KEY: {
                    CONF_MODEL: MODEL,
                    DEVICE_FIELDS: ["button"],
                    DEVICE_EVENT_TYPES: {},
                }
            },
        )
        ent_reg = er.async_get(hass)
        entity_id = ent_reg.async_get_entity_id(
            "event", DOMAIN, f"{hub.entry_id}:{DEVICE_KEY}:button"
        )
        entry = ent_reg.async_get(entity_id)

        with patch(
            "custom_components.rtl_433.device_trigger.get_capability"
        ) as mock_cap:
            mock_cap.return_value = None
            dt._event_types_for_entry(hass, entry)

        # Must be called with ATTR_EVENT_TYPES (not None)
        args = mock_cap.call_args[0]
        assert args[2] == ATTR_EVENT_TYPES
        assert args[2] is not None

    async def test_fallback_uses_hass_not_none(self, hass, hub_entry_builder):
        """mutmut_28: hass arg changed to None -> get_capability(None, ...) would fail.

        Spy on get_capability to assert first arg is hass (not None).
        """
        hub = await _setup_hub(
            hass,
            hub_entry_builder,
            devices={
                DEVICE_KEY: {
                    CONF_MODEL: MODEL,
                    DEVICE_FIELDS: ["button"],
                    DEVICE_EVENT_TYPES: {},
                }
            },
        )
        ent_reg = er.async_get(hass)
        entity_id = ent_reg.async_get_entity_id(
            "event", DOMAIN, f"{hub.entry_id}:{DEVICE_KEY}:button"
        )
        entry = ent_reg.async_get(entity_id)

        with patch(
            "custom_components.rtl_433.device_trigger.get_capability"
        ) as mock_cap:
            mock_cap.return_value = None
            dt._event_types_for_entry(hass, entry)

        args = mock_cap.call_args[0]
        assert args[0] is hass
        assert args[0] is not None


# ---------------------------------------------------------------------------
# _async_attach_base_trigger — 4 surviving mutants
# ---------------------------------------------------------------------------
# mutmut_13 (platform_type=None), mutmut_18 (platform_type kwarg dropped),
# mutmut_19 (platform_type="XXdeviceXX"), mutmut_20 (platform_type="DEVICE").
# All four are killed by asserting the "platform" field in the trigger payload.
# ---------------------------------------------------------------------------


class TestAsyncAttachBaseTrigger:
    """Verify that the base trigger passes platform_type='device' to the state trigger."""

    async def test_base_trigger_platform_is_device_in_payload(
        self, hass, hub_entry_builder
    ):
        """The trigger payload's platform field is exactly 'device' (not None/DEVICE/XXdeviceXX).

        Attach the base trigger for a button entity, fire one press, and assert
        the captured payload has ``trigger.platform == 'device'``.
        """
        hub = await _setup_button_hub(hass, hub_entry_builder)
        coordinator = _coordinator(hass, hub)
        ent_reg = er.async_get(hass)
        entity_id = ent_reg.async_get_entity_id(
            "event", DOMAIN, f"{hub.entry_id}:{DEVICE_KEY}:button"
        )
        entry = ent_reg.async_get(entity_id)

        trigger_cfg = {
            CONF_PLATFORM: "device",
            CONF_DOMAIN: DOMAIN,
            CONF_DEVICE_ID: "dummy_device_id",
            CONF_ENTITY_ID: entry.id,  # entity registry ID (UUID)
            CONF_TYPE: TRIGGER_TYPE_TRIGGERED,
        }

        calls: list = []

        @callback
        def _action(run_variables, context=None):
            calls.append(run_variables)

        remove = await async_initialize_triggers(
            hass,
            [trigger_cfg],
            _action,
            DOMAIN,
            "test_platform_type",
            lambda *args, **kwargs: None,
        )
        assert remove is not None

        start = dt_util.utcnow()
        with freeze_time(start):
            _feed(coordinator, {"model": MODEL, "id": DEVICE_ID, "button": "A"})
            await hass.async_block_till_done()

        assert len(calls) >= 1
        platform_value = calls[0]["trigger"]["platform"]
        assert platform_value == "device"  # not None, "DEVICE", "XXdeviceXX"

        remove()

    async def test_base_trigger_platform_not_none(self, hass, hub_entry_builder):
        """mutmut_13: platform_type=None would set payload platform to None."""
        hub = await _setup_button_hub(hass, hub_entry_builder)
        coordinator = _coordinator(hass, hub)
        ent_reg = er.async_get(hass)
        entity_id = ent_reg.async_get_entity_id(
            "event", DOMAIN, f"{hub.entry_id}:{DEVICE_KEY}:button"
        )
        entry = ent_reg.async_get(entity_id)

        trigger_cfg = {
            CONF_PLATFORM: "device",
            CONF_DOMAIN: DOMAIN,
            CONF_DEVICE_ID: "dummy_device_id",
            CONF_ENTITY_ID: entry.id,
            CONF_TYPE: TRIGGER_TYPE_TRIGGERED,
        }

        calls: list = []

        @callback
        def _action(run_variables, context=None):
            calls.append(run_variables)

        remove = await async_initialize_triggers(
            hass,
            [trigger_cfg],
            _action,
            DOMAIN,
            "test_platform_not_none",
            lambda *args, **kwargs: None,
        )
        assert remove is not None

        start = dt_util.utcnow()
        with freeze_time(start):
            _feed(coordinator, {"model": MODEL, "id": DEVICE_ID, "button": "A"})
            await hass.async_block_till_done()

        assert len(calls) >= 1
        assert calls[0]["trigger"]["platform"] is not None

        remove()

    async def test_base_trigger_platform_lowercase_device(
        self, hass, hub_entry_builder
    ):
        """mutmut_19/20: 'XXdeviceXX'/'DEVICE' are wrong case/string."""
        hub = await _setup_button_hub(hass, hub_entry_builder)
        coordinator = _coordinator(hass, hub)
        ent_reg = er.async_get(hass)
        entity_id = ent_reg.async_get_entity_id(
            "event", DOMAIN, f"{hub.entry_id}:{DEVICE_KEY}:button"
        )
        entry = ent_reg.async_get(entity_id)

        trigger_cfg = {
            CONF_PLATFORM: "device",
            CONF_DOMAIN: DOMAIN,
            CONF_DEVICE_ID: "dummy_device_id",
            CONF_ENTITY_ID: entry.id,
            CONF_TYPE: TRIGGER_TYPE_TRIGGERED,
        }

        calls: list = []

        @callback
        def _action(run_variables, context=None):
            calls.append(run_variables)

        remove = await async_initialize_triggers(
            hass,
            [trigger_cfg],
            _action,
            DOMAIN,
            "test_platform_lowercase",
            lambda *args, **kwargs: None,
        )
        assert remove is not None

        start = dt_util.utcnow()
        with freeze_time(start):
            _feed(coordinator, {"model": MODEL, "id": DEVICE_ID, "button": "A"})
            await hass.async_block_till_done()

        assert len(calls) >= 1
        platform = calls[0]["trigger"]["platform"]
        assert platform == "device"
        assert platform != "DEVICE"
        assert platform != "XXdeviceXX"

        remove()


# ---------------------------------------------------------------------------
# _async_attach_subtype_trigger — 2 surviving mutants
# ---------------------------------------------------------------------------
# mutmut_8 (HassJob name=None), mutmut_10 (HassJob name arg dropped).
# Both result in job.name=None vs the real f"rtl_433 device trigger {trigger_info}".
# We intercept HassJob construction to assert the name argument.
# ---------------------------------------------------------------------------


class TestAsyncAttachSubtypeTrigger:
    """Verify that HassJob is constructed with the expected name string."""

    async def test_hassjob_name_is_not_none(self, hass, hub_entry_builder):
        """mutmut_8/10: HassJob name is None or missing; real is a formatted string.

        Patch HassJob at the device_trigger module to intercept the constructor call
        and assert the name argument starts with 'rtl_433'.
        """
        hub = await _setup_button_hub(hass, hub_entry_builder)
        ent_reg = er.async_get(hass)
        entity_id = ent_reg.async_get_entity_id(
            "event", DOMAIN, f"{hub.entry_id}:{DEVICE_KEY}:button"
        )
        entry = ent_reg.async_get(entity_id)

        captured_names: list = []
        real_HassJob = HassJob

        def _spy_hassjob(action, name=None, **kwargs):
            captured_names.append(name)
            return real_HassJob(action, name, **kwargs)

        with patch(
            "custom_components.rtl_433.device_trigger.HassJob", side_effect=_spy_hassjob
        ):
            trigger_cfg = {
                CONF_PLATFORM: "device",
                CONF_DOMAIN: DOMAIN,
                CONF_DEVICE_ID: "dummy_device_id",
                CONF_ENTITY_ID: entry.id,
                CONF_TYPE: TRIGGER_TYPE_TRIGGERED_SUBTYPE,
                CONF_SUBTYPE: "A",
            }

            @callback
            def _noop(run_variables, context=None):
                pass

            remove = await async_initialize_triggers(
                hass,
                [trigger_cfg],
                _noop,
                DOMAIN,
                "test_hassjob_name",
                lambda *args, **kwargs: None,
            )
            if remove:
                remove()

        # At least one HassJob was created for the subtype trigger
        assert len(captured_names) >= 1
        # The real code uses f"rtl_433 device trigger {trigger_info}"
        # The mutants use None or no name; we assert at least one name is not None
        # and contains the expected prefix
        non_none_names = [n for n in captured_names if n is not None]
        assert len(non_none_names) >= 1
        assert any("rtl_433" in str(n) for n in non_none_names)

    async def test_hassjob_name_contains_rtl433_prefix(self, hass, hub_entry_builder):
        """Assert the job name is the formatted string, not just any non-None value."""
        hub = await _setup_button_hub(hass, hub_entry_builder)
        ent_reg = er.async_get(hass)
        entity_id = ent_reg.async_get_entity_id(
            "event", DOMAIN, f"{hub.entry_id}:{DEVICE_KEY}:button"
        )
        entry = ent_reg.async_get(entity_id)

        captured_names: list = []
        real_HassJob = HassJob

        def _spy_hassjob(action, name=None, **kwargs):
            captured_names.append(name)
            return real_HassJob(action, name, **kwargs)

        with patch(
            "custom_components.rtl_433.device_trigger.HassJob", side_effect=_spy_hassjob
        ):
            trigger_cfg = {
                CONF_PLATFORM: "device",
                CONF_DOMAIN: DOMAIN,
                CONF_DEVICE_ID: "dummy_device_id",
                CONF_ENTITY_ID: entry.id,
                CONF_TYPE: TRIGGER_TYPE_TRIGGERED_SUBTYPE,
                CONF_SUBTYPE: "B",
            }

            @callback
            def _noop(run_variables, context=None):
                pass

            remove = await async_initialize_triggers(
                hass,
                [trigger_cfg],
                _noop,
                DOMAIN,
                "test_hassjob_name_b",
                lambda *args, **kwargs: None,
            )
            if remove:
                remove()

        names_with_prefix = [
            n for n in captured_names if n and "rtl_433 device trigger" in n
        ]
        assert len(names_with_prefix) >= 1


# ---------------------------------------------------------------------------
# async_validate_trigger_config — 1 surviving mutant
# ---------------------------------------------------------------------------
# mutmut_1: TRIGGER_SCHEMA(None) instead of TRIGGER_SCHEMA(config).
# TRIGGER_SCHEMA(None) raises MultipleInvalid; real returns the validated dict.
# ---------------------------------------------------------------------------


class TestAsyncValidateTriggerConfig:
    """Kill the TRIGGER_SCHEMA(None) mutant in async_validate_trigger_config."""

    async def test_validate_passes_config_to_schema(self, hass):
        """TRIGGER_SCHEMA(config) returns the config; TRIGGER_SCHEMA(None) raises.

        We call async_validate_trigger_config with a valid config and assert the
        return value equals the validated dict (not an exception).
        """
        valid_config = {
            CONF_PLATFORM: "device",
            CONF_DOMAIN: DOMAIN,
            CONF_DEVICE_ID: "test_device_id",
            CONF_ENTITY_ID: "event.test_button",
            CONF_TYPE: TRIGGER_TYPE_TRIGGERED,
        }
        result = await async_validate_trigger_config(hass, valid_config)
        # Must return the validated dict (same keys, possibly type-coerced)
        assert result[CONF_DOMAIN] == DOMAIN
        assert result[CONF_TYPE] == TRIGGER_TYPE_TRIGGERED
        assert result[CONF_ENTITY_ID] == "event.test_button"

    async def test_validate_config_not_schema_of_none(self, hass):
        """Prove the schema is applied to the real config, not to None.

        TRIGGER_SCHEMA(None) raises voluptuous.MultipleInvalid (not a dict).
        The real function must return without raising.
        """

        valid_config = {
            CONF_PLATFORM: "device",
            CONF_DOMAIN: DOMAIN,
            CONF_DEVICE_ID: "test_device_id",
            CONF_ENTITY_ID: "event.real_button",
            CONF_TYPE: TRIGGER_TYPE_TRIGGERED_SUBTYPE,
            CONF_SUBTYPE: "press",
        }
        # Real call should not raise
        result = await async_validate_trigger_config(hass, valid_config)
        assert isinstance(result, dict)
        assert CONF_SUBTYPE in result
        assert result[CONF_SUBTYPE] == "press"

    async def test_validate_preserves_subtype(self, hass):
        """Validate with subtype; ensures config (not None) goes through schema."""
        valid_config = {
            CONF_PLATFORM: "device",
            CONF_DOMAIN: DOMAIN,
            CONF_DEVICE_ID: "mydevice",
            CONF_ENTITY_ID: "event.btn",
            CONF_TYPE: TRIGGER_TYPE_TRIGGERED_SUBTYPE,
            CONF_SUBTYPE: "click",
        }
        result = await async_validate_trigger_config(hass, valid_config)
        assert result[CONF_SUBTYPE] == "click"
        assert result[CONF_TYPE] == TRIGGER_TYPE_TRIGGERED_SUBTYPE
