---
id: 3
group: "ui"
dependencies: [2]
status: "completed"
created: 2026-08-31
skills:
  - home-assistant-config-flow
  - python
complexity_score: 6
complexity_notes: "Two new flow steps plus live-coordinator access, empty-state handling, translations, and persistence of two separate entry.data keys."
---
# Add the "Add discovered devices" and "Ignored devices" options-flow steps

## Objective

Give the user the observe-then-approve UI: an options-flow step that lists every
pending device with enough detail to judge it, adds the selected ones, and
ignores the ones they never want to see again — plus a second step to un-ignore.

## Skills Required

- `home-assistant-config-flow` — `OptionsFlow` steps, `SelectSelector`,
  `async_show_menu` / `async_show_form`, and translation wiring.
- `python` — label construction and entry-data persistence.

## Acceptance Criteria

- [ ] `async_step_init`'s menu offers `add_devices` and `ignored_devices`
      alongside the existing `hub`, `device`, and `mappings` entries.
- [ ] `async_step_add_devices` lists every pending device, most recently seen
      first, each labelled with model, device key, sighting count, signal level
      (when the device reports one), and relative last-seen.
- [ ] The step exposes two independent optional multi-selects: one that adopts
      the selected keys, one that ignores them.
- [ ] Submitting adopts each selected key through `coordinator.adopt_device` and
      persists it into `entry.data[CONF_DEVICES]`.
- [ ] Submitting ignores each selected key through `coordinator.ignore_device`
      and persists it into `entry.data[CONF_IGNORED_DEVICES]`.
- [x] A key selected in both lists applies **nothing** and re-shows the form
      with the `add_and_ignore_conflict` error. (The original wording said
      "is ignored, not added"; that contradicts reporting an error, and applying
      a side effect on a submit the flow rejects is worse. Task 005 tests the
      apply-nothing behaviour.)
- [ ] With no pending devices, the step aborts with a clear "nothing waiting"
      message instead of rendering an empty form.
- [ ] `async_step_ignored_devices` lists the currently ignored keys and
      un-ignores the selected ones, updating both `entry.data` and the running
      coordinator; it reports an empty state when nothing is ignored.
- [ ] Both steps degrade gracefully (abort with a clear reason) when the hub's
      coordinator is not loaded.
- [ ] Every user-visible string is in `translations/en.json`; no literal UI text
      in Python.
- [ ] `uv run pytest tests/` passes and `uv run ruff check`/`format --check` pass.

Use your internal Todo tool to track these and keep on track.

## Technical Requirements

- Files: `custom_components/rtl_433/options_flow.py`,
  `custom_components/rtl_433/translations/en.json`,
  `custom_components/rtl_433/__init__.py` (live ignore-list update).
- The running coordinator is at `hass.data[DOMAIN][entry.entry_id]`; the options
  flow reaches the entry through `self.config_entry`.
- Use `SelectSelector` with `SelectSelectorConfig(multiple=True, mode=SelectSelectorMode.LIST)`
  and `SelectOptionDict(value=..., label=...)`, matching the existing device
  picker in `async_step_device`.
- hassfest forbids literal URLs in translation strings; pass any link as a
  description placeholder, as `MAPPINGS_DOCS_URL` already does.

## Input Dependencies

- Task 1: `coordinator.pending`, `.ignored`, `.adopt_device`, `.ignore_device`,
  and `CONF_IGNORED_DEVICES`.
- Task 2: an `options_flow.py` hub step with the discovery toggle already removed.

## Output Artifacts

- The two flow steps exercised by task 5's tests and screenshotted by task 6.

## Implementation Notes

<details>
<summary>Detailed implementation guidance</summary>

### Menu

```python
return self.async_show_menu(
    step_id="init",
    menu_options=["add_devices", "hub", "device", "ignored_devices", "mappings"],
)
```

Put `add_devices` first — it is the step users will reach for.

### Reaching the coordinator

```python
def _coordinator(self) -> Rtl433Coordinator | None:
    """Return the hub's running coordinator, or ``None`` when unloaded."""
    return self.hass.data.get(DOMAIN, {}).get(self.config_entry.entry_id)
```

Import `Rtl433Coordinator` under `TYPE_CHECKING` if a circular import bites.

### Labels

Build one label per pending device so the user can tell a real sensor from a
one-off bad decode without leaving the form:

```python
def _pending_label(record: PendingDevice, now: datetime) -> str:
    """Describe one pending device densely enough to judge it.

    A weak neighbour device and a bad decode look identical by name alone, so the
    label leads with the model and key and then carries the three signals that
    actually discriminate: how often it has been heard, how strong it is, and how
    recently it transmitted.
    """
    parts = [f"{record.model or 'unknown'} ({record.key})"]
    parts.append(f"seen {record.count}x")
    snr = record.event.fields.get("snr")
    if snr is None:
        snr = record.event.fields.get("rssi")
    if snr is not None:
        parts.append(f"{snr} dB")
    parts.append(f"last {int((now - record.last_seen).total_seconds())}s ago")
    return " - ".join(parts)
```

Sort options by `record.last_seen` descending — the reporter in issue #128 saw 77
devices in a day, so the most recent must be reachable without scrolling past
stale ones.

### The add/ignore form

```python
async def async_step_add_devices(
    self, user_input: dict[str, Any] | None = None
) -> ConfigFlowResult:
    """List devices heard but not yet added, and add or ignore them.

    The pending list lives in coordinator memory and is rebuilt from live traffic
    after every restart, so an empty list here is normal right after a reload --
    it means nothing has transmitted yet, not that discovery is broken.
    """
    coordinator = self._coordinator()
    if coordinator is None:
        return self.async_abort(reason="hub_not_loaded")
    if not coordinator.pending:
        return self.async_abort(reason="no_pending_devices")

    now = dt_util.utcnow()
    records = sorted(
        coordinator.pending.values(), key=lambda r: r.last_seen, reverse=True
    )
    options = [
        SelectOptionDict(value=r.key, label=_pending_label(r, now)) for r in records
    ]

    errors: dict[str, str] = {}
    if user_input is not None:
        add = list(user_input.get(CONF_ADD_DEVICES, []))
        ignore = list(user_input.get(CONF_IGNORE_DEVICES, []))
        if set(add) & set(ignore):
            errors["base"] = "add_and_ignore_conflict"
        else:
            return await self._apply_add_and_ignore(add, ignore)

    selector = SelectSelector(
        SelectSelectorConfig(
            options=options, multiple=True, mode=SelectSelectorMode.LIST
        )
    )
    return self.async_show_form(
        step_id="add_devices",
        data_schema=vol.Schema(
            {
                vol.Optional(CONF_ADD_DEVICES, default=[]): selector,
                vol.Optional(CONF_IGNORE_DEVICES, default=[]): selector,
            }
        ),
        errors=errors,
    )
```

Define `CONF_ADD_DEVICES = "add"` and `CONF_IGNORE_DEVICES = "ignore"` as
module-level selector keys next to the existing `CONF_DEVICE = "device"`.

### Applying the selection

```python
async def _apply_add_and_ignore(
    self, add: list[str], ignore: list[str]
) -> ConfigFlowResult:
    """Adopt and ignore the selected devices in one write.

    Adoption goes through the coordinator so the device is built by the same
    ``new_device_callback`` seam a live first sighting used; the entry-data write
    is what makes it survive a restart. Both lists are persisted in a single
    ``async_update_entry`` so a half-applied submit cannot leave the ignore list
    and the devices map disagreeing.
    """
    entry = self.config_entry
    coordinator = self._coordinator()
    devices = {k: dict(v) for k, v in entry.data.get(CONF_DEVICES, {}).items()}
    ignored = list(entry.data.get(CONF_IGNORED_DEVICES, []))

    for key in add:
        record = coordinator.adopt_device(key) if coordinator else None
        if record is None:
            continue
        devices.setdefault(key, {})[CONF_MODEL] = record.model

    for key in ignore:
        if coordinator is not None:
            coordinator.ignore_device(key)
        if key not in ignored:
            ignored.append(key)

    self.hass.config_entries.async_update_entry(
        entry,
        data={**entry.data, CONF_DEVICES: devices, CONF_IGNORED_DEVICES: ignored},
    )
    return self.async_create_entry(title="", data=entry.options)
```

Check how the existing code shapes a device record in `entry.data[CONF_DEVICES]`
(see `__init__.py`'s `async_upsert_device` and `device_replace.py`) and match it —
at minimum the `CONF_MODEL` field, plus `DEVICE_FIELDS` if the upsert normally
writes it. Reuse the existing upsert helper if one is reachable rather than
duplicating the record shape.

Note `async_create_entry(title="", data=entry.options)` returns the options
unchanged: this step writes to `entry.data`, not options, matching how the
existing device step persists per-device settings.

### Ignored-devices step

```python
async def async_step_ignored_devices(
    self, user_input: dict[str, Any] | None = None
) -> ConfigFlowResult:
    """Un-ignore devices so they can be offered again.

    Un-ignoring is not retroactive: the pending list is in-memory and the device
    was never recorded while ignored, so it reappears on its next transmission.
    """
```

Render a single multi-select of the currently ignored keys (label them with the
key, and the model from `entry.data[CONF_DEVICES]` if it was ever adopted).
On submit, remove the selected keys from `entry.data[CONF_IGNORED_DEVICES]` and
from `coordinator.ignored`. Abort with `no_ignored_devices` when the list is
empty.

### Live ignore-list updates

In `__init__.py`'s `_async_update_listener`, push the new ignore list into the
running coordinator the same way the availability timeout is pushed — a set
assignment, no reload:

```python
coordinator.ignored = set(entry.data.get(CONF_IGNORED_DEVICES, []))
```

Add it *before* the reload-triggering comparisons so an ignore-only change never
falls through to a reload, and document why in the listener's docstring
(tearing down the WebSocket to ignore a device would be gratuitous).

### Translations

Add to `translations/en.json` under `options.step`:

- `add_devices` — title "Add discovered devices", a description explaining that
  the list holds devices heard since the last restart and that ignoring one hides
  it permanently, plus `data` labels for `add` ("Add these devices") and `ignore`
  ("Ignore these devices").
- `ignored_devices` — title "Ignored devices", description, and a `data` label
  for the un-ignore select.

Add to `options.abort`: `hub_not_loaded`, `no_pending_devices`,
`no_ignored_devices`. Add to `options.error`: `add_and_ignore_conflict`.

Also add the two new menu entries under `options.step.init.menu_options`
following the structure already there for `hub` / `device` / `mappings`.

Use the verb **Ignore** / **Ignored** throughout. "Reject" must not appear.

</details>


## Addendum: replace-step regression (added during execution)

`async_step_replace_target` draws its candidate set from
`entry.data[CONF_DEVICES]` union `coordinator.devices`. Under this plan a
battery-swapped device transmits under a **new** key that is *pending*, so it
appears in neither set and the replace flow can no longer offer the very device
it exists to adopt. This is a regression introduced by plan 29, not a
pre-existing gap, so it is fixed here rather than deferred:

- [ ] `async_step_replace_target` also offers pending device keys as replacement
      targets, labelled so a pending candidate is distinguishable from an
      adopted one.
- [ ] Choosing a pending key as the replacement target re-keys the kept device
      onto it and clears it from `coordinator.pending`, so the user is not left
      with both a re-keyed device and a stale pending candidate.
- [ ] Task 005 covers this path.
