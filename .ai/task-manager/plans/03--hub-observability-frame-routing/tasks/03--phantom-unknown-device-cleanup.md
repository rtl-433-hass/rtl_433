---
id: 3
group: "setup"
dependencies: []
status: "pending"
created: "2026-05-26"
skills:
  - python
  - home-assistant
---
# Phantom "unknown" device cleanup on hub setup

## Objective
Remove any phantom `"unknown"` device that earlier versions persisted before the
frame-routing fix. During hub setup, idempotently drop an `"unknown"` key from
`entry.data[CONF_DEVICES]` (writing the entry back only if it changed) and, when
a device-registry device with identifier `(DOMAIN, f"{entry_id}:unknown")`
exists, remove it (which removes its entities too). Because Task 1's classifier
prevents recreation, this converges to a clean state after one run and is a no-op
on every subsequent setup.

## Skills Required
- `python` — dict filtering, idempotent guards.
- `home-assistant` — config-entry data updates, device registry removal.

## Acceptance Criteria
- [ ] `async_setup_entry` in `__init__.py` performs a one-time idempotent cleanup that removes the `"unknown"` key from `entry.data[CONF_DEVICES]` when present, persisting via `hass.config_entries.async_update_entry` **only** when the map actually changed.
- [ ] If a device-registry device with identifier `(DOMAIN, f"{entry.entry_id}:unknown")` exists, it is removed via the device registry (cascading to its entities); absent ⇒ no-op.
- [ ] The cleanup never touches the hub device `(DOMAIN, entry.entry_id)` or any real nested device.
- [ ] A new test in `tests/test_lifecycle.py` builds a hub entry whose `data[CONF_DEVICES]` contains an `"unknown"` record (plus a real device), runs setup, asserts the `"unknown"` record is gone and the real device remains, and asserts a second setup/reload makes no further change.
- [ ] `uv run pytest tests/` passes; `uv run ruff check custom_components/rtl_433` is clean.

Use your internal Todo tool to track these and keep on track.

## Technical Requirements
- File: `custom_components/rtl_433/__init__.py`; test in `tests/test_lifecycle.py`.
- Registry: `homeassistant.helpers.device_registry` (already imported as `dr`).
- Run the cleanup before the platforms are forwarded (so phantom entities are never re-instantiated for this setup).

## Input Dependencies
None. Phase 1 task. (Logically pairs with Task 1's classifier, but shares no code or files with it.)

## Output Artifacts
- The cleanup routine in `async_setup_entry` (referenced by docs in Task 6).

## Implementation Notes

<details>
<summary>Detailed implementation guidance</summary>

### Where to put it
In `async_setup_entry`, after the hub device is registered
(`device_registry.async_get_or_create(... identifiers={(DOMAIN, entry.entry_id)} ...)`)
and before `await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)`.
A small module-level helper keeps `async_setup_entry` readable:

```python
PHANTOM_DEVICE_KEY = "unknown"


def _cleanup_phantom_unknown_device(
    hass: HomeAssistant, entry: ConfigEntry, device_registry: dr.DeviceRegistry
) -> None:
    """Remove a pre-fix phantom ``unknown`` device from the map and registry.

    Idempotent: drops the ``unknown`` key from ``entry.data[CONF_DEVICES]`` (only
    persisting when it changed) and removes the stale registry device
    ``(DOMAIN, f"{entry_id}:unknown")`` if present. Never touches the hub device
    or real nested devices. Safe to run on every setup.
    """
    devices = entry.data.get(CONF_DEVICES, {})
    if PHANTOM_DEVICE_KEY in devices:
        cleaned = {k: v for k, v in devices.items() if k != PHANTOM_DEVICE_KEY}
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_DEVICES: cleaned}
        )

    phantom = device_registry.async_get_device(
        identifiers={(DOMAIN, f"{entry.entry_id}:{PHANTOM_DEVICE_KEY}")}
    )
    if phantom is not None:
        device_registry.async_remove_device(phantom.id)
```

Call it in `async_setup_entry`:

```python
device_registry = dr.async_get(hass)
device_registry.async_get_or_create(
    config_entry_id=entry.entry_id,
    identifiers={(DOMAIN, entry.entry_id)},
    manufacturer="rtl_433",
    name=entry.title,
    model="rtl_433 server",
)
_cleanup_phantom_unknown_device(hass, entry, device_registry)
```

`CONF_DEVICES` and `DOMAIN` are already imported in `__init__.py`.

### Idempotency
- Reading `entry.data.get(CONF_DEVICES, {})` and rewriting only when the key was present means the second setup writes nothing.
- `async_get_device(...)` returning `None` on the second run means no registry call.

### Test (`tests/test_lifecycle.py`)
Follow the existing `_setup_hub` helper pattern. Example skeleton:

```python
async def test_phantom_unknown_device_cleaned_up(hass, hub_entry_builder):
    real_key = "Acurite-606TX-42"
    hub = hub_entry_builder(
        availability_timeout=600,
        devices={
            "unknown": {CONF_MODEL: "", DEVICE_FIELDS: ["frequencies"]},
            real_key: {CONF_MODEL: "Acurite-606TX", DEVICE_FIELDS: ["temperature_C"]},
        },
    )
    hub.add_to_hass(hass)

    dev_reg = dr.async_get(hass)
    # Pre-seed a stale phantom registry device as a prior version would have.
    dev_reg.async_get_or_create(
        config_entry_id=hub.entry_id,
        identifiers={(DOMAIN, f"{hub.entry_id}:unknown")},
    )

    assert await hass.config_entries.async_setup(hub.entry_id)
    await hass.async_block_till_done()

    assert "unknown" not in hub.data.get(CONF_DEVICES, {})
    assert real_key in hub.data[CONF_DEVICES]
    assert dev_reg.async_get_device(
        identifiers={(DOMAIN, f"{hub.entry_id}:unknown")}
    ) is None

    # Re-running setup is a no-op (reload).
    assert await hass.config_entries.async_reload(hub.entry_id)
    await hass.async_block_till_done()
    assert "unknown" not in hub.data.get(CONF_DEVICES, {})
```

The `_no_socket` autouse fixture in `test_lifecycle.py` already stubs the connect
loop, so setup won't open a socket. Import `dr`, `CONF_DEVICES`, `CONF_MODEL`,
`DEVICE_FIELDS`, `DOMAIN` as the existing tests do.

### Gotchas
- Use `async_remove_device` (not entity-by-entity); HA cascades entity removal.
- Do not remove devices whose identifier is exactly `(DOMAIN, entry.entry_id)` (the hub) — the `:unknown` suffix guarantees you won't, as long as you match the full `f"{entry_id}:unknown"` identifier.
</details>
