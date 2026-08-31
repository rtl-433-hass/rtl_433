# AGENTS.md

Machine-oriented notes for AI agents and maintainers working on this
integration. For end-user docs see [README.md](README.md); for contribution
conventions (commits, releases, CI) see [CONTRIBUTING.md](CONTRIBUTING.md).

## Repository shape

- `custom_components/rtl_433/` — the integration.
  - `device_library/*.yaml` — the shipped, data-driven device mappings.
  - `coordinator/` — package (`base.py`) for the push coordinator, now a **thin
    Home Assistant adapter** over `pyrtl_433.Rtl433Client` (see
    [Runtime dependency](#runtime-dependency-pyrtl_433) below). `base.py` owns and
    drives the client; the mixins (`_events.py`, `_sdr.py`, `_watchdog.py`) hold
    the HA-side policy (event fan-out, managed-SDR desired state, the silence
    watchdog and the hub-connection availability gate) the library deliberately
    leaves out.
  - `config_flow.py`, `__init__.py`, `const.py`, `entity.py`, `mapping.py`,
    `normalizer.py`, `diagnostics.py`, `repairs.py`, `sensor.py`,
    `binary_sensor.py`, `event.py`, `translations/en.json`. `normalizer.py` now
    holds **only** the local `_safe_token` entity-slug helper; the actual event
    normalizer (`normalize` / `device_key` / `NormalizedEvent` /
    `DEFAULT_SKIP_KEYS`) lives in `pyrtl_433.normalizer`. `sdr_settings.py` is a
    thin adapter over `pyrtl_433.sdr` (see below).
  - `__init__.py` keeps only the steady-state config-entry lifecycle
    (`async_setup_entry` / `async_unload_entry` / `_async_update_listener` /
    `async_remove_config_entry_device`). Three sibling modules hold the rest:
    `migration.py` (config-entry v1→v2 migration + one-time legacy cleanups,
    re-exported `async_migrate_entry`), `library.py` (mapping-library load/merge),
    and `hub_settings.py` (hub-entry setting resolvers: `_hub_*`,
    `_calibration_map`).
- `docs/device-library.md` — **authoritative** device-library reference.
- `tests/` — unit tests. `tests/integration/` — container/screenshot harness.
  `tests/fixtures/generated/` — **do not hand-edit**: golden events decoded from
  real `.cu8` captures by `scripts/regen_capture_fixtures.py` and diffed in CI,
  so the device library is checked against rtl_433's real wire output rather
  than a hand-transcribed guess. See that directory's `README.md`.

## Upstreaming to Home Assistant core (shared domain — frozen contract)

This integration is being upstreamed into Home Assistant **core** as a minimal,
iteratively-expanded integration (plan 26). Core and this HACS build share the
**single `rtl_433` domain**: Home Assistant loads `custom_components/rtl_433/` in
preference to a same-named core integration (logging a "custom integration"
warning), so the HACS build stays the feature-ahead parallel channel while core
catches up. The minimal core build lives on the `rtl_433-integration` branch of a
`home-assistant/core` fork; **do not invent a second domain.**

Because both builds share the domain, three identity surfaces are a **frozen
compatibility contract** — they MUST stay byte-identical across both builds and
must not change without a coordinated, non-downgrading migration shipped in both:

- entity `unique_id` formats,
- device `identifiers` tuples,
- the config-entry `VERSION`/`MINOR_VERSION` ladder and its migrations.

The authoritative spec is [`COMPATIBILITY_CONTRACT.md`](COMPATIBILITY_CONTRACT.md);
`tests/test_migration_roundtrip.py` guards it. Per-module upstreaming progress and
the ordered follow-up PR sequence are tracked in
[`CORE_UPSTREAM.md`](CORE_UPSTREAM.md).

## Runtime dependency (pyrtl_433)

The integration has **one third-party runtime dependency**:
`pyrtl_433==0.2.0` (declared in `manifest.json` `requirements` and mirrored in
`requirements.txt`; Home Assistant installs it on load). It is the extracted
rtl_433 protocol layer — the WebSocket connect/reconnect loop and JSON frame
parsing, the event **normalizer**, the reconnect-**replay/stale classifier**, and
the **SDR `/cmd`** command definitions and getters/setters. The integration no
longer carries its own copy of any of that; it consumes the library:

- **`pyrtl_433.Rtl433Client`** — owns the transport. The coordinator
  (`coordinator/base.py`) **owns and drives one client per hub**, constructed with
  Home Assistant's **shared aiohttp session** (`async_get_clientsession`), which the
  client therefore **never closes** on `stop()` (HA owns the session lifecycle).
  Events arrive via the client's **`on_event`** callback →
  `_EventProcessingMixin._on_client_event` (HA-side dispatch), and connectivity /
  meta / stats / dev-info changes via **`on_hub_update`** → `_emit_hub_update`
  (connect/disconnect edge handling, hub-identity refresh, `signal_hub_update`
  fan-out). The library owns neither the managed-SDR policy nor the availability
  watchdog, so those are driven HA-side off the connect edge and a time interval.
- **`pyrtl_433.normalizer`** — `normalize` / `device_key` / `NormalizedEvent` /
  `DEFAULT_SKIP_KEYS`. Consumers import these directly from the library. The only
  remaining local shim is `normalizer._safe_token` (the private entity-slug helper
  the library does not export), so unique_ids and dispatcher signals stay
  byte-identical.
- **`pyrtl_433.replay`** — `classify_replay` / `ReplayVerdict` / `parse_event_time`
  plus `DISCOVERY_BACKLOG_GRACE` / `REPLAY_STALE_THRESHOLD`. The client applies the
  classification and stamps the verdict on the `NormalizedEvent`;
  `coordinator/_events.py` re-derives only the HA-side **pre-connection backlog
  gate** off `_connection_time` using the same boundary constant.
- **`pyrtl_433.sdr`** — the wire-protocol half of the managed SDR controls
  (`SdrCommand` / `SDR_COMMANDS`: how to read each field from `meta`, the `/cmd`
  command, `val`/`arg` kind, value transforms, capability/availability gates).
  `sdr_settings.py` is the **thin integration adapter** that re-supplies the HA
  entity metadata the library drops (name, unique-id token, platform, Number
  bounds/mode/unit, Select options) and merges it with the library's protocol
  callables (taken **by reference** from `SDR_COMMANDS`) into the `SdrSetting`
  records the coordinator and platforms consume. It re-exports the library's helper
  functions and stable keys unchanged, so every existing consumer keeps the same
  import names.
- **`CannotConnect`** is the library's error, imported and **re-exported** from the
  coordinator package so existing import sites (`config_flow.py`, `repairs.py`) are
  unchanged. `validate_connection` delegates to `Rtl433Client.validate_connection`.
- **`/cmd` write path.** pyrtl_433 0.1.0 exposes no public setter; the coordinator's
  `_send_cmd` delegates to the client's underscore-prefixed **`Rtl433Client._send_cmd`**
  (a known private-API wart to revisit when the library grows a public alias). All
  `/cmd` issuance is serialized **inside the client** (its own send lock), so a user
  write and a reconnect enforcement replay can never interleave.

## Config-entry model (hub + nested devices)

The integration is **rfxtrx-style**, not Battery-Notes-style:

- **One config entry per rtl_433 server** (the hub, `integration_type: "hub"`).
  Platforms are forwarded once on that entry
  (`async_forward_entry_setups(entry, PLATFORMS)`).
- The RF devices it decodes are **device-registry devices nested under the hub
  entry**, *not* separate config entries. They are recreated on startup from the
  per-hub `entry.data["devices"]` map (the single source of truth: model,
  observed mapped fields, optional per-device timeout override) and added at
  runtime via the new-device dispatcher signal (the Quality-Scale
  `dynamic-devices` rule).
- **Observation and adoption are separate states; nothing is ever auto-added.**
  A frame whose `device_key` is not in the coordinator's `adopted` set (seeded
  from `entry.data["devices"]`) is routed by `_record_pending`
  (`coordinator/_events.py`) into the coordinator's in-memory `pending` map — a
  `PendingDevice` per candidate (latest `NormalizedEvent`, sighting count,
  first/last seen) — and goes **no further**: no registry device, no entities, no
  dispatch, and none of the adopted-device runtime state (`devices`,
  `last_seen`, `available`, `seen_fields`, `device_fields`) is touched, so the
  availability watchdog, diagnostics and the entity platforms keep seeing exactly
  the adopted set. Keys on `entry.data["ignored_devices"]` are dropped outright.
  Replay and pre-connection **backlog** frames never create a candidate (a
  reconnect must not repopulate the list), which is the post-connection
  registration gate applied to candidacy — see the coordinator's
  replay/registration notes. The pending map is **memory-only by design**: empty
  after a restart or reload, refilled by live traffic. Do not add persistence, a
  TTL, or an eviction policy.
  A device becomes real only through `coordinator.adopt_device` (from the
  options flow, below), which promotes the stored event into runtime state and
  fires the **same** `new_device_callback` / `SIGNAL_NEW_DEVICE` seam a live
  sighting used — one registration path, not two. There is **no** persistent
  notification for a heard device (the per-device notification, and the per-hub
  discovery toggle that used to gate auto-add, were both removed); the
  `INFO` log line in `_record_pending` is the only signal.
- `async_remove_config_entry_device` (`__init__.py`) backs the per-device
  **Delete** affordance (the `stale-devices` rule): it returns `False` for the
  hub device (so the hub can't be removed out from under its entry) and `True`
  for nested RF devices, dropping the device from the devices map and **evicting
  its `device_key` from coordinator runtime state** (`coordinator.forget_device`,
  which also drops it from `adopted`) so its next transmission makes it a
  **pending candidate again** — deletion returns a device to the list rather than
  silently recreating it. To make one go away for good the user ignores it.
- A nested device's identity (`device_key`) is **re-pointable**: a battery swap
  usually makes a sensor draw a new transmitter id, and
  `async_replace_device` (`device_replace.py`, options `replace` →
  `replace_target` steps) re-points an existing device and every entity under it
  from the old key onto the new one, so `entity_id` — and therefore recorder
  history, statistics, dashboards and automations — survives. That helper is the
  **only** sanctioned place to rewrite a nested device's registry `identifiers`
  or entity `unique_id`s; do not open-code a re-key elsewhere. It re-emits the
  `COMPATIBILITY_CONTRACT.md` identifier/unique_id templates verbatim (only the
  `device_key` value changes), so the contract is unaffected by a replace.
- `async_migrate_entry` (`migration.py`, config-entry `VERSION` 1 → 2) performs a
  **seamless in-place upgrade from 0.1.0**: it re-homes the legacy per-device
  config entries' registry devices/entities onto the hub entry (preserving
  unique_ids, entity_ids, and history), folds their state into the hub's devices
  map, and removes the obsolete per-device entries. The minor-7 → 8 step
  (`_strip_discovery_toggle`) drops the retired discovery key from `entry.data`
  and `entry.options`; it never rewrites `entry.data["devices"]`, so every
  already-adopted device, override and calibration is preserved untouched.
- Adoption and per-device configuration live in the **hub OptionsFlow**
  (`options_flow.py`): a menu led by the two approval steps — `add_devices`
  (renders `coordinator.pending` newest-first, one row per candidate labelled
  model, key, sighting count, signal level and relative last-seen, with two
  independent multi-selects: *Add* → `adopt_device` + `async_upsert_device`,
  *Ignore* → `entry.data["ignored_devices"]`; aborts `no_pending_devices` on an
  empty list) and `ignored_devices` (un-ignores selected keys; not retroactive —
  the device returns on its next transmission). The ignore list is applied
  **live** through the update listener, never a reload. The user-facing verb is
  **Ignore/Ignored**, matching HA's ignored-discovery vocabulary; "reject" must
  not appear. The menu then carries a *Hub settings* step (default timeout +
  managed-settings, written to `entry.options`) and a *Device settings* pair — a
  `device` picker step
  followed by a `device_settings` step (per-device timeout override, commodity
  and motion clear-delay, written into `entry.data["devices"]`). The picker is a
  separate step so every default on `device_settings` is derived from the
  **selected** device; the picker labels devices whose commodity was detected.
- **Utility-meter calibration** (`calibration.py`, options `device_settings` →
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
- **The update listener is the *only* place that reloads a hub entry.** Home
  Assistant deprecated pairing a config-entry update listener with the reloading
  config-flow helpers in 2026.6 (it double-reloads and races; it becomes an error
  in 2026.12 — issue #168), so every flow that re-points a hub only **writes**:
  `async_step_reconfigure` uses `async_update_and_abort` (not
  `async_update_reload_and_abort`), the Supervisor discovery step passes
  `reload_on_update=False` to `_abort_if_unique_id_configured`, and
  `async_rebind_hub` does not reload either. `_async_update_listener` then
  compares `(host, port, path, secure, unique_id)` (`_hub_connection`,
  `hub_settings.py`) against `coordinator.connection_snapshot` and reloads once
  when it differs — the same snapshot-vs-live pattern as `manage_settings` /
  `calibration_snapshot` / `user_mappings_snapshot`. Never reintroduce a
  flow-side reload.
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
  manual flow — `manage_settings` and an optional `initial_frequency` in MHz);
  `async_step_user` likewise aborts
  `already_configured` if a `host:port` is already owned by a discovered entry.
  Both add flows persist `manage_settings` and, when it is on and
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
  staleness automations. It is **not** exempt from the hub-connection gate (see
  [Hub-connection availability gate](#hub-connection-availability-gate)): with
  the socket down the timestamp only records when the integration stopped
  listening, so the sensor goes unavailable with the rest of the device.

## Event platform (`event.py`, value-as-type, auto-populated)

`Rtl433Event` (`event.py`) is the third platform (`Platform.EVENT` in
`PLATFORMS`). Unlike the Last seen sensor it is **field-driven** — built via
`async_setup_hub_platform` for descriptors whose `platform == "event"`, with
**no `per_device_factory`** — using the **unchanged shared 5-arg constructor**.
Invariants that must survive refactors of `async_setup_hub_platform`, the
coordinator watchdog, and the devices map:

- **Flag-based watchdog dedupe.** It overrides `_handle_dispatch` to suppress the
  watchdog's re-dispatch on the frame's **classification** (`event.is_repaint`),
  **never on value-equality**. The watchdog re-sends the device's cached last
  event purely so entities re-read availability; a genuine live repeat (even of
  the same value) is a distinct transmission that **must** fire (a doorbell
  pressed twice fires twice). If this ever keyed off `==`, genuine repeats would
  be silently dropped. `is_repaint` supersedes an earlier object-identity dedupe,
  which was unreliable: a replay-seeded cache left the anchor unset after a
  restart, and `_dispatch`'s flag rewrite mints a fresh object anyway.
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
- **Unmodified availability; no construction-time replay.** `available` is **not**
  overridden — the entity takes the base gate whole: the hub connection *and* the
  per-device silence timeout (see
  [Hub-connection availability gate](#hub-connection-availability-gate)). This
  matches zigbee2mqtt (its `event` discovery payload carries the bridge-state and
  per-device availability topics, `availability_mode: all`) and core (Shelly's
  event entities inherit `CoordinatorEntity.available`; ESPHome's follow the
  device connection). The silence timeout is harmless in practice because
  button/doorbell/motion/contact devices classify as event-driven and resolve to
  the never-expire class default. **Nothing re-fires on the way back:** the
  reconnect replay is `is_replay` so `_handle_dispatch` returns before
  `_trigger_event` (the timestamp never advances), core's `event.received`
  trigger sets `_excluded_from_states = {STATE_UNAVAILABLE}`, and
  `device_trigger.py`'s listener returns on an `old_state` of `unavailable`.
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
- **No re-fire on a restore.** HA's `EventEntity` restores its last `event_type`
  + timestamp (for display); a raw listener would re-deliver that stale event
  (e.g. a days-old doorbell `ring`) as if it just happened. The listener
  **ignores both restore shapes**, and dropping either one reopens the bug:
  - `old_state is None` — an **HA restart**, the entity's first appearance in the
    state machine. (The base trigger previously delegated to the core `state`
    trigger, which fires on a match_all `None`→state transition — the same
    re-fire-on-restart bug, now closed for both paths.)
  - `old_state.state == STATE_UNAVAILABLE` — a **config-entry reload** *or* a
    **hub outage**, either of which takes the entity
    `<event>` → `unavailable` → restored `<event>` while the listener is still
    attached. Since event entities are gated on the hub connection this is the
    common case, not the rare one.

  It also ignores a `new_state` that is `None`/`unavailable`/`unknown` (the
  unload edge). An `old_state` of `unknown` is deliberately allowed: the very
  first press rises from the never-fired `unknown` state and must fire.

## WebSocket frames & hub observability

Durable contracts for how streamed frames become device/hub updates and drive the
hub diagnostic entities. Frame classification, normalization, the replay/stale
classifier, and the `/cmd` getters now live **inside `pyrtl_433.Rtl433Client`**;
the coordinator (`coordinator/base.py`, `sensor.py`, `binary_sensor.py`) consumes
them via the client's callbacks and adds the HA-side policy (dispatch, the
registration gate, hub-identity refresh, sensor mapping). The method names below
name the library's internals unless stated otherwise — they are documented here
because these are the contracts the integration relies on:

- **Frame classification** (client-side `_classify_frame`). A streamed frame is treated as a
  decoded-device event **iff** it has a `model` key **or** an identity key
  (`id` / `channel` / `subtype`, kept in sync with `pyrtl_433.normalizer.IDENTITY_KEYS`).
  A `{"shutdown": ...}` frame drives the **connectivity** sensor (flips it off).
  A **server log frame** (`{"time", "src", "lvl", "msg"}`, rtl_433 ≥ 23.11 —
  recognized by `msg` + `lvl` with no model/identity keys) goes to the client's
  `_handle_log`: `src == "Auto Level"` messages are parsed
  (`pyrtl_433.autolevel`, exact upstream wording, unparsable ⇒ ignored) into
  the client's `noise_level` / `min_level` snapshots — the **only** source of
  the receiver noise floor rtl_433 offers (no structured getter exists) — and
  fire `on_hub_update` on change; the raw frame also reaches the optional
  `on_log` callback (unused by the integration today).
  **Every other frame is ignored** on the socket (`meta`, periodic state/stats,
  RPC `result`/`error`). This is why non-event frames no longer create a phantom
  `"unknown"` device or pollute `seen_fields` / the diagnostics
  `unmatched_field_keys`.
- **Replay/stale suppression** (client-side `classify_replay` / `parse_event_time`,
  `pyrtl_433.replay`). On every
  (re)connect the server replays up to its last 100 events, so the client
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
  the replay. A suppressed `event` transmission logs at **DEBUG**
  (`Rtl433Event._handle_dispatch`) — it happens on every reconnect, so it is not
  INFO-worthy. The classification rides on the
  dispatch carrier: `NormalizedEvent.is_replay` / `event_time` (stamped via
  `dataclasses.replace` after `normalize`; live is the default), so dispatch
  needs no extra signature. The **watchdog re-dispatch passes `is_replay=False`**
  so its unavailable re-paint of a cached (maybe-replay) event is never
  suppressed. **Assumes the server and HA clocks are roughly NTP-synced**
  (a local-naive `time` is read in HA's configured zone — the coordinator passes
  `event_tz=dt_util.get_default_time_zone()` into the `pyrtl_433` client, so
  classification does not depend on the host process zone). **Limitation:** with server
  timestamps disabled (`report_meta notime`) there is no usable `time`, so every
  frame is treated as live and **events fire on replay**.
- **Post-connection device-registration gate** (HA-side, `_events.py`
  `_on_client_event` / `_maybe_register_device`, `_connection_time`,
  `DISCOVERY_BACKLOG_GRACE`). Distinct from the replay/event
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
- **Hub observability data source** (client-side, `pyrtl_433.Rtl433Client`). SDR/meta
  and server stats are **not** read
  from the socket. The client issues one-shot HTTP GETs to `scheme://host:port/cmd`
  at the **server root** (`https` when `secure`/`wss`, else `http`) — the `/cmd` URL
  never derives from the configured WS `path`, so a proxy that
  hides `/cmd` degrades gracefully (the stream + connectivity sensor stay up; the
  meta/stats/gain/ppm sensors read `unknown`). Each getter swallows its own
  errors so it can never raise into the client's connect loop or the HA watchdog.
  The request uses the `cmd` query param; scalar getters are read defensively
  through a `{"result": ...}` unwrap. The coordinator surfaces the results via
  read-only properties (`meta`, `stats`, `dev_info`, `dev_query`) that delegate to
  the client, and a `_refresh_meta` that delegates to `Rtl433Client.refresh_meta`.
- **Exact getter set** (client-owned): `get_meta` + `get_gain` + `get_ppm_error` +
  `get_stats` + `get_dev_info` + `get_dev_query`. **Gain and ppm are absent from
  `get_meta`** — they come from `get_gain` (string; empty ⇒ `auto`) and
  `get_ppm_error` (int) respectively. **Hop interval = `hop_times[0]`.**
  `get_dev_info`/`get_dev_query` are the SDR's identity and the client fetches them
  **only on (re)connect** (not on its interval tick): they are static
  per dongle. When the identity changes, the client fires `on_hub_update`, and the
  coordinator's `_maybe_refresh_hub_identity` then fires the HA-side
  `hub_info_callback` so `__init__.py` refreshes the **hub** device-registry entry's
  `manufacturer`/`model`/`serial_number` (replacing the generic `rtl_433` /
  `rtl_433 server` placeholders). Empty when no SDR is open (e.g. `-D manual`),
  in which case the placeholders are kept.
  The client re-polls **both** meta and stats
  on a fixed interval (60 s) while connected, on top of the
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
  - Noise level / minimum detection level hub sensors → the coordinator's
    `noise_level` / `min_level` properties (delegating to the client's parsed
    "Auto Level" snapshots above) — **socket-sourced**, not `/cmd`-sourced, so
    they survive a proxy that hides `/cmd` but stay `unknown` unless the server
    runs `-Y autolevel` and/or `-M noise[:secs]`. Both are dB gauges →
    **`MEASUREMENT`** (`SIGNAL_STRENGTH` device class).
- **Phantom-unknown cleanup.** `async_setup_entry` (`__init__.py`) calls
  `_cleanup_phantom_unknown_device` (`migration.py`), which **idempotently** removes a legacy
  persisted `"unknown"` device from `entry.data["devices"]` and the matching
  registry device `(DOMAIN, f"{entry_id}:unknown")`. Safe on every setup; the
  classifier above prevents recreation.

## Hub-connection availability gate

Durable contract for the second availability gate (`coordinator/_watchdog.py`,
`entity.py`, `event.py`, `sensor.py`). The per-device *silence* timeouts answer
"has this radio transmitted lately?", which only means anything while the
integration is listening; this gate answers "is the integration listening at
all?". End-user docs live in
[docs/availability.md](docs/availability.md#hub-connection).

- **`coordinator.hub_available` is exactly `self.connected`.** No grace window,
  no debounce, no timer: the socket drops, every device behind the hub is
  unavailable on the same tick. **Do not add a delay here.** It was tried and
  removed deliberately — a delay presents readings as current while the
  integration knows it cannot hear the radio, which is what the Silver-tier
  `entity-unavailable` rule exists to prevent, and every Home Assistant
  integration gating on a live connection flag (`mqtt`, `zwave_js`, `esphome`,
  `deconz`, `unifi`, and newer arrivals like `harbor`) flips instantly. The
  recent core additions that *do* debounce (roborock's `MIN_UNAVAILABLE_DURATION`,
  netatmo's `UNAVAILABLE_AFTER_ERRORS`) sit in the coordinator poll-failure path,
  tolerating transient API errors — not a transport known to be down.
- **The debounce lives in `repairs.py`, not here.** `_UNREACHABLE_GRACE` (90 s)
  delays the user-facing "server unreachable" *notification* so a routine server
  restart does not raise one. Entities tell the truth immediately; the
  notification waits until the outage looks real. That split is the design — do
  not collapse it by moving the delay onto the entities.
- **`_disconnected_since` is reporting only.** It feeds the reconnect log line's
  outage duration and the diagnostics dump. Nothing about availability reads it.
- **Every device entity reads it.** `Rtl433Entity.available` short-circuits on it
  *before* the silence check, and `Rtl433LastSeenSensor` routes through it too.
  **Never-expire devices are not exempt** — that exemption is from *silence*, not
  from the transport being gone.
- **`Rtl433Event` is not an exception** — it does **not** override `available`, so
  event entities go unavailable with everything else. zigbee2mqtt does the same
  (its `event` discovery payload carries the `bridge/state` topic plus the
  per-device one, `availability_mode: all`), as do core's Shelly and ESPHome
  event entities. Do not "fix" this by hardcoding `True`. Coming back does not
  re-fire anything: the reconnect replay is `is_replay` so `_handle_dispatch`
  returns before `_trigger_event` (the timestamp never advances), core's
  `event.received` trigger sets `_excluded_from_states = {STATE_UNAVAILABLE}`,
  and `device_trigger.py`'s listener returns on an `old_state` of `unavailable`.
  The one accepted cost is HA's restore — `EventEntity.async_internal_added_to_hass`
  parses the stored state string, so a shutdown *during* an outage persists
  `unavailable` and loses the last-fired record. That is what every core event
  integration already does.
- **Values survive an unavailable restart.** `Rtl433Sensor`,
  `Rtl433LastSeenSensor` and `Rtl433BinarySensor` persist their value through
  `extra_restore_state_data`, because HA writes `unavailable` as the *state* when
  `available` is False and the state string is then unrestorable. Without it a
  restart during an outage strands every never-expire contact at `unknown` until
  it next transmits — possibly days.
- **The hub's own diagnostic sensors read it too.** `Rtl433HubSensor.available`
  returns `hub_available`: every value it renders is HTTP `/cmd`-sourced, so an
  outage freezes it with nothing on the entity to say so. A key missing from a
  *live* payload still reads `unknown` (a `None` native value), not unavailable.
  Two hub entities stay ungated: `Rtl433HubConnectivity` (it *is* the connection
  report — `available` is hardcoded `True` and it flips `off` on the drop with no
  grace window — same as the devices now) and `Rtl433HubControl` (availability is
  a capability gate on
  `meta`).
- **The clock starts at `async_start`,** not at the first drop, so a Home
  Assistant restart while the server is down expires the restored states at once
  instead of leaving them available forever.
- **Lazy gate, edge-driven repaint.** Entities evaluate `hub_available` on every
  state read, so it is always correct; the coordinator only *repaints*. The
  disconnect edge and the connect edge each call it, and each watchdog tick
  re-checks as a cheap backstop in case an edge is ever missed. All
  three funnel into `_async_sync_hub_availability`, which dispatches
  `SIGNAL_HUB_AVAILABILITY` **once per flip** (a hub-wide signal, deliberately
  separate from `SIGNAL_HUB_UPDATE`, which also fires on every meta/stats refresh
  and would otherwise write state for every device entity on each poll). Both
  `Rtl433Entity` and `Rtl433HubEntity` subscribe: `SIGNAL_HUB_UPDATE` covers the
  connect/disconnect edges, which is exactly when a gated entity's `available`
  changes.
- **Logging.** The library logs drops at DEBUG under its own logger, which is
  invisible to anyone debugging the integration. The coordinator logs the loss
  and the recovery (with the outage duration) at **INFO**, and the moment the
  devices are marked unavailable at **WARNING** (naming the URL and the device
  count — from the *persisted* device map, not the live-session one, which is
  empty in the restart-while-down case the gate exists for). A teardown
  (`async_stop`) is not an outage and logs neither — the `_started` guard in
  `_emit_hub_update` covers the socket close it performs. A failed
  `async_start` cancels the timer it armed, so an entry left in `setup_retry`
  does not leak a repaint onto the coordinator the retry installs.
- **Tests default to a connected hub.** Feeding events straight into the client
  leaves `connected` False, which the gate reads as one long outage, so the
  autouse `tests/conftest.py::hub_connected_by_default` fixture marks every
  started coordinator connected (including after a reload, which rebuilds it).
  Modules that exercise the outage side opt out with
  `@pytest.mark.hub_disconnected` and drive the edges themselves.

## Hub SDR controls (HA-managed settings)

Durable contracts for the optional HA-managed SDR controls (`sdr_settings.py`,
`coordinator/base.py`, `__init__.py`, the `number`/`select`/`switch` platforms).
End-user docs live in
[docs/hub-entities.md](docs/hub-entities.md#managing-sdr-settings-from-home-assistant) —
keep this contributor-facing.

- **Settings-registry contract** (`sdr_settings.py`, import-disjoint like
  `mapping.py`). The wire-protocol half — how to read each field from `meta`, the
  `/cmd` command, the `val`/`arg` kind, the value transforms, and the
  capability/availability gates — lives in **`pyrtl_433.sdr`** (`SdrCommand` /
  `SDR_COMMANDS`), which drops all HA entity metadata. `sdr_settings.py` is the
  thin **adapter** that merges that protocol contract (protocol callables taken
  **by reference** from `SDR_COMMANDS`, so argument composition is byte-identical)
  with the HA metadata the library omits, producing the `SDR_SETTINGS` list of
  `SdrSetting` records the control set is built from; each `SdrSetting` is pure
  data plus tiny callables so the coordinator and the platforms can iterate it
  **without importing each other**.
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
- **Adoption + full enforcement on reconnect** (`coordinator/base.py`, driven
  HA-side off the client's connect edge: `_emit_hub_update` → `_on_connect` →
  `_seed_desired_on_first_connect`, `_sdr.py`). The library client owns the
  transport but **not** this managed-SDR policy. When `manage_settings` is
  on: on first connect (when `_desired` is empty) `_adopt_from_server()` seeds the
  desired state from `self.meta`; then `_enforce_all()` **replays every managed
  field on every (re)connect**, so values survive an rtl_433 restart. Both run
  after a `_refresh_meta`, are wrapped so a failure can never kill the connection,
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
    read-back) goes through the coordinator's `_send_cmd`, which delegates to the
    client's underscore-prefixed `Rtl433Client._send_cmd` (the only write path in
    pyrtl_433 0.1.0). The send lock lives **inside the client**, so a user write
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
  timeout and ignore-list changes are applied live with no reload. The same
  listener also owns the reload for a changed connection target / stable radio id
  (see the config-flow section) — no flow reloads a hub itself.
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
   creation, so a malformed entry fails fast. Add a fixture under
   `tests/fixtures/` too: `tests/test_fixture_coverage.py` sweeps every fixture
   and fails on any field with neither a descriptor nor a skip-key entry, which
   is the only automated check that a key actually matches. Field names are
   case-sensitive and a mismatch is **silent** — no entity, no warning, no error
   (SCMplus emits `Consumption`, ERT-SCM emits `consumption_data`).
4. **Read the diagnostics' unmatched keys.** The hub diagnostics export contains
   an `unmatched_field_keys` list — JSON keys that are neither skipped nor
   mapped. Download it from **Settings → Devices & Services → rtl_433 → ⋮ →
   Download diagnostics**. Every key there is a one-line YAML addition; the list
   shrinks as you add mappings. See the
   [add-a-mapping workflow](docs/device-library.md#add-a-mapping-workflow).

For an installation-local change that should **not** be committed, use the
hub's *Device mappings* options step instead of editing the shipped library (see
[Adding device mappings](docs/device-library.md#adding-device-mappings)).

## Running the unit tests

Dependencies and tools are managed with [uv](https://docs.astral.sh/uv/), the
same as CI. Install uv with `curl -LsSf https://astral.sh/uv/install.sh | sh`,
then just run:

```bash
uv run pytest tests/
```

That works from a **fresh clone or a git worktree** with no setup: the test
dependencies are declared in the `dev` dependency group in `pyproject.toml`,
which uv installs by default, so `uv run` resolves them and populates `.venv` on
first use (a minute or two; instant thereafter). `uv.lock` is generated locally
and is **not** committed — it stays in `.gitignore` with the other dependency
locks and caches.

`[tool.uv] environments` pins the resolution to `python_full_version >= 3.14.2`.
That is deliberate and load-bearing: `requires-python` is `>=3.14` (what the
*integration* needs, tracked against Home Assistant), but the pinned test stack
pulls in a `homeassistant` requiring `>=3.14.2`, so an unrestricted resolution is
unsatisfiable. Constraining the environment keeps the project's declared support
honest rather than tightening it to satisfy a dev-only dependency.

> **If you see `Failed to spawn: pytest`**, the `dev` group is missing or
> resolution failed. Note this failure mode is **silent**: uv creates an empty
> environment, fails to find pytest, and **still exits 0**, so a green-looking
> run has actually executed nothing. Always confirm a real pass/fail count before
> trusting an exit code — this bit a worktree session before the `dev` group
> existed.

The pins are duplicated in `requirements_test.txt`, which CI and pip users
install directly (`uv pip install -r requirements_test.txt`); a renovate
packageRule groups the two so they are bumped in one PR and cannot drift.

`requirements_test.txt` pins `pytest-homeassistant-custom-component`, which pulls
in the matching Home Assistant version and the full pytest stack (asyncio, cov,
timeout, xdist, freezegun). To match CI, include coverage:

```bash
uv run pytest --cov=custom_components/rtl_433 tests/
```

`addopts` in `pyproject.toml` passes `-n auto`, so the suite runs across all CPUs
via xdist (~80s to ~18s on 8 cores). Pass `-n0` to force a serial run:

```bash
uv run pytest -n0 tests/test_coordinator.py   # single file: ~4s serial, ~9s under xdist
uv run pytest -n0 --pdb tests/                # xdist swallows --pdb and -s
```

Prefer `-n0` when selecting one file or a handful of tests -- xdist spends about
four seconds starting an interpreter and importing Home Assistant *per worker*,
which costs more than it saves below roughly a hundred tests. Mutation runs pin
`-n0` themselves; see `[tool.mutmut]` in `pyproject.toml`.

CI runs on Python 3.14 (the minimum Home Assistant 2026.4 supports), and
`pyproject.toml` sets `requires-python = ">=3.14"`. **The codebase uses 3.14-only
syntax and will not parse on an older interpreter.** In particular it relies on
[PEP 758](https://peps.python.org/pep-0758/), which allows unparenthesized
exception tuples:

```python
except OSError, yaml.YAMLError:   # valid on 3.14+, SyntaxError on <= 3.13
```

This appears in `calibration.py`, `coordinator/_sdr.py`, `mapping/_loader.py`,
`mapping/_transform.py`, `migration.py`, and `sensor.py`. Running `python -m
compileall`, a linter, or a type checker under 3.13 or earlier reports
`SyntaxError: multiple exception types must be parenthesized` on these files.
That is a stale interpreter, **not** a defect — check `python --version` before
concluding the tree is broken, and do not "fix" it by adding parentheses.

## Mutation testing (mutmut)

Line coverage proves a line ran; it does not prove a test would *fail* if the
line were wrong. Mutation testing closes that gap: [mutmut](https://github.com/boxed/mutmut)
introduces small faults ("mutants") into `custom_components/rtl_433/` and checks
that some test fails for each. A surviving mutant is a behaviour no test asserts.

Config lives in `[tool.mutmut]` in `pyproject.toml` (whole package in scope).
mutmut copies the package plus `tests/` into a `mutants/` working tree (git-ignored)
and forks once per mutant.

**Scope note (post-`pyrtl_433` extraction).** The transport, event normalizer,
replay/stale classifier, and SDR `/cmd` protocol logic now live in the
`pyrtl_433` library, which carries **its own** test + mutation coverage. Those
extracted shards are therefore **retired from this repo's mutation scope** — the
in-scope modules (`normalizer.py`'s `_safe_token`, `sdr_settings.py`'s HA-metadata
adapter, and the coordinator's HA-side policy) are the thin seams that remain
here. The baseline/timings reflect only what still ships in
`custom_components/rtl_433/`.

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
- The bridge also tails rtl_433's `-F log` output and re-frames each log line as
  the structured `{"time","src","lvl","msg"}` frame a real `-F http` server
  pushes, which is the only channel carrying the "Auto Level" noise-floor data
  behind the hub's noise sensors. The replay feeds RF silence between capture
  passes so that noise floor genuinely moves; without it `-Y autolevel` never
  logs an adjustment. The bridge serves no `/cmd`, so the `/cmd`-sourced hub
  sensors read `unknown` in the harness — never synthesize them.

Full runbook:

- **[tests/integration/README.md](tests/integration/README.md)**

## Guardrails for automated changes

- Prefer **YAML library edits** over Python: most device support is data.
- Keep `object_suffix` values **stable** — changing one orphans existing
  entities.
- Keep `const.py` the single source of truth for config keys and defaults
  (`DEFAULT_PORT=8433`, `DEFAULT_PATH="/ws"`, `DEFAULT_AVAILABILITY_TIMEOUT=600`)
  and for the dispatcher signals (`SIGNAL_NEW_DEVICE`, `SIGNAL_HUB_UPDATE` — the
  latter fans connectivity/meta/stats changes out to the hub entities — and
  `SIGNAL_HUB_AVAILABILITY`, the per-flip device repaint behind the
  hub-connection gate).
- Always run `pytest tests/` before proposing a change, and follow the
  conventional-commit and lint rules in [CONTRIBUTING.md](CONTRIBUTING.md).
- Always open pull requests with a **conventional-commit-style title** that
  summarizes the branch's changes (e.g. `feat(rtl_433): add hub observability
  sensors`), matching the commit convention above.
