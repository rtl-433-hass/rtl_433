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

## Identity ABI: PR1 carries contract revision 2

[`COMPATIBILITY_CONTRACT.md`](COMPATIBILITY_CONTRACT.md) is at **revision 2**. Revision 2
replaced the v2 "hub" model (one config entry == one rtl_433 server) with the **location /
receiver-subentry** model: a config entry is a location, each rtl_433 server is a receiver
config subentry, and a decoded RF device is **unioned** across the location's receivers.

**PR1 MUST be written against revision 2 from the start.** It must not land the revision-1
(v2 "hub") shape and then break it in a follow-up: both builds share the `rtl_433` domain
and the same registries, so a Core build on revision 1 and a HACS build on revision 2 would
disagree about every receiver-owned `unique_id` and about the version ladder itself.

Hard prerequisites for the Core build — none of these are optional, and a PR1 missing any
of them cannot coexist with the HACS build:

1. **`VERSION = 3` / `MINOR_VERSION = 1`** on the config flow, and a future-schema guard of
   **`if entry.version > 3: return False`**. A Core build still rejecting `version > 2`
   treats every entry the HACS build writes as unsupported. This is the single most
   load-bearing line in the slice (`COMPATIBILITY_CONTRACT.md` §1).
2. **The four `unique_id` templates of §2 verbatim**, including the easily-missed
   four-segment per-receiver link template
   `f"{location_entry_id}:{device_key}:{receiver_subentry_id}:{object_suffix}"` for
   `rssi` / `snr` / `last_seen`. The unioned device-field template is unchanged from
   revision 1, so a naive port looks correct right up to the point a second receiver exists.
3. **The three device-registry identifier tuples of §3** plus the `via_device_id` links —
   merged devices link to the **location** device, never to a receiver.
4. **The registry-ownership rule of §5**: merged devices and every entity on them,
   per-receiver link entities included, are added with **no** `config_subentry_id`. Home
   Assistant only logs a deprecation for this today; it **raises in HA Core 2027.8**.
5. **The reserved `receiver` token of §6** in both identity parsers.

`migration.py` stays HACS-only for now, so the Core build does not yet have to *perform* the
v2 → v3 conversion — but it must accept its output, which items 1–5 are exactly what
guarantee.

### Sequencing decision (2026-09-12)

**Upstream state checked**: PR1 is
[home-assistant/core#175811 "Add rtl_433 integration"](https://github.com/home-assistant/core/pull/175811)
— **open, still a draft, `reviewDecision: REVIEW_REQUIRED`, no review activity since it was
opened (2026-07-06)**; branch `rtl_433-integration`, base `dev`. It is **not merged**, and
`homeassistant/components/rtl_433/` does not exist in `dev`.

**Decision: amend the draft in place; do not open a second PR and do not wait.** Because the
draft has attracted no review yet, rewriting its identity surfaces onto revision 2 costs a
force-push and no reviewer's time — strictly cheaper than landing revision 1 and owing core a
migration PR immediately afterwards. Plan 27 therefore lands in HACS first (it is the change
that *defines* revision 2), and the draft is rebased onto the revision-2 templates before it
is marked ready for review.

**Revisit this decision if** the draft has received a substantive review, or has been merged,
before it is amended. In either case the break stops being free and becomes a coordinated
two-build migration; see `COMPATIBILITY_CONTRACT.md`'s change-control section.

Status values:
- **in-PR** — included in an open or scoped upstream PR (see the PR tag).
- **upstreamed** — merged into Home Assistant Core (record the landing PR/commit).
- **HACS-only** — ships in this repo only; not yet scoped for upstream.
- **in `pyrtl_433`** — no longer a module of this integration at all: it lives in
  the `pyrtl_433` PyPI requirement, so core gets it for free the moment the
  requirement lands. Nothing to upstream.

## Per-module status

Inventory derived from the actual contents of `custom_components/rtl_433/` (top-level
modules plus the `coordinator/` subpackage).

PR1 rows are all **contract revision 2** surfaces: see
[Identity ABI: PR1 carries contract revision 2](#identity-abi-pr1-carries-contract-revision-2)
above before touching any of them upstream.

| Module | Status | Landing PR/commit |
| --- | --- | --- |
| `__init__.py` | in-PR (PR1, Bronze) — location device, receiver device, `via_device_id` links, registry ownership (contract §3, §5) | |
| `manifest.json` | in-PR (PR1, Bronze) | |
| `const.py` | in-PR (PR1, Bronze) — `receiver_identity`, `RESERVED_DEVICE_KEYS` (contract §4, §6) | |
| `config_flow.py` | in-PR (PR1, Bronze) — **`VERSION = 3` / `MINOR_VERSION = 1`** plus the receiver subentry flow (contract §1) | |
| `coordinator/` (`base.py`, `_events.py`, `_sdr.py`, `_watchdog.py`, `__init__.py`) | in-PR (PR1, Bronze) — one coordinator per receiver subentry | |
| `sensor.py` | in-PR (PR1, Bronze) — receiver diagnostics carry the receiver segment (contract §2) | |
| `entity.py` | in-PR (PR1, Bronze) — shared base entity; `field_unique_id` is the single definition of both device-field templates (contract §2) | |
| `aggregator.py` | HACS-only — the location fan-in (union, link-field exclusion, merged availability, coverage) | |
| `binary_sensor.py` | HACS-only | |
| `diagnostics.py` | HACS-only | |
| `event.py` | HACS-only | |
| `device_trigger.py` | HACS-only — one of the two identity parsers (contract §6) | |
| `number.py` | HACS-only | |
| `select.py` | HACS-only | |
| `switch.py` | HACS-only | |
| `settings.py` | HACS-only — shared builders behind the options flow and the panel | |
| `receiver_settings.py` | HACS-only — receiver-subentry resolvers | |
| `sdr_settings.py` | HACS-only | |
| `repairs.py` | HACS-only | |
| `options_flow.py` | HACS-only | |
| `calibration.py` | HACS-only | |
| `adoption.py` | HACS-only — location-scoped adopt / ignore / un-ignore | |
| `websocket_api.py` + `frontend/rtl_433-panel.js` | HACS-only — admin-gated panel support; not a public API | |
| `device_replace.py` | HACS-only — the only sanctioned in-place re-key (device_key change, receiver consolidation) | |
| device-library YAML (per-domain: `temperature`, `humidity_moisture`, `pressure`, `rain`, `wind`, `air_quality`, `light_uv`, `power_electrical`, `binary_states`, `events`, `misc`, `_skip_keys`) | in `pyrtl_433` (`pyrtl_433.library`, shipped as package data) | pyrtl_433 0.3.0 |
| `library.py` (HA-side load/merge + `hass.data` cache over `pyrtl_433.library`) | HACS-only | |
| descriptor loader / override / transform layer | in `pyrtl_433` (`pyrtl_433.library`) | pyrtl_433 0.3.0 |
| entity-slug and device-naming helpers | in `pyrtl_433` (`pyrtl_433.naming`) | pyrtl_433 0.3.0 |
| event-driven availability classifier | in `pyrtl_433` (`pyrtl_433.availability`) | pyrtl_433 0.3.0 |
| `migration.py` (config-entry migration) | HACS-only | |
| `translations/` | in-PR (PR1, Bronze) — scoped alongside config flow | |
| `brand/` (in-repo brand assets) | out of scope — see brands PR note below | |

Notes:
- `entity.py`, `translations/`, `const.py`, and `coordinator/` are shared infrastructure
  pulled into PR1 because the Bronze `sensor` slice cannot function without them.
- The data-normalization layer (the YAML library, its loader/override/transform code,
  the naming helpers and the event-driven classifier) is **no longer a module of this
  integration**: it moved into the `pyrtl_433` requirement in 0.3.0, so core inherits it
  with the dependency and there is nothing left to upstream. Only `library.py` — the
  thin Home Assistant wrapper that runs the load in the executor and caches the merged
  registry on `hass.data` — remains HACS-only, and it lands with the PRs whose platforms
  first depend on the richer descriptor output.
- `migration.py` follows once the core config-entry shape is fixed and needs versioning. It
  stays HACS-only through PR1: core does not have to *perform* the v2 → v3 conversion, but
  PR1 must accept its output (the `> 3` guard and the revision-2 templates).
- The union model's own modules (`aggregator.py`, `adoption.py`, `device_replace.py`, the
  WebSocket/panel pair) are HACS-only and sit after the Gold refinement layer below. PR1 is
  unaffected by their absence: a single-receiver location produces exactly the revision-2
  identity surfaces with no aggregator in the picture, which is what makes shipping the new
  templates in a minimal slice possible at all.

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
10. **`library.py`** — Gold. The Home Assistant load/merge wrapper that turns the
    `pyrtl_433` device library into the per-entry merged registry the platforms look
    descriptors up in; expands the entity descriptions once the platforms consuming them
    are upstream. The library data and loader themselves arrive with the `pyrtl_433`
    requirement, so this PR is only the `hass.data` caching seam.
11. **user mapping overrides** — Gold. The user-facing field-mapping/override surface
    (`CONF_USER_MAPPINGS`, the options-flow editor step, `merge_overrides`); depends on
    (10) landing first. Validation/normalization is `pyrtl_433.library`'s, so this is the
    storage + UI half only.
12. **`options_flow`** — Gold. Configuration UX that tunes behavior across the platforms;
    lands once the platforms and settings surfaces it configures all exist.
13. **`receiver_settings` / `settings`** — Gold. Shared receiver-level settings model and the
    builders over it; formalized alongside/after the control platforms
    (`number`/`select`/`switch`) that consume it.
14. **`sdr_settings`** — Gold/Platinum. SDR-device settings model; the deepest control
    surface, landed last so the full control stack above it is already upstream.

> Ordering guidance: `diagnostics` (Silver) is intentionally pulled forward; the remaining
> Silver items (`event`, `device_trigger`, `repairs`) precede the Gold refinement layer
> (`calibration`, `library.py`, user mapping overrides, `options_flow`, `receiver_settings`,
> `sdr_settings`). `receiver_settings`/`sdr_settings` are listed last as tracked line items
> even though their supporting code lands with the `number`/`select`/`switch` control PRs.
> The union-model modules (`aggregator.py`, `adoption.py`, `device_replace.py`, the
> WebSocket API and the panel) are not in this sequence yet: they are the multi-receiver
> half of the location model, and core only needs them once a core-side location can hold
> more than one receiver.

## Out of scope for this workflow run

The following are explicitly **not** performed by this automation run and are handled
manually:
- **Opening the upstream PRs** against `home-assistant/core`.
- **The documentation PR** against `home-assistant/home-assistant.io`.
- **The brands PR** against `home-assistant/brands` (icon/logo assets).
