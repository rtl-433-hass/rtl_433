---
id: 2
group: "sdr-resync"
dependencies: [1]
status: "completed"
created: 2026-05-28
skills:
  - python
  - pytest
---
# Tests for the resync button + coroutine

## Objective
Lock in the resync behavior and button gating in `tests/test_sdr_controls.py`.

## Skills Required
- `python`, `pytest` — existing SDR-controls test harness (`hass`, `aioclient_mock`, `hass_storage`).

## Acceptance Criteria
- [ ] **Press reseeds from server (refresh-first)**: seed a stale desired state + persist it, register `_META_SINGLE` getters, set `connected = True`, drive `async_resync_sdr()` → `get_desired("center_frequency") == 433920000`, gain-auto managed correctly, Store payload under `sdr_store_key(entry.entry_id)` holds re-adopted values.
- [ ] **Hop-mode guard**: with `_META_HOPPING` getters, `async_resync_sdr()` leaves `center_frequency` unmanaged (`get_desired(...) is None`, `not is_managed("center_frequency")`), other fields managed.
- [ ] **/cmd-down no-op (no data loss)**: seed + persist stale desired state, ensure `coordinator.meta` empty, all getters HTTP 500, drive `async_resync_sdr()` → does not raise, `meta` still empty, desired state + Store payload UNCHANGED (guard returned before clear), NO setter `/cmd` issued.
- [ ] **Button gating**: with `_no_socket`, a `manage_settings=True` hub has exactly one button entity with unique_id ending `:hub:resync_sdr` on the hub device; a `manage_settings=False` hub has none.
- [ ] **Always-available**: button defines no `available` override; `button.available` is `True` with meta populated AND empty.
- [ ] `Platform.BUTTON` is in `const.PLATFORMS`.
- [ ] `python -m pytest tests/test_sdr_controls.py -q` and full `pytest -q` pass; `ruff` clean.

## Technical Requirements
- File: `tests/test_sdr_controls.py`. Reuse the managed-hub `coordinator` fixture (`:110-121`), `_META_SINGLE`/`_META_HOPPING` (`:60-71`), `_mock_setters` (`:89-107`), `_no_socket` autouse (`:344-352`), `sdr_store_key` + `hass_storage` (`:299-338`).

## Input Dependencies
- Task 1.

## Output Artifacts
- New tests in `tests/test_sdr_controls.py`.

## Implementation Notes
<details>
<summary>Detailed guidance</summary>
- Study the existing tests in `tests/test_sdr_controls.py` to reuse the meta/getter/setter mocking and Store-assertion helpers exactly.
- For "no setter issued" in the /cmd-down test, assert against the setter mock (`_mock_setters`) call count, or that `aioclient_mock` recorded no setter POSTs.
- For the integration button-gating test, set up a real hub entry (as other integration tests do) with `manage_settings` true/false and query the entity registry for the `:hub:resync_sdr` unique_id.

### Meaningful Test Strategy Guidelines
"write a few tests, mostly integration". Test the resync composition (refresh-first guard, reseed, hop guard, no-op-on-outage) and button gating — not `ButtonEntity` internals. Combine related assertions per test where natural.
</details>
