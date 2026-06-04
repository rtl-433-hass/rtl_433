---
id: 2
group: "config-flow"
dependencies: [1]
status: "completed"
created: "2026-06-04"
skills:
  - python
---
# Discovery-time orphan reconciliation (B1)

## Objective
When Supervisor discovery presents an **unknown** radio `unique_id` (a likely
replacement on a new `host:port`) and at least one hub entry already exists, offer
a guided **"this radio replaces hub X / it's a new radio"** choice instead of
silently creating a duplicate empty entry.

## Skills Required
- `python` — Home Assistant config-flow discovery (`async_step_hassio`).

## Acceptance Criteria
- [ ] A new `async_step_hassio_replace` step presents a select: one option per existing hub entry (value = `entry_id`, label = entry title) plus a `"__new__"` ("It's a new radio") option, defaulting to `"__new__"`.
- [ ] Choosing a hub rebinds it to the discovered radio id via `async_rebind_hub` (task 1) and ends the flow; a populated-collision returns `already_configured`.
- [ ] Choosing `"__new__"` falls through to the existing `async_step_hassio_confirm` (today's new-entry behavior, unchanged).
- [ ] `async_step_hassio` routes to `async_step_hassio_replace` **only** on the genuinely-new path (unknown id, no same-host:port adoption) **and** when ≥1 hub entry exists; otherwise behavior is unchanged.
- [ ] The discovered radio `unique_id` is carried into the replace step (added to `self._discovery`).

Use your internal Todo tool to track these and keep on track.

## Technical Requirements
- File: `custom_components/rtl_433/config_flow.py`.
- Use `SelectSelector` for the labeled hub picker.

## Input Dependencies
- Task 1: `async_rebind_hub`.

## Output Artifacts
- `async_step_hassio_replace` step + `replaces` field key — consumed by task 4 (translations) and task 6 (tests).

## Implementation Notes
<details>
<summary>Step-by-step</summary>

**1. Imports** — add to the selector import block:
```python
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
)
```

**2. Carry the radio id into the discovery dict.** In `async_step_hassio`, in the
"genuinely new radio" path (after `self.async_set_unique_id(radio_uid)` /
`_abort_if_unique_id_configured`), add `"unique_id": radio_uid` to `self._discovery`:
```python
self._discovery = {
    "unique_id": radio_uid,
    CONF_HOST: host,
    CONF_PORT: port,
    CONF_PATH: path,
    CONF_SECURE: secure,
    "addon": addon,
}
self.context["title_placeholders"] = {"name": f"{addon} ({host}:{port})"}
# Offer a guided replace when other hubs already exist; else add as new.
if self._async_current_entries():
    return await self.async_step_hassio_replace()
return await self.async_step_hassio_confirm()
```

**3. New step** — add after `async_step_hassio_confirm`:
```python
async def async_step_hassio_replace(
    self, user_input: dict[str, Any] | None = None
) -> ConfigFlowResult:
    """Offer to rebind an existing hub to a newly discovered radio.

    Shown when discovery sees an unknown radio id while hubs already exist (the
    likely "replacement landed on a new host:port" case). The user explicitly
    chooses to replace a specific hub or to add the radio as new; we never
    auto-rebind silently.
    """
    assert self._discovery is not None
    disc = self._discovery
    entries = self._async_current_entries()
    options = [
        SelectOptionDict(value=e.entry_id, label=e.title or e.entry_id)
        for e in entries
    ]
    options.append(SelectOptionDict(value="__new__", label="It's a new radio"))
    placeholders = {
        "addon": disc["addon"],
        "host": disc[CONF_HOST],
        "port": str(disc[CONF_PORT]),
    }

    if user_input is not None:
        choice = user_input["replaces"]
        if choice == "__new__":
            return await self.async_step_hassio_confirm()
        entry = self.hass.config_entries.async_get_entry(choice)
        if entry is None:
            return await self.async_step_hassio_confirm()
        status = await async_rebind_hub(
            self.hass,
            entry,
            disc["unique_id"],
            {
                CONF_HOST: disc[CONF_HOST],
                CONF_PORT: disc[CONF_PORT],
                CONF_PATH: disc[CONF_PATH],
                CONF_SECURE: disc[CONF_SECURE],
            },
            title=f"rtl_433 ({disc[CONF_HOST]})",
        )
        if status == "already_configured":
            return self.async_abort(reason="already_configured")
        return self.async_abort(reason="rebind_successful")

    schema = vol.Schema(
        {
            vol.Required("replaces", default="__new__"): SelectSelector(
                SelectSelectorConfig(options=options)
            )
        }
    )
    return self.async_show_form(
        step_id="hassio_replace",
        data_schema=schema,
        description_placeholders=placeholders,
    )
```

**4.** All current config entries are hubs (the v2 one-entry-per-server model), so
`self._async_current_entries()` is the correct hub list. No need to filter by
entry type.

**5. Run** `uv run pytest tests/test_config_flow.py -q` (existing discovery tests
that assert new-entry creation will now route through the replace step only when a
hub already exists — most existing tests start from zero entries, so they keep
hitting `async_step_hassio_confirm`; verify they still pass). New coverage is in
task 6.
</details>
