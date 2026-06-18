---
id: 3
group: "repairs"
dependencies: [1]
status: "completed"
created: "2026-06-04"
skills:
  - python
---
# Actionable unreachable-server repair fix flow (C′)

## Objective
Replace the bare `ConfirmRepairFlow()` for the `server_unreachable` issue with a
custom fix flow that **embeds the rebind form**, so a user whose dongle died (and
whose hub is therefore unreachable) can re-point the hub at the replacement radio
directly from the repair card — reusing the shared `async_rebind_hub` helper.

## Skills Required
- `python` — Home Assistant repairs (`RepairsFlow`) + issue registry.

## Acceptance Criteria
- [ ] `async_create_fix_flow` returns a custom `RepairsFlow` for issue ids prefixed `server_unreachable` (recovering `entry_id` from the issue id); all other issues still return `ConfirmRepairFlow()`.
- [ ] The fix flow's form shows the radio-id (free-text, pre-filled with the entry's current `unique_id`) + host/port/path/secure (pre-filled from `entry.data`).
- [ ] On submit it validates connectivity, rebinds via `async_rebind_hub`, clears the unreachable issue, and finishes; `cannot_connect` re-shows the form; a populated-collision re-shows with an error.
- [ ] Submitting unchanged fields is a safe no-op rebind that still clears the card.

Use your internal Todo tool to track these and keep on track.

## Technical Requirements
- File: `custom_components/rtl_433/repairs.py`.
- Reuse `async_rebind_hub`, `CONF_RADIO_ID`, `CONF_SECURE` from `config_flow.py`; `Rtl433Coordinator.validate_connection` + `CannotConnect` from `coordinator`.
- `config_flow.py` must NOT import `repairs.py` (avoid a cycle); `repairs.py` importing `config_flow.py` is fine.

## Input Dependencies
- Task 1: `async_rebind_hub`, `CONF_RADIO_ID`.

## Output Artifacts
- `server_unreachable` fix-flow form (step id `confirm`) — consumed by task 4 (translations) and task 7 (tests).

## Implementation Notes
<details>
<summary>Step-by-step</summary>

**1. Imports** (top of `repairs.py`):
```python
import voluptuous as vol
from .config_flow import CONF_SECURE, async_rebind_hub
from .const import (
    CONF_HOST, CONF_PATH, CONF_PORT, CONF_RADIO_ID, DEFAULT_PATH, DEFAULT_PORT,
    DOMAIN, LOGGER, signal_hub_update,  # extend the existing .const import
)
from .coordinator import CannotConnect, Rtl433Coordinator  # extend existing import
```
(`Rtl433Coordinator` is already imported; add `CannotConnect`.)

**2. Custom flow class** (add above `async_create_fix_flow`):
```python
class HubRadioReplaceRepairFlow(RepairsFlow):
    """Fix flow for an unreachable hub: re-point it at a replacement radio.

    The dead radio is exactly what raised this issue, so this is the natural
    recovery surface. Leaving the fields unchanged simply revalidates and clears
    the card; entering a new radio id re-points the hub (preserving entry_id, so
    devices/entities/history survive) via the shared rebind helper.
    """

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> Any:
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> Any:
        entry = self._entry
        data = entry.data
        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_RADIO_ID, default=entry.unique_id or ""
                ): str,
                vol.Required(CONF_HOST, default=data.get(CONF_HOST, "")): str,
                vol.Required(
                    CONF_PORT, default=data.get(CONF_PORT, DEFAULT_PORT)
                ): int,
                vol.Required(
                    CONF_PATH, default=data.get(CONF_PATH, DEFAULT_PATH)
                ): str,
                vol.Optional(CONF_SECURE, default=data.get(CONF_SECURE, False)): bool,
            }
        )
        if user_input is not None:
            host = user_input[CONF_HOST]
            port = user_input[CONF_PORT]
            path = user_input[CONF_PATH]
            secure = user_input[CONF_SECURE]
            try:
                await Rtl433Coordinator.validate_connection(
                    self.hass, host, port, path, secure=secure
                )
            except CannotConnect:
                return self.async_show_form(
                    step_id="confirm",
                    data_schema=schema,
                    errors={"base": "cannot_connect"},
                    description_placeholders={"title": entry.title},
                )
            new_uid = (user_input.get(CONF_RADIO_ID) or "").strip() or (
                entry.unique_id or ""
            )
            status = await async_rebind_hub(
                self.hass,
                entry,
                new_uid,
                {
                    CONF_HOST: host,
                    CONF_PORT: port,
                    CONF_PATH: path,
                    CONF_SECURE: secure,
                },
                title=f"rtl_433 ({host})",
            )
            if status == "already_configured":
                return self.async_show_form(
                    step_id="confirm",
                    data_schema=schema,
                    errors={"base": "id_in_use"},
                    description_placeholders={"title": entry.title},
                )
            async_clear_hub_unreachable(self.hass, entry)
            return self.async_create_entry(title="", data={})

        return self.async_show_form(
            step_id="confirm",
            data_schema=schema,
            description_placeholders={"title": entry.title},
        )
```

**3. Route in `async_create_fix_flow`:**
```python
async def async_create_fix_flow(hass, issue_id, data):
    """Return the repair flow for an issue."""
    if issue_id.startswith(ISSUE_UNREACHABLE):
        entry_id = issue_id[len(ISSUE_UNREACHABLE) + 1:]
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is not None:
            return HubRadioReplaceRepairFlow(entry)
    return ConfirmRepairFlow()
```
Note `ISSUE_SAMPLE_RATE_LOW` ("sample_rate_low_for_band") does not start with
"server_unreachable", so it correctly keeps `ConfirmRepairFlow()`.

**4.** Keep `ConfigEntry`, `HomeAssistant`, `Any` imports present (already imported).

**5. Run** `uv run pytest tests/test_diagnostics_repairs.py -q` to confirm existing
repairs tests still pass. New coverage is in task 7.
</details>
