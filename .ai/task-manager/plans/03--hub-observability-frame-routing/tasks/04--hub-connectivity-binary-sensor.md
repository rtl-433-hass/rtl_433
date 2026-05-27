---
id: 4
group: "entities"
dependencies: [1]
status: "completed"
created: "2026-05-26"
skills:
  - python
  - home-assistant
---
# Hub connectivity binary_sensor + hub entity base

## Objective
Surface the WebSocket connection state immediately. Add a shared hub-entity base
that attaches to the existing hub device `(DOMAIN, entry_id)` and subscribes to
the hub-update dispatcher signal, then register a single
`device_class=connectivity` binary_sensor whose state follows
`coordinator.connected`: on while the socket is open, off while the connect loop
is retrying or after a `shutdown` frame. The connectivity entity is itself always
available (it *reports* connectivity), so it does not use the per-device
availability/timeout model.

## Skills Required
- `python` — entity subclassing, dispatcher subscription lifecycle.
- `home-assistant` — `BinarySensorEntity`, `DeviceInfo`, `EntityCategory`, dispatcher.

## Acceptance Criteria
- [ ] A new hub-entity base (`Rtl433HubEntity`, in `entity.py`) attaches to the hub device via `DeviceInfo(identifiers={(DOMAIN, hub_entry_id)})`, sets `_attr_has_entity_name = True` and `_attr_should_poll = False`, subscribes to `signal_hub_update(hub_entry_id)` in `async_added_to_hass`, and unsubscribes in `async_will_remove_from_hass`. It does **not** inherit the per-device timeout availability of `Rtl433Entity`.
- [ ] `binary_sensor.py` defines `Rtl433HubConnectivity(Rtl433HubEntity, BinarySensorEntity)` with `device_class = BinarySensorDeviceClass.CONNECTIVITY`, `entity_category = EntityCategory.DIAGNOSTIC`, a stable `unique_id` (`f"{entry_id}:hub:connectivity"`), `is_on` reading `coordinator.connected`, and `available` always `True`.
- [ ] `binary_sensor.py`'s `async_setup_entry` adds exactly one connectivity entity for the hub, in addition to the existing per-device `async_setup_hub_platform` call.
- [ ] Tests in `tests/test_lifecycle.py` assert: the connectivity binary_sensor exists on the hub device, reads "on" when `coordinator.connected` is True (after a hub-update dispatch), and flips to "off" after a `{"shutdown":"goodbye"}` frame is fed.
- [ ] `uv run pytest tests/` passes; `uv run ruff check custom_components/rtl_433` is clean.

Use your internal Todo tool to track these and keep on track.

## Technical Requirements
- Files: `custom_components/rtl_433/entity.py`, `custom_components/rtl_433/binary_sensor.py`; tests in `tests/test_lifecycle.py`.
- Hub device identifier: `(DOMAIN, entry.entry_id)` (registered in `__init__.py`).
- Reuse `signal_hub_update` from `const.py` (Task 1).

## Input Dependencies
- Task 1: `signal_hub_update` / `SIGNAL_HUB_UPDATE`, `coordinator.connected`, and the connect-loop / shutdown emits that drive updates.

## Output Artifacts
- `Rtl433HubEntity` base in `entity.py` (consumed by Task 5's diagnostic sensors).
- The connectivity binary_sensor (referenced by docs in Task 6).

## Implementation Notes

<details>
<summary>Detailed implementation guidance</summary>

### 1. `entity.py` — `Rtl433HubEntity` base
Add a new base class (separate from `Rtl433Entity`; it must NOT use the
per-device `last_seen`/timeout availability). Place it after `Rtl433Entity`:

```python
from homeassistant.helpers.entity import DeviceInfo, EntityCategory  # already imported
from homeassistant.helpers.entity import Entity
from .const import signal_hub_update  # add to the existing const import block


class Rtl433HubEntity(Entity):
    """Base for statically-registered entities on the hub device itself.

    Unlike :class:`Rtl433Entity` (one per device field, availability gated by the
    per-device timeout), hub entities are one-per-hub, attach to the hub device,
    and re-read the coordinator's hub state on every ``signal_hub_update``.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, coordinator: Rtl433Coordinator, hub_entry_id: str) -> None:
        self._coordinator = coordinator
        self._hub_entry_id = hub_entry_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, hub_entry_id)},
        )
        self._unsub_hub: Callable[[], None] | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._unsub_hub = async_dispatcher_connect(
            self.hass,
            signal_hub_update(self._hub_entry_id),
            self._handle_hub_update,
        )

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub_hub is not None:
            self._unsub_hub()
            self._unsub_hub = None

    @callback
    def _handle_hub_update(self) -> None:
        """Re-read hub state and write the entity state."""
        self.async_write_ha_state()
```

`Callable`, `callback`, `async_dispatcher_connect`, `DOMAIN`, and
`Rtl433Coordinator` (TYPE_CHECKING import) are already available in `entity.py`.
`Entity` is the base from `homeassistant.helpers.entity` — import it. Note the
dispatch is **no-arg** (matches `_emit_hub_update` in Task 1).

### 2. `binary_sensor.py` — connectivity entity
Add the class and register it. Imports needed:

```python
from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.helpers.entity import EntityCategory
from .entity import Rtl433HubEntity, Rtl433Entity, async_setup_hub_platform
```

```python
class Rtl433HubConnectivity(Rtl433HubEntity, BinarySensorEntity):
    """Reports whether the hub's WebSocket connection is currently open."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "Connectivity"

    def __init__(self, coordinator: Rtl433Coordinator, hub_entry_id: str) -> None:
        super().__init__(coordinator, hub_entry_id)
        self._attr_unique_id = f"{hub_entry_id}:hub:connectivity"

    @property
    def is_on(self) -> bool:
        return self._coordinator.connected

    @property
    def available(self) -> bool:
        # The connectivity entity reports connection state, so it is always
        # available regardless of whether the socket is currently open.
        return True
```

Update `async_setup_entry` to add the hub entity alongside the per-device setup:

```python
async def async_setup_entry(hass, entry, async_add_entities) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([Rtl433HubConnectivity(coordinator, entry.entry_id)])
    await async_setup_hub_platform(
        hass, entry, async_add_entities, PLATFORM, Rtl433BinarySensor
    )
```

`DOMAIN` must be imported in `binary_sensor.py` (add `from .const import DOMAIN`).

### 3. Tests (`tests/test_lifecycle.py`)
The `_no_socket` autouse fixture stubs `_connect_loop`, so `coordinator.connected`
stays `False` after setup unless you set it. Suggested test:

```python
from homeassistant.helpers.dispatcher import async_dispatcher_send
from custom_components.rtl_433.const import signal_hub_update

async def test_hub_connectivity_sensor(hass, hub_entry_builder):
    hub = await _setup_hub(hass, hub_entry_builder)
    coordinator = _coordinator(hass, hub)

    ent_reg = er.async_get(hass)
    entity_id = ent_reg.async_get_entity_id(
        "binary_sensor", DOMAIN, f"{hub.entry_id}:hub:connectivity"
    )
    assert entity_id is not None

    # Mark connected and notify -> state on.
    coordinator.connected = True
    async_dispatcher_send(hass, signal_hub_update(hub.entry_id))
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "on"

    # A shutdown frame flips it back off (Task 1 path).
    _feed(coordinator, {"shutdown": "goodbye"})
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "off"

    # The entity belongs to the hub device.
    dev_reg = dr.async_get(hass)
    hub_device = dev_reg.async_get_device(identifiers={(DOMAIN, hub.entry_id)})
    assert ent_reg.async_get(entity_id).device_id == hub_device.id
```

### Gotchas
- `connectivity` device class: `on` = connected, `off` = disconnected (HA semantics) — `is_on = coordinator.connected` is correct.
- Keep `unique_id` stable (`:hub:connectivity`) — changing it orphans the entity.
- The hub entity must NOT subclass `Rtl433Entity` (that class's `available` depends on per-device `last_seen`, which is wrong for a hub-wide entity).
- With `_attr_has_entity_name = True` and `_attr_name = "Connectivity"`, HA renders the entity name as "<hub title> Connectivity"; no translation file change is required.
</details>
