---
id: 2
group: "coordinator"
dependencies: [1]
status: "pending"
created: 2026-08-31
skills:
  - home-assistant-integration
  - python
---
# Remove the discovery_enabled toggle and migrate existing entries

## Objective

Delete the per-hub `discovery_enabled` setting from every layer of the
integration and strip it from existing config entries with a minor-version
migration. With adoption now explicit (task 1), the toggle gates nothing and
keeping it would present users with a second, contradictory discovery concept.

## Skills Required

- `home-assistant-integration` — config entries, config/options flows,
  translations, and the minor-version migration pattern in `migration.py`.
- `python` — a wide, mechanical refactor across the package and its tests.

## Acceptance Criteria

- [ ] `CONF_DISCOVERY_ENABLED` is removed from `const.py`.
- [ ] The toggle is removed from both `config_flow.py` steps (the user step and
      the reconfigure step), including the values written into `entry.data`.
- [ ] The toggle is removed from the `options_flow.py` hub step schema and its
      persisted options.
- [ ] `_hub_discovery_enabled` is removed from `hub_settings.py` and all callers.
- [ ] The coordinator no longer accepts or stores `discovery_enabled`.
- [ ] `diagnostics.py` no longer reports `discovery_enabled`.
- [ ] The update listener in `__init__.py` no longer pushes the toggle into the
      running coordinator.
- [ ] The `discovery_enabled` strings are removed from
      `translations/en.json` (config, options, and any selector sections).
- [ ] `MINOR_VERSION` is bumped to 8 and a migration strips `discovery_enabled`
      from both `entry.data` and `entry.options` on existing entries.
- [ ] The migration does **not** modify `entry.data[CONF_DEVICES]`: every
      already-adopted device, per-device override, and calibration survives
      untouched.
- [ ] `grep -rn "discovery_enabled\|CONF_DISCOVERY_ENABLED" custom_components/ tests/`
      returns hits only inside the migration that strips the key and its test.
- [ ] `uv run pytest tests/` passes; existing tests that set or assert the toggle
      are updated.
- [ ] `uv run ruff check custom_components/ tests/` and
      `uv run ruff format --check custom_components/ tests/` pass.

Use your internal Todo tool to track these and keep on track.

## Technical Requirements

- Files: `const.py`, `config_flow.py`, `options_flow.py`, `hub_settings.py`,
  `coordinator/base.py`, `coordinator/_events.py`, `diagnostics.py`,
  `__init__.py`, `migration.py`, `translations/en.json`, plus the test modules
  listed below.
- The config entry is at `VERSION = 2`, `MINOR_VERSION = 7` in `config_flow.py`;
  `migration.py` holds `async_migrate_entry` and the existing per-minor-version
  helpers to copy the pattern from.
- Backwards compatibility for the *toggle itself* is explicitly not required —
  the user approved this behavioural break. Backwards compatibility for
  *adopted devices* is required and non-negotiable.

## Input Dependencies

- Task 1: the pending/adopted routing that makes the toggle redundant, and
  `CONF_IGNORED_DEVICES` in `const.py`.

## Output Artifacts

- A toggle-free `options_flow.py` hub step for task 3 to extend.
- The `MINOR_VERSION = 8` migration asserted by task 5's migration test.

## Implementation Notes

<details>
<summary>Detailed implementation guidance</summary>

### Known reference sites

`grep -rn "CONF_DISCOVERY_ENABLED\|discovery_enabled" custom_components/ tests/`
finds roughly 140 hits. The production ones cluster as:

- `const.py:54` — the constant definition, plus the `SIGNAL_NEW_DEVICE` comment
  around line 255 that says the signal is "gated by the per-hub discovery
  toggle". Fix that comment: the signal is now dispatched for adopted devices and
  by explicit adoption.
- `config_flow.py` — the import, `vol.Optional(CONF_DISCOVERY_ENABLED, default=True): bool`
  in the user-step schema (~line 129) and the reconfigure-step schema (~line 439),
  and the two places the value is written into the entry data (~lines 200, 466).
- `options_flow.py` — the import, the `discovery_default` lookup (~line 130) and
  the schema entry (~line 145). Also fix the module docstring, which says the hub
  step "persists the per-hub discovery toggle".
- `hub_settings.py` — `_hub_discovery_enabled` (~line 30) and its export.
- `coordinator/base.py` — the `discovery_enabled` constructor parameter,
  the attribute assignment, the class-docstring line, and the module-docstring
  paragraphs describing the discovery gate.
- `coordinator/_events.py` — already largely handled by task 1; remove any
  residual mention.
- `diagnostics.py:102` — `diagnostics["discovery_enabled"] = ...`.
- `__init__.py` — the `_hub_discovery_enabled` import and call site, the
  `coordinator.discovery_enabled = ...` assignment in `_async_update_listener`
  (~line 307) and the log line just below it, plus the docstring paragraph about
  discovery-toggle changes being applied live.
- `translations/en.json` — `data` / `data_description` entries under
  `config.step.user`, `config.step.reconfigure`, and `options.step.hub`.

Work from the grep, not from this list alone; treat the list as a checklist, and
re-run the grep as the completion gate.

### Migration

In `config_flow.py` bump `MINOR_VERSION = 7` to `8`. In `migration.py`, follow
the existing minor-version helper pattern:

```python
def _strip_discovery_toggle(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Drop the retired ``discovery_enabled`` key from an existing entry.

    Discovery is no longer a toggle: every heard device is offered as a pending
    candidate and nothing is added without an explicit user action, so the key
    gates nothing. Removing it prevents a stale value from lingering in
    diagnostics and config-entry exports. Idempotent, and deliberately touches
    nothing else -- adopted devices and their per-device settings must survive
    the upgrade untouched.
    """
    data = {k: v for k, v in entry.data.items() if k != "discovery_enabled"}
    options = {k: v for k, v in entry.options.items() if k != "discovery_enabled"}
    if data == dict(entry.data) and options == dict(entry.options):
        return
    hass.config_entries.async_update_entry(entry, data=data, options=options)
```

Use the string literal `"discovery_enabled"` here, not a constant — the constant
is being deleted, and the migration must keep working against entries written by
older versions. This is the one place the string is allowed to survive.

Wire it into `async_migrate_entry` behind the minor-version-8 bump, matching how
the existing minor bumps are sequenced, and make sure the entry's
`minor_version` is updated so the migration does not re-run.

### Tests

Modules that reference the toggle: `tests/conftest.py`,
`tests/test_config_flow.py`, `tests/test_mut_config_flow.py`,
`tests/test_coordinator.py`, `tests/test_lifecycle.py`,
`tests/test_diagnostics_repairs.py`, `tests/test_mut_diagnostics.py`,
`tests/test_mut_init.py`, `tests/test_mut_entity.py`,
`tests/test_mut_binary_sensor.py`, `tests/test_sdr_controls.py`.

Remove the key from fixtures and schema assertions. Where a test asserted "with
discovery off, no device is created", the assertion is now the *default*
behaviour and the test either becomes redundant (delete it — task 4 covers the
real contract) or is rewritten to assert the pending-list outcome. Do not leave a
test that passes because it no longer asserts anything.

Note `tests/test_config_flow.py` asserts the exact set of schema keys in several
places; those lists need the key removed.

</details>
