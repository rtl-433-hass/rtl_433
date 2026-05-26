---
id: 6
group: "docs"
dependencies: [1, 2, 3, 4, 5]
status: "pending"
created: "2026-05-26"
skills:
  - technical-writing
---
# Documentation: README hub entities + AGENTS frame/getter contracts

## Objective
Document the new hub-level observability so users and future agents understand
it. Add a "Hub entities" description to `README.md` (connectivity, SDR/meta
diagnostics including gain & ppm, server stats) and note the `/cmd` reachability
requirement and periodic stats refresh. Record the durable contracts in
`AGENTS.md`: the frame-classification rule, the HTTP-getter sourcing at the
server root, the exact getter set, and the verified Data Contracts. Optionally
enrich `WEBSOCKET_API.md` with the `get_stats` payload and the gain/ppm-absent
note.

## Skills Required
- `technical-writing` — concise, accurate Markdown matching the existing docs' tone.

## Acceptance Criteria
- [ ] `README.md` documents the hub entities: a connectivity binary_sensor (immediate server up/down), SDR/meta diagnostic sensors (center frequency, sample rate, conversion mode, hop interval, gain, frequency correction/ppm), and server-stats sensors (decoded events, OOK frames, FSK frames, enabled decoders). It states that stats reflect `get_stats` and refresh periodically, and that these hub sensors require the server's `/cmd` endpoint to be reachable at `host:port/cmd` (graceful degradation otherwise: the event stream and connectivity sensor keep working).
- [ ] `AGENTS.md` records: (a) the frame-classification contract — a frame is a device event iff it has `model` or an identity key (`id`/`channel`/`subtype`), only `shutdown` is otherwise handled, everything else is ignored; (b) that hub meta/stats come from `/cmd` HTTP getters at the **server root** (`scheme://host:port/cmd`, never the WS path); (c) the exact getter set (`get_meta` + `get_gain` + `get_ppm_error` + `get_stats`, with gain/ppm absent from `get_meta`); (d) the verified Data Contracts.
- [ ] (Optional) `WEBSOCKET_API.md` notes the `get_stats` payload shape and that `get_meta` carries neither gain nor ppm.
- [ ] Markdown lint passes (`.markdownlint.json` / pre-commit) and the docs match the code merged by Tasks 1-5.

Use your internal Todo tool to track these and keep on track.

## Technical Requirements
- Files: `README.md`, `AGENTS.md`, and optionally `WEBSOCKET_API.md`.
- Keep within the existing heading structure and writing style.

## Input Dependencies
- Tasks 1-5: the implemented behavior the docs describe (classifier, getters, cleanup, connectivity sensor, diagnostic sensors).

## Output Artifacts
- Updated end-user and agent documentation.

## Implementation Notes

<details>
<summary>Detailed implementation guidance</summary>

### README.md
- `## Features` (line ~31) — add a bullet for hub-level observability entities (connectivity, SDR/meta diagnostics, server stats).
- Add a short `## Hub entities` subsection (or fold into an existing section near `## Availability`/`## Discovery`). Cover:
  - **Connectivity** binary_sensor: on/off mirrors the WebSocket; flips off immediately on a server shutdown notice.
  - **SDR / meta diagnostics**: center frequency, sample rate, conversion mode, hop interval (from `get_meta`); gain (from `get_gain`, empty ⇒ "auto"); frequency correction/ppm (from `get_ppm_error`). `frequencies`/`hop_times` arrays appear as attributes.
  - **Server stats**: decoded events (cumulative, tolerates server counter resets), OOK frames, FSK frames, enabled decoders (from `get_stats`); the per-protocol `stats[]` and `since` are attributes.
  - **Requirement**: these hub sensors fetch over HTTP at `http(s)://host:port/cmd` (the server root). If a reverse proxy exposes only the WebSocket path and not `/cmd`, the meta/stats/gain/ppm sensors stay `unknown` while the event stream and connectivity sensor keep working.
  - **Refresh**: `get_stats` is re-polled on a fixed interval while connected; meta/gain/ppm are fetched once per (re)connect.

### AGENTS.md
- Under "Config-entry model" or a new short "WebSocket frames & hub observability" subsection, add:
  - **Frame classification contract**: the coordinator treats a frame as a decoded-device event iff it has `model` **or** an identity key (`id`/`channel`/`subtype`); a `shutdown` frame drives the connectivity sensor; all other frames (`meta`, state/stats, RPC `result`/`error`) are ignored on the socket. This is why non-event frames no longer create a phantom `"unknown"` device or pollute `seen_fields`/`unmatched_field_keys`.
  - **Hub observability data source**: SDR/meta and server stats come from one-shot HTTP GETs to `scheme://host:port/cmd` at the server root (https when secure/wss), never the streaming socket and never the configured WS path. Exact getters: `get_meta` (center_frequency, samp_rate, conversion_mode, frequencies[], hop_times[], duration, stats_interval, report_* flags — **no gain, no ppm**), `get_gain` (string, empty ⇒ auto), `get_ppm_error` (int), `get_stats` (`enabled`, `since`, `frames.{count,fsk,events}`, `stats[]`). Hop interval = `hop_times[0]`.
  - **Phantom cleanup**: `async_setup_entry` idempotently removes a legacy persisted `"unknown"` device from the devices map and registry.
- Keep the existing guardrails list intact; add the new constant if it belongs (`SIGNAL_HUB_UPDATE` is in `const.py`).

### WEBSOCKET_API.md (optional)
The doc already has `### meta object` (line ~184) and a "Commands / Queries
(getters)" section (~90). Add the `get_stats` payload shape near the getters and
a one-line note under the meta object that gain and ppm are not part of it (use
`get_gain` / `get_ppm_error`). Only do this if it does not duplicate content
excessively — it is explicitly optional in the plan.

### Lint
Run the repo's markdown lint (pre-commit `markdownlint` and `prettier`) or at
least keep line lengths and list styles consistent with the surrounding file.
</details>
