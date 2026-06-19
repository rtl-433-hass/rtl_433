# Device Discovery

Devices are added automatically to Home Assistant the first time they send a
message. You may need to trigger doorbells and motion sensors manually to
register them. New devices trigger a notification inside Home Assistant. Note
that weak signals may show up as unexpected devices or as devices with missing
fields.

![Acurite-Tower device page showing Temperature 26.7 C, Humidity 74.0%, Battery 100%, and signal diagnostics](images/02-device-page.png)

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

If unwanted devices keep registering, disable device discovery in the hub
settings. This is highly recommended in urban areas!
