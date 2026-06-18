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
  - `__init__.py` keeps only the steady-state config-entry lifecycle
    (`async_setup_entry` / `async_unload_entry` / `_async_update_listener` /
    `async_remove_config_entry_device`). Three sibling modules hold the rest:
    `migration.py` (config-entry v1→v2 migration + one-time legacy cleanups,
    re-exported `async_migrate_entry`), `library.py` (mapping-library load/merge),
    and `hub_settings.py` (hub-entry setting resolvers: `_hub_*`,
    `_calibration_map`).
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
  toggle (the Quality-Scale `dynamic-devices` rule) **and by the post-connection
  registration gate** (see the coordinator's replay/registration notes: a
  previously-unknown device auto-registers only once a frame timestamped at/after
  the connection is seen, so a server's pre-connection backlog never floods the
  device list). `new_device_callback` (`__init__.py`) also raises a
  `persistent_notification` with a stable per-device `notification_id`, gated on
  `entry.data[CONF_DEVICES]` (not the coordinator's per-session discovery state)
  so restarts/reloads don't re-notify; in-app only.
- `async_remove_config_entry_device` (`__init__.py`) backs the per-device
  **Delete** affordance (the `stale-devices` rule): it returns `False` for the
  hub device (so the hub can't be removed out from under its entry) and `True`
  for nested RF devices, dropping the device from the devices map and **evicting
  its `device_key` from coordinator runtime state** (`coordinator.forget_device`)
  so it can re-appear if it transmits again with discovery on. There is no
  persistent ignore list.
- `async_migrate_entry` (`migration.py`, config-entry `VERSION` 1 → 2) performs a
  **seamless in-place upgrade from 0.1.0**: it re-homes the legacy per-device
  config entries' registry devices/entities onto the hub entry (preserving
  unique_ids, entity_ids, and history), folds their state into the hub's devices
  map, and removes the obsolete per-device entries.
- Per-device configuration lives in the **hub OptionsFlow** (`config_flow.py`):
  a menu with a *Hub settings* step (discovery toggle + default timeout, written
  to `entry.options`) and a *Device settings* step (per-device timeout override,
  written into `entry.data["devices"]`).
- **Utility-meter calibration** (`calibration.py`, options `Device settings` →
  `calibration` step) writes a `DEVICE_CALIBRATION` sub-record (`{commodity,
  unit, scale}`) into `entry.data[CONF_DEVICES][device_key]` next to
  `timeout_override`. It overlays the consumption descriptor (the
  `CONSUMPTION_FIELD_KEYS` only) at entity build — precedence tier #1 above the
  `models:`/global library lookup. Applied via **reload**: the device-step write
  fires `_async_update_listener` (`__init__.py`), which `async_reload`s the hub
  **only when the normalized calibration map differs** from the coordinator's
  setup snapshot (`coordinator.calibration_snapshot` / `_calibration_map`), so
  routine devices-map upserts never reload — mirroring the `manage_settings`
  reload pattern. `device_class`/native unit/`state_class` are construction-time,
  hence the rebuild; recalibration orphans prior long-term statistics (expected).
  User-facing detail is in `docs/calibration.md` and `docs/device-library.md` —
  keep this contributor-facing.
- `Rtl433ConfigFlow` also implements `async_step_reconfigure` (`config_flow.py`)
  to edit a hub's connection params (host/port/path/secure) in place — "same
  server, new address". The nested-device map is preserved because the new params
  are merged via `data_updates=` (which leaves `entry.data["devices"]` and
  `manage_settings` untouched). The `unique_id` handling is identity-aware: a
  legacy/manual entry (`hub:…` or none) **recomputes** `hub:{host}:{port}`
  (aborting only on collision with a *different* entry); a discovered/adopted
  entry **preserves** its stable radio `unique_id` by default, but the form also
  offers an optional `radio_id` field to **rebind** it to a *new* stable radio id
  — the "replace a dead dongle" path. Rebinds funnel through the module-level
  `async_rebind_hub(hass, entry, new_unique_id, conn_updates, title=…)` helper,
  which preserves `entry_id` (so every nested device/entity/history survives),
  aborts `already_configured` when the target id is owned by a *populated* entry,
  and adopts-and-deletes an *empty orphan* entry that already holds the target id
  (the duplicate Supervisor discovery may auto-create on a new `host:port`). The
  same helper backs the discovery `hassio_replace` step and the rebind form
  embedded in the `server_unreachable` repair fix flow (`repairs.py`).
- **Config-flow sources and dual identity scheme.** `Rtl433ConfigFlow` supports
  `user` (manual add), `reconfigure`, and `hassio` (Supervisor add-on discovery),
  plus the options flow above. Two `unique_id` schemes coexist:
  - **Manual hubs** key on `unique_id = hub:{host}:{port}` (`_hub_unique_id`).
  - **Add-on-discovered radios** key on the add-on's advertised stable per-radio
    `unique_id` (`serial:…` / `usbpath:…` / `template:…`), carried in the
    `hassio` discovery message.
  `async_step_hassio` (`config_flow.py`) reconciles the two: a discovery message
  that matches an existing entry by `host:port` (`_find_entry_by_host_port`)
  **adopts/re-keys** that entry onto the stable radio id (migration; aborts
  `already_configured`), so a manually-added hub and its later discovery never
  duplicate and the entry's history is preserved. A genuinely new radio
  (unknown stable id, no `host:port` match) is routed through
  `async_step_hassio_replace` **when at least one hub already exists** — a guided
  step that offers to rebind one of those hubs onto the new radio (the likely
  "replacement landed on a new `host:port`" case) or to add it as new; with no
  existing hubs it goes straight to `async_step_hassio_confirm` (a confirmation
  that revalidates
  connectivity before creating the entry and offers the same setup choices as the
  manual flow — `manage_settings`, `discovery_enabled`, and an optional
  `initial_frequency` in MHz); `async_step_user` likewise aborts
  `already_configured` if a `host:port` is already owned by a discovered entry.
  Both add flows persist `discovery_enabled` and, when `manage_settings` is on and
  a frequency was entered, `initial_frequency` (MHz) into `entry.data`; the latter
  is applied to the managed desired state **exactly once** at first connect —
  authoritatively overriding the adopted/persisted center frequency (gated on a
  persisted `initial_freq_seeded` flag, not on the desired store being empty), and
  never re-applied after the user later changes the frequency via the control.

## Per-device "Last seen" sensor (synthetic, non-field-driven)

The per-device **Last seen** sensor (`Rtl433LastSeenSensor`, `sensor.py`) is
**synthetic** — it is *not* driven by a device-library field. Two invariants
must survive any refactor of `async_setup_hub_platform` (`entity.py`) and of the
base `async_added_to_hass` baseline:

- **Created unconditionally, once per device, on the `sensor` platform only.**
  It is built from a small synthetic `FieldDescriptor` (`LAST_SEEN_DESCRIPTOR`:
  sentinel `field_key="__last_seen__"` that no rtl_433 event can carry,
  `object_suffix="last_seen"`, `device_class=timestamp`, diagnostic,
  descriptor `enabled_by_default=False`) and added via the **`per_device_factory`
  hook** of `async_setup_hub_platform` (`async_setup_entry` passes
  `per_device_factory=Rtl433LastSeenSensor`). It is **disabled by default for
  periodic devices** but the sensor flips `_attr_entity_registry_enabled_default`
  to `True` for **event-driven devices** (`coordinator.is_event_driven_device`),
  since those never expire and the timestamp is their only freshness signal; a
  one-time minor-6 migration re-enables already-created instances the integration
  disabled. The `binary_sensor` platform
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
- **Optional `event_map` (doorbell `ring`).** A descriptor's `event_map` maps a
  stringified raw value to a named type (unmapped values pass through as
  `str(value)`); doorbell `secret_knock` maps `0 → ring`, `1 → secret_knock`.
  Mapped types are declared up front in `event_types`, and a `device_class:
  doorbell` entity must advertise `ring` (`DoorbellEventType.RING`, HA standard;
  else removed in HA 2027.4) — the constructor force-inserts it if absent.
- **Type-only fired event.** `_trigger_event(event_type)` is called with **no
  extra attributes** (the type is the whole payload); there is **no `payload`
  and no `value_transform`** — the raw value is stringified directly.
- **Always available; no construction-time replay.** `available` is always
  `True` (events are momentary; a timeout would hide the entity almost always).
  `_async_restore_state` is a **no-op** — HA's
  `EventEntity.async_internal_added_to_hass` restores the last displayed event.
  The entity does **not** seed/replay `coordinator.devices[key]` on construction
  (that would fire a stale event before the entity is added to hass).

## Motion / occupancy binary_sensor (`clear_delay`, synthesized off)

Detect-only PIR/occupancy hardware (`motion`) emits an `on` and **never an
off**, so `motion` is a `binary_sensor` (device class `occupancy`, `payload: {
on: "1" }`, in `device_library/misc.yaml`) that **synthesizes** the off via a
timer. Contracts that must survive refactors:

- **`clear_delay` descriptor attribute** (`FieldDescriptor.clear_delay: int |
  None`, `mapping.py`). `binary_sensor`-only seconds value; a non-int is logged
  and dropped at load. Its presence is what marks a descriptor as detect-only.
- **`Rtl433BinarySensor` timer** (`binary_sensor.py`). On each `on`, `_schedule_clear`
  **cancels and reschedules** a single `async_call_later` one-shot, so the off
  window **restarts on every retrigger**; `_clear` writes `is_on = False`.
  `async_will_remove_from_hass` **cancels** the pending timer (never write after
  removal). `_async_restore_state` **does not restore a stale `on`** for a
  `clear_delay` descriptor (no live timer would clear it) — it returns early, so
  the sensor comes back off/unknown until the next detection. Scheduling is
  guarded until `hass` is set; the initial arm happens in `async_added_to_hass`.
- **Per-device override.** `DEVICE_MOTION_CLEAR_DELAY` (`"motion_clear_delay"`,
  `const.py`) holds an optional per-device int in the device record;
  `DEFAULT_MOTION_CLEAR_DELAY = 90`. The options-flow device step shows a *Motion
  clear delay (seconds)* field **only for motion-bearing devices** (descriptor
  with a truthy `clear_delay`) and persists it. At runtime
  `effective_clear_delay_resolver(device_key)` (set on the coordinator in
  `__init__.py`) returns the per-device value, else the 90 s default;
  `Rtl433BinarySensor._effective_clear_delay` consumes it (falling back to the
  descriptor default if the resolver errors/returns `None`).
- **event → binary migration** (`_migrate_motion_event_to_binary_sensor`,
  `migration.py`). Earlier versions exposed motion as `event.*_motion`; the
  entity_id is now `binary_sensor.*_motion` (**a BC break**). At setup the sweep
  removes the orphaned `event`-domain registry entries whose unique-id tail is
  `:motion`, drops the `motion` slot from any persisted `DEVICE_EVENT_TYPES` (so
  the event platform never recreates it), and — only if it removed at least one —
  raises a single integration-wide repairs issue `motion_moved_to_binary_sensor`
  (`is_fixable=False`, WARNING; stable id, so never duplicated across hubs or
  restarts). Idempotent and safe on every startup.

## Device triggers (`device_trigger.py`)

`device_trigger.py` exposes the `event` entities (button / doorbell) as
UI-pickable **device triggers**. Contracts that must survive refactors:

- **Discovered by file presence, not `PLATFORMS`.** HA's device-automation
  machinery loads it purely because the module exists at
  `custom_components/rtl_433/device_trigger.py`; it is **not** an entity platform
  and must **not** be added to `const.py` `PLATFORMS`.
- **Triggers only.** No conditions, no actions — `async_get_triggers` /
  `async_attach_trigger` only.
- **Per-event-entity granularity with an optional `event_type` subtype.** Each
  event entity yields one base trigger ("<entity> triggered") plus one subtyped
  trigger per known `event_type` ("<entity> triggered: <code>"). The subtype list
  is sourced from the **persisted** `entry.data[CONF_DEVICES][key][DEVICE_EVENT_TYPES][field]`
  (restart-surviving), falling back to the loaded entity's live `event_types`
  capability attribute when nothing is persisted yet.
- **Unified firing mechanism** (`_async_attach_event_trigger`). Both the base and
  the subtyped trigger use **one** custom `async_track_state_change_event`
  listener; `subtype` (`None` for the base) decides whether the `event_type`
  filter applies. `Rtl433Event` writes a fresh timestamp state on every genuine
  transmission, so a state change **is** a transmission and the listener fires
  with **no** `old == new` dedupe — two consecutive same-value presses each fire.
  (This is why neither path can reuse the core `state` trigger's `attribute`/`to`
  filter, which early-returns on `old_value == new_value`, `triggers/state.py`.)
  The listener replicates the core trigger's `device`-platform payload + context
  by hand.
- **No re-fire on the restore at startup.** The listener **ignores a `None`
  `old_state`**. Across a restart HA's `EventEntity` restores its last
  `event_type` + timestamp (for display), surfacing as a `state_changed` with
  `old_state is None` carrying the old `event_type`; a raw listener would
  re-deliver that stale event (e.g. a days-old doorbell `ring`) on **every** HA
  restart. A momentary event never legitimately fires on the entity's first
  appearance in the state machine, so the `None`-`old_state` restore/initial-add
  is suppressed. (The base trigger previously delegated to the core `state`
  trigger, which fires on a match_all `None`→state transition — the same
  re-fire-on-restart bug, now closed for both paths.)

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
- **Replay/stale suppression** (`_process_event`, `_parse_event_time`). On every
  (re)connect the server replays up to its last 100 events, so the coordinator
  reads the raw `time` **before `normalize()`** (which drops it) and classifies
  each frame via **three signals**: a **high-water mark** of the max event `time`
  ever parsed (a frame at or below it is an **already-seen replay**); the event
  **age vs `REPLAY_STALE_THRESHOLD`** (30 s — an unseen-but-old frame is a
  **stale gap event** that occurred while HA was disconnected); and a
  **pre-connection backlog gate** (`event_time < _connection_time -
  DISCOVERY_BACKLOG_GRACE` — the same gate the device-registration step below
  uses). The backlog gate is what closes the **HA-restart re-delivery** case: on
  a fresh process the high-water mark is unset, so a doorbell pressed *seconds*
  before the restart is recent enough to pass the age test, yet it predates the
  reconnect and so must not re-fire. Any of the three outcomes
  **seeds sensor values** but must **NOT** fire `event` entities or refresh
  `last_seen` / `available`, so a genuinely-offline device is not resurrected by
  the replay. A suppressed `event` transmission logs **once at
  INFO** (`Rtl433Event._handle_dispatch`). The classification rides on the
  dispatch carrier: `NormalizedEvent.is_replay` / `event_time` (stamped via
  `dataclasses.replace` after `normalize`; live is the default), so dispatch
  needs no extra signature. The **watchdog re-dispatch passes `is_replay=False`**
  so its unavailable re-paint of a cached (maybe-replay) event is never
  suppressed. **Assumes the server and HA clocks are roughly NTP-synced**
  (local-naive `time` is read in HA's time zone). **Limitation:** with server
  timestamps disabled (`report_meta notime`) there is no usable `time`, so every
  frame is treated as live and **events fire on replay**.
- **Post-connection device-registration gate** (`_process_event`,
  `_connection_time`, `DISCOVERY_BACKLOG_GRACE`). Distinct from the replay/event
  classification above: it governs **whether a previously-unknown device
  auto-registers**, not whether events fire. The coordinator stamps
  `_connection_time` (UTC) on every successful connect and clears it on drop. A
  previously-unknown device fires `new_device_callback` only when the triggering
  frame is timestamped at/after `_connection_time - DISCOVERY_BACKLOG_GRACE`
  (5 s skew grace), so the server's pre-connection backlog (replayed on connect)
  seeds runtime state **without** registering devices. Registration keys off a
  separate per-process `_discovered` set (not `devices` membership), so a device
  first seen in the backlog still registers on its first genuine post-connection
  frame; `forget_device` re-arms it. A frame with **no parseable `time`** is
  treated as post-connection (registers), and once disconnected
  (`_connection_time is None`) the gate is open. **Assumes the server and HA
  clocks are roughly NTP-synced.**
- **Hub observability data source.** SDR/meta and server stats are **not** read
  from the socket. They come from one-shot HTTP GETs to `scheme://host:port/cmd`
  at the **server root** (`https` when `secure`/`wss`, else `http`) —
  `_build_cmd_url` never derives from the configured WS `path`, so a proxy that
  hides `/cmd` degrades gracefully (the stream + connectivity sensor stay up; the
  meta/stats/gain/ppm sensors read `unknown`). Each getter swallows its own
  errors (`_fetch_cmd`) so it can never raise into the connect loop or watchdog.
  The request uses the `cmd` query param; scalar getters are read defensively
  through a `{"result": ...}` unwrap (`_unwrap_result`).
- **Exact getter set** (`_refresh_meta` + `_refresh_stats` + `_refresh_dev_info`):
  `get_meta` + `get_gain` + `get_ppm_error` + `get_stats` + `get_dev_info` +
  `get_dev_query`. **Gain and ppm are absent from
  `get_meta`** — they come from `get_gain` (string; empty ⇒ `auto`) and
  `get_ppm_error` (int) respectively. **Hop interval = `hop_times[0]`.**
  `get_dev_info`/`get_dev_query` are the SDR's identity and are fetched **only on
  (re)connect** (`_refresh_dev_info`, not on the interval tick): they are static
  per dongle. When the identity changes, the coordinator's `hub_info_callback`
  fires so `__init__.py` refreshes the **hub** device-registry entry's
  `manufacturer`/`model`/`serial_number` (replacing the generic `rtl_433` /
  `rtl_433 server` placeholders). Empty when no SDR is open (e.g. `-D manual`),
  in which case the placeholders are kept.
  `_async_refresh_tick` re-polls **both** `_refresh_meta` and `_refresh_stats`
  on a fixed interval (`_REFRESH_INTERVAL`, 60 s) while connected, on top of the
  once-per-(re)connect refresh and the post-write read-back. Re-polling meta on
  the interval is what lets the "actual" SDR sensors converge to the server's
  current values within the window — a single post-write read-back can race the
  SDR retune, so without the tick the actual sensor could stay stale until the
  next reconnect.
- **Verified Data Contracts** (do not invent fields — see
  [docs/websocket-api.md](docs/websocket-api.md)):
  - `get_meta` → `center_frequency`, `samp_rate`, `conversion_mode`,
    `frequencies[]`, `hop_times[]`, `duration`, `stats_interval`, `report_*`
    flags (**no `gain`, no `ppm`**).
  - `get_gain` → string (empty ⇒ auto); `get_ppm_error` → int.
  - `get_dev_info` → librtlsdr USB label JSON
    `{"vendor": <str>, "product": <str>, "serial": <str>}` (mapped to the hub
    device's `manufacturer`/`model`/`serial_number`); `get_dev_query` → the `-d`
    selector string rtl_433 opened. Both empty/unset when no SDR device is open.
  - `get_stats` → `{"enabled": <int>, "since": <str>, "frames": {"count":
    <ook>, "fsk": <fsk>, "events": <decoded>}, "stats": [<per-protocol>...]}`.
    Hub sensors map `frames.events` → decoded events, `frames.count` → OOK
    frames, and `frames.fsk` → FSK frames, all **`TOTAL_INCREASING`** (cumulative
    since-start counters that tolerate the server-restart reset, so HA records
    long-term statistics); `enabled` → enabled decoders is a gauge →
    **`MEASUREMENT`**; `stats[]` / `since` are surfaced as attributes.
- **Phantom-unknown cleanup.** `async_setup_entry` (`__init__.py`) calls
  `_cleanup_phantom_unknown_device` (`migration.py`), which **idempotently** removes a legacy
  persisted `"unknown"` device from `entry.data["devices"]` and the matching
  registry device `(DOMAIN, f"{entry_id}:unknown")`. Safe on every setup; the
  classifier above prevents recreation.

## Hub SDR controls (HA-managed settings)

Durable contracts for the optional HA-managed SDR controls (`sdr_settings.py`,
`coordinator/base.py`, `__init__.py`, the `number`/`select`/`switch` platforms).
End-user docs live in
[docs/hub-entities.md](docs/hub-entities.md#managing-sdr-settings-from-home-assistant) —
keep this contributor-facing.

- **Settings-registry contract** (`sdr_settings.py`, the single source of truth
  for the control set; import-disjoint like `mapping.py`). `SDR_SETTINGS` is the
  authoritative list; each `SdrSetting` is pure data plus tiny callables so the
  coordinator and the platforms can iterate it **without importing each other**.
  Six fields (gain is a **pair** sharing one command — seven registry entries):
  - `center_frequency` → number, command `center_frequency`, `val` = Hz on the
    wire, but **presented in MHz**: `read` converts `meta["center_frequency"]`
    Hz→MHz and `to_command` converts the desired MHz value back to integer Hz, so
    the desired-state value, the Number control, and the diagnostic sensor are all
    MHz while `meta` stays Hz. The desired-state Store is versioned
    (`SDR_STORE_VERSION = 2`); `_SdrStore._async_migrate_func` converts a v1
    (Hz) persisted `center_frequency` to MHz on load.
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
  Commands and arg/val kinds follow [docs/websocket-api.md](docs/websocket-api.md)
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
  `_connect_loop` → `_seed_desired_on_first_connect`). When `manage_settings` is
  on: on first connect (when `_desired` is empty) `_adopt_from_server()` seeds the
  desired state from `self.meta`; then `_enforce_all()` **replays every managed
  field on every (re)connect**, so values survive an rtl_433 restart. Both run
  after `_refresh_meta`, are wrapped so a failure can never kill the connect loop,
  and every `/cmd` is best-effort.
  - **Authoritative setup frequency:** a configured `initial_center_frequency` is
    applied **once** in `_seed_desired_on_first_connect` (gated on the persisted
    `initial_freq_seeded` flag, **independent of whether `_desired` is empty**), so
    the user's explicit setup choice wins over the adopted/persisted center
    frequency even on a re-connect or after management was toggled on later, and is
    never re-applied once the user changes it via the control.
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
(except `_skip_keys.yaml`) into one field-keyed table cached in `DATA_LIBRARY`.
`DATA_LIBRARY` now caches the **shipped library only** — per-hub user overrides
are merged separately per entry (see [Per-hub user overrides](#per-hub-user-overrides-data-flow)).

A mapping entry, keyed by the exact rtl_433 field name:

```yaml
temperature_C:
  platform: sensor            # sensor | binary_sensor | event
  device_class: temperature   # HA device class, or null
  unit_of_measurement: "°C"   # unit, or null
  state_class: measurement    # measurement | total | total_increasing | null
  name: null                  # optional; null/omit => HA names it from device_class
  value_transform: { round: 1 }  # numeric transform (sensors)
  object_suffix: T            # short, STABLE unique-id token
```

`name` is **optional**: omit it (or set `null`) to let HA derive a translated
name from `device_class` — the convention for fields whose name would just
repeat the device class. Set an explicit name only when it adds information the
device class doesn't (e.g. "Battery mV", "Gust speed"). The two truly required
attributes are `platform` and `object_suffix`.

`binary_sensor` entries use `payload: { on: "<raw>", off: "<raw>" }` instead of
`value_transform`. `event` entries (in `events.yaml`) use neither — the value is
stringified to the fired `event_type` and `device_class` is an
`EventDeviceClass`; see the [Event platform](#event-platform-eventpy-value-as-type-auto-populated)
section above. `_skip_keys.yaml` lists fields that must never become entities.

An optional reserved top-level **`models:`** block (`model → {field_key →
descriptor}`, same per-field schema; `mapping.py` `Registry.models`) carries
**model-scoped** overrides — `lookup(field_key, model, registry)` resolves
model-scoped → global → `None`. Precedence is **specificity-first**: per-device
calibration > model-scoped (user > shipped) > global (user > shipped), so a
*shipped* model entry beats a *user-override global* entry for a matching model.
Per-hub user overrides support `models:` too. Full detail (incl. the
illustrative non-real-model worked example) is in `docs/device-library.md`; do
not duplicate it here.

**Do not invent attributes here.** The full schema — every attribute, the
`value_transform` keys and their application order, binary payloads, the
skip-keys file, and the per-hub user-override semantics — is defined in:

- **[docs/device-library.md](docs/device-library.md)** (authoritative).

## Per-hub user overrides (data flow)

User overrides are **per hub**, stored in `entry.data[CONF_USER_MAPPINGS]`
(`CONF_USER_MAPPINGS = "user_mappings"`, `const.py`) — **not** a global file.

- **`DATA_LIBRARY` caches the shipped library only.** Per-hub overrides are
  merged into a per-entry library cached in `DATA_ENTRY_LIBRARY[entry_id]`; the
  lookup at entity build reads that per-entry merged registry. There is **no**
  global override layer.
- **`load_user_overrides` was removed.** Nothing reads
  `<config>/rtl_433_mappings.yaml` at runtime anymore — the file-reading code
  path is gone.
- **One-time import on upgrade** (`async_migrate_entry`, `migration.py`). On the
  config-entry migration, any existing `<config>/rtl_433_mappings.yaml` is read
  **once**, normalized, and folded into each existing entry's
  `CONF_USER_MAPPINGS`. The file is then **ignored and left untouched** on disk
  (never edited or deleted). Hubs added after the upgrade start with empty
  overrides.
- **Editing surface: `async_step_mappings`** (the options-flow *Device mappings*
  step, `config_flow.py`). It presents an `ObjectSelector` / `ha-yaml-editor`
  pre-filled with the hub's current `CONF_USER_MAPPINGS`. The editor blocks
  invalid YAML syntax; on submit the integration **validates the mapping schema**
  and re-shows the form with a **per-field error** (offending field + reason)
  instead of silently dropping invalid entries. A successful save writes
  `CONF_USER_MAPPINGS` into `entry.data` and triggers an **automatic reload** of
  that hub (entities rebuild) — no HA restart. The editor returns parsed YAML, so
  comments/formatting are not preserved.

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
hub's per-hub user overrides (the options-flow *Device mappings* step) instead
of editing the shipped library (see
[User overrides](docs/device-library.md#user-overrides)).

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

CI runs on Python 3.14 (the minimum Home Assistant 2026.4 supports).

## Mutation testing (mutmut)

Line coverage proves a line ran; it does not prove a test would *fail* if the
line were wrong. Mutation testing closes that gap: [mutmut](https://github.com/boxed/mutmut)
introduces small faults ("mutants") into `custom_components/rtl_433/` and checks
that some test fails for each. A surviving mutant is a behaviour no test asserts.

Config lives in `[tool.mutmut]` in `pyproject.toml` (whole package in scope).
mutmut copies the package plus `tests/` into a `mutants/` working tree (git-ignored)
and forks once per mutant.

```bash
uv run mutmut run                              # full run (writes results under mutants/)
uv run mutmut results                          # list surviving mutants
uv run mutmut show <mutant_name>               # see the exact mutation diff
uv run mutmut run "custom_components.rtl_433.<module>.*"   # re-run one module
```

Workflow for raising a module's score:

1. `uv run mutmut run` then `uv run mutmut results` to find survivors.
2. For each survivor, add a **test** that asserts the exact behaviour the mutation
   breaks (precise return values, both branches, boundaries, dispatched signals,
   entity attributes). Kill mutants with tests only.
3. Re-run that module and confirm the survivor is gone.

Hard rules:

- **Never** edit `custom_components/` to make a mutant die — this is test-only work.
- **Never** add `# pragma: no mutate`, disable a mutator, or otherwise suppress a
  mutant. Genuinely-equivalent survivors are simply recorded in the baseline.
- The committed baseline `scripts/mutation_baseline.json` ratchets **upward only**.

The baseline and gate are driven by two stdlib-only helpers:

```bash
uv run python scripts/mutation_stats.py > stats.json          # per-file killed/total
uv run python scripts/mutation_ratchet.py --mode floor  --stats stats.json   # CI gate (PR + main)
uv run python scripts/mutation_ratchet.py --mode strict --stats stats.json   # local: is the baseline still representative?
uv run python scripts/mutation_ratchet.py --mode floor  --stats stats.json --update  # ratchet baseline upward
```

CI (`.github/workflows/mutation.yml`) enforces the per-file **floor**: a file
fails only if its score drops below its recorded value by more than a tolerance
band of `max(2% of the file's mutants, 3 mutants)`. The band is in mutant units
because that is how the variance behaves — mutmut drifts a mutant or two
run-to-run (the async coordinator especially), and a scoped PR run is a slight
lower bound on the full-suite score (a few mutants are killed only by tests in
other files). A flat percentage would be far too tight on a small file (1 mutant
≈ 3% on a 29-mutant file) and needlessly loose on a large one, so the absolute
floor protects small files while the fraction scales for large ones. A real
regression kills far more than the band; a sub-band dip on a small file passes the
PR gate and is re-measured by the nightly full run. The baseline only ratchets
**upward**: refresh it in the same PR with `--update` when you genuinely improve a
file. New mutation tests live in `tests/test_mut_*.py`.

Because a full run is slow (~50 min), CI splits the work two ways — by **scope**
(how many modules) and by **shard** (parallel across modules):

- **Scope is chosen by trigger.** **Pull requests** mutate only the modules the PR
  could affect — changed package modules, plus the source module a changed
  `tests/test_*.py` exercises (`scripts/mutation_targets.py` does the mapping).
  Typical PRs finish in a couple of minutes and still block on a per-file
  regression in touched code. A change to mutation infra (`pyproject.toml`,
  `requirements_test.txt`, `scripts/mutation_*`, `tests/conftest.py`, the workflow)
  or a broad/unmappable test escalates the PR to the full package. **Pushes to
  `main` and a nightly schedule** always run the **full** package, so the whole
  baseline stays honest and the "a test was weakened but its source is unchanged"
  blind spot is caught within a day. For a scoped run, the touched files are passed
  to `scripts/mutation_stats.py --paths` so unscoped (un-run) mutants aren't counted.
- **Whatever is in scope is split across a 6-way matrix** (one `mutation` job,
  shards 0–5). `scripts/mutation_shards.py` does a deterministic LPT partition of
  the whole package, weighting each module by its **measured mutmut run time**
  from `scripts/mutation_timings.json` (count-balancing is wrong — per-mutant time
  varies ~2.5x across modules, e.g. `entity.py` vs `coordinator/base.py`), then
  `--restrict` keeps only this shard's in-scope modules. So a full run fans out
  down to roughly the slowest single module (`mapping.py`); a scoped PR fans its
  handful of modules out too. Six shards (not more) because only ~5 modules
  dominate the time — extra shards sit near-idle and widen the spread without
  lowering the pole. The union of the shard checks equals a single whole-scope
  check (mutmut copies the whole package into `mutants/` regardless of the filter,
  so imports resolve; the filter only restricts which mutants execute). The job
  runs on every trigger and decides its own scope, so no matrix leg is skipped at
  the job level. A `mutation-gate` job (status check name "Mutation floor") fans
  the matrix back into one stable signal that fails if any shard failed.
  - `scripts/mutation_timings.json` is a committed profile, refreshed like the
    baseline: after a full `mutmut run`, `python scripts/mutation_timings.py`
    rewrites it. A module absent from it falls back to a count-based estimate, so
    a stale profile degrades gracefully (a slightly suboptimal split, never wrong).
  - Note: mutmut strips the `__init__` segment from mutant names, so a package
    `__init__.py`'s mutants live directly under the package's dotted name. The
    sharder matches those via the `x_*`/`xǁ*` trampoline prefixes (not the naive
    `<pkg>.__init__.*`, which matches nothing and would leave them unrun).

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
