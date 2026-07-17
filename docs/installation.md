# Installation

## Recommended Setup Order

If you plan to use the
[rtl_433 add-on](https://github.com/rtl-433-hass/rtl_433-hass-addons) on Home
Assistant OS or another Supervisor-based install, set things up in this order:

1. Install this integration (HACS or manual, below).
2. Restart Home Assistant so the integration is loaded.
3. Install and start the add-on.

With the integration already loaded, each radio the add-on detects appears as a
discovered **rtl_433** card under **Settings → Devices & Services** — no manual
connection details needed. See
[Add-On Discovery](configuration.md#home-assistant-os-add-on-discovery).

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

## Next Step

If you are using the rtl_433 add-on, install and start it now; each detected
radio appears as a discovered **rtl_433** card under **Settings → Devices &
Services**, ready to add with one click.

For any other rtl_433 server, add a hub manually using the
[configuration guide](configuration.md). You will see the connection form below.

![The rtl_433 config flow form: host, port, WebSocket path, secure toggle, managed-settings and discovery toggles, and initial frequency](images/06-config-user.png)
