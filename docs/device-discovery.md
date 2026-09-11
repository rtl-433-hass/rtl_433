# Adding Devices to Home Assistant

By default, rtl_433 listens for new devices but doesn't add them to Home
Assistant. In a busy or dense area, you're likely to see far more sensors than
you want to keep. You may also see devices that don't really exist, due to
corrupt or weak decodes. Use this page to add the devices you want, and ignore
those you don't.

Doorbells, remotes, and motion sensors only transmit when something happens, so
trigger them once to make them show up.

## The rtl_433 Page

Open **Settings → Devices & Services → rtl_433 → Configure (the gear icon)**.
This is the rtl_433 page, and it works like the Zigbee and Z-Wave pages: it
opens on an overview of the receiver, with everything else one click away. It is
available to administrators only.

The overview has three parts:

- a card at the top saying whether the receiver is **Online**, and how many
  devices you have added to it
- **My network**, with links to this receiver's devices and entities on the
  usual Home Assistant pages
- **Receiver settings**, **Device settings** and **Device mappings**, which each
  open their own page

and, bottom right, an **Add or replace device** button.

![The rtl_433 page: a status card with a green tick reading Online and 7 devices; a My network card listing Devices, 7 devices and Entities, 33 entities, each with a chevron; then a card of three rows — Receiver settings (availability timeout and whether Home Assistant manages the receiver), Device settings (per-device timeout overrides and utility-meter calibration) and Device mappings (YAML overrides for how fields become entities); and a blue Add or replace device button in the bottom right](images/18-rtl-433-page.png)

If you have more than one receiver, each gets its own settings card, headed by
the receiver it belongs to.

## Adding Devices

Click **Add or replace device**. Every device that has been seen is listed here,
newest first. If you recently restarted Home Assistant and nothing has
transmitted, this page may be empty until something transmits.

![The rtl_433 page: a toolbar, a row of Receiver settings / Device settings / Device mappings buttons, then a grid of device cards, each with a blue heading giving the model and device key, its sighting count, signal level and last-seen age, its latest readings named as Home Assistant entities, an Area picker, and Ignore and Add buttons](images/17-discovery-panel.png)

Each card shows the device type rtl_433 decoded, and the device key which
includes any ID, channel, and subtype information.

| On the card | What it tells you |
| --- | --- |
| **Sightings** | How many times the device has transmitted since Home Assistant started. A real sensor keeps checking in; a bad decode is usually heard once. |
| **Signal** | The signal-to-noise ratio of the most recent message, or its RSSI when no SNR was reported. Only shown when the server reports levels; your own sensors are normally the strongest. |
| **Last seen** | How long ago the device last transmitted. Hover over it for the exact first and last times. |
| **Readings** | The most recent message, shown as the entities adding it would create. |

Readings that cannot be mapped to Home Assistant will not show here. See
[Device Library](device-library.md) for information about adding support for new
devices and fields.

Select an **Area** to assign the device there, or leave it blank if that doesn't
make sense.

Click **Add** to create the device. The card stays where it is and turns green,
with a link to the device that was just created.

![An Acurite-Tower device page showing Temperature 26.7 C, Humidity 74.0%, Battery 100%, and signal diagnostics](images/02-device-page.png)

Otherwise, **Ignore** hides the device.

### Clearing the List

A receiver left running for a few weeks in a built-up area accumulates hundreds
of devices, and the one you came to add is somewhere among them. **Clear
discovered devices** will empty the list so you can easily add the ones you
want.

## Receiver and Device Settings

**Receiver settings** is the default availability timeout for every device on
this receiver, and whether Home Assistant manages the server's own SDR settings
— see [Configuration](configuration.md#reconfigure-vs-configure).

**Device settings** targets one device you have already added: its availability
timeout override, its motion clear delay, and its
[utility-meter calibration](calibration.md). Pick the device at the top and the
rest of the form rebuilds from it, showing only the settings that device
actually has.

**Device mappings** is this receiver's
[device-library overrides](device-library.md), as YAML. Clearing the editor
removes them all. The device mappings are automatically validated (but not
tested!) when saving.

## Ignoring Devices

Sometimes devices are detected that you will never want to add to Home
Assistant. Click **Ignore** on the card to hide it at the bottom of the list. If
you made a mistake, go back to **Add or replace device**, click **Show ignored
devices** under the cards, and then **Un-ignore**:

![The rtl_433 page with the ignored-devices section revealed, showing one ignored device and its Un-ignore button](images/16-ignored-devices.png)

The device reappears the next time it transmits, which for a door or motion
sensor means the next time it is triggered.

## Deleting Devices

To remove a device you no longer want, open it under **Settings → Devices &
Services → rtl_433 → the device → Delete**.

Deleting removes the device and its entities from Home Assistant, but it does not
stop the transmitter. The device returns to the discovered list the next time it
transmits, so you can add it back. To keep it out of the list, ignore it
instead.

## Post-Connection Registration

Only devices seen after the integration connects count as live sightings. On
connect, the rtl_433 server replays its recent backlog. The integration uses
frame timestamps to tell that replay apart from live traffic: backlog frames
refresh the values of devices you have already added, but they never put a
device on the discovered list, so a reconnect does not fill it with everything
that transmitted while Home Assistant was away.

A device you have not added appears the first time it transmits after the
connection. This assumes the rtl_433 server and Home Assistant clocks are roughly
in sync, and that the server stamps its events with a readable timestamp — see
[Event Timestamps](configuration.md#event-timestamps) for the accepted forms and
what changes without one.
