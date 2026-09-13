# rtl_433 for Home Assistant

[![CI - Test](https://github.com/rtl-433-hass/rtl_433/actions/workflows/test.yml/badge.svg)](https://github.com/rtl-433-hass/rtl_433/actions/workflows/test.yml)
[![CI - Lint](https://github.com/rtl-433-hass/rtl_433/actions/workflows/lint.yml/badge.svg)](https://github.com/rtl-433-hass/rtl_433/actions/workflows/lint.yml)
[![CI - Validate](https://github.com/rtl-433-hass/rtl_433/actions/workflows/validate.yml/badge.svg)](https://github.com/rtl-433-hass/rtl_433/actions/workflows/validate.yml)
[![hacs](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz)

A Home Assistant custom integration that connects to an
[rtl_433](https://github.com/merbanan/rtl_433) HTTP server's WebSocket stream and
turns decoded 433 MHz / ISM-band devices into native Home Assistant sensors,
binary sensors, and event entities.

It is a local-push integration: events arrive over WebSocket as rtl_433 decodes
them, with no cloud dependency and no polling.

**Full documentation:** <https://rtl-433-hass.github.io/rtl_433/latest/>

## Receivers, Radios, and Locations

**A receiver is a computer running rtl_433; it contains a radio.** The radio is
the SDR dongle that receives the airwaves; the receiver is the machine — a Raspberry
Pi, a NAS, your Home Assistant box — running the `rtl_433` process that decodes
what the radio picks up and serves it over a WebSocket.

Home Assistant groups receivers into a **location**: one integration entry
holding one receiver per rtl_433 server. A location is a place whose receivers
can plausibly hear the same sensors — a house, a barn and its yard, one
apartment. Set-up creates the location and its first receiver together, and
**Add a receiver** on the integration page adds the rest.

Receivers in one location are **unioned**. A sensor two of them receive is a single
Home Assistant device with a single set of entities, updating whenever *any*
receiver receives it — so a weather station at the edge of the garden stops dropping
out just because one server missed a transmission, and you never end up with two
copies of the same thermometer.

Add a **second location** only for a genuinely distant site — a cottage, a
detached workshop across town — where the receivers could never hear the same
transmitter. Devices never merge across locations, which is exactly what keeps
two identical sensors at two sites from being mistaken for one.

## Highlights

- One location holds every rtl_433 server that can hear the same sensors;
  decoded RF devices appear as nested Home Assistant devices under it.
- **Union across receivers**: one physical sensor received by several receivers is
  one device and one entity per mapped field, fed by whichever receiver receives it.
- **Per-receiver signal detail is kept**, because "how well does *this* receiver
  hear that sensor" is a different measurement per receiver: **RSSI**, **SNR**
  and **Last seen** stay one entity per sensor and receiver ("RSSI Attic"), and
  the panel's **Signal coverage** page shows the comparison with no entity
  enabled at all.
- A device stays available while **at least one** receiver is connected *and*
  has received it inside its availability timeout.
- Device support is data-driven through a YAML
  [device library](docs/device-library.md) shipped by the `pyrtl_433` dependency,
  with per-location user overrides available from the Home Assistant UI.
- Every device your receivers receive is listed on **one** union add-device page for
  you to add or ignore; approving once covers the whole location, and nothing is
  added to Home Assistant on its own.
- Supports class-aware availability, Last seen timestamps, momentary event
  entities, synthesized-off motion sensors, and utility-meter calibration.
- Optional Home Assistant-managed radio controls can adopt and re-apply each
  receiver's settings such as frequency, sample rate, gain, ppm, conversion mode,
  and hop interval.

## Quick Start

The recommended setup on Home Assistant OS (or any Supervisor-based install):

1. Install this integration through HACS as a custom repository, or copy
   `custom_components/rtl_433` into Home Assistant's `custom_components` folder.
2. Restart Home Assistant so the integration is loaded.
3. Install and start the
   [rtl_433 add-on](https://github.com/rtl-433-hass/rtl_433-hass-addons) with a
   supported USB radio plugged in.
4. Each radio the add-on detects appears as a discovered **rtl_433** card under
   **Settings → Devices & Services**. Click **Add** and confirm — no host or
   port needs to be typed. The first one creates a location and its receiver;
   later ones offer to join that location or start a new one.

The order matters: install the integration and restart Home Assistant *before*
starting the add-on, so the integration is loaded when the add-on publishes its
discovery information.

Not using the add-on? Run any rtl_433 server with its HTTP/WebSocket output
enabled (usually `rtl_433 -F http`), then add **rtl_433** from **Settings →
Devices & Services → Add Integration** and enter the rtl_433 server host, port,
path, and security settings.

### Adding More Receivers to One Location

A second rtl_433 server covering the same place belongs in the **same location**,
not in a second entry. On **Settings → Devices & Services → rtl_433**, use the
location's **Add a receiver** control and enter that server's host, port and path.
Its radio settings are its own; everything else — which devices are added, which
are ignored, the availability timeout, the mapping overrides — is the location's
and already applies to it.

From then on, every sensor both receivers receive is one device. The **Add or
replace device** page shows one card per sensor rather than one per receiver,
listing which receivers received it and the readings from whichever received it last,
and adding it once covers the location.

Removing a receiver drops that receiver's own entities — its radio controls,
diagnostics, connectivity, and its per-receiver signal entities — and leaves the
merged devices and their history alone. Removing a location's **last** receiver
removes the location with it, since a location with nothing listening cannot
start up again.

See the [installation](https://rtl-433-hass.github.io/rtl_433/latest/installation/)
and [configuration](https://rtl-433-hass.github.io/rtl_433/latest/configuration/)
guides for the full setup flow.

## Repository Links

- [Documentation source](docs/index.md)
- [Contributing guide](CONTRIBUTING.md)
- [Device-library guide](docs/device-library.md) (schema reference:
  [pyrtl_433](https://rtl-433-hass.github.io/pyrtl_433/latest/device-library/))
- [Issue tracker](https://github.com/rtl-433-hass/rtl_433/issues)
