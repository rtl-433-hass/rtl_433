---
id: 7
group: "documentation"
dependencies: [2, 3, 4, 5]
status: "pending"
created: 2026-05-27
skills:
  - technical-writing
---
# Documentation: README.md + AGENTS.md for Hub SDR Controls

## Objective
Document the HA-managed SDR controls for both end users (README.md) and future automated
contributors (AGENTS.md), reflecting the behavior implemented in Tasks 1–5.

## Skills Required
- `technical-writing` — concise, accurate user- and contributor-facing docs matching the
  existing voice and structure of `README.md` / `AGENTS.md`.

## Acceptance Criteria
- [ ] **README.md** documents: the new HA-managed SDR controls (number/select/switch on
      the hub) and which fields they cover; the management toggle ("Manage rtl_433
      settings from Home Assistant", default on) and that it can be set at initial setup
      **and** later in hub options; the **HA-as-authority** behavior ("once a hub is
      managed, change settings in Home Assistant, not the rtl_433 config file"); the
      toggle as the **only** re-sync path with the exact dance (**off → restart rtl_433 →
      on**); that controls require the server's `/cmd` endpoint reachable at
      `host:port/cmd`; and that **hopping** and **multi-stage gain** setups should be
      managed via the rtl_433 config (or with the toggle off). Cross-reference the
      existing "Hub entities" section and note that in managed mode the five folded
      diagnostic sensors are replaced by their controls (center frequency keeps its
      actual sensor).
- [ ] **AGENTS.md** records, as durable contracts: the settings-registry contract (the
      six fields, their getters, `/cmd` setter commands, value mappings, and the
      capability gate); adoption + full-enforcement-on-reconnect semantics with the
      hop-mode and `/cmd`-down guards; the `Store` persistence location (keyed by entry
      id, **not** `entry.options`) and why; the management-toggle behavior, the
      reload-only-on-toggle-change listener rule, and the deliberate absence of a
      re-adopt button/service; and the out-of-scope-but-anticipated upstream items
      (decoder enable/disable, device selection, hop lists) with the suggestion that
      upstream advertise capabilities.
- [ ] Wording is consistent with the implemented entity names, the `CONF_MANAGE_SETTINGS`
      option, and the registry field list; no invented commands or fields (cross-check
      against `WEBSOCKET_API.md` and `sdr_settings.py`).
- [ ] Markdown lint/style stays consistent with the surrounding docs (headings, list
      style, code fences).

Use your internal Todo tool to track these and keep on track.

## Technical Requirements
- Extend the existing README **"Hub entities"** section (around its current SDR/meta
  diagnostics bullet) rather than inventing a new top-level section, unless a short new
  subsection (e.g. "### Managing SDR settings from Home Assistant") reads more clearly.
- Extend the existing AGENTS **"WebSocket frames & hub observability"** section or add a
  sibling section ("## Hub SDR controls (HA-managed settings)") immediately after it.
- Optional, not required: a one-line note in `WEBSOCKET_API.md` that the live SDR control
  and config-setter commands are now exercised by the integration over `/cmd`.

## Input Dependencies
- Tasks 2–5 (the implemented behavior, entity names, option key, and registry fields the
  docs describe).

## Output Artifacts
- Updated `README.md` and `AGENTS.md` (and optionally `WEBSOCKET_API.md`).

## Implementation Notes
<details>
<summary>Detailed guidance</summary>

- Read the current `README.md` "Hub entities" section (≈lines 166–189) and AGENTS.md
  "WebSocket frames & hub observability" section (≈lines 116–161) first and match their
  tone and depth.
- Pull the exact field list, command names, and value mappings from `sdr_settings.py`
  (Task 1) and the toggle key from `const.py` (`CONF_MANAGE_SETTINGS`). Confirm the
  re-sync dance and the no-re-adopt rationale against the plan's clarifications so the
  user docs match the implemented semantics.
- Keep README user-focused (what the controls do, how to opt out, the re-sync dance, the
  `/cmd` reachability requirement, hopping/multi-stage-gain caveats) and AGENTS
  contributor-focused (the registry contract, adoption/enforcement/guards, Store
  location, listener reload rule, no re-adopt action, anticipated upstream items).
</details>
