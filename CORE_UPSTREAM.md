# CORE_UPSTREAM.md — Home Assistant Core Upstreaming Tracker

**This file is the source of truth for upstreaming progress.** Update it every time an
upstream PR lands or its status changes.

## Strategy: single shared domain

The integration upstreams into Home Assistant Core under a **single shared `rtl_433`
domain**. Rather than shipping a monolithic PR, we land one Bronze-tier vertical slice
first (a single `sensor` platform plus the config flow, coordinator, and manifest), then
add the remaining platforms and support modules as a sequence of follow-up PRs. Every PR
targets the *same* domain and config-entry shape, so the HACS custom component and the
core integration stay behaviorally aligned throughout the (months-long) review.

Modules that have not yet landed upstream remain **HACS-only**: they ship in this repo's
`custom_components/rtl_433/` but are not yet part of the core integration. This ledger
tracks the delta so the long-lived core branch does not silently drift from HACS.

Status values:
- **in-PR** — included in an open or scoped upstream PR (see the PR tag).
- **upstreamed** — merged into Home Assistant Core (record the landing PR/commit).
- **HACS-only** — ships in this repo only; not yet scoped for upstream.

## Per-module status

Inventory derived from the actual contents of `custom_components/rtl_433/` (top-level
modules plus the `coordinator/`, `mapping/`, and `device_library/` subpackages).

| Module | Status | Landing PR/commit |
| --- | --- | --- |
| `__init__.py` | in-PR (PR1, Bronze) | |
| `manifest.json` | in-PR (PR1, Bronze) | |
| `const.py` | in-PR (PR1, Bronze) | |
| `config_flow.py` | in-PR (PR1, Bronze) | |
| `coordinator/` (`base.py`, `_events.py`, `_sdr.py`, `_watchdog.py`, `__init__.py`) | in-PR (PR1, Bronze) | |
| `sensor.py` | in-PR (PR1, Bronze) | |
| `entity.py` | in-PR (PR1, Bronze) — shared base entity | |
| `binary_sensor.py` | HACS-only | |
| `diagnostics.py` | HACS-only | |
| `event.py` | HACS-only | |
| `device_trigger.py` | HACS-only | |
| `number.py` | HACS-only | |
| `select.py` | HACS-only | |
| `switch.py` | HACS-only | |
| `hub_settings.py` | HACS-only | |
| `sdr_settings.py` | HACS-only | |
| `repairs.py` | HACS-only | |
| `options_flow.py` | HACS-only | |
| `calibration.py` | HACS-only | |
| `device_library/` (per-domain YAML: `temperature`, `humidity_moisture`, `pressure`, `rain`, `wind`, `air_quality`, `light_uv`, `power_electrical`, `binary_states`, `events`, `misc`, `_skip_keys`) | HACS-only | |
| `library.py` (device-library loader) | HACS-only | |
| `mapping/` (`_loader.py`, `_model.py`, `_overrides.py`, `_transform.py`, `__init__.py`) | HACS-only | |
| `normalizer.py` | HACS-only | |
| `migration.py` (config-entry migration) | HACS-only | |
| `translations/` | in-PR (PR1, Bronze) — scoped alongside config flow | |
| `brand/` (in-repo brand assets) | out of scope — see brands PR note below | |

Notes:
- `entity.py`, `translations/`, `const.py`, and `coordinator/` are shared infrastructure
  pulled into PR1 because the Bronze `sensor` slice cannot function without them.
- `library.py`/`device_library/`, `mapping/`, and `normalizer.py` are the data-normalization
  layer; they land with the PRs whose platforms first depend on their richer output.
- `migration.py` follows once the core config-entry shape is fixed and needs versioning.

## Ordered follow-up PR sequence

Ordered by quality-scale tier dependencies (what each PR *unlocks*), not by feature
preference. Each PR builds on the domain/coordinator established by PR1.

1. **`binary_sensor`** — Bronze. Second platform on the existing coordinator; lowest risk,
   reinforces the multi-platform entity pattern before anything harder.
2. **`diagnostics`** — Silver. Cheap, expected early; `diagnostics` is a Silver-tier
   requirement and has no platform dependencies, so it should land as soon as there are
   entities to redact.
3. **`event`** — Bronze/Silver platform. Adds the event platform central to rtl_433's
   push model; must exist before anything that consumes device events.
4. **`device_trigger`** — Silver. Depends on `event`; exposes automation triggers derived
   from the event platform, so it cannot precede it.
5. **`number`** — Silver/Gold. First SDR control surface; introduces the write path and
   the shared `sdr_settings` plumbing that `select`/`switch` reuse.
6. **`select`** — Silver/Gold. Grouped with `number`; shares the `hub_settings`/
   `sdr_settings` dependency and the control-write pattern.
7. **`switch`** — Silver/Gold. Completes the SDR control trio on the same shared settings
   plumbing; kept after `number`/`select` to land the group cohesively.
8. **`repairs`** — Silver/Gold. Issue-registry surface; needs real platforms and control
   paths in place to have actionable issues to raise.
9. **`calibration`** — Gold. Per-device calibration UX; a refinement layer over the sensor
   platforms, so it follows the platforms it adjusts.
10. **`device_library`** — Gold. Richer per-domain normalization/entity metadata; expands
    the entity descriptions once the platforms consuming them are upstream.
11. **`mapping`** — Gold. User-facing field-mapping/override layer built on top of the
    device library; depends on the normalization data landing first.
12. **`options_flow`** — Gold. Configuration UX that tunes behavior across the platforms;
    lands once the platforms and settings surfaces it configures all exist.
13. **`hub_settings`** — Gold. Shared hub-level settings model; formalized alongside/after
    the control platforms (`number`/`select`/`switch`) that consume it.
14. **`sdr_settings`** — Gold/Platinum. SDR-device settings model; the deepest control
    surface, landed last so the full control stack above it is already upstream.

> Ordering guidance: `diagnostics` (Silver) is intentionally pulled forward; the remaining
> Silver items (`event`, `device_trigger`, `repairs`) precede the Gold refinement layer
> (`calibration`, `device_library`, `mapping`, `options_flow`, `hub_settings`,
> `sdr_settings`). `hub_settings`/`sdr_settings` are listed last as tracked line items even
> though their supporting code lands with the `number`/`select`/`switch` control PRs.

## Out of scope for this workflow run

The following are explicitly **not** performed by this automation run and are handled
manually:
- **Opening the upstream PRs** against `home-assistant/core`.
- **The documentation PR** against `home-assistant/home-assistant.io`.
- **The brands PR** against `home-assistant/brands` (icon/logo assets).
