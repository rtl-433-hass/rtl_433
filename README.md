# rtl_433 for Home Assistant

[![CI - Test](https://github.com/rtl-433-hass/rtl_433/actions/workflows/test.yml/badge.svg)](https://github.com/rtl-433-hass/rtl_433/actions/workflows/test.yml)
[![CI - Lint](https://github.com/rtl-433-hass/rtl_433/actions/workflows/lint.yml/badge.svg)](https://github.com/rtl-433-hass/rtl_433/actions/workflows/lint.yml)
[![CI - Validate](https://github.com/rtl-433-hass/rtl_433/actions/workflows/validate.yml/badge.svg)](https://github.com/rtl-433-hass/rtl_433/actions/workflows/validate.yml)
[![hacs](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz)

A Home Assistant custom integration that connects to an
[rtl_433](https://github.com/merbanan/rtl_433) HTTP server's WebSocket stream and
turns the 433 MHz / ISM-band devices it decodes (weather stations, soil/leak
sensors, door contacts, energy meters, remotes, doorbells, and more) into
native Home Assistant sensors, binary sensors, and event entities.

It is a **local push** integration: events arrive over a WebSocket as rtl_433
decodes them, so there is no polling and no cloud dependency.

## Overview

[rtl_433](https://github.com/merbanan/rtl_433) decodes RF transmissions from an
SDR and can expose decoded events over an HTTP/WebSocket API (`-F http`). This
integration connects to that endpoint, normalizes each event into a stable
device identity, and maps the raw fields to Home Assistant entities using a
data-driven [device library](docs/device-library.md). You add one hub per
rtl_433 server, and the devices it decodes appear automatically as nested
devices under that hub (rfxtrx-style).

You run one rtl_433 server (with your SDR); this integration is the Home
Assistant side. It does **not** talk to an SDR directly and ships no native
requirements.

## Features

- **Local push** over the rtl_433 WebSocket — no polling, no cloud.
- **Data-driven device library** — device support is YAML, not Python. Add or
  correct a device with a small, reviewable
  [mapping change](docs/device-library.md).
- **Per-hub user overrides** — add or correct mappings from the hub's options
  using Home Assistant's built-in YAML editor; validated before save and applied
  by an automatic reload. No file editing or restart required.
- **Automatic nested devices** — each newly observed device is added
  automatically as a device-registry device under the hub, gated by a per-hub
  discovery toggle. Remove one from its device page; with discovery on it
  re-appears the next time it transmits.
- **Configurable availability** — entities go `unavailable` after a silence
  window; set a hub-wide default and override it per device.
- **Multiple servers** — add one hub per rtl_433 server; identities are scoped
  per hub so two servers that see the same device model never collide.
- **Hub observability** — each hub gets diagnostic entities for its connection,
  SDR/meta configuration (center frequency, sample rate, gain, ppm, …), and
  server statistics (decoded events, OOK/FSK frames). See
  [Hub entities](#hub-entities).
- **Managed SDR settings (optional)** — let Home Assistant own the receiver's
  SDR settings. When on, the hub exposes number/select/switch **controls** for
  frequency, sample rate, gain, ppm, conversion mode, and hop interval; Home
  Assistant adopts the server's current values and re-applies them after every
  reconnect, so your settings survive an rtl_433 restart. See
  [Managing SDR settings from Home Assistant](#managing-sdr-settings-from-home-assistant).
- **Per-device signal diagnostics** — when rtl_433 reports level data, each
  device exposes disabled-by-default `Frequency`, `RSSI`, `SNR`, and `Noise`
  diagnostic sensors. See
  [Per-device signal diagnostics](#per-device-signal-diagnostics).
- **Diagnostics feedback loop** — downloadable diagnostics list the
  `unmatched_field_keys` a hub has seen, telling you exactly what to add to the
  library.

## Installation

### HACS (custom repository)

This integration is not (yet) in the default HACS store, so add it as a custom
repository:

1. In Home Assistant, open **HACS**.
2. Click the **⋮** menu (top right) → **Custom repositories**.
3. Enter the repository URL `https://github.com/rtl-433-hass/rtl_433` and choose
   the **Integration** category, then **Add**.
4. Search for **rtl_433** in HACS, open it, and click **Download**.
5. **Restart Home Assistant.**

### Manual

1. Copy the `custom_components/rtl_433` directory from this repository into your
   Home Assistant `<config>/custom_components/` directory, so you end up with
   `<config>/custom_components/rtl_433/`.
2. **Restart Home Assistant.**

## Configuration

Add a hub from **Settings → Devices & Services → Add Integration → rtl_433**.
Each hub points at one rtl_433 server's WebSocket endpoint.

| Field | Default | Description |
| --- | --- | --- |
| **Host** | *(required)* | Hostname or IP of the machine running rtl_433. |
| **Port** | `8433` | The rtl_433 HTTP-API port (`-F http` default). |
| **Path** | `/ws` | The WebSocket path on the rtl_433 HTTP server. |
| **Secure** | off | When on, connect with `wss://` instead of `ws://` (TLS). |
| **Manage rtl_433 settings from Home Assistant** | on | When on, expose SDR controls on the hub and let Home Assistant adopt and enforce the receiver's settings. See [Managing SDR settings from Home Assistant](#managing-sdr-settings-from-home-assistant). |
| **Discover new devices** | on | When on, newly observed devices on this server are added automatically. Turn off to start with discovery disabled; changeable later in the hub options. |
| **Initial frequency (MHz)** | `433.92` | The receiver's center frequency in MHz, pre-filled with the common 433.92 MHz band. The value you enter is applied **once** at first connect and takes priority over the frequency the server is currently using. Only applies when **Manage rtl_433 settings from Home Assistant** is on. |

The integration validates that it can reach the WebSocket before creating the
hub. The hub's identity is derived from `host:port`, so the same server cannot
be added twice.

The **Manage rtl_433 settings from Home Assistant** toggle can be changed later
from the hub options (see [Editing options](#editing-options)).

### Automatic discovery (Home Assistant OS add-on)

If you run the
[rtl_433 add-on](https://github.com/rtl-433-hass/rtl_433-hass-addons) on Home
Assistant OS, each radio it detects is published to Home Assistant's Supervisor
discovery and shows up under **Settings → Devices & Services** as a discovered
**rtl_433** card. Click **Add** and confirm; the radio is configured
automatically — no host or port to type. The confirm step offers the same
optional setup choices as the manual flow (**Manage rtl_433 settings**,
**Discover new devices**, and an **Initial frequency** in MHz).

- **Stable across restarts and port changes.** A discovered radio keeps the same
  config entry (and its nested-device history) across add-on restarts and USB
  port reassignments, because the add-on advertises a stable per-radio
  identifier. Multi-dongle stability is best when each dongle stays in a fixed
  USB port or is flashed with a unique serial; see the
  [add-on](https://github.com/rtl-433-hass/rtl_433-hass-addons) for the
  hardware-identity details.
- **Manual setup still works.** Entering host/port yourself (above) remains fully
  supported for remote or non-add-on rtl_433 servers, and a radio already added
  via discovery is never added a second time.

To point an existing hub at the same server's new address, open **Settings →
Devices & Services → rtl_433 → the hub → Reconfigure** and update the
host/port/path/secure connection target in place; nested devices and their
history are preserved. Use **Reconfigure** for the connection target, and
**Configure** (options) for the discovery toggle, availability timeouts, and the
**Manage rtl_433 settings from Home Assistant** toggle.

### `ws://`, `wss://`, and authentication

- By default the connection is plain **`ws://host:port/path`**.
- Turning on the **Secure** toggle makes it **`wss://`** (TLS). rtl_433's own
  HTTP server does not terminate TLS, so to use `wss://` you put a TLS
  **reverse proxy** (for example nginx or Caddy) in front of rtl_433 and point
  the hub at the proxy.
- There is **no in-integration authentication**. rtl_433's HTTP-API is
  unauthenticated; if you need access control, place it behind a reverse proxy
  or restrict it on your network. The integration sends no credentials.

## Discovery

RF devices appear automatically as **nested devices under the hub** — there is
no card to accept or dismiss:

- When the hub decodes a device it does not yet know, it **adds it
  automatically** as a Home Assistant device under the hub, along with its
  sensor / binary_sensor entities, and raises an **in-app persistent
  notification** (stable per-device id, so a deleted device that later
  re-appears replaces its notification rather than duplicating it). Restarting
  Home Assistant does **not** re-notify for already-known devices.
- **Only devices heard after connecting are added.** On connect the rtl_433
  server replays its recent backlog; the hub ignores those older messages for the
  purpose of adding devices, so a new hub does **not** get flooded with everything
  the receiver decoded before you connected. A previously-unknown device is added
  the first time it transmits *after* the connection. (This relies on the rtl_433
  server and Home Assistant clocks being roughly in sync.)
- To get rid of an unwanted device, open it under **Settings → Devices &
  Services → rtl_433 → the device → Delete**. There is no persistent ignore
  list: with discovery **on**, a deleted device **re-appears** the next time it
  transmits. To keep it gone, turn the hub's discovery toggle off first.

Each hub has its own **discovery toggle** (see options below). Turning discovery
**off** stops new devices on that hub from being added; devices that already
exist keep updating. Turning it back **on** lets new (and previously deleted)
devices appear again as they transmit.

## Availability

RF devices announce their presence only by transmitting, so the integration uses
a silence-based availability model: if no event for a device arrives within its
**availability timeout**, its entities become `unavailable`.

**Two transmit cadences.** How long a device can reasonably stay silent before
"silent" really means "gone" depends on what kind of device it is:

- **Periodic transmitters** (weather / temperature / soil / air quality) send on
  a fixed cadence regardless of any event, so a short timeout suits them — a few
  missed transmissions is a real problem.
- **Event-driven devices** (door/window contacts, motion/PIR, security sensors)
  only transmit on an event, plus at most an occasional supervision heartbeat.
  Long silences are *normal* — a short timeout would flap them to `unavailable`
  constantly. Some cheap generic sensors send **no heartbeat at all**.

| Device | Type | Typical cadence |
| --- | --- | --- |
| Acurite temperature | Periodic | ~16 s |
| Ecowitt / Fine Offset WH51 soil | Periodic | ~72 s |
| Ecowitt WH41 air quality | Periodic | ~10 min |
| GE / Interlogix motion | Event-driven | heartbeat ~1 h |
| Honeywell 5800 contacts | Event-driven | heartbeat ~70–90 min |
| Generic EV1527 door / PIR | Event-driven | **none** — silent for days |
| TPMS (parked vehicle) | Event-driven | **none** — silent until driven |

- **Hub default** — set on the hub options flow (**Hub settings**). When you
  leave it unset, the timeout is chosen **per device by its Home Assistant device
  class** (see below). If you set it explicitly, your value becomes the default
  for every device on the hub that has no per-device override.
- **Device-class-aware defaults** — when no explicit timeout applies, the
  integration picks the default from the device's class: **event-driven** devices
  (door, window, opening/contact, motion, plus buttons/doorbells) **never expire**
  — once seen they stay available, since they only transmit on an event and have
  no reliable check-in, so any finite timeout would eventually flap them (and
  their battery and other entities) to `unavailable`. **Periodic** sensors keep
  **600 seconds (10 minutes)**. This applies **automatically**, including to
  existing installs that never customized the hub timeout; any explicit per-device
  or hub value you have set is always preserved.
- **Never expire** — set the timeout to `0` (as the hub default or a per-device
  override) and the device is **never** marked `unavailable`. This is the
  automatic class default for event-driven devices; set it explicitly to extend
  the same behavior to a periodic device (e.g. a slow reporter you do not want
  flagged), or per-hub for everything.
- **Per-device override** — set through the hub options flow (**Device
  settings**): pick a device and give it an optional timeout (including `0`) that
  overrides the hub default for that one device. Leave it empty to clear the
  override and fall back to the hub default (or, if that is unset, the
  device-class default).
- **Resolution order** — per-device override → hub default (if you set one) →
  device-class default → 600 s fallback.
- **Restart behavior** — on a Home Assistant restart, the last known states are
  **restored first**, then the timeout runs from the restart; entities only flip
  to `unavailable` once the (restored) silence window elapses without a fresh
  event.
- **Last seen** — every device also gets a diagnostic `timestamp` sensor named
  **Last seen** that reports when the device was last heard from. It is
  **enabled by default for event-driven devices** (door/window contacts,
  motion/PIR, buttons, doorbells) — they never expire, so their availability no
  longer signals freshness and this timestamp is the only such signal. For
  periodic devices it ships **disabled by default** (their `available` already
  conveys freshness); enable it from the device page when you want it. Unlike the
  measurement sensors — which become `unavailable` after the availability
  timeout elapses with no transmission — the Last seen sensor **stays
  available** and keeps showing the last-heard time, so you can build "no signal
  for N minutes" staleness alerts and dashboards against it. It restores its
  previous value across restarts.
- **Event entities** — momentary, fire-and-forget RF (remote buttons,
  doorbells, key fobs) becomes a native HA **event** entity rather than
  a sensor with a faked "off". Each transmission fires one event whose type is
  the transmitted value; like the Last seen sensor, event entities **stay
  available** between presses instead of going `unavailable`. The shipped
  mappings are in
  [`device_library/events.yaml`](docs/device-library.md#event-entities).
- **Motion / occupancy** — detect-only PIR sensors (which send a trip but never
  an "off") become an occupancy `binary_sensor` that auto-clears to off a set
  time after the last detection (default 90 s, tunable per device in the device
  options). See
  [Motion / occupancy](docs/device-library.md#motion--occupancy).
- **No late event replays** — on reconnect or a Home Assistant restart, rtl_433
  replays its recent event history. Momentary RF events that occurred **while HA
  was disconnected** are intentionally **not re-fired**, so a doorbell press
  from an hour ago can't trigger your automations late (they are logged at
  INFO instead). Their latest readings still seed the corresponding sensors. No
  configuration; nothing to set up.

Both options apply **live** — changing the discovery toggle or a timeout takes
effect without reloading the hub or tearing down the WebSocket.

### Editing options

Open **Settings → Devices & Services → rtl_433 → Configure** on the hub. The
options flow presents a menu:

- **Hub settings** — the **discovery toggle**, the **default availability
  timeout**, and the **Manage rtl_433 settings from Home Assistant** toggle for
  this server.
- **Device settings** — pick a known device and set or clear its **per-device
  availability-timeout override** and, for utility meters, its **consumption
  calibration** (see [Utility-meter calibration](#utility-meter-calibration)).

Changing the **Manage rtl_433 settings from Home Assistant** toggle reloads the
hub (the SDR controls appear or disappear); changing the discovery toggle or a
timeout applies live.

To instead change the hub's connection target (host/port/path/secure) — the
same server at a new address — use **Reconfigure** rather than **Configure**;
nested devices and their history are preserved (see
[Configuration](#configuration)).

## Per-device signal diagnostics

When rtl_433 is configured to report **level data**, each event carries the
radio's per-transmission **frequency**, **RSSI**, **SNR**, and **noise**. The
integration maps these to four **diagnostic** sensors on the device —
`Frequency` (MHz), `RSSI`, `SNR`, and `Noise` (dB) — that are **disabled by
default**. Enable the ones you want from the device page (or in the entity
settings) to chart signal strength and reception quality over time, e.g. to find
a better antenna placement.

These fields are only present when rtl_433 emits level data:

- **Using the [rtl_433 Home Assistant add-on](https://github.com/rtl-433-hass/rtl_433-hass-addons)?**
  Update to a version that enables level reporting (`report_meta level`, the
  config-file equivalent of the `-M level` CLI flag); recent versions do this out
  of the box.
- **Running rtl_433 yourself?** Start it with `-M level` (or add
  `report_meta level` to your rtl_433 config file).

If your rtl_433 does not report level data, these four sensors simply never
appear — nothing else is affected, so the integration works the same on older
setups.

## Debug logging

If a device fires a duplicate, spurious, or late event — or an automation
triggers more often than the physical device transmits — turn on DEBUG logging
for the integration. It emits a compact trace for every event frame so you can
see exactly what the integration received and what it did with it.

Add this to your Home Assistant `configuration.yaml` and restart (or call the
`logger.set_level` service for a live change):

```yaml
logger:
  logs:
    custom_components.rtl_433: debug
```

Every decoded event frame logs one **ingestion** line, followed (for event
entities) by a **fired** or a suppression line:

| Log line | Meaning |
| --- | --- |
| `rtl_433 RX <device> fields=... time=... -> LIVE` | A genuine live transmission. Refreshes availability; event entities fire. (`LIVE (no-timestamp)` is a frame with no parseable `time`, also treated as live.) |
| `... -> REPLAY (event_time<=high_water)` | An already-seen frame from the server's reconnect replay. Seeds sensor values only; does **not** re-fire events or refresh availability. |
| `... -> STALE-GAP (age>threshold)` | A frame that occurred while Home Assistant was disconnected and is older than the staleness threshold. Suppressed (no fire). |
| `... -> BACKLOG (pre-connection)` | A replayed frame timestamped before this connection began. Suppressed (no fire). |
| `rtl_433 fired <type> for <device> field=<f> value=<v>` | An event entity fired this event type for a live transmission. |
| `rtl_433 skipped watchdog re-paint for <device> (no re-fire)` | The availability watchdog re-painted a cached event; it was deduped and did **not** fire. |
| `rtl_433 suppressed replayed/stale ...` (INFO) | A real-but-stale event that *would* have fired was suppressed (logged at INFO so you see the late event). |

Example of one live doorbell press:

```
rtl_433 RX Acme-Doorbell/42 fields={'button': 1} time=2026-06-15T20:00:01 -> LIVE (event_time>high_water)
rtl_433 fired ring for Acme-Doorbell/42 field=button value=1
```

Use the trace to attribute a duplicate or spurious event to its source:

- **Two `LIVE` ingestion lines for one physical press** → the duplicate is
  coming from **rtl_433** (a bad decode or a device that transmits the same
  press twice). The integration is faithfully reporting two distinct
  transmissions.
- **`REPLAY` / `BACKLOG` (or `STALE-GAP`) on startup or reconnect, with no
  `fired` line** → the **integration correctly suppressed** a queued duplicate
  from the server's replay; no event fired, so this is working as intended.
- **A single `LIVE` line and a single `fired` line, but the automation runs
  multiple times** → the duplication is in your **automation** (e.g. multiple
  triggers, or a trigger that matches more than you expect), not in the
  integration or rtl_433.

## Hub entities

Besides the per-device sensors, each hub exposes its own **diagnostic entities**
on the hub device so you can watch the server itself:

- **Connectivity** (binary_sensor) — `on` while the hub's WebSocket connection is
  open, `off` otherwise. It flips `off` immediately when the server announces a
  shutdown, rather than waiting for a silence timeout.
- **SDR / meta diagnostics** (sensors) — the receiver's current configuration:
  **center frequency**, **sample rate**, **conversion mode**, **hop interval**,
  **gain** (an empty value reads `auto`), and **frequency correction** (ppm). The
  configured `frequencies` and `hop_times` arrays are exposed as attributes on
  the center-frequency sensor.
- **Server statistics** (sensors) — **decoded events** (cumulative; tolerates the
  server's counter resetting), **OOK frames**, **FSK frames**, and **enabled
  decoders**. The per-protocol `stats[]` breakdown and the `since` timestamp are
  exposed as attributes on the decoded-events sensor.

These hub sensors fetch their data over HTTP from the rtl_433 server's `/cmd`
endpoint at the **server root** — `http(s)://host:port/cmd` — independent of the
configured WebSocket path. If a reverse proxy exposes only the WebSocket path and
not `/cmd`, those sensors gracefully degrade to `unknown` while the event stream
and the connectivity sensor keep working. The statistics refresh periodically
while the hub is connected; the SDR/meta values are fetched on each (re)connect.

When **Manage rtl_433 settings from Home Assistant** is on (the default), the
five SDR/meta diagnostic **sensors** above (sample rate, conversion mode, hop
interval, gain, ppm) are replaced by the **controls** described below — the
center-frequency sensor stays, since it still reports the receiver's actual
tuned frequency.

### Managing SDR settings from Home Assistant

By default a new hub adopts and manages the receiver's SDR settings. With
**Manage rtl_433 settings from Home Assistant** on, the hub gains a set of
**controls** (under the hub device, in the config-entity category):

- **Center frequency** (number, MHz) — shown only when the receiver uses a
  **single** frequency (see the hopping note below). This control is the way to
  set or change the frequency **at any time**: editing it sends the retune
  immediately and Home Assistant re-applies it on every reconnect. The
  **Initial frequency** setup field is only a convenience that pre-seeds this
  value once on a brand-new hub.
- **Sample rate** (number, Hz)
- **Frequency correction** (number, ppm)
- **Gain** (number, dB) paired with an **Auto gain** switch — with Auto gain
  **on**, gain is set to automatic and the dB value is ignored; with it **off**,
  the **Gain** number's value is sent.
- **Conversion mode** (select: `native` / `si` / `customary`)
- **Hop interval** (number, seconds) — the dwell time per frequency, so it
  applies only when the receiver hops between **multiple** frequencies; with a
  single frequency there is nothing to hop between and the control is hidden.

What "managed" means:

- On the **first connect** Home Assistant **adopts** the server's current
  settings into its desired state, then **re-applies all managed settings on
  every reconnect**, so your values survive an rtl_433 restart. If you set an
  **Initial frequency** at setup, that value is applied **once** and takes
  priority over the adopted frequency, so the receiver tunes to the frequency you
  chose rather than the server's current one. After that the **Center frequency**
  control owns the value and your later changes are preserved.
  - The **Initial frequency** seed only fires for a hub that stored it at setup.
    A hub added **before** the field existed (or one where you left it at the
    default) has nothing to seed, so it simply adopts the server's current
    frequency — set yours with the **Center frequency** control. You do **not**
    need to remove and re-add the hub.
- **Home Assistant becomes the authority.** Once a hub is managed, change these
  settings **in Home Assistant**, not in the rtl_433 config file — Home
  Assistant re-applies its stored values on the next reconnect and will override
  a direct edit you made to the rtl_433 config.

**Re-syncing from the rtl_433 config (the only way).** There is deliberately no
"re-adopt" button or service. If you have changed the rtl_433 config directly
and want Home Assistant to pick up those values, do this dance:

1. Turn **Manage rtl_433 settings from Home Assistant** **off** (this clears
   Home Assistant's stored desired state).
2. **Restart rtl_433** so it loads its config.
3. Turn the toggle back **on** — on the next connect Home Assistant re-adopts
   the server's now-current settings from scratch.

**Requirements and caveats:**

- The controls need the server's **`/cmd` endpoint reachable** at the server
  root, `http(s)://host:port/cmd` (independent of the WebSocket path). Behind a
  reverse proxy that hides `/cmd`, commands cannot be sent.
- **Hopping setups** (more than one configured frequency) keep **center
  frequency unmanaged**, so Home Assistant never pins a hopping receiver to a
  single frequency — and the **Center frequency** control is hidden
  (unavailable) while the **Hop interval** control becomes available. With a
  single frequency it is the reverse. The frequency *list* itself can only be
  set in the rtl_433 config; the API has no command for it.
- **Multi-stage gain strings** are not supported by the single gain control —
  manage those through the rtl_433 config (or turn the toggle off).
- **Sample rate is independent of frequency.** rtl_433 does not widen the sample
  rate when it is retuned, so moving a single-frequency receiver into the upper
  ISM bands (≥ 800 MHz) while it is still at the default 250 kHz raises a
  dismissible advisory suggesting a wider **Sample rate** (e.g. 1024000 Hz). Many
  devices decode fine at 250 kHz, so it is only a hint — dismiss it if your setup
  works.

**Turning management off** removes all the controls, stops Home Assistant from
sending any commands, and clears its stored desired state; the six read-only
SDR/meta diagnostic [Hub entities](#hub-entities) sensors come back. The
receiver's settings are left untouched.

## Replacing a radio

If your RTL-SDR dongle dies, you can swap in a replacement without losing any of
your decoded devices, their history, or your automations — the hub config entry
is **re-pointed at the new radio in place**.

1. Remove the dead dongle and plug in the replacement (any USB port).
2. In the **rtl_433 add-on**, stamp the replacement with a fresh serial if needed
   (`force_randomize_serial` / `randomize_default_serial`), restart it, and note
   the new radio's **ID (`unique_id`)** and **`host:port`** from the add-on
   log/status.
3. In **Home Assistant**, either:
   - open the **"rtl_433 server unreachable"** repair card — it appears once the
     old radio stops responding — and enter the replacement's radio ID and
     connection details to re-point the hub; **or**
   - go to the rtl_433 hub → **Reconfigure** and enter the new **radio ID** (plus
     host/port if they changed). If discovery already created a duplicate hub for
     the new radio, the reconfigure adopts its identity and removes the duplicate.
4. All decoded sensors, their history, and your automations are preserved,
   because the hub keeps the same internal entry.

## Device library and user overrides

Device support is a set of themed YAML files (the **device library**) that map
each rtl_433 field name to a Home Assistant entity descriptor. Adding or
correcting a device is a small YAML change — no Python. The schema, file layout,
add-a-mapping workflow, and the diagnostics feedback loop are documented in the
contributor guide:

- **[docs/device-library.md](docs/device-library.md)** — the authoritative
  device-library reference.

You can extend or correct the shipped library for your own installation, without
editing the integration files and without touching disk, from the hub's options:

> **Settings → Devices & Services → rtl_433 → Configure → Device mappings**

This opens Home Assistant's built-in YAML editor pre-filled with that hub's
current overrides. Overrides use the same schema as the shipped library:
top-level keys are rtl_433 field names, values are entry mappings, and they may
include a `skip_keys:` list. Overrides win over shipped entries (full
replacement), new fields are added, and `skip_keys` are unioned.

**Overriding one device model.** A top-level key changes a field for *every*
device that emits it. To correct a mapping for a single model without touching
others, nest it under a `models:` block keyed by the exact rtl_433 `model`
string — a model-scoped entry always wins over a global one for that model:

```yaml
models:
  Acurite-Tower: # exact rtl_433 model string
    temperature_C:
      platform: sensor
      device_class: temperature
      unit_of_measurement: "°C"
      state_class: measurement
      name: Outdoor temperature # rename just this model's sensor
      value_transform: { round: 2 }
      object_suffix: T
```

Mapping overrides are **global or model-scoped** — they apply to all devices of a
model, never a single physical unit. Per-*instance* settings (availability
timeout, meter calibration, motion clear delay) live in **Device settings**
instead. See [model-scoped mappings](docs/device-library.md#model-scoped-mappings-models)
for the resolution order and a worked example.

Overrides are stored **per hub** — each hub has its own set. The editor blocks
invalid YAML and **validates the mapping schema on save**, rejecting bad input
with a per-field error rather than silently dropping it; on save the hub
**reloads automatically** so the change takes effect with no restart. If you had
a `<config>/rtl_433_mappings.yaml` from an earlier version, it is **imported once
into each existing hub** on upgrade and then ignored (the file is left on disk,
untouched). See [User overrides](docs/device-library.md#user-overrides) for the
details and examples.

## Utility-meter calibration

Utility meters (electricity, gas, and water meters decoded by the SCM / ERT /
SCMplus protocols) report a raw **consumption counter**, but the RF signal does
**not** carry that counter's unit or scale — different meters report in different
granularities (e.g. some in 1 kWh, others in 10 Wh), so the integration cannot
derive it automatically. Out of the box the consumption sensor is therefore a
plain, unitless `total_increasing` counter, which is **not** eligible for Home
Assistant's Energy dashboard.

To make it Energy-dashboard-eligible, calibrate the device. Open **Settings →
Devices & Services → rtl_433 → Configure → Device settings**, pick the meter, and
set its **consumption calibration**:

- **Commodity** — `none` / `energy` / `gas` / `water`. This sets the sensor's
  `device_class`. Choosing `none` clears any calibration and the sensor reverts
  to the unitless counter. When the meter reports a `MeterType` (or `ert_type`)
  hint, the commodity is **pre-filled** from it; you can always override it.
- **Base unit** — the unit the calibrated counter is expressed in, constrained to
  the units Home Assistant recognizes as convertible for that commodity
  (energy → Wh/kWh/MWh; gas/water → m³/ft³/L/…). Picking a convertible base unit
  is what makes the sensor Energy-dashboard-eligible.
- **Scale** — a multiplier applied to the raw counter so the stored value is in
  the chosen base unit (raw × scale).

Once calibrated, the consumption sensor gains a real `device_class`, the base
unit, and `state_class: total_increasing`, so you can add it to the **Energy
dashboard**. You do **not** need to pick your display unit here: with a
convertible base unit set, Home Assistant does its **own per-entity display-unit
conversion** — switch a water meter from L to gal (or a gas meter between m³ and
ft³) in the entity's settings and HA converts it for you. The integration ships
no conversion engine of its own.

> **Recalibration orphans prior long-term statistics.** Changing the commodity,
> base unit, or scale changes the sensor's native unit / device class, which Home
> Assistant treats as a non-convertible change to a counter it has been recording.
> The entity keeps its ID, but its **previous long-term statistics are orphaned**
> (and the first time a previously-unitless sensor gains a unit the recorder may
> flag the change once). This is inherent to Home Assistant, not specific to this
> integration — calibrate intentionally, ideally once. Saving a calibration
> reloads the hub so the sensor is rebuilt with the new unit/class.

For models whose unit/scale *is* known, a contributor can ship a model-scoped
mapping in the [device library](docs/device-library.md#model-scoped-mappings-models)
so those meters work with no per-device calibration at all.

## Screenshot gallery

These captures are produced by the containerized harness (see
[tests/integration/README.md](tests/integration/README.md)) replaying a real
Acurite capture.

### Device page

An auto-added **Acurite-Tower** device, nested under the hub, with its
temperature, humidity, and battery sensors, plus RSSI / SNR / noise diagnostics.

![Acurite-Tower device page showing Temperature 26.7 °C, Humidity 74.0%, Battery 100%, and signal diagnostics](docs/images/02-device-page.png)

### Hub options flow

The hub options flow (**Configure** on the hub) opens a menu: the discovery
toggle and default availability timeout live under **Hub settings**, per-device
timeout overrides and calibration under **Device settings**, and the per-hub
mapping overrides under **Device mappings**.

![Hub options flow menu showing Hub settings, Device settings, and Device mappings](docs/images/03-options-flow.png)

### Device mappings

The **Device mappings** step opens Home Assistant's built-in YAML editor
pre-filled with that hub's current overrides. Edit the per-hub mappings as
YAML — the schema matches the shipped library — and the hub reloads
automatically on save. See [User overrides](docs/device-library.md#user-overrides).

![Device mappings step showing the YAML editor pre-filled with an example override that adds a custom field and re-classifies battery_ok as a low-battery problem sensor](docs/images/05-mapping-overrides.png)

### Unavailable state

After the stream stops and the availability timeout elapses, the device's
entities flip to `unavailable`.

![Device entities showing the unavailable state after the availability timeout](docs/images/04-unavailable-state.png)

## Multiple servers (instances)

You can add **one hub per rtl_433 server**. Each hub:

- Owns its own WebSocket connection and discovery toggle.
- Scopes device identities to itself — unique IDs are
  **instance-scoped** (`<hub-entry-id>:<device-key>`) — so two servers that
  decode the same model + id produce distinct entities and never collide.

**Architecture:** there is **one config entry per server** (the hub). The RF
devices it decodes are **device-registry devices nested under that hub entry**,
not separate config entries — the same shape Home Assistant's core `rfxtrx`
integration uses. Deleting the hub removes all of its nested devices and
entities, leaving no orphans; deleting a single device (from its device page)
removes just that one.

**Upgrading from 0.1.0:** the upgrade is **seamless and in place** — no
uninstall. On first start the integration re-homes your existing devices and
entities onto the hub entry, preserving their entity IDs and history, so
dashboards and automations keep working.

**Breaking change — motion is now a binary_sensor.** Motion is now an occupancy
`binary_sensor` (with a synthesized auto-off) instead of an event entity, so its
entity_id changes from `event.*_motion` to `binary_sensor.*_motion`. Update any
automations, dashboards, or scripts that referenced the old `event.*_motion`
entity. On upgrade the integration removes the orphaned old entity and raises a
one-time repairs issue flagging the move.

## Development and links

- **Device-library contributor guide:** [docs/device-library.md](docs/device-library.md)
- **AI-agent / maintenance notes:** [AGENTS.md](AGENTS.md)
- **Contributing (commits, releases, CI):** [CONTRIBUTING.md](CONTRIBUTING.md)
- **Integration & screenshot harness:** [tests/integration/README.md](tests/integration/README.md)
- **Issue tracker:** <https://github.com/rtl-433-hass/rtl_433/issues>

Run the unit tests locally (dependencies are managed with
[uv](https://docs.astral.sh/uv/)). The test stack pins Home Assistant 2026.4+,
which **requires Python 3.14** — newer than many distros ship — so install that
interpreter through uv first (no root or system Python changes needed) and pin
the virtualenv to it:

```bash
uv python install 3.14          # standalone CPython 3.14, managed by uv
uv venv --python 3.14           # create .venv on 3.14 (omitting --python may pick an older system Python and fail to install)
uv pip install -r requirements_test.txt
uv run pytest tests/
```
