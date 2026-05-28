---
id: 1
group: "sdr-resync"
dependencies: []
status: "completed"
created: 2026-05-28
skills:
  - python
  - home-assistant
---
# Implement resync coroutine + button platform + PLATFORMS registration

## Objective
Add a one-click "Re-sync SDR settings from server" hub button that re-adopts the rtl_433 server's current SDR settings into HA's managed desired state, replacing the off/restart/on dance. Implements plan Components 1–3.

## Skills Required
- `python`, `home-assistant` — coordinator desired-state model, `ButtonEntity`, hub-entity attachment, `PLATFORMS` forwarding.

## Acceptance Criteria
- [ ] **Component 1** — new public `async_resync_sdr()` coroutine on the coordinator (`coordinator/base.py`), **refresh-first**: `_refresh_meta()` → if `self.meta` empty, `return` (no clear); else `clear_desired_state()` → `_adopt_from_server()` → `_enforce_all()`. Best-effort, never raises into the caller. **No outer `_cmd_lock`** (it is non-reentrant; `_enforce_all`'s `_send_cmd` self-locks).
- [ ] **Component 2** — new `custom_components/rtl_433/button.py`: `Rtl433ResyncButton(Rtl433HubEntity, ButtonEntity)` with `_attr_name = "Re-sync SDR settings from server"`, `_attr_entity_category = EntityCategory.CONFIG`, `_attr_unique_id = f"{hub_entry_id}:hub:resync_sdr"`, **no `available` override** (inherits `True`). `async_press()` awaits `coordinator.async_resync_sdr()`. `async_setup_entry` resolves the coordinator from `hass.data[DOMAIN][entry.entry_id]`, returns early when `not coordinator.manage_settings`, else adds exactly one button.
- [ ] **Component 3** — `Platform.BUTTON` added to `PLATFORMS` (`const.py`).
- [ ] Hop-mode guard preserved (reuses `_adopt_from_server`, which skips `center_frequency` when `len(frequencies) > 1`).
- [ ] `ruff` clean; existing `tests/test_sdr_controls.py` still passes (no regressions from the PLATFORMS addition).

## Technical Requirements
- Files: `custom_components/rtl_433/coordinator/base.py`, `custom_components/rtl_433/button.py` (new), `custom_components/rtl_433/const.py`.
- Mirror `number.py`/`select.py`/`switch.py` `async_setup_entry` shape and `Rtl433HubEntity` usage (`entity.py:218-298`).

## Input Dependencies
None.

## Output Artifacts
- `async_resync_sdr()`, `button.py`, updated `PLATFORMS` — consumed by tests (task 2) and docs (task 3).

## Implementation Notes
<details>
<summary>Detailed guidance</summary>
- Read `_refresh_meta` (`base.py:457`), `_adopt_from_server` (`base.py:612`, hop guard `:627-631`), `clear_desired_state` (`base.py:689`), `_enforce_all` (`base.py:654`), `_send_cmd` lock (`base.py:553`), `_cmd_lock` (`base.py:224`). Confirm method names/lines (may have drifted).
- `async_resync_sdr()` skeleton:
  ```python
  async def async_resync_sdr(self) -> None:
      """Re-adopt the server's current SDR settings into managed desired state.

      Refresh-first: if the server's /cmd is unreachable (meta stays empty) this
      returns without clearing, leaving the existing desired state intact.
      """
      await self._refresh_meta()
      if not self.meta:
          return
      await self.clear_desired_state()
      self._adopt_from_server()
      await self._enforce_all()
  ```
  Match the real signatures (some may be sync vs async — check before awaiting). Do NOT wrap in `async with self._cmd_lock`.
- `button.py`: model it on `switch.py`/`select.py`. Subclass `Rtl433HubEntity` (NOT `Rtl433HubControl`) + `ButtonEntity`. Constructor takes `(coordinator, hub_entry_id)` like the other hub entities; set the `_attr_*` class/instance attrs. `async_setup_entry(hass, entry, async_add_entities)`.
- `const.py`: add `Platform.BUTTON` to the `PLATFORMS` list (keep ordering sensible).
- Verify: `python -m ruff check custom_components/`; `python -c "import ast; ast.parse(open('custom_components/rtl_433/button.py').read())"`; `python -m pytest tests/test_sdr_controls.py -q` (should still pass).
</details>
