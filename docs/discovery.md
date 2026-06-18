# Discovery

RF devices appear automatically as nested devices under the hub. There is no
separate discovery card to accept or dismiss for each RF device.

When the hub decodes a device it does not yet know, it adds the device under the
hub and creates its sensor, binary sensor, and event entities. It also raises an
in-app persistent notification with a stable per-device ID. Restarting Home
Assistant does not re-notify for already-known devices.

## Post-Connection Registration

Only devices heard after the integration connects are automatically registered.
On connect, the rtl_433 server replays its recent backlog. The integration uses
frame timestamps to seed runtime state from that backlog without flooding the
device registry with devices that transmitted before Home Assistant connected.

A previously unknown device is added the first time it transmits after the
connection. This assumes the rtl_433 server and Home Assistant clocks are roughly
in sync.

## Discovery Toggle

Each hub has its own discovery toggle. Turning discovery off stops new devices on
that hub from being added. Devices that already exist keep updating. Turning it
back on lets new and previously deleted devices appear again when they transmit.

## Deleting Devices

To remove an unwanted device, open it under **Settings → Devices & Services →
rtl_433 → the device → Delete**.

There is no persistent ignore list. With discovery on, a deleted device reappears
the next time it transmits. To keep it gone, turn the hub's discovery toggle off
before deleting it.
