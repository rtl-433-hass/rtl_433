# rtl_433 WebSocket API

This document describes the WebSocket control/streaming API exposed by the
rtl_433 HTTP server. It is derived from the implementation in `src/http_server.c`
(`ev_handler`, `json_parse`, `rpc_exec`, `rpc_response_ws`).

A second, unrelated API is documented at the end:
[Home Assistant discovery commands](#home-assistant-discovery-commands), the
commands this integration adds to *Home Assistant's* own WebSocket API.

The WebSocket API shares its command dispatcher (`rpc_exec`) with the `/cmd` and
`/jsonrpc` HTTP endpoints, so the command set is identical across all three; only
the framing differs.

## Starting the server

```sh
rtl_433 -F http                       # bind 0.0.0.0:8433 (all interfaces)
rtl_433 -F http://127.0.0.1:8433      # bind localhost only
rtl_433 -F http:127.0.0.1             # localhost, default port 8433
```

`-F http[:[//]bind[:port]]` uses default bind `0.0.0.0` and default port `8433`.

## Connecting

Open a WebSocket to the server root:

```text
ws://<host>:<port>/
```

The server runs HTTP and WebSocket on the same port; any request carrying a
WebSocket `Upgrade` is handled as a WS connection regardless of path.

Example with [`websocat`](https://github.com/vi/websocat):

```sh
websocat ws://127.0.0.1:8433/
# then type a command and press enter:
{"cmd":"get_center_frequency"}
```

### On connect

Immediately after the handshake the server pushes, as text frames:

1. A **`meta`** object describing current configuration. See
   [meta object](#meta-object).
2. A replay of up to the **last 100 events** from the in-memory history ring
   buffer (`DEFAULT_HISTORY_SIZE`).

After that, the connection continuously receives every decoded event/state as it
occurs. See [Event stream](#event-stream).

## Request format (client → server)

Each command is a single JSON object in one text frame:

```json
{"cmd": "<command>", "arg": "<string>", "val": <integer>}
```

| Field | Type | Notes |
| --- | --- | --- |
| `cmd` | string | **Required.** The command name. |
| `arg` | string | Optional string argument, such as gain value or meta selector. |
| `val` | integer | Optional. Parsed with `strtol` base-10 into a `uint32_t`. Non-integers/floats are truncated; negative values wrap to large unsigned. |

Parsing limits: the JSON tokenizer accepts at most 16 tokens, so keep payloads
small and flat. Unknown keys are ignored with a server-side log warning.

## Response format (server → client)

Responses are JSON text frames. There is no request/response correlation ID,
unlike `/jsonrpc`, and responses are interleaved with the unsolicited event
stream. A client must be prepared to receive event frames at any time.

| Result kind | Frame |
| --- | --- |
| Success, string value | `{"result": "<string>"}` |
| Success, no value | `{"result": null}` |
| Success, signed integer | `{"result": <int>}` |
| Success, unsigned integer | `{"result": <uint>}` |
| Success, JSON payload | Over WebSocket, the raw JSON object/string is sent directly, not wrapped in `result`. Used by `get_stats`, `get_meta`, `get_protocols`, `get_dev_info`. Over HTTP `/cmd` and `/jsonrpc`, these are wrapped in `result`. |
| Error, command rejected | `{"error": {"code": <int>, "message": "<msg>"}}` |
| Error, JSON parse failed | `{"error":"Invalid command"}` |

!!! note "Framing difference for JSON-payload getters"
    The WebSocket responder (`rpc_response_ws`) sends
    `get_stats`/`get_meta`/`get_protocols`/`get_dev_info` as bare JSON frames,
    but the HTTP `/cmd` responder (`rpc_response_jsoncmd`) wraps every reply in
    `{"result": ...}`. A client polling over `/cmd` must unwrap `result` for
    all getters, not just scalar ones.

## Commands

### Queries (getters)

Return the current value with no side effects.

| `cmd` | Returns |
| --- | --- |
| `get_dev_query` | Device query string (`{"result": ...}`). |
| `get_dev_info` | Device info string, sent as a raw frame. |
| `get_gain` | Gain string (`{"result": ...}`). |
| `get_ppm_error` | `{"result": <int>}`. |
| `get_hop_interval` | `{"result": <int>}` for the first hop time. |
| `get_center_frequency` | `{"result": <uint>}`. |
| `get_sample_rate` | `{"result": <uint>}`. |
| `get_grab_mode` | `{"result": <int>}`. |
| `get_raw_mode` | `{"result": <int>}`. |
| `get_verbosity` | `{"result": <int>}`. |
| `get_verbose_bits` | `{"result": <int>}`. |
| `get_conversion_mode` | `{"result": <int>}`. |
| `get_stats` | Report/statistics JSON, sent as a raw frame. |
| `get_meta` | [meta object](#meta-object), sent as a raw frame. |
| `get_protocols` | [protocols object](#protocols-object), sent as a raw frame. |

Getters reflect live `cfg` values. Some, such as `get_dev_info`, may be empty or
unset when no SDR device is open, such as `-D manual`.

### Live SDR control (applied immediately)

These call into the SDR driver and take effect on the running receiver. Each
returns `{"result": "Ok"}` on success.

The Home Assistant integration exercises these live SDR controls and the
configuration-setter commands below over `/cmd` for its managed SDR controls.

| `cmd` | Argument | Effect |
| --- | --- | --- |
| `center_frequency` | `val` in Hz | Retune center frequency. |
| `sample_rate` | `val` in Hz | Set sample rate. |
| `ppm_error` | `val` | Set frequency correction in ppm. |
| `gain` | `arg` string, e.g. `"32.8"` or empty for auto | Set tuner gain. Returns `Missing arg` if `arg` is absent. |

### Configuration setters (applied on next use)

Mutate configuration fields; return `{"result": "Ok"}`.

| `cmd` | Argument | Effect |
| --- | --- | --- |
| `hop_interval` | `val` seconds | Set frequency-hop interval. |
| `convert` | `val` | Set unit conversion mode (`native`, `si`, `customary`) as an integer. |
| `raw_mode` | `val` | Set raw mode. |
| `verbosity` | `val` | Set log verbosity. |
| `verbose_bits` | `val` | Set bit-row verbosity. |
| `report_meta` | `arg` + `val` | Configure output metadata. |

`report_meta` selects a sub-setting via `arg`:

| `arg` | Effect |
| --- | --- |
| `time` | Timestamps as date. |
| `reltime` | Timestamps as sample offset. |
| `notime` | Timestamps off. |
| `hires` | High-resolution time = `val`. |
| `utc` | UTC time = `val`. |
| `protocol` | Report protocol number = `val`. |
| `level` | Report signal level = `val`. |
| `bits` | Bit-row verbosity = `val`. |
| `description` | Report description = `val`. |
| any other or absent value | Report meta level = `val`. |

A missing `arg` returns a `Missing arg` error.

### Stubs / not implemented

| `cmd` | Behavior |
| --- | --- |
| `protocol` | No-op. Returns `{"result": "Ok"}` but does nothing because decoder enable/disable is not wired up. |
| `device` | Returns `{"error": {"code": -1, "message": "Not implemented"}}`, or `Missing arg` if `arg` is absent. |

Unknown commands return `{"error": {"code": -1, "message": "Unknown method"}}`;
an empty or invalid `cmd` returns `Method invalid`.

## Event stream

After connecting, all decoded output is broadcast to the WebSocket as text
frames:

- **Events**: JSON objects containing a decoded record, including a `model` key
  plus device fields, such as
  `{"time":"...","model":"...","id":...,"temperature_C":...}`.
- **States**: larger JSON objects emitted for periodic statistics/state.
- **Log frames** (rtl_433 ≥ 23.11): the server's own log output, forwarded as
  `{"time":"...","src":"<subsystem>","lvl":<int>,"msg":"<text>"}` objects. The
  HTTP output consumes all log levels, but the server's global verbosity
  (default `WARNING`; raise with `-v`) gates what is generated. Notably the
  pulse detector's noise estimates (`src` `"Auto Level"`, from `-Y autolevel`
  adjustments and `-M noise[:secs]` periodic reports) arrive this way — the
  only place rtl_433 surfaces its noise floor; there is no structured getter.

On server shutdown each WebSocket receives `{"shutdown":"goodbye"}`.

WebSocket connections do not receive the CRLF keep-alive used by the `/events`
and `/stream` HTTP endpoints.

## Reference objects

### meta object

Sent on connect and via `get_meta`:

```json
{
  "frequencies": [...],
  "hop_times": [...],
  "center_frequency": 433920000,
  "duration": 0,
  "samp_rate": 250000,
  "conversion_mode": 0,
  "fsk_pulse_detect_mode": 0,
  "after_successful_events_flag": 0,
  "report_meta": 0,
  "report_protocol": 0,
  "report_time": 0,
  "report_time_hires": 0,
  "report_time_tz": 0,
  "report_time_utc": 0,
  "report_description": 0,
  "report_stats": 0,
  "stats_interval": 0
}
```

The meta object carries neither gain nor ppm. Read those from `get_gain`
(string; empty means auto) and `get_ppm_error` (int) instead.

### stats object

Returned by `get_stats`, sent as a raw frame:

```json
{
  "enabled": 234,
  "since": "2024-01-01T00:00:00",
  "frames": { "count": 0, "fsk": 0, "events": 0 },
  "stats": [ /* per-protocol stat entries */ ]
}
```

`enabled` is the count of enabled decoders; `frames.count` is OOK frames,
`frames.fsk` is FSK frames, and `frames.events` is the cumulative decoded-event
count. It may reset when the server restarts.

### protocols object

Returned by `get_protocols`. Contains a `protocols` array; each registered
protocol entry includes:

| Field | Meaning |
| --- | --- |
| `num` | Protocol number, omitted for dynamic/flex decoders. |
| `name` | Protocol name. |
| `mod` | Modulation ID. |
| `short`, `long`, `reset`, `gap`, `sync`, `tolerance` | Timing parameters. |
| `fields` | Array of output field names. |
| `def` | Enabled by default, `0` or `1`. |
| `en` | Currently enabled, `0` or `1`. |
| `verbose`, `verbose_bits` | Per-decoder verbosity. |

## Related endpoints (same command set)

| Endpoint | Transport | Notes |
| --- | --- | --- |
| `ws://host:port/` | WebSocket | This API. |
| `/cmd` | HTTP GET query or POST form | `cmd`, `arg`, `val` as parameters. |
| `/jsonrpc` | HTTP POST | JSON-RPC 2.0 (`method`, `params`, `id`). |
| `/events` | HTTP chunked stream | Event stream only, no commands. |
| `/stream` | HTTP plain stream | Event stream only, no commands. |
| `/metrics` | HTTP GET | OpenMetrics/Prometheus exposition. |

## Security characteristics

The HTTP/WebSocket server has no authentication or authorization and, by default,
binds to all interfaces (`0.0.0.0:8433`). CORS is fully open
(`Access-Control-Allow-Origin: *`). Any client that can reach the port can read
the decoded data stream and change live SDR settings. Traffic is plain HTTP.

This is intentional: upstream considers rtl_433 safe to use but not secure and
recommends it not be exposed to the internet. Bind to `127.0.0.1` and/or place a
reverse proxy with TLS and authentication in front if remote access is required.

## Home Assistant discovery commands

Everything above belongs to the rtl_433 server. This section is a different API:
the commands this integration registers on **Home Assistant's own** WebSocket API
at `ws://<home-assistant>:8123/api/websocket`. They back the discovery panel, and
they are equally usable from a script or from the browser's developer console —
the panel adds no logic of its own on top of them, so anything it can do, these
can do.

They are the programmatic form of [Device Discovery](device-discovery.md): see
what the receiver has heard, then add, ignore or un-ignore it — and, since the
panel became the integration's configuration page, read and write the hub's
settings too.

### Authentication

Connect and authenticate the way you would for any Home Assistant WebSocket
client (a long-lived access token). **Every command below requires an
administrator**, so a token issued for a non-admin user is refused:

```json
{"id": 5, "type": "result", "success": false,
 "error": {"code": "unauthorized", "message": "Unauthorized"}}
```

### Command summary

| `type` | Parameters | Returns |
| --- | --- | --- |
| `rtl_433/hubs` | — | Every configured hub, loaded or not. |
| `rtl_433/devices/pending` | `entry_id` | One hub's discovered devices and its ignore list. |
| `rtl_433/devices/add` | `entry_id`, `device_keys` | `applied` / `skipped` keys. |
| `rtl_433/devices/ignore` | `entry_id`, `device_keys` | `applied` / `skipped` keys. |
| `rtl_433/devices/unignore` | `entry_id`, `device_keys` | `applied` / `skipped` keys. |
| `rtl_433/devices/subscribe` | `entry_id` | A subscription pushing the `pending` payload on change. |
| `rtl_433/devices/replace` | `entry_id`, `device_key`, `replaces` | Re-points an existing device onto a candidate. |
| `rtl_433/devices/clear` | `entry_id` | Forgets every candidate; `cleared` counts them. |
| `rtl_433/settings/get` | `entry_id` | Everything the three settings forms render. |
| `rtl_433/settings/hub` | `entry_id`, `availability_timeout`, `manage_settings` | The hub's stored options. |
| `rtl_433/settings/device` | `entry_id`, `device_key`, + overrides | That device's stored settings. |
| `rtl_433/settings/mappings` | `entry_id`, `yaml` | The stored override document, re-rendered. |

`entry_id` is a hub's config-entry id, from `rtl_433/hubs`. `device_keys` is a
list, so one message can add or ignore several devices.

Errors are the usual `{"success": false, "error": {...}}` result:

| `error.code` | Meaning |
| --- | --- |
| `unauthorized` | The connection's user is not an administrator. |
| `not_found` | No rtl_433 hub has that `entry_id`. An entry belonging to another integration reads the same way — these commands never reach into one. |
| `not_loaded` | The hub exists but is not set up — reloading, or its server is unreachable. The discovered list lives in memory, so there is nothing to answer with until it loads. |
| `replace_failed` | The replacement cannot be made: an unknown survivor, or the same key on both sides. Distinct from `not_loaded` so a script can tell "retry in a moment" from "this request cannot work". |
| `invalid_mappings` | The submitted device-mapping document is not YAML, is not a mapping, or breaks the override schema. `error.message` carries every problem found. Nothing is stored. |

### `rtl_433/hubs`

Lists the hubs, so a caller can pick one. Hubs that failed to load are listed
too, flagged rather than hidden.

```json
{"id": 1, "type": "rtl_433/hubs"}
```

```json
{
  "id": 1,
  "type": "result",
  "success": true,
  "result": {
    "hubs": [
      {
        "entry_id": "01M1DJ2TAV2NPMPB2JA4ZHDR2P",
        "title": "rtl_433 (wsbridge)",
        "loaded": true
      }
    ]
  }
}
```

### `rtl_433/devices/pending`

Returns one hub's discovered devices, most recently heard first, together with
the keys it is ignoring.

```json
{"id": 2, "type": "rtl_433/devices/pending",
 "entry_id": "01M1DJ2TAV2NPMPB2JA4ZHDR2P"}
```

The `result`, with four of its six devices left out:

```json
{
  "pending": [
    {
      "key": "Acurite-Tower-12053-chC",
      "model": "Acurite-Tower",
      "count": 915,
      "signal": 39.134,
      "first_seen": "2026-09-01T04:01:51.881359+00:00",
      "last_seen": "2026-09-01T04:07:02.042143+00:00",
      "readings": [
        {"key": "humidity", "name": "Humidity", "value": 74.0,
         "display": "74.0%", "unit": "%", "platform": "sensor",
         "entity_category": null, "icon": "mdi:water-percent"},
        {"key": "temperature_C", "name": "Temperature", "value": 26.7,
         "display": "26.7 °C", "unit": "°C", "platform": "sensor",
         "entity_category": null, "icon": "mdi:thermometer"},
        {"key": "battery_ok", "name": "Battery", "value": 100,
         "display": "100%", "unit": "%", "platform": "sensor",
         "entity_category": "diagnostic", "icon": "mdi:battery"}
      ]
    },
    {
      "key": "LeakDetector-9-21",
      "model": "LeakDetector-9",
      "count": 39,
      "signal": null,
      "first_seen": "2026-09-01T04:01:56.472199+00:00",
      "last_seen": "2026-09-01T04:07:00.516721+00:00",
      "readings": [
        {"key": "detect_wet", "name": "Water sensor", "value": true,
         "display": "Wet", "unit": null, "platform": "binary_sensor",
         "entity_category": null, "icon": "mdi:water"},
        {"key": "battery_ok", "name": "Battery", "value": 100,
         "display": "100%", "unit": "%", "platform": "sensor",
         "entity_category": "diagnostic", "icon": "mdi:battery"}
      ]
    }
  ],
  "ignored": []
}
```

| Field | Meaning |
| --- | --- |
| `key` | The device key: the decoded model plus the id, channel and subtype it reported. This is the id every command below takes. |
| `model` | The model rtl_433 decoded. |
| `count` | Sightings since Home Assistant started. The list is memory-only, so this counts from the last restart or hub reload. |
| `signal` | The most recent message's SNR, or its RSSI when no SNR was reported, in dB. `null` when the server reports no levels (it needs `-M level`). |
| `first_seen`, `last_seen` | ISO 8601 timestamps. |
| `readings` | The most recent message, resolved through the device library into the entities adoption would create. Ordered as a device page orders them: readings first, then diagnostics, alphabetical within each. |

Each reading describes the entity that field would become:

| Key | Meaning |
| --- | --- |
| `key` | The rtl_433 field name, as it arrived in the frame. |
| `name` | The name Home Assistant will give the entity — the library descriptor's own `name`, else the translated device-class name from core's `entity_component` strings. |
| `value` | The value the entity will hold: a real `true`/`false` for a binary field, the mapped event type for an `event` field, and the scaled number for a sensor. |
| `display` | That value rendered as the entity's **state**, ready to print: `"74.0%"`, `"26.7 °C"`, `"Wet"`, `"Open"`. |
| `unit` | The unit the entity will report, or `null`. |
| `platform` | `sensor`, `binary_sensor` or `event`. |
| `entity_category` | `"diagnostic"` for the fields Home Assistant files under Diagnostic, else `null`. |
| `icon` | The icon Home Assistant will show, from core's own `icons.json` for the device class, or `null` when it describes none. |

`display` is worth using in preference to re-formatting `value`. It is built
from the same descriptor, unit and translated vocabulary the entity itself
uses, so it says `"Wet"` where a `moisture` sensor says Wet and `"Open"` where
an `opening` sensor says Open — not `true`/`false`, and not On/Off.

One known gap: Home Assistant converts a few device classes to the configured
unit system (for this library, wind speed, rainfall and pressure). `display`
does not, so on a US-customary installation those three read in metric here and
in imperial on the device page.

Two kinds of field are deliberately absent, because neither produces an entity
the user would see: one the device library does not map at all, and one it maps
with `enabled_by_default: false` (the `time`, `freq`, `rssi`, `snr` and `noise`
diagnostics). Read the raw frame from the device's own entities after adoption,
or from `rtl_433/events` above, if you need it verbatim.

`ignored` carries one `{"key", "model"}` per ignored device. The model is an
empty string for a device that was ignored while still pending, which is the
usual case — nothing is stored about a device that was never added.

### `rtl_433/devices/add`, `.../ignore`, `.../unignore`

The three actions. All take the same parameters and return the same shape, and
all do exactly what the equivalent options-flow step does — there is one
implementation behind both.

```json
{"id": 3, "type": "rtl_433/devices/add",
 "entry_id": "01M1DJ2TAV2NPMPB2JA4ZHDR2P",
 "device_keys": ["Acurite-Tower-12053-chC", "LeakDetector-9-21"]}
```

```json
{"applied": ["Acurite-Tower-12053-chC"], "skipped": ["LeakDetector-9-21"]}
```

`skipped` is not an error. It means the key was not in a state the action
applies to, and each command means something slightly different by that:

| Command | `applied` | `skipped` |
| --- | --- | --- |
| `add` | The device was created, with its entities. | The key was no longer discovered — usually already added, or ignored. |
| `ignore` | The key was added to the ignore list and dropped from the discovered list. | It was already ignored. |
| `unignore` | The key was taken off the ignore list. | It was not on it. |

`applied` means the key is no longer ignored, not that it is back on the
discovered list. The device returns there on its next transmission.

### `rtl_433/devices/subscribe`

Subscribes to one hub's discovered devices. The event payload is exactly what
`rtl_433/devices/pending` returns.

```json
{"id": 4, "type": "rtl_433/devices/subscribe",
 "entry_id": "01M1DJ2TAV2NPMPB2JA4ZHDR2P"}
```

The success result comes first, then the current payload immediately as the first
event — so a client never has to call `pending` as well — and then a new payload
whenever it changes:

```json
{"id": 4, "type": "result", "success": true, "result": null}
{"id": 4, "type": "event", "event": {"pending": [...], "ignored": [...]}}
```

!!! warning "One message per change, not one per transmission"
    A device that is already on the list transmitting again does **not** push a
    message. Those repeat sightings only age a row's `count` and `last_seen`, so
    they are coalesced: the payload is re-checked on a five-second timer and sent
    only when it actually differs from the last one sent. A receiver in a busy
    area decodes constantly, and a push per frame would flood every open
    connection.

    Membership changes — a device heard for the first time, or added, ignored or
    un-ignored — are pushed immediately.

    So a client must not count messages to count transmissions, and must not
    assume the counts it holds are current to the second. Read `count` and
    `last_seen` from the payload.

Unsubscribe the standard way, naming the subscription's id:

```json
{"id": 9, "type": "unsubscribe_events", "subscription": 4}
```

## Home Assistant settings commands

The same three forms the panel's settings pages render. They are ordinary
commands, so anything those pages do is scriptable — including the parts that
are awkward by hand, like setting the same calibration on a dozen meters.

### `rtl_433/settings/get`

Everything the three forms need, in one call — they are one screenful, and the
alternative is three round trips to fill controls the user may never open.

```json
{"id": 10, "type": "rtl_433/settings/get",
 "entry_id": "01M1DJ2TAV2NPMPB2JA4ZHDR2P"}
```

```json
{"id": 10, "type": "result", "success": true, "result": {
  "hub": {"availability_timeout": 600, "manage_settings": true},
  "defaults": {"availability_timeout": 600, "motion_clear_delay": 90},
  "devices": [
    {"device_key": "SCM-12345", "label": "SCM (SCM-12345) — gas detected",
     "model": "SCM", "timeout_override": null, "motion_clear_delay": null,
     "motion": false, "commodity": "gas", "calibration": null}
  ],
  "commodities": ["none", "energy", "gas", "water"],
  "commodity_units": {"gas": ["m³", "ft³", "L", "CCF"], "...": []},
  "mappings": "",
  "mappings_docs_url": "https://github.com/..."
}}
```

`commodity_units` travels in the payload rather than being a constant a client
carries, because which units Home Assistant will convert for a given commodity is
a fact about the integration's calibration table. A client with its own copy will
eventually offer a unit the entity build refuses, and nothing fails visibly — the
sensor just stops being eligible for the Energy dashboard.

`commodity` is a *suggestion*, not a stored value: it is the device's existing
calibration when it has one, and otherwise a guess from the `MeterType` /
`ert_type` fields of its last decoded frame.

### `rtl_433/settings/hub`

```json
{"id": 11, "type": "rtl_433/settings/hub",
 "entry_id": "01M1DJ2TAV2NPMPB2JA4ZHDR2P",
 "availability_timeout": 1800, "manage_settings": true}
```

!!! note "The default timeout is not stored"
    Submitting `availability_timeout` equal to the shipped default (600) *removes*
    the stored value rather than saving it, so the per-device-class defaults keep
    applying. That matters most for event-driven devices — a doorbell that has not
    rung in ten minutes is not unavailable. Any other value is stored as given,
    including `0`, which means "never expire".

### `rtl_433/settings/device`

Every override is optional and nullable, and the two mean the same thing: clear
it, falling back to the hub default or the library descriptor.

```json
{"id": 12, "type": "rtl_433/settings/device",
 "entry_id": "01M1DJ2TAV2NPMPB2JA4ZHDR2P",
 "device_key": "SCM-12345",
 "timeout_override": 1800,
 "commodity": "gas", "unit": "m³", "scale": 0.01,
 "motion_clear_delay": null}
```

The reply is that device's row from `rtl_433/settings/get`, rebuilt from what was
actually stored. Read the calibration back from it rather than assuming what you
sent: `{commodity, unit, scale}` is normalized on the way in, and a commodity of
`none`, an unknown one, or a unit that is not convertible for the commodity all
store *no* calibration at all.

`motion_clear_delay` only does anything on a device with a field that auto-clears
(`motion` is `true` in the payload for those); elsewhere it is a control with
nothing behind it.

### `rtl_433/settings/mappings`

The hub's [device-library overrides](device-library.md), as the YAML text the
documentation writes them in.

```json
{"id": 13, "type": "rtl_433/settings/mappings",
 "entry_id": "01M1DJ2TAV2NPMPB2JA4ZHDR2P",
 "yaml": "temperature_C:\n  platform: sensor\n  unit_of_measurement: K\n"}
```

An empty or whitespace-only document removes every override — clearing the editor
is how they are all removed. A document that will not store comes back as
`invalid_mappings` with every problem found joined into `error.message`, and the
overrides already stored are left exactly as they were.
