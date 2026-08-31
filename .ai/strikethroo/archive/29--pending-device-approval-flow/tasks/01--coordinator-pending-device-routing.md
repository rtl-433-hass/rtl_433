---
id: 1
group: "coordinator"
dependencies: []
status: "completed"
created: 2026-08-31
skills:
  - home-assistant-integration
  - async-python
---
# Split observation from adoption in the coordinator

## Objective

Stop the coordinator from auto-registering newly heard devices. Route every frame
for a device that is not already adopted into an in-memory **pending** list (or
drop it if the device key is on the hub's **ignored** list), expose an adoption
API the options flow will call, delete the per-device persistent notification,
and make device deletion return a device to pending instead of silently
recreating it.

## Skills Required

- `home-assistant-integration` — config-entry data, device/entity registries,
  dispatcher signals, and the existing coordinator/mixin architecture.
- `async-python` — the event-loop callback path in `coordinator/_events.py`.

## Acceptance Criteria

- [ ] `custom_components/rtl_433/const.py` defines `CONF_IGNORED_DEVICES`.
- [ ] A `PendingDevice` record type holds a pending candidate's latest
      `NormalizedEvent`, sighting count, and first/last-seen timestamps.
- [ ] `Rtl433Coordinator` exposes `adopted: set[str]`, `ignored: set[str]`, and
      `pending: dict[str, PendingDevice]`, seeded at construction from
      `entry.data[CONF_DEVICES]` and `entry.data[CONF_IGNORED_DEVICES]`.
- [ ] A frame whose `device_key` is **not** in `adopted` never mutates `devices`,
      `last_seen`, `available`, `seen_fields`, or `device_fields`, and never
      calls `_dispatch` or `new_device_callback`.
- [ ] A frame for a key in `ignored` creates no pending entry (debug log only).
- [ ] A backlog frame (`is_backlog`) and a replayed frame (`is_replay`) create no
      pending entry, so a reconnect never repopulates the list.
- [ ] Repeated sightings of the same pending key update the stored event, bump
      the count, and refresh `last_seen` rather than creating duplicates.
- [ ] `async_adopt_device(key)` promotes a pending record into runtime state and
      fires the existing `new_device_callback` seam so entities are built exactly
      as the old auto-add path built them.
- [ ] `ignore_device(key)` drops the key from `pending` and adds it to `ignored`.
- [ ] The `persistent_notification.async_create` call in `__init__.py`'s
      `new_device_callback` is deleted, along with its stale docstring text.
- [ ] `async_remove_config_entry_device` leaves the deleted key out of `adopted`
      so its next transmission makes it a pending candidate again.
- [ ] `uv run pytest tests/` passes; existing tests that asserted auto-add
      behaviour are updated to the new contract.
- [ ] `uv run ruff check custom_components/ tests/` and
      `uv run ruff format --check custom_components/ tests/` pass.

Use your internal Todo tool to track these and keep on track.

## Technical Requirements

- Files: `custom_components/rtl_433/const.py`,
  `custom_components/rtl_433/coordinator/base.py`,
  `custom_components/rtl_433/coordinator/_events.py`,
  `custom_components/rtl_433/__init__.py`.
- `NormalizedEvent` is a frozen slotted dataclass from `pyrtl_433.normalizer`
  with `device_key`, `model`, `identity`, `fields`, `is_replay`, `event_time`,
  `is_repaint`. Signal level for the UI comes from `event.fields.get("snr")` /
  `event.fields.get("rssi")`.
- Timestamps use `homeassistant.util.dt as dt_util` (`dt_util.utcnow()`),
  matching the existing code.
- Do **not** add persistence, a TTL, or eviction for the pending list — it is
  in-memory by explicit product decision and must be empty after a restart.

## Input Dependencies

None. This is the foundation task.

## Output Artifacts

- `CONF_IGNORED_DEVICES` constant consumed by tasks 2 and 3.
- `coordinator.pending` / `.ignored` / `.adopted` and the
  `async_adopt_device` / `ignore_device` API consumed by task 3's options flow
  and asserted by task 4's tests.

## Implementation Notes

<details>
<summary>Detailed implementation guidance</summary>

### 1. `const.py`

Add next to `CONF_DEVICES`:

```python
# Per-hub list of device keys the user has explicitly ignored. Ignored devices
# never enter the coordinator's pending list, so they cannot be adopted by
# accident and do not reappear after a restart. Mirrors Home Assistant's
# "ignored discovered integrations": the user-facing verb is *Ignore*, never
# "reject". Stored under ``entry.data`` as a list of device keys.
CONF_IGNORED_DEVICES: Final = "ignored_devices"
```

Leave `CONF_DISCOVERY_ENABLED` alone — task 2 removes it.

### 2. `PendingDevice`

Put it in `coordinator/base.py` (near the top, after the imports) so both the
coordinator and the options flow can import it:

```python
@dataclass(slots=True)
class PendingDevice:
    """One device heard but not yet adopted into Home Assistant.

    Held in memory only: the pending list is rebuilt from live traffic after
    every restart or reload by design, so an unwanted device never outlives the
    session that heard it. ``event`` is the most recent frame, kept so adoption
    can seed the device's entities from real data instead of waiting for the
    next transmission.
    """

    key: str
    model: str
    event: NormalizedEvent
    count: int
    first_seen: datetime
    last_seen: datetime
```

### 3. Coordinator state (`coordinator/base.py`)

Add an `adopted_keys` and `ignored_keys` argument to `Rtl433Coordinator.__init__`
(keyword-only, defaulting to `None`), following the existing `skip_keys` /
`event_driven_keys` style, and store:

```python
self.adopted: set[str] = set(adopted_keys or ())
self.ignored: set[str] = set(ignored_keys or ())
self.pending: dict[str, PendingDevice] = {}
```

Document them in the class docstring's attribute list alongside `devices` /
`last_seen` / `available`.

In `__init__.py`'s `async_setup_entry`, pass
`adopted_keys=set(entry.data.get(CONF_DEVICES, {}))` and
`ignored_keys=set(entry.data.get(CONF_IGNORED_DEVICES, []))`.

`forget_device(key)` (already used by the delete path) must also discard the key
from `self.adopted` and `self.pending`.

### 4. Routing (`coordinator/_events.py`)

`_on_client_event` currently, in order: reads `key`/`is_replay`, derives
`is_backlog`, sets `self.devices[key]`, updates `seen_fields`/`device_fields`,
refreshes `last_seen`/`available` for live frames, calls
`_maybe_register_device`, then `_dispatch`.

Restructure so the adopted check happens **immediately after `is_backlog` is
derived** and before any shared-state mutation:

```python
if key not in self.adopted:
    self._record_pending(key, normalized, is_replay=is_replay, is_backlog=is_backlog)
    return
```

Everything below that line is unchanged and now runs for adopted devices only.

Replace `_maybe_register_device` with `_record_pending`:

```python
def _record_pending(
    self,
    key: str,
    normalized: NormalizedEvent,
    *,
    is_replay: bool,
    is_backlog: bool,
) -> None:
    """Record a not-yet-adopted device as a pending candidate.

    Deliberately touches none of the adopted-device runtime state (``devices``,
    ``last_seen``, ``available``, ``seen_fields``, ``device_fields``) and never
    dispatches: a pending device has no Home Assistant device and no entities,
    so the availability watchdog, diagnostics, and the entity platforms must
    continue to see exactly the set of devices they see today.

    Replays and pre-connection backlog frames are re-broadcasts of already
    transmitted events, never a device's first live transmission, so they must
    not create a candidate -- otherwise every reconnect would repopulate the
    list with stale entries. Ignored keys are dropped outright.
    """
    if is_replay or is_backlog:
        return
    if key in self.ignored:
        LOGGER.debug("rtl_433 ignoring device %s (on the hub's ignore list)", key)
        return

    now = dt_util.utcnow()
    existing = self.pending.get(key)
    if existing is None:
        self.pending[key] = PendingDevice(
            key=key,
            model=normalized.model,
            event=normalized,
            count=1,
            first_seen=now,
            last_seen=now,
        )
        LOGGER.info(
            "rtl_433 heard a new device %s (model %s); add it from the hub's "
            "options to create it in Home Assistant",
            key,
            normalized.model,
        )
        return

    existing.event = normalized
    existing.model = normalized.model or existing.model
    existing.count += 1
    existing.last_seen = now
```

Keep the `_trace_unmapped_fields` call for adopted devices only (it stays where
it is, above the new early return? No — it must move below the adopted check, so
pending devices do not pollute `seen_fields`). Verify the final ordering: the
adopted check comes first, then `self.devices[key] = normalized`, then the field
tracking, then availability, then `_dispatch`.

Update the module docstring: it currently describes `_maybe_register_device` and
the discovery gate.

### 5. Adoption / ignore API (`coordinator/base.py`)

```python
@callback
def adopt_device(self, key: str) -> PendingDevice | None:
    """Promote a pending device into adopted runtime state.

    Seeds the same runtime state a live first sighting would have written, then
    fires ``new_device_callback`` -- the identical seam the auto-add path used --
    so the entity platforms build the device and its entities exactly as before.
    Returns the promoted record, or ``None`` when the key is not pending (the
    device stopped transmitting between the form render and the submit).
    """
    record = self.pending.pop(key, None)
    if record is None:
        return None

    event = record.event
    self.adopted.add(key)
    self._discovered.add(key)
    self.devices[key] = event
    self.last_seen[key] = record.last_seen
    self.available[key] = True
    field_keys = set(event.fields)
    self.seen_fields |= field_keys
    self.device_fields.setdefault(key, set()).update(field_keys)

    if self.new_device_callback is not None:
        self.new_device_callback(key, event.model, False)
    return record

@callback
def ignore_device(self, key: str) -> None:
    """Drop a pending device and never record it again this session."""
    self.pending.pop(key, None)
    self.ignored.add(key)
```

`adopt_device` is a plain callback (no awaits) because `new_device_callback`
dispatches synchronously; the options flow persists `entry.data` around it.

### 6. `__init__.py`

- Delete the `persistent_notification.async_create(...)` block and the
  `is_new_device` computation in `new_device_callback`; the callback now only
  dispatches `signal_new_device`. Remove the now-unused
  `from homeassistant.components import persistent_notification` import and
  rewrite the docstring — the paragraphs about "the in-app persistent
  notification" and the `is_replay` notification gate no longer apply. Keep the
  `is_replay` parameter: the entity platforms still receive it.
- In `async_remove_config_entry_device`, `coordinator.forget_device(device_key)`
  now also clears `adopted`/`pending` (step 3), so update the comment: the
  device's next transmission makes it a *pending candidate* again rather than
  re-registering it while discovery is on.

### 7. Existing tests

`uv run pytest tests/` will fail in modules that assert a device appears after
feeding an event (`tests/test_coordinator.py`, `tests/test_lifecycle.py`,
`tests/test_mut_init.py`, `tests/test_mut_entity.py`, and others). Update them to
the new contract: either seed `entry.data[CONF_DEVICES]` with the key so the
device is already adopted, or call `coordinator.adopt_device(key)` after feeding
the frame. Do **not** weaken an assertion to make it pass — the point of each
test must survive the change. Task 4 adds the new dedicated coverage; this task
only keeps the existing suite honest and green.

</details>
