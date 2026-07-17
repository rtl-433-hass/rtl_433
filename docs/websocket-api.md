# rtl_433 WebSocket API

This document describes the WebSocket control/streaming API exposed by the
rtl_433 HTTP server. It is derived from the implementation in `src/http_server.c`
(`ev_handler`, `json_parse`, `rpc_exec`, `rpc_response_ws`).

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
