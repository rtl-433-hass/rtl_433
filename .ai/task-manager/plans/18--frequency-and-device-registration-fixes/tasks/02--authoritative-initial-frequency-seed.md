---
id: 2
group: "sdr-frequency"
dependencies: [1]
status: "pending"
created: 2026-06-01
skills:
  - python
---
# Authoritative initial-frequency seed + test de-duplication (Issue 2)

## Objective
Make the setup-time `initial_center_frequency` win over adopted/persisted SDR state exactly once after setup, regardless of whether the desired-state store is already populated when first-connect seeding runs. Extract the first-connect seeding into a real coordinator method, gate the one-time override on a persisted "consumed" flag, and replace the divergent test helper so the real path is covered.

## Skills Required
- `python` (Home Assistant coordinator / asyncio, pytest)

## Acceptance Criteria
- [ ] First-connect SDR seeding (currently inline in `_connect_loop`, `base.py:420-435`) is extracted into a single coordinator method called by `_connect_loop`.
- [ ] When `initial_center_frequency` is configured and the one-time seed has not been consumed, the desired center frequency is forced to the configured value, marked managed, and persisted — even when `_desired` is already non-empty at that point.
- [ ] The one-time seed is recorded as consumed in the existing SDR `Store`, so it is honored across restarts/reconnects but never re-applied after the user later changes the frequency via the control.
- [ ] Adoption of the other SDR fields is unchanged; `_enforce_all` still runs after seeding.
- [ ] The test-only `_seed_initial_frequency` helper in `tests/test_sdr_controls.py` is deleted; existing initial-frequency tests call the extracted production method.
- [ ] A new regression reproduces the reported bug: with `_desired` already populated (e.g. an adopted `center_frequency`) before seeding, the configured `initial_center_frequency` still wins (`get_desired`, `is_managed`, and `_command_args` all reflect the configured value).
- [ ] Full suite passes via `uv run pytest tests/ -q`.

## Technical Requirements
- Files: `custom_components/rtl_433/coordinator/base.py`, `tests/test_sdr_controls.py`.
- The SDR `Store` payload is `{"values": ..., "managed": ...}` (see `_persist_desired` / `async_load_desired_state`, ~lines 607-627).

## Input Dependencies
- Task 1 (both tasks edit `_connect_loop`; serialized to avoid merge conflicts).

## Output Artifacts
- Extracted first-connect seeding method on the coordinator.
- Updated tests exercising the real path.

## Implementation Notes

<details>
<summary>Detailed implementation guidance</summary>

**Root cause recap**: `_connect_loop` only layers the configured frequency over adoption inside `if not self._desired:` (`base.py:422`, override at `:429-434`). If `_desired` is already populated when that runs (a prior adopt that persisted, a reconnect/retry, or management toggled on after entry creation), the override is skipped and the server's adopted default (433.92 MHz) wins. The existing test passes only because `test_initial_frequency_seeds_over_adoption_on_first_connect` calls a test-only `_seed_initial_frequency` helper that re-implements the production branch, so the real path is untested. `test_initial_frequency_not_seeded_when_desired_nonempty` literally documents the bug as intended behavior.

**1. Extract a real seeding method**
- Create e.g. `async def _seed_desired_on_first_connect(self) -> None:` on the coordinator, moving the body currently inside `_connect_loop`'s `if self.manage_settings:` first-connect branch.
- Keep: `if not self._desired: await self._adopt_from_server()` for adopting the OTHER fields. Adoption of the other settings still only runs when desired is empty (unchanged).
- Replace the inline frequency override with a one-time authoritative seed that is NOT gated on `_desired` being empty (see step 2).
- In `_connect_loop`, replace the inline block (~`:420-435`) with: `if self.manage_settings: try: await self._seed_desired_on_first_connect(); await self._enforce_all() except Exception ...` keeping the existing broad-except/debug-log behavior that protects the connect loop.

**2. One-time consumed flag**
- Add persisted state recording that the initial-frequency seed has been consumed. Extend the Store payload with a flag, e.g. `seeded_initial_frequency: bool`, written by `_persist_desired` and read by `async_load_desired_state` into an instance attribute (e.g. `self._initial_freq_seeded: bool`, default `False`). Keep backward compatibility for stores lacking the key (treat missing as `False`).
- In `_seed_desired_on_first_connect`: `if self.initial_center_frequency is not None and not self._initial_freq_seeded:` set `self._desired[KEY_CENTER_FREQUENCY] = self.initial_center_frequency`, `self._managed.add(KEY_CENTER_FREQUENCY)`, set `self._initial_freq_seeded = True`, then `await self._persist_desired()`.
- Because the flag is persisted and set on first application, a later user change via `set_sdr` (which persists its own value) is never overwritten — the seed does not re-fire on subsequent connects/restarts.
- Ensure `clear_desired_state` (management turned off, ~line 787) also resets/removes the flag so re-enabling management can re-seed (re-adoption already happens there).

**3. Update `_persist_desired` / `async_load_desired_state`**
- `_persist_desired` (~line 624): include the new flag in the saved dict.
- `async_load_desired_state` (~line 607): load the flag into `self._initial_freq_seeded` (default `False` when absent or when management is off / store removed).

**4. Tests** (`tests/test_sdr_controls.py`)
- Delete `_seed_initial_frequency` (~lines 235-249).
- Repoint `test_initial_frequency_seeds_over_adoption_on_first_connect` to call `await coordinator._seed_desired_on_first_connect()`; keep the existing assertions (desired == 915.0, managed, `_command_args == ("center_frequency", 915000000, None)`, store persisted).
- Replace/repurpose `test_initial_frequency_not_seeded_when_desired_nonempty` into a REGRESSION asserting the NEW behavior: pre-populate `coordinator._desired = {KEY_CENTER_FREQUENCY: 433.92}` and `_managed = {KEY_CENTER_FREQUENCY}` with `initial_center_frequency=915.0` and `_initial_freq_seeded=False`, call `_seed_desired_on_first_connect()`, and assert `get_desired(KEY_CENTER_FREQUENCY) == 915.0` and `is_managed` is True. (This is the case that currently fails.)
- Add a test that a second call to `_seed_desired_on_first_connect()` after the flag is consumed does NOT overwrite a value the user changed (e.g. set desired to 868.0 and `_initial_freq_seeded=True`, call again, assert it stays 868.0).
- Reuse existing `_META_SINGLE`, `hub_entry_builder`, `hass_storage`, `sdr_store_key` fixtures/imports already in the file.

**Validation**
- `uv run pytest tests/test_sdr_controls.py -q` then `uv run pytest tests/ -q` (Python 3.14 via `uv`; system Python is 3.13 — see project memory). Confirm the new regression fails if the Component-3 change is temporarily reverted.
</details>
