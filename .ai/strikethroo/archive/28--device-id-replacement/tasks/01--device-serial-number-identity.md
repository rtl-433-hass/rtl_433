---
id: 1
group: "device-identity"
dependencies: []
status: "completed"
created: 2026-08-31
skills:
  - home-assistant-integration
---
# Surface the decoded identity as the nested device's serial number

## Objective

Make a nested rtl_433 device's transmitter identity (`id`, plus `channel` /
`subtype` when present) visible on the Home Assistant device pane as a
**serial number**, independently of the device name — so it survives a user
rename and can be read before and after a battery swap.

## Skills Required

`home-assistant-integration` — constructing `DeviceInfo` and understanding which
of its fields Home Assistant renders on the device info card.

## Acceptance Criteria

- [ ] A new helper in `custom_components/rtl_433/entity.py` returns the identity
      suffix of a `device_key` given the model (e.g. `Fineoffset-WH51-00c50f` +
      `Fineoffset-WH51` -> `00c50f`; `Foo-5-ch3-st2` + `Foo` -> `5-ch3-st2`).
- [ ] The helper returns `None` for a model-only device (where the key equals the
      model token, so there is no suffix) and for an empty model.
- [ ] The nested device's `DeviceInfo` (`entity.py`, currently around line 181)
      sets `serial_number` from that helper.
- [ ] A model-only device sets no `serial_number` (the key is absent or `None`,
      never an empty string).
- [ ] The `identifiers` tuple and every entity `unique_id` are **unchanged** —
      this task touches presentation only.
- [ ] `uv run pytest tests/` passes.

Use your internal Todo tool to track these and keep on track.

## Technical Requirements

- File: `custom_components/rtl_433/entity.py`.
- `DeviceInfo` is imported from `homeassistant.helpers.entity` and already
  supports `serial_number`; the hub device sets the same field from the dongle's
  `dev_info` at `__init__.py:218`, which is the in-repo precedent.
- `_safe_token` comes from the local `.normalizer` module.
- Do **not** modify `identifiers`, `via_device`, `unique_id`, or anything else
  named in `COMPATIBILITY_CONTRACT.md`.

## Input Dependencies

None. This task is independent and can run in parallel with task 2.

## Output Artifacts

- `_device_identity(model, device_key) -> str | None` (or similar) in
  `entity.py`.
- `serial_number` populated on the nested-device `DeviceInfo`.

## Implementation Notes

<details>
<summary>Step-by-step implementation</summary>

1. Open `custom_components/rtl_433/entity.py` and find `_device_display_name`
   (around line 78). It already performs exactly the suffix extraction needed:

   ```python
   suffix = device_key.removeprefix(f"{_safe_token(model)}-")
   if not suffix or suffix == device_key:
       return model
   return f"{model} {suffix}"
   ```

2. Add a sibling helper immediately after it that returns the suffix *raw*
   instead of concatenating it onto the model:

   ```python
   def _device_identity(model: str, device_key: str) -> str | None:
       """Return the identity suffix of ``device_key``: the decoded id plus any
       ``ch``/``st`` tokens, with the model prefix stripped.

       This is the same suffix ``_device_display_name`` folds into the device
       name, surfaced separately so it can be shown as a serial number that
       survives a user rename. Returns ``None`` for a model-only device (the key
       is just the model token, so there is nothing that distinguishes one unit
       from another) and when the model is unknown.
       """
       if not model:
           return None
       suffix = device_key.removeprefix(f"{_safe_token(model)}-")
       if not suffix or suffix == device_key:
           return None
       return suffix
   ```

   Returning `None` (not `""`) for the model-only case matters: step 3 must be
   able to omit the field entirely.

3. Find the nested-device `DeviceInfo` construction in `Rtl433Entity.__init__`
   (around line 181):

   ```python
   device_name = _device_display_name(model, device_key)
   self._attr_device_info = DeviceInfo(
       identifiers={(DOMAIN, f"{hub_entry_id}:{device_key}")},
       name=device_name,
       model=model or None,
       manufacturer=MANUFACTURER,
       via_device=(DOMAIN, hub_entry_id),
   )
   ```

   Add `serial_number=_device_identity(model, device_key),` to the call. Passing
   `None` is the correct way to leave the field unset — this matches how
   `model=model or None` already handles the unknown-model case, so no
   conditional construction is needed.

4. Do not touch `identifiers` or `via_device`. Leave `_device_display_name` and
   its call site exactly as they are — the name keeps including the suffix; the
   serial number is additional, not a replacement.

5. Add or extend a test asserting both branches. A natural home is wherever
   entity construction is already exercised (`tests/test_entity.py` if present,
   otherwise the existing sensor/binary_sensor tests): construct an entity for a
   key like `Fineoffset-WH51-00c50f` and assert
   `entity.device_info["serial_number"] == "00c50f"`; construct one for a
   model-only key (key == model token) and assert the value is `None`. Note that
   task 4 owns the broader test sweep — keep this to the two direct assertions so
   the two tasks do not duplicate each other.

6. Run `uv run pytest tests/` (Python 3.14 via `uv`; the system Python cannot
   import the test stack).
</details>
