# rtl_433 for Home Assistant

The rtl_433 integration connects Home Assistant to an
[rtl_433](https://github.com/merbanan/rtl_433) HTTP/WebSocket server and turns
decoded RF devices into native Home Assistant entities.

rtl_433 receives 433 MHz and other ISM-band transmissions with an SDR and can
expose decoded events through its HTTP API. This integration connects to that
WebSocket endpoint, normalizes each event into a stable device identity, and maps
raw fields to Home Assistant sensors, binary sensors, and events through the
[device library](device-library.md).

You run one rtl_433 server with your SDR; this integration is the Home Assistant
side. It does not talk to an SDR directly and ships no native requirements.

## Features

- **Local push** over the rtl_433 WebSocket, with no cloud dependency and no
  polling.
- **Data-driven device library**: device support is YAML, not Python.
- **Per-hub user overrides**: add or correct mappings from the hub options flow
  using Home Assistant's YAML editor.
- **Automatic nested devices**: newly observed RF devices are added under the hub
  when discovery is enabled.
- **Configurable availability**: silence-based availability with hub defaults,
  device overrides, and event-driven class defaults.
- **Multiple servers**: add one hub per rtl_433 server; device identities are
  scoped per hub.
- **Hub observability**: diagnostic entities for connectivity, SDR/meta values,
  and server statistics.
- **Optional managed SDR settings**: Home Assistant can adopt and re-apply rtl_433
  SDR settings after reconnects.
- **Diagnostics feedback loop**: downloadable diagnostics show unmapped fields so
  missing device support is easy to identify.

## Where to Start

- Install the integration with [Installation](installation.md).
- Add a hub with [Configuration](configuration.md).
- Learn how devices appear in [Discovery](discovery.md).
- Tune timeouts in [Availability](availability.md).
- Add or fix field mappings in [Device Library](device-library.md).
