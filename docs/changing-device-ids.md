# Managing Devices That Change IDs

Many battery-powered sensors pick a new random transmitter id every time their
batteries are changed. rtl_433 identifies a device by that id, so the sensor
comes back as a brand-new device with new entities and no history, while the
original stops updating and eventually goes unavailable.

rtl_433 can't tell the difference between a new device and one that changed its
ID. To re-link the device, go to the **Add or replace device** page.

Find the new device's card and click **Replace**, then pick the device it
replaces: the one you already have, whose history you want to keep. Devices of
the same model are listed first, since a battery swap does not change the model.
The button only appears once there is at least one added device the candidate
could stand in for.

The device you keep takes over the new id. If you had already added the
replacement as a device of its own, it and its entities are removed. The kept
device's entity ids do not change, so its
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

The replacement does not have to be added first. The card you start from is a
device you have not added, which is exactly what a battery-swapped sensor looks
like; it only has to have been heard once. If it is not on the page yet, wait
until it transmits again.

To confirm you are picking the right device, check the **Serial number** on the
device info card: it is the id rtl_433 decoded for that device, plus its channel
and subtype when it has them. Unlike the device name, the serial number is not
affected by renaming the device, so it always shows the transmitter the device
is currently tracking — the old id before a replace, the new one after.
