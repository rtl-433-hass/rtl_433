# Installation

## HACS Custom Repository

This integration is not yet in the default HACS store, so add it as a custom
repository:

1. In Home Assistant, open **HACS**.
2. Click the **⋮** menu in the top right, then **Custom repositories**.
3. Enter `https://github.com/rtl-433-hass/rtl_433` and choose the **Integration**
   category.
4. Search for **rtl_433**, open it, and click **Download**.
5. Restart Home Assistant.

## Manual Install

1. Copy `custom_components/rtl_433` from this repository into Home Assistant's
   `<config>/custom_components/` directory.
2. Confirm the final path is `<config>/custom_components/rtl_433/`.
3. Restart Home Assistant.

## rtl_433 Server Requirement

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

The integration does not control SDR hardware directly; rtl_433 owns the receiver
and exposes decoded frames to Home Assistant.

## Next Step

After installation, add a hub from Home Assistant using the
[configuration guide](configuration.md).
