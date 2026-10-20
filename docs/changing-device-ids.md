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

To confirm you are picking the right device, check the **Serial number** on the
device info card: it is the ID rtl_433 decoded for that device, plus its channel
and subtype when it has them. Unlike the device name, the serial number is not
affected by renaming the device, so it always shows the transmitter the device
is currently tracking — the old ID before a replace, the new one after.

## Suggested replacements in Repairs

When an added device goes quiet and a new candidate looks like its successor,
the integration raises a notice in **Settings → System → Repairs**. It shows the
old and new IDs, the temperature readings when available, the signal level, and
the time between the old device's last report and the new candidate's first.

The suggestion requires the same model, channel and subtype, at least ten
minutes of silence from the old device, and at least two sightings of the
candidate. The old device must not have reported since the candidate appeared.
When both report temperature or humidity, those readings must be similar. If
multiple candidates fit, or a candidate fits multiple added devices, no
suggestion is offered. The notice disappears if the match becomes ambiguous or
the old device reports again.

These clues cannot prove that this is the same physical device. Nearby sensors
can share a model, channel and similar readings, and reception gaps can look
like a battery change. **Check the physical device before confirming.** Nothing
is replaced automatically. You can close the dialog and use the rtl_433 page to
ignore an unrelated candidate or choose a replacement manually.

Confirming the repair uses the same Replace action described above and preserves
the existing entities and history. The suggestion is checked again when you
submit; if it is no longer valid, nothing changes. Suggestions are rebuilt from
live traffic after a restart or reload.

After a confirmed replacement, the integration fires
`rtl_433_device_id_changed` with `entry_id`, `device_id`, `name`, `old_key`,
`new_key`, and `automatic: false` for automations that need to record the change.
