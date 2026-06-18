---
id: 2
group: "config-flow-discovery"
dependencies: [1]
status: "completed"
created: 2026-06-01
skills:
  - pytest
---
# Add config-flow tests for Supervisor discovery

## Objective
Add focused, mostly-integration tests that exercise the new Supervisor (hassio) discovery behaviour in the rtl_433 config flow: the happy-path discovery → confirm → entry creation, adoption/migration of a pre-existing entry, same-radio port-change update, manual-flow dedup against a discovered entry, reconfigure preserving a stable id, and a failed confirmation. Tests verify *this integration's* discovery/identity logic — not Home Assistant framework plumbing.

## Skills Required
- `pytest` — Home Assistant config-flow testing with the `hass` fixture and flow result assertions.

## Acceptance Criteria
- [ ] A test drives `hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_HASSIO}, data=HassioServiceInfo(...))`, then completes `hassio_confirm`, and asserts a `create_entry` result whose entry `unique_id` equals the advertised radio id and whose `data` includes `manage_settings` and the host/port/path/secure from discovery.
- [ ] A test pre-creates a manual entry on the same `host:port` (e.g. `unique_id="hub:<host>:<port>"`) and asserts the discovery flow aborts with `already_configured`, the total entry count is unchanged, and the surviving entry's `unique_id` has been re-keyed to the advertised radio id (adoption/migration).
- [ ] A test asserts that the same radio re-advertised on a *different* port updates the existing entry's stored `port` in place and aborts (no duplicate).
- [ ] A test asserts the manual `user` step aborts with `already_configured` when the host:port is already owned by a discovered entry.
- [ ] A test asserts the `reconfigure` flow preserves a stable (non-`hub:`) radio `unique_id` (it is not rewritten to `hub:host:port`).
- [ ] A test asserts a failing `validate_connection` during `hassio_confirm` re-shows the form with a `cannot_connect` error.
- [ ] `uv run --python 3.14 pytest tests/test_config_flow.py -q` passes (all new and existing tests).

## Technical Requirements
- Import `SOURCE_HASSIO` from `homeassistant.config_entries` and `HassioServiceInfo` from `homeassistant.helpers.service_info.hassio`.
- Reuse the existing `VALIDATE = "custom_components.rtl_433.config_flow.Rtl433Coordinator.validate_connection"` patch target and the `hub_entry_builder` fixture already used in `tests/test_config_flow.py`.
- A discovery payload looks like `HassioServiceInfo(config={"host": "core-rtl433", "port": 8433, "path": "/ws", "secure": False, "unique_id": "serial:0123", "addon": "rtl_433"}, name="rtl_433", slug="abc123", uuid="deadbeef")`.

## Input Dependencies
- Task 1: `async_step_hassio`, `async_step_hassio_confirm`, the user-step dedup guard, and reconfigure stable-id preservation must exist.

## Output Artifacts
- New discovery tests added to `tests/test_config_flow.py` (or a new `tests/test_config_flow_discovery.py` reusing the same patch target and fixtures).

## Implementation Notes

<details>
<summary>Test scenarios and patterns</summary>

Follow the existing patterns in `tests/test_config_flow.py` (patch `VALIDATE`, use `hub_entry_builder`, assert on `result["type"]`/`result["step_id"]`/`result["reason"]`). Mantra: "write a few tests, mostly integration" — combine related assertions; do not write a test per trivial branch.

**Helper**: define a small factory for the discovery info, e.g.
```python
from homeassistant.config_entries import SOURCE_HASSIO
from homeassistant.helpers.service_info.hassio import HassioServiceInfo

def _disc(host="core-rtl433", port=8433, uid="serial:0123"):
    return HassioServiceInfo(
        config={"host": host, "port": port, "path": "/ws",
                "secure": False, "unique_id": uid, "addon": "rtl_433"},
        name="rtl_433", slug="abc123", uuid="deadbeef",
    )
```

**Test 1 — happy path → confirm → create.** With `validate_connection` mocked to succeed:
```python
result = await hass.config_entries.flow.async_init(
    DOMAIN, context={"source": SOURCE_HASSIO}, data=_disc())
assert result["type"] is FlowResultType.FORM
assert result["step_id"] == "hassio_confirm"
result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
assert result["type"] is FlowResultType.CREATE_ENTRY
entry = result["result"]
assert entry.unique_id == "serial:0123"
assert entry.data["manage_settings"] in (True, False)  # default present
assert entry.data["host"] == "core-rtl433" and entry.data["port"] == 8433
```

**Test 2 — adoption/migration.** Pre-create a manual entry on the same host:port with `unique_id="hub:core-rtl433:8433"` (use `hub_entry_builder` or `MockConfigEntry(... unique_id=..., data={host, port, ...})`, added to hass). Drive `_disc()`. Assert:
```python
assert result["type"] is FlowResultType.ABORT
assert result["reason"] == "already_configured"
entries = hass.config_entries.async_entries(DOMAIN)
assert len(entries) == 1
assert entries[0].unique_id == "serial:0123"   # re-keyed
```

**Test 3 — port-change update.** Pre-create a discovered-style entry `unique_id="serial:0123"`, `data` host:8433. Drive `_disc(port=8434)`. Assert ABORT `already_configured` and the surviving entry's `data["port"] == 8434`, count unchanged.

**Test 4 — manual dedup against discovered.** Pre-create entry `unique_id="serial:0123"`, host:8433. Run the manual `user` step with the same host/port (`context={"source": SOURCE_USER}`, `validate_connection` mocked ok). Assert ABORT `already_configured` and no new entry.

**Test 5 — reconfigure preserves stable id.** Pre-create entry `unique_id="serial:0123"`, host:8433. Start a reconfigure (`entry.start_reconfigure_flow(hass)` or `context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id}`), submit changed host/port with `validate_connection` ok. Assert the entry's `unique_id` is still `"serial:0123"` (not `"hub:..."`) and the connection data updated. Mirror the existing reconfigure tests for the invocation idiom.

**Test 6 — confirm cannot_connect.** Patch `validate_connection` to raise `CannotConnect`. Init `_disc()` → reach `hassio_confirm`, configure `{}`. Assert `result["type"] is FlowResultType.FORM`, `result["step_id"] == "hassio_confirm"`, `result["errors"] == {"base": "cannot_connect"}`.

Run `uv run --python 3.14 pytest tests/test_config_flow.py -q` and confirm green.
</details>
