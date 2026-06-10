"""Device-automation triggers for the integration's ``event`` entities.

This module is discovered by Home Assistant's device-automation machinery purely
by its presence at ``custom_components/rtl_433/device_trigger.py`` (mapping
``DeviceAutomationType.TRIGGER`` -> module name ``device_trigger``); it is **not**
an entity platform and must **not** be added to ``const.py`` ``PLATFORMS``.

It exposes **triggers only** (no conditions, no actions). For every ``event``
entity of an rtl_433 device (button / motion / doorbell), it offers:

* one **base** trigger ("<entity> triggered") that fires on every genuine
  transmission, and
* one optional **subtyped** trigger per persisted ``event_type``
  ("<entity> triggered: <code>") that fires only for that specific value.

**Both triggers use one custom ``async_track_state_change_event`` listener**
(``_async_attach_event_trigger``). ``Rtl433Event`` writes a fresh timestamp state
on every genuine transmission (``event.py``; core ``EventEntity._trigger_event``),
so a state change *is* a transmission. The listener fires with **no**
``old == new`` dedupe, so two consecutive presses of the same button each fire —
the reason neither trigger can reuse the core ``state`` trigger's
``attribute``/``to`` filter, which early-returns when ``old_value == new_value``
(``homeassistant/components/homeassistant/triggers/state.py``). The base trigger
fires on every transmission; a subtyped trigger fires only when the new state's
``event_type`` equals its ``subtype``.

**Neither fires on the entity's restore at startup.** Across a restart HA's
``EventEntity`` restores its last ``event_type`` + timestamp (for display), which
surfaces as a ``state_changed`` with ``old_state is None`` carrying the old
``event_type`` — and a raw listener would re-deliver that stale event (e.g. a
doorbell "ring" from days ago) on **every** HA restart. A momentary event never
legitimately fires on the entity's first appearance in the state machine, so the
listener ignores a ``None`` ``old_state`` (the restore / initial add).
"""

from __future__ import annotations

import voluptuous as vol

from homeassistant.components.device_automation import (
    DEVICE_TRIGGER_BASE_SCHEMA,
    async_get_entity_registry_entry_or_raise,
)
from homeassistant.components.event.const import ATTR_EVENT_TYPE, ATTR_EVENT_TYPES
from homeassistant.const import (
    CONF_DEVICE_ID,
    CONF_DOMAIN,
    CONF_ENTITY_ID,
    CONF_PLATFORM,
    CONF_TYPE,
)
from homeassistant.core import (
    CALLBACK_TYPE,
    Event,
    EventStateChangedData,
    HassJob,
    HomeAssistant,
    callback,
)
from homeassistant.helpers import config_validation as cv, entity_registry as er
from homeassistant.helpers.entity import get_capability
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.trigger import TriggerActionType, TriggerInfo
from homeassistant.helpers.typing import ConfigType

from .const import CONF_DEVICES, DEVICE_EVENT_TYPES, DOMAIN

# Two trigger ``type`` values per event entity: a base "triggered" (fires on any
# transmission) and a "triggered_subtype" whose specific ``event_type`` value is
# carried in the separate ``subtype`` field. The two distinct ``type`` values map
# to the two ``device_automation.trigger_type`` translation keys so the picker
# renders "<entity> triggered" vs. "<entity> triggered: <code>" (the frontend
# substitutes the raw ``subtype`` into ``{subtype}``).
CONF_SUBTYPE = "subtype"
TRIGGER_TYPE_TRIGGERED = "triggered"
TRIGGER_TYPE_TRIGGERED_SUBTYPE = "triggered_subtype"

# Event-platform domain (``"event"``); kept local so enumeration filters to this
# integration's event entities without importing the whole event component.
EVENT_DOMAIN = "event"

TRIGGER_SCHEMA = DEVICE_TRIGGER_BASE_SCHEMA.extend(
    {
        vol.Required(CONF_ENTITY_ID): cv.entity_id_or_uuid,
        vol.Required(CONF_TYPE): vol.In(
            [TRIGGER_TYPE_TRIGGERED, TRIGGER_TYPE_TRIGGERED_SUBTYPE]
        ),
        vol.Optional(CONF_SUBTYPE): cv.string,
    }
)


async def async_validate_trigger_config(
    hass: HomeAssistant, config: ConfigType
) -> ConfigType:
    """Validate the device-trigger config against ``TRIGGER_SCHEMA``."""
    return TRIGGER_SCHEMA(config)


async def async_get_triggers(
    hass: HomeAssistant, device_id: str
) -> list[dict[str, str]]:
    """List the device triggers for a device's rtl_433 event entities.

    Enumerates the device's entities from the entity registry, keeps only this
    integration's ``event`` entities, and returns one base trigger per entity
    plus one subtyped trigger per persisted (with a live-attribute fallback)
    ``event_type``.
    """
    entity_registry = er.async_get(hass)

    triggers: list[dict[str, str]] = []
    for entry in er.async_entries_for_device(entity_registry, device_id):
        if entry.domain != EVENT_DOMAIN or entry.platform != DOMAIN:
            continue

        base = {
            CONF_PLATFORM: "device",
            CONF_DOMAIN: DOMAIN,
            CONF_DEVICE_ID: device_id,
            CONF_ENTITY_ID: entry.id,
            CONF_TYPE: TRIGGER_TYPE_TRIGGERED,
        }
        triggers.append(base)

        for event_type in _event_types_for_entry(hass, entry):
            triggers.append(
                {
                    **base,
                    CONF_TYPE: TRIGGER_TYPE_TRIGGERED_SUBTYPE,
                    CONF_SUBTYPE: event_type,
                }
            )

    return triggers


def _event_types_for_entry(hass: HomeAssistant, entry: er.RegistryEntry) -> list[str]:
    """Return the known ``event_type`` values for one event entity.

    Prefers the restart-surviving persisted list under
    ``entry.data[CONF_DEVICES][device_key][DEVICE_EVENT_TYPES][field_key]``,
    falling back to the entity's live ``event_types`` capability attribute when
    the entity is loaded but nothing is persisted yet.
    """
    device_key, field_key = _device_field_from_unique_id(entry.unique_id)
    if device_key is not None and field_key is not None:
        hub_entry = hass.config_entries.async_get_entry(entry.config_entry_id)
        if hub_entry is not None:
            persisted = (
                hub_entry.data.get(CONF_DEVICES, {})
                .get(device_key, {})
                .get(DEVICE_EVENT_TYPES, {})
                .get(field_key, [])
            )
            if persisted:
                return list(persisted)

    # Fallback: the loaded entity's live capability attribute.
    capability = get_capability(hass, entry.entity_id, ATTR_EVENT_TYPES)
    if capability:
        return list(capability)
    return []


def _device_field_from_unique_id(
    unique_id: str | None,
) -> tuple[str | None, str | None]:
    """Recover ``(device_key, field_key)`` from an event entity's unique_id.

    The unique_id is ``f"{hub_entry_id}:{device_key}:{object_suffix}"``
    (``entity.py``); the ``device_key`` never contains a colon (it is built from
    safe tokens, ``normalizer.device_key``) and an event field's
    ``object_suffix`` equals its ``field_key`` (``device_library/events.yaml``).
    """
    if not unique_id:
        return None, None
    parts = unique_id.split(":", 2)
    if len(parts) != 3:
        return None, None
    _hub_entry_id, device_key, field_key = parts
    return device_key, field_key


async def async_attach_trigger(
    hass: HomeAssistant,
    config: ConfigType,
    action: TriggerActionType,
    trigger_info: TriggerInfo,
) -> CALLBACK_TYPE:
    """Attach the configured device trigger to the automation engine.

    Both the base trigger (no ``subtype``) and a subtyped trigger use the same
    custom state-change listener; ``subtype`` (``None`` for the base) decides
    whether the ``event_type`` filter applies.
    """
    return await _async_attach_event_trigger(
        hass, config, action, trigger_info, config.get(CONF_SUBTYPE)
    )


async def _async_attach_event_trigger(
    hass: HomeAssistant,
    config: ConfigType,
    action: TriggerActionType,
    trigger_info: TriggerInfo,
    subtype: str | None,
) -> CALLBACK_TYPE:
    """Attach a custom state-change listener that fires once per transmission.

    Shared by the base trigger (``subtype is None`` — fires on every
    transmission) and a subtyped trigger (fires only when the new state's
    ``event_type`` equals ``subtype``). Two behaviours both paths need:

    * **No ``old == new`` dedupe** — every matching transmission fires, so two
      consecutive same-value presses each fire. This is the reason neither path
      can reuse the core ``state`` trigger's ``attribute``/``to`` filter, which
      early-returns on ``old_value == new_value``.
    * **Ignore the entity's restore at startup** — ``Rtl433Event`` (via HA's
      ``EventEntity``) restores its last ``event_type`` + timestamp across a
      restart for display, surfacing as a ``state_changed`` with
      ``old_state is None``. A momentary event never legitimately fires on the
      entity's first appearance in the state machine (a genuine transmission
      always transitions from a pre-existing state), so a ``None`` ``old_state``
      is the restore / initial add and must not re-deliver the stale event (the
      "doorbell re-fires on every HA restart" bug).

    The trigger payload + context match what the core state trigger produces for
    a ``device``-platform trigger.
    """
    entity_id = async_get_entity_registry_entry_or_raise(
        hass, config[CONF_ENTITY_ID]
    ).entity_id

    job = HassJob(action, f"rtl_433 device trigger {trigger_info}")
    trigger_data = trigger_info["trigger_data"]

    @callback
    def _listener(event: Event[EventStateChangedData]) -> None:
        """Fire the action for a genuine transmission (not the startup restore)."""
        old_state = event.data["old_state"]
        new_state = event.data["new_state"]
        # ``old_state is None`` is the entity's restore / initial add, not a
        # transmission: suppress it so a restored event_type does not re-fire on
        # every HA restart. ``new_state is None`` is the entity being removed.
        if old_state is None or new_state is None:
            return
        # Subtyped trigger: only the matching ``event_type`` fires. No
        # ``old == new`` dedupe, so consecutive same-value presses each fire.
        if subtype is not None and new_state.attributes.get(ATTR_EVENT_TYPE) != subtype:
            return
        description = (
            f"event {subtype} on {entity_id}"
            if subtype is not None
            else f"event on {entity_id}"
        )
        hass.async_run_hass_job(
            job,
            {
                "trigger": {
                    **trigger_data,
                    "platform": "device",
                    "entity_id": entity_id,
                    "description": description,
                }
            },
            new_state.context,
        )

    return async_track_state_change_event(hass, [entity_id], _listener)
