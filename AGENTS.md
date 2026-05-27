# AGENTS.md

Machine-oriented notes for AI agents and maintainers working on this
integration. For end-user docs see [README.md](README.md); for contribution
conventions (commits, releases, CI) see [CONTRIBUTING.md](CONTRIBUTING.md).

## Repository shape

- `custom_components/rtl_433/` — the integration.
  - `device_library/*.yaml` — the shipped, data-driven device mappings.
  - `coordinator/` — package (`base.py`) for the push WebSocket coordinator.
  - `config_flow.py`, `__init__.py`, `const.py`, `entity.py`, `mapping.py`,
    `normalizer.py`, `diagnostics.py`, `repairs.py`, `sensor.py`,
    `binary_sensor.py`, `translations/en.json`.
- `docs/device-library.md` — **authoritative** device-library reference.
- `tests/` — unit tests. `tests/integration/` — container/screenshot harness.

## Config-entry model (hub + nested devices)

The integration is **rfxtrx-style**, not Battery-Notes-style:

- **One config entry per rtl_433 server** (the hub, `integration_type: "hub"`).
  Platforms are forwarded once on that entry
  (`async_forward_entry_setups(entry, PLATFORMS)`).
- The RF devices it decodes are **device-registry devices nested under the hub
  entry**, *not* separate config entries. They are recreated on startup from the
  per-hub `entry.data["devices"]` map (the single source of truth: model,
  observed mapped fields, optional per-device timeout override) and added at
  runtime via the new-device dispatcher signal — gated by the hub's discovery
  toggle (the Quality-Scale `dynamic-devices` rule).
- `async_remove_config_entry_device` (`__init__.py`) backs the per-device
  **Delete** affordance (the `stale-devices` rule): it returns `False` for the
  hub device (so the hub can't be removed out from under its entry) and `True`
  for nested RF devices, dropping the device from the devices map and **evicting
  its `device_key` from coordinator runtime state** (`coordinator.forget_device`)
  so it can re-appear if it transmits again with discovery on. There is no
  persistent ignore list.
- `async_migrate_entry` (`__init__.py`, config-entry `VERSION` 1 → 2) performs a
  **seamless in-place upgrade from 0.1.0**: it re-homes the legacy per-device
  config entries' registry devices/entities onto the hub entry (preserving
  unique_ids, entity_ids, and history), folds their state into the hub's devices
  map, and removes the obsolete per-device entries.
- Per-device configuration lives in the **hub OptionsFlow** (`config_flow.py`):
  a menu with a *Hub settings* step (discovery toggle + default timeout, written
  to `entry.options`) and a *Device settings* step (per-device timeout override,
  written into `entry.data["devices"]`).

## WebSocket frames & hub observability

Durable contracts for the coordinator's frame routing and the hub diagnostic
entities (`coordinator/base.py`, `sensor.py`, `binary_sensor.py`):

- **Frame classification** (`_classify_frame`). A streamed frame is treated as a
  decoded-device event **iff** it has a `model` key **or** an identity key
  (`id` / `channel` / `subtype`, kept in sync with `normalizer.IDENTITY_KEYS`).
  A `{"shutdown": ...}` frame drives the **connectivity** sensor (flips it off).
  **Every other frame is ignored** on the socket (`meta`, periodic state/stats,
  RPC `result`/`error`). This is why non-event frames no longer create a phantom
  `"unknown"` device or pollute `seen_fields` / the diagnostics
  `unmatched_field_keys`.
- **Hub observability data source.** SDR/meta and server stats are **not** read
  from the socket. They come from one-shot HTTP GETs to `scheme://host:port/cmd`
  at the **server root** (`https` when `secure`/`wss`, else `http`) —
  `_build_cmd_url` never derives from the configured WS `path`, so a proxy that
  hides `/cmd` degrades gracefully (the stream + connectivity sensor stay up; the
  meta/stats/gain/ppm sensors read `unknown`). Each getter swallows its own
  errors (`_fetch_cmd`) so it can never raise into the connect loop or watchdog.
  The request uses the `cmd` query param; scalar getters are read defensively
  through a `{"result": ...}` unwrap (`_unwrap_result`).
- **Exact getter set** (`_refresh_meta` + `_refresh_stats`): `get_meta` +
  `get_gain` + `get_ppm_error` + `get_stats`. **Gain and ppm are absent from
  `get_meta`** — they come from `get_gain` (string; empty ⇒ `auto`) and
  `get_ppm_error` (int) respectively. **Hop interval = `hop_times[0]`.**
  `_refresh_stats` re-polls `get_stats` on a fixed interval
  (`_STATS_REFRESH_INTERVAL`, 60 s) while connected; `_refresh_meta` runs once
  per (re)connect.
- **Verified Data Contracts** (do not invent fields — see
  [WEBSOCKET_API.md](WEBSOCKET_API.md)):
  - `get_meta` → `center_frequency`, `samp_rate`, `conversion_mode`,
    `frequencies[]`, `hop_times[]`, `duration`, `stats_interval`, `report_*`
    flags (**no `gain`, no `ppm`**).
  - `get_gain` → string (empty ⇒ auto); `get_ppm_error` → int.
  - `get_stats` → `{"enabled": <int>, "since": <str>, "frames": {"count":
    <ook>, "fsk": <fsk>, "events": <decoded>}, "stats": [<per-protocol>...]}`.
    Hub sensors map `frames.events` → decoded events
    (`TOTAL_INCREASING`, tolerates resets), `frames.count` → OOK frames,
    `frames.fsk` → FSK frames, `enabled` → enabled decoders, with `stats[]` /
    `since` surfaced as attributes.
- **Phantom-unknown cleanup.** `async_setup_entry` (`__init__.py`) calls
  `_cleanup_phantom_unknown_device`, which **idempotently** removes a legacy
  persisted `"unknown"` device from `entry.data["devices"]` and the matching
  registry device `(DOMAIN, f"{entry_id}:unknown")`. Safe on every setup; the
  classifier above prevents recreation.

## Device-library YAML format (summary)

Device support is data, not code: each rtl_433 JSON field name maps to one Home
Assistant entity descriptor. Files live in
`custom_components/rtl_433/device_library/`; the loader merges every `*.yaml`
(except `_skip_keys.yaml`) into one field-keyed table, then layers any user
override file on top.

A mapping entry, keyed by the exact rtl_433 field name:

```yaml
temperature_C:
  platform: sensor            # sensor | binary_sensor
  device_class: temperature   # HA device class, or null
  unit_of_measurement: "°C"   # unit, or null
  state_class: measurement    # measurement | total | total_increasing | null
  name: Temperature           # entity name
  value_transform: { round: 1 }  # numeric transform (sensors)
  object_suffix: T            # short, STABLE unique-id token
```

`binary_sensor` entries use `payload: { on: "<raw>", off: "<raw>" }` instead of
`value_transform`. `_skip_keys.yaml` lists fields that must never become
entities.

**Do not invent attributes here.** The full schema — every attribute, the
`value_transform` keys and their application order, binary payloads, the
skip-keys file, and the `<config>/rtl_433_mappings.yaml` user override
semantics — is defined in:

- **[docs/device-library.md](docs/device-library.md)** (authoritative).

## Add-a-mapping workflow

1. **Find the exact field name.** rtl_433 field names are case-sensitive and
   unit-suffixed (`temperature_C`, not `temperature`). Get them from the device
   diagnostics (next step) or the live rtl_433 stream.
2. **Edit the themed file** under
   `custom_components/rtl_433/device_library/` that matches the field's domain
   (e.g. `temperature.yaml`, `humidity_moisture.yaml`, `wind.yaml`), or
   `misc.yaml` if nothing fits. Add an entry keyed by the field name following
   the schema in `docs/device-library.md`. Copy a similar existing entry as a
   template. If the field is identity/noise, add it to `_skip_keys.yaml`
   instead.
3. **Run the unit tests** (see below). They cover library loading and entity
   creation, so a malformed entry fails fast.
4. **Read the diagnostics' unmatched keys.** The hub diagnostics export contains
   an `unmatched_field_keys` list — JSON keys that are neither skipped nor
   mapped. Download it from **Settings → Devices & Services → rtl_433 → ⋮ →
   Download diagnostics**. Every key there is a one-line YAML addition; the list
   shrinks as you add mappings. See the
   [diagnostics feedback loop](docs/device-library.md#diagnostics-feedback-loop).

For an installation-local change that should **not** be committed, use the
user-override file `<config>/rtl_433_mappings.yaml` instead of editing the
shipped library (see [User overrides](docs/device-library.md#user-overrides)).

## Running the unit tests

Dependencies and tools are managed with [uv](https://docs.astral.sh/uv/), the
same as CI. Install uv with `curl -LsSf https://astral.sh/uv/install.sh | sh`,
then:

```bash
uv venv
uv pip install -r requirements_test.txt
uv run pytest tests/
```

`requirements_test.txt` pins `pytest-homeassistant-custom-component`, which pulls
in the matching Home Assistant version and the full pytest stack (asyncio, cov,
timeout, xdist, freezegun). To match CI, include coverage:

```bash
uv run pytest --cov=custom_components/rtl_433 tests/
```

CI runs on Python 3.13 (the minimum Home Assistant 2026.x supports).

## Running the container / screenshot harness

The end-to-end harness drives the integration against **real RF captures** (no
SDR hardware) and captures the documentation screenshots with Playwright. It is
fully documented, including prerequisites, the orchestrator steps
(`./run-harness.sh full`), and an important honest caveat:

- Because `rtl_433 -r <file> -F http` runs in file **test mode** and exits
  before its mongoose HTTP/WebSocket loop starts, the native `-F http` server
  never answers requests from a file/FIFO. The harness therefore uses a tiny
  Node **ws-bridge** that tails rtl_433's `-F json` output and re-broadcasts each
  event on `ws://0.0.0.0:8433/ws` — the same frame shape the coordinator expects.
  The bridge is a transport stand-in **for the harness only**; it is not part of
  the shipped integration.

Full runbook:

- **[tests/integration/README.md](tests/integration/README.md)**

## Guardrails for automated changes

- Prefer **YAML library edits** over Python: most device support is data.
- Keep `object_suffix` values **stable** — changing one orphans existing
  entities.
- Keep `const.py` the single source of truth for config keys and defaults
  (`DEFAULT_PORT=8433`, `DEFAULT_PATH="/ws"`, `DEFAULT_AVAILABILITY_TIMEOUT=600`)
  and for the dispatcher signals (`SIGNAL_NEW_DEVICE`, `SIGNAL_HUB_UPDATE` — the
  latter fans connectivity/meta/stats changes out to the hub entities).
- Always run `pytest tests/` before proposing a change, and follow the
  conventional-commit and lint rules in [CONTRIBUTING.md](CONTRIBUTING.md).
