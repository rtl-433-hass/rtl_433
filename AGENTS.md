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
    `binary_sensor.py`, `event.py`, `translations/en.json`.
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
- `Rtl433ConfigFlow` also implements `async_step_reconfigure` (`config_flow.py`)
  to edit a hub's connection params (host/port/path/secure) in place — "same
  server, new address". The `host:port` `unique_id` is recomputed (aborting only
  on collision with a *different* entry), and the nested-device map is preserved
  because the new params are merged via `data_updates=` (which leaves
  `entry.data["devices"]` and `manage_settings` untouched).

## Per-device "Last seen" sensor (synthetic, non-field-driven)

The per-device **Last seen** sensor (`Rtl433LastSeenSensor`, `sensor.py`) is
**synthetic** — it is *not* driven by a device-library field. Two invariants
must survive any refactor of `async_setup_hub_platform` (`entity.py`) and of the
base `async_added_to_hass` baseline:

- **Created unconditionally, once per device, on the `sensor` platform only.**
  It is built from a small synthetic `FieldDescriptor` (`LAST_SEEN_DESCRIPTOR`:
  sentinel `field_key="__last_seen__"` that no rtl_433 event can carry,
  `object_suffix="last_seen"`, `device_class=timestamp`, diagnostic,
  `enabled_by_default=True`) and added via the **`per_device_factory` hook** of
  `async_setup_hub_platform` (`async_setup_entry` passes
  `per_device_factory=Rtl433LastSeenSensor`). The `binary_sensor` platform
  passes **no** factory, so it creates none. The factory runs exactly once per
  `device_key` across both the initial devices-map build and the new-device
  handler (`_build_extra` / `extra_created`), and is passed as a callable so
  `entity.py` never imports the platform modules.
- **Holds its OWN `native_value`, never the base startup baseline.** The
  sentinel `field_key` is never in `event.fields`, so `_apply_value` is a no-op
  and the field-driven path never fires. The value is sourced from
  `coordinator.last_seen[device_key]` **only when `coordinator.devices` has an
  entry for that device** (a real event this session) — the presence of a
  devices-map entry is what distinguishes a true timestamp from the base's
  `async_added_to_hass` "baseline last_seen = now". Otherwise it restores the
  prior value as a **tz-aware datetime** (`dt_util.parse_datetime`), and it
  re-reads `coordinator.last_seen` on every dispatch (overridden
  `_handle_dispatch`). If it ever adopted the baseline it would read "now" after
  every restart.
- **Always-available override.** It overrides `available` to be true whenever it
  has a value, so it stays readable after the device falls silent (it ignores
  the per-device availability timeout) and can drive "last_seen older than X"
  staleness automations.

## Event platform (`event.py`, value-as-type, auto-populated)

`Rtl433Event` (`event.py`) is the third platform (`Platform.EVENT` in
`PLATFORMS`). Unlike the Last seen sensor it is **field-driven** — built via
`async_setup_hub_platform` for descriptors whose `platform == "event"`, with
**no `per_device_factory`** — using the **unchanged shared 5-arg constructor**.
Invariants that must survive refactors of `async_setup_hub_platform`, the
coordinator watchdog, and the devices map:

- **Identity-based watchdog dedupe.** It overrides `_handle_dispatch` to dedupe
  the watchdog's re-dispatch by **object identity** (`event is
  self._last_fired_event`), **not value-equality**. The watchdog re-sends the
  *same cached* `NormalizedEvent` object when a device goes stale; a genuine
  live repeat (even of the same value) is a *distinct* frozen `NormalizedEvent`
  from `normalize()` that **must** fire (a doorbell pressed twice fires twice).
  If this ever became `==`, genuine repeats would be silently dropped.
- **Auto-populated, persisted `event_types`.** Types are not declared in YAML;
  each newly seen `str(value)` is appended to `_attr_event_types` **before**
  firing (HA validates the fired type against the current list) and persisted
  per device-field under
  `entry.data[CONF_DEVICES][key][DEVICE_EVENT_TYPES][field]` via
  `async_upsert_event_types` (idempotent union write, stored sorted). The entity
  reads the persisted list in `__init__` from `coordinator.entry.data` (a
  **copy**, so in-place growth never mutates the persisted dict).
- **Type-only fired event.** `_trigger_event(event_type)` is called with **no
  extra attributes** (the type is the whole payload); there is **no `payload`
  and no `value_transform`** — the raw value is stringified directly.
- **Always available; no construction-time replay.** `available` is always
  `True` (events are momentary; a timeout would hide the entity almost always).
  `_async_restore_state` is a **no-op** — HA's
  `EventEntity.async_internal_added_to_hass` restores the last displayed event.
  The entity does **not** seed/replay `coordinator.devices[key]` on construction
  (that would fire a stale event before the entity is added to hass).

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
  `_async_refresh_tick` re-polls **both** `_refresh_meta` and `_refresh_stats`
  on a fixed interval (`_REFRESH_INTERVAL`, 60 s) while connected, on top of the
  once-per-(re)connect refresh and the post-write read-back. Re-polling meta on
  the interval is what lets the "actual" SDR sensors converge to the server's
  current values within the window — a single post-write read-back can race the
  SDR retune, so without the tick the actual sensor could stay stale until the
  next reconnect.
- **Verified Data Contracts** (do not invent fields — see
  [WEBSOCKET_API.md](WEBSOCKET_API.md)):
  - `get_meta` → `center_frequency`, `samp_rate`, `conversion_mode`,
    `frequencies[]`, `hop_times[]`, `duration`, `stats_interval`, `report_*`
    flags (**no `gain`, no `ppm`**).
  - `get_gain` → string (empty ⇒ auto); `get_ppm_error` → int.
  - `get_stats` → `{"enabled": <int>, "since": <str>, "frames": {"count":
    <ook>, "fsk": <fsk>, "events": <decoded>}, "stats": [<per-protocol>...]}`.
    Hub sensors map `frames.events` → decoded events, `frames.count` → OOK
    frames, and `frames.fsk` → FSK frames, all **`TOTAL_INCREASING`** (cumulative
    since-start counters that tolerate the server-restart reset, so HA records
    long-term statistics); `enabled` → enabled decoders is a gauge →
    **`MEASUREMENT`**; `stats[]` / `since` are surfaced as attributes.
- **Phantom-unknown cleanup.** `async_setup_entry` (`__init__.py`) calls
  `_cleanup_phantom_unknown_device`, which **idempotently** removes a legacy
  persisted `"unknown"` device from `entry.data["devices"]` and the matching
  registry device `(DOMAIN, f"{entry_id}:unknown")`. Safe on every setup; the
  classifier above prevents recreation.

## Hub SDR controls (HA-managed settings)

Durable contracts for the optional HA-managed SDR controls (`sdr_settings.py`,
`coordinator/base.py`, `__init__.py`, the `number`/`select`/`switch` platforms).
End-user docs live in
[README](README.md#managing-sdr-settings-from-home-assistant) — keep this
contributor-facing.

- **Settings-registry contract** (`sdr_settings.py`, the single source of truth
  for the control set; import-disjoint like `mapping.py`). `SDR_SETTINGS` is the
  authoritative list; each `SdrSetting` is pure data plus tiny callables so the
  coordinator and the platforms can iterate it **without importing each other**.
  Six fields (gain is a **pair** sharing one command — seven registry entries):
  - `center_frequency` → number, command `center_frequency`, `val` = Hz; read
    `meta["center_frequency"]`.
  - `sample_rate` → number, command `sample_rate`, `val` = Hz; read
    `meta["samp_rate"]` (the meta key differs from the registry key).
  - `ppm_error` → number, command `ppm_error`, `val` = int; read
    `meta["ppm_error"]`.
  - `gain` → number (dB), command `gain`, `arg` = dB string; read parsed from
    the gain string.
  - `gain_auto` → switch, command `gain`, `arg`; read `gain == ""`. **The gain
    pair shares the one `gain` command**: the coordinator stores two desired
    keys (`gain` dB float + `gain_auto` bool) but composes a single `arg` via
    `gain_command_arg()` (empty ⇒ auto, else `f"{db:g}"`) and **emits `gain`
    exactly once** per write/replay.
  - `conversion_mode` → select (`native`/`si`/`customary`), command `convert`,
    `val` = int. The option **index is the `val`** — tuple order is load-bearing
    (`native`→0, `si`→1, `customary`→2; `conversion_label_to_val` /
    `conversion_val_to_label`).
  - `hop_interval` → number, command `hop_interval`, `val` = seconds; read
    `hop_times[0]`.
  Commands and arg/val kinds follow [WEBSOCKET_API.md](WEBSOCKET_API.md)
  exactly — **do not invent fields**. Number bounds are deliberately wide
  (`NumberMode.BOX`); the server clamps/rejects, HA is not the authority on
  ranges. Each entry carries a **`capability` gate** (`Callable[[meta], bool]`,
  today always `_always`) so future per-server capability advertisement can
  hide unsupported fields without touching consumers.
  - **Runtime `available` gate** (`Callable[[meta], bool]`, default `_always`):
    distinct from `capability` (evaluated once at setup to decide whether the
    entity is *created*), `available` is read by `Rtl433HubControl.available` on
    **every `signal_hub_update`** to decide whether the *created* control reports
    available for the current `meta`. Two fields override it, keyed on
    `len(meta["frequencies"])` (unknown/pre-connect ⇒ available): `hop_interval`
    is available **only when hopping** (`> 1` frequency — a single frequency has
    nothing to hop between), and `center_frequency` is available **only when not
    hopping** (`≤ 1`), mirroring the adoption hop-mode guard so a hopping receiver
    is never pinned. The API has no command to set the frequency *list*, so these
    modes are mutually exclusive and set in the rtl_433 config.
- **Adoption + full enforcement on reconnect** (`coordinator/base.py`,
  `_connect_loop`). When `manage_settings` is on: on first connect (when
  `_desired` is empty) `_adopt_from_server()` seeds the desired state from
  `self.meta`; then `_enforce_all()` **replays every managed field on every
  (re)connect**, so values survive an rtl_433 restart. Both run after
  `_refresh_meta`, are wrapped so a failure can never kill the connect loop, and
  every `/cmd` is best-effort.
  - **Hop-mode guard:** adoption **skips `center_frequency` when
    `len(frequencies) > 1`** so HA never pins a hopping receiver to one freq.
  - **`/cmd`-down guard:** if `self.meta` is empty (getters failed / proxy hides
    `/cmd`) adoption seeds **nothing** and leaves the Store empty — never raises.
  - **Serialization lock:** all issuance (user write, reconnect replay,
    read-back) goes through `_send_cmd` under `self._cmd_lock`, so a user write
    and a reconnect replay can never interleave requests to the same server.
    `arg` is sent **verbatim including the empty string** (the gain "auto"
    sentinel), so the gain command always passes `arg` and never omits it.
- **`Store` persistence (keyed by entry id, NOT `entry.options`).** Desired
  state persists in a `homeassistant.helpers.storage.Store` keyed by
  `sdr_store_key(entry_id)` (`const.py`, `SDR_STORE_VERSION`), as
  `{"values": {...}, "managed": [...]}`. It is **deliberately not** stored in
  `entry.options`: an options write churns the config entry (reloads), and a
  desired-state value change must not. The public entity API is
  `get_desired(field)`, `is_managed(field)`, `set_sdr(field, value)` (persist
  first, then enforce if connected — a failed send **keeps** the desired value),
  and `clear_desired_state()`.
- **Management-toggle behavior** (`CONF_MANAGE_SETTINGS = "manage_settings"`,
  `const.py`; default `DEFAULT_MANAGE_SETTINGS = True`). Offered on the initial
  connection form **and** in hub options. ON ⇒ controls created, adopt + enforce
  as above, and the five folded SDR/meta diagnostic **sensors** are replaced by
  their controls (center-frequency keeps its actual sensor). OFF ⇒ no controls,
  no commands; `async_load_desired_state` **wipes the Store on load**
  (`async_remove`) so a later re-enable re-adopts from scratch, and all six Plan
  3 read-only sensors remain.
- **Reload-only-on-toggle-change listener** (`_async_update_listener`,
  `__init__.py`). The listener compares the new effective `manage_settings`
  against the running `coordinator.manage_settings` and **reloads the entry only
  when the toggle changed** (the entity set + adopt/enforce behaviour flips);
  discovery-toggle and timeout changes are applied live with no reload.
- **HA is the authority; no re-adopt action — by design.** Once managed, HA
  re-applies its stored values on reconnect and **overrides later direct edits**
  to the rtl_433 config. There is deliberately **no re-adopt button/service**.
  The **only** re-sync path is the toggle dance: **off → restart rtl_433 → on**
  (the now-empty Store re-adopts the live config value on the next connect).
  Document any change to this in the README in lockstep.
- **Out of scope but anticipated** (the `capability` gate exists for these):
  decoder enable/disable, device selection, and multi-frequency **hop lists** —
  some unimplemented upstream. Multi-stage gain strings are likewise out of
  scope for the single gain control. If implementing these, the cleaner path is
  to have **upstream advertise capabilities** that the gate can consult, rather
  than probing.

## Device-library YAML format (summary)

Device support is data, not code: each rtl_433 JSON field name maps to one Home
Assistant entity descriptor. Files live in
`custom_components/rtl_433/device_library/`; the loader merges every `*.yaml`
(except `_skip_keys.yaml`) into one field-keyed table, then layers any user
override file on top.

A mapping entry, keyed by the exact rtl_433 field name:

```yaml
temperature_C:
  platform: sensor            # sensor | binary_sensor | event
  device_class: temperature   # HA device class, or null
  unit_of_measurement: "°C"   # unit, or null
  state_class: measurement    # measurement | total | total_increasing | null
  name: Temperature           # entity name
  value_transform: { round: 1 }  # numeric transform (sensors)
  object_suffix: T            # short, STABLE unique-id token
```

`binary_sensor` entries use `payload: { on: "<raw>", off: "<raw>" }` instead of
`value_transform`. `event` entries (in `events.yaml`) use neither — the value is
stringified to the fired `event_type` and `device_class` is an
`EventDeviceClass`; see the [Event platform](#event-platform-eventpy-value-as-type-auto-populated)
section above. `_skip_keys.yaml` lists fields that must never become entities.

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
- Always open pull requests with a **conventional-commit-style title** that
  summarizes the branch's changes (e.g. `feat(rtl_433): add hub observability
  sensors`), matching the commit convention above.
