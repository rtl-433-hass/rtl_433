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
in sync, and that the server stamps its events with a readable timestamp — see
[Event Timestamps](configuration.md#event-timestamps) for the accepted forms and
what changes without one.

## Discovery Toggle

Each hub has its own discovery toggle. Turning discovery off stops new devices on
that hub from being added. Devices that already exist keep updating. Turning it
back on lets new and previously deleted devices appear again when they transmit.

## Deleting Devices

To remove an unwanted device, open it under **Settings → Devices & Services →
rtl_433 → the device → Delete**.

If unwanted devices keep registering, disable device discovery in the hub
settings. This is highly recommended in urban areas!

## Replacing a Device That Changed Id

Many battery-powered sensors pick a new random transmitter id every time their
batteries are changed. rtl_433 identifies a device by that id, so the sensor
comes back as a brand-new device with new entities and no history, while the
original stops updating and eventually goes unavailable.

To move the original device onto its new id, open **Settings → Devices &
Services → rtl_433 → Configure → Replace device**. Pick the **Device to keep** —
the existing device whose history you want to preserve — then pick the **New
device**, the one that appeared after the batteries were changed. Devices of the
same model are listed first, since a battery swap does not change the model.

The device you keep takes over the new id, and the duplicate device and its
entities are removed. The kept device's entity ids do not change, so its
history, statistics, dashboards and automations carry straight through, and its
calibration, availability timeout override, motion clear delay and event types
come with it. Any field the replacement has already reported is added to the
device's known fields. The short history the duplicate recorded before the
replace is discarded along with it.

Because those entity ids are kept exactly as they were, they still spell out the
*old* id — an entity named `sensor.acurite_986_1a2b_temperature` keeps that name
after being re-pointed at id `9f3c`. That is what preserves the history, so it is
worth leaving alone. You can rename the entity if the stale id bothers you, but
renaming it starts a new history under the new entity id.

This works with device discovery turned off, which is recommended above for
urban areas. The replacement only has to have been *heard* by the receiver at
least once — it does not need to have been registered as a device. If the
replacement is not in the list yet, wait until it transmits again and reopen the
step.

To confirm you are picking the right device, check the **Serial number** on the
device info card: it is the id rtl_433 decoded for that device, plus its channel
and subtype when it has them. Unlike the device name, the serial number is not
affected by renaming the device, so it always shows the transmitter the device
is currently tracking — the old id before a replace, the new one after.
