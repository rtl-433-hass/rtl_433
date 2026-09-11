# Managing Devices That Change IDs

Many battery-powered sensors pick a new random transmitter ID every time their
batteries are changed. rtl_433 identifies a device by that ID, so the sensor
comes back as a brand-new device with new entities and no history, while the
original stops updating and eventually goes unavailable.

rtl_433 can't tell the difference between a new device and one that changed its
ID. To re-link the device, go to the **Add or replace device** page.

Find the new device's card and click **Replace**, then pick the device it
replaces. Devices of the same model are listed first, since a battery swap does
not change the model. The button only appears once there is at least one added
device the candidate could stand in for.

The device is updated to take over the new ID. Entity IDs **do not change** even
if they contain the old ID. Automations, history, dashboards, and so on will all
keep working as they did before.

The replacement does not have to be added first. The card you start from is a
device you have not added, which is exactly what a battery-swapped sensor looks
like; it only has to have been heard once. If it is not on the page yet, wait
until it transmits again.

To confirm you are picking the right device, check the **Serial number** on the
device info card: it is the ID rtl_433 decoded for that device, plus its channel
and subtype when it has them. Unlike the device name, the serial number is not
affected by renaming the device, so it always shows the transmitter the device
is currently tracking — the old ID before a replace, the new one after.
