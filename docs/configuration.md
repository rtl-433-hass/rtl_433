# Configuration

There are two ways to create a hub: automatically through
[add-on discovery](#home-assistant-os-add-on-discovery) (recommended), or
[manually](#manual-configuration) for any other rtl_433 server. Each hub points
at one rtl_433 server's WebSocket endpoint.

## Home Assistant OS Add-On Discovery

If you run the
[rtl_433 add-on](https://github.com/rtl-433-hass/rtl_433-hass-addons) on Home
Assistant OS, each radio it detects is published through Supervisor discovery.
It appears under **Settings → Devices & Services** as a discovered **rtl_433**
card. Click **Add** and confirm; no host or port needs to be typed.

For discovery to work, this integration must already be installed and loaded
when the add-on starts — install the integration, restart Home Assistant, and
then start the add-on. If you started the add-on first and no card appeared,
restart the add-on so it republishes discovery.

Discovered radios use the add-on's stable per-radio identifier, so the same hub
and nested-device history can survive add-on restarts and USB port changes. For
multi-dongle setups, stability is best when each dongle stays in a fixed USB port
or has a unique serial.

## Manual Configuration

Add a hub from **Settings → Devices & Services → Add Integration → rtl_433**.

![The rtl_433 config flow form with host, port, WebSocket path, secure toggle, managed-settings and discovery toggles, and initial frequency](images/06-config-user.png)

| Field | Default | Description |
| --- | --- | --- |
| **Host** | required | Hostname or IP of the machine running rtl_433. |
| **Port** | `8433` | The rtl_433 HTTP API port. |
| **Path** | `/ws` | The WebSocket path on the rtl_433 HTTP server. |
| **Secure** | off | Connect with `wss://` instead of `ws://`. |
| **Manage rtl_433 settings from Home Assistant** | on | Expose SDR controls and let Home Assistant adopt and enforce receiver settings. |
| **Discover new devices** | on | Add newly observed devices automatically. |
| **Initial frequency (MHz)** | `433.92` | Center frequency to apply once on first connect when managed settings are enabled. |

The integration validates that the WebSocket can be reached before creating the
hub. Manual hub identity is derived from `host:port`, so the same server cannot
be added twice.

## Manual rtl_433 Configuration

The integration connects to rtl_433's HTTP/WebSocket server. Start rtl_433 with
HTTP output enabled, for example:

```sh
rtl_433 -F http
```

By default rtl_433 binds to `0.0.0.0:8433`. For localhost-only operation, use a
bind address such as:

```sh
rtl_433 -F http://127.0.0.1:8433
```

### Event Timestamps

Every time Home Assistant connects, rtl_433 re-broadcasts a short buffer of
recent events. The integration reads the `time` field on each frame to tell that
backlog apart from traffic it is hearing live, so devices that have gone quiet
are not marked available again and their event entities and device triggers do
not fire a second time.

That only works if the timestamps can be read. rtl_433 emits `time` as a JSON
string in every mode, and these forms are understood:

| rtl_433 setting | Example `time` | |
| --- | --- | --- |
| default | `2026-05-25 10:00:00` | Local wall clock, whole seconds. |
| `time:iso` | `2026-05-25T10:00:00Z` | ISO-8601, with an offset or `Z`. |
| `time:unix` | `1779706800` | Epoch seconds. |
| `time:off` | *(no field)* | **Timestamps off — see below.** |

Adding `usec` to any of them (`time:iso:usec`) adds a fractional part.

Epoch stamps are worth calling out: `time:unix` was unreadable before the
integration picked up pyrtl_433 0.4.0, and an unreadable stamp is treated the
same as no stamp at all. Servers configured that way were silently getting the
`time:off` behaviour below, and now work as intended with no change needed.

With **no readable timestamp** the integration cannot distinguish a replay from
a live transmission, so it treats every frame as live — the safe direction for a
real event, but it means the re-broadcast backlog is ingested afresh on each
reconnect.

The recommended setting is the most precise one:

```
report_meta time:iso:usec:tz
```

or, on the command line, `-M time:iso:usec:tz`. A sub-second stamp also lets the
integration separate two transmissions from the same device inside one second.

## Reconfigure vs Configure

Use **Reconfigure** to point an existing hub at the same server's new address:
host, port, path, or secure mode. Devices and their history are preserved.

Use **Configure** for hub options:

- **Hub settings**: discovery toggle, default availability timeout, and the
  managed-settings toggle.
- **Device settings**: per-device availability timeout, motion clear delay, and
  utility-meter calibration.
- **Device mappings**: per-hub mapping overrides.

![Hub options flow menu showing Hub settings, Device settings, and Device mappings](images/03-options-flow.png)

The **Hub settings** step configures discovery and the default availability
timeout for every device on the hub:

![Hub settings step with the discovery toggle, default availability timeout, and managed-settings toggle](images/07-hub-settings.png)

The **Device settings** step targets one device for a timeout override, motion
clear delay, or utility-meter calibration. You pick the device first, then
configure it — every field on the second form is pre-filled from the device you
picked, and fields that do not apply to it (such as the motion clear delay on a
non-motion device) are hidden:

![Device picker step, with a utility meter labelled with its detected commodity](images/13-device-picker.png)

![Device settings step with the availability timeout override and meter commodity selector](images/08-device-settings.png)

Changing discovery or timeout options applies live. Changing the managed-settings
toggle reloads the hub because the entity set changes.

## ws, wss, and Authentication

By default the integration connects to `ws://host:port/path`. Turning on
**Secure** connects with `wss://`.

rtl_433's built-in HTTP server does not terminate TLS. To use `wss://`, put a
TLS reverse proxy such as nginx or Caddy in front of rtl_433 and point the hub at
the proxy.

rtl_433's HTTP API is unauthenticated, and the integration sends no credentials.
If you need access control, restrict it on your network or place it behind a
reverse proxy.
