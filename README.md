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

## Highlights

- Add one hub per rtl_433 server; decoded RF devices appear as nested Home
  Assistant devices under that hub.
- Device support is data-driven through a YAML
  [device library](docs/device-library.md), with per-hub user overrides available
  from the Home Assistant options flow.
- Supports automatic device discovery, class-aware availability, Last seen
  timestamps, momentary event entities, synthesized-off motion sensors, and
  utility-meter calibration.
- Optional Home Assistant-managed SDR controls can adopt and re-apply receiver
  settings such as frequency, sample rate, gain, ppm, conversion mode, and hop
  interval.

## Quick Start

1. Run rtl_433 with its HTTP/WebSocket output enabled, usually `rtl_433 -F http`.
2. Install this integration through HACS as a custom repository, or copy
   `custom_components/rtl_433` into Home Assistant's `custom_components` folder.
3. Restart Home Assistant.
4. Add **rtl_433** from **Settings → Devices & Services → Add Integration** and
   enter the rtl_433 server host, port, path, and security settings.

See the [installation](https://rtl-433-hass.github.io/rtl_433/latest/installation/)
and [configuration](https://rtl-433-hass.github.io/rtl_433/latest/configuration/)
guides for the full setup flow.

## Repository Links

- [Documentation source](docs/index.md)
- [Contributing guide](CONTRIBUTING.md)
- [Device-library reference](docs/device-library.md)
- [Issue tracker](https://github.com/rtl-433-hass/rtl_433/issues)
