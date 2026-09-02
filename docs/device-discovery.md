# Device Discovery

Nothing is added to Home Assistant on its own. Every device your rtl_433 server
decodes is heard and held on a list of discovered devices, and you decide which
ones become real devices.

That list is where neighbours' sensors, weak signals, and bad decodes end up. In
a busy area a receiver hears far more than you want to keep, which is why the
integration waits for you to choose.

Doorbells, remotes, and motion sensors only transmit when something happens, so
trigger them once to make them show up.

## Adding Devices

Open **Settings → Devices & Services → rtl_433 → Configure**. That is the
rtl_433 page: it lists every device the receiver has heard and you have not
added, and it is where the receiver's own settings live too. It is available to
administrators only.

![The rtl_433 page: a toolbar, a row of Receiver settings / Device settings / Device mappings buttons, then a grid of device cards, each with a blue heading giving the model and device key, its sighting count, signal level and last-seen age, its latest readings named as Home Assistant entities, an Area picker, and Ignore and Add buttons](images/17-discovery-panel.png)

Each candidate gets a card, newest discovery first. Cards keep their place as
devices transmit, so a card does not move under the cursor while you are reading
it.

The blue heading is the device's identity: the model rtl_433 decoded, and below
it the device key — the id rtl_433 uses to tell one device of that model from
another, with its channel and subtype when it reports them.

| On the card | What it tells you |
| --- | --- |
| **Sightings** | How many times the device has transmitted since Home Assistant started. A real sensor keeps checking in; a bad decode is usually heard once. |
| **Signal** | The signal-to-noise ratio of the most recent message, or its RSSI when no SNR was reported. Only shown when the server reports levels; your own sensors are normally the strongest. |
| **Last seen** | How long ago the device last transmitted. Hover over it for the exact first and last times. |
| **Readings** | The most recent message, shown as the entities adding it would create — `Temperature 21.4 °C`, not `temperature_C: 21.4`. This is usually the quickest way to tell two identical sensors apart. |

The readings are the ones you would actually get. A field the device library
does not map creates no entity, and one it maps as disabled by default (the
`SNR`, `RSSI` and `Noise` diagnostics) is not something you would see on the
device page, so neither is listed here.

Pick an **Area** before adding to have the new device filed there straight away.
Leave it on *No area* to sort it out later on the device page.

**Add** creates that device and its entities immediately, and starts recording
history from that point. The card stays where it is and turns green, with a link
to the device that was just created:

![An Acurite-Tower device page showing Temperature 26.7 C, Humidity 74.0%, Battery 100%, and signal diagnostics](images/02-device-page.png)

**Ignore** hides the device until you un-ignore it — see [Ignoring
Devices](#ignoring-devices).

The page is live. A device heard while it is open appears on its own, sighting
counts climb as devices transmit, and nothing needs a reload. So trigger a
doorbell or open a door sensor and watch it arrive.

The page is for administrators only. Home Assistant hides it from other users
and refuses its commands.

Everything the page does is also a Home Assistant WebSocket command, so the same
list and the same actions are available to a script — see [Home Assistant
discovery commands](websocket-api.md#home-assistant-discovery-commands).

### Clearing the List

A receiver left running for a few weeks in a built-up area accumulates hundreds
of candidates, and the one you came to add is somewhere among them. **Clear
discovered devices** empties the list so it refills from live traffic: trigger
the doorbell, and it is the only thing on the screen.

Nothing is lost. The list has always been held in memory only, so every device
cleared comes back on its next transmission. Devices you have ignored stay
ignored — that is a decision, and this is not the control for undoing it.

## Receiver and Device Settings

The same page carries this receiver's settings, as three dialogs above the
discovered devices.

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
removes them all. A document that will not store is refused with every problem
listed, and the overrides you already had are left alone.

## Ignoring Devices

Devices you never want to see again are ignored. Click **Ignore** on the card.
An ignored device is dropped from the list, is never offered again, and stays
ignored across restarts. It is the way to make a neighbour's sensor go away for
good.

Ignoring is not deleting: an ignored device is simply not offered, and its
messages are dropped as they arrive.

To undo it, click **Show ignored devices** under the cards and then
**Un-ignore**:

![The rtl_433 page with the ignored-devices section revealed, showing one ignored device and its Un-ignore button](images/16-ignored-devices.png)

The device reappears the next time it transmits, which for a door or motion
sensor means the next time it is triggered.

## The Discovered List Is Temporary

The list of discovered devices is held in memory only. It is empty after a
restart or a reload of the hub, and fills again as devices transmit. A sensor
that reports every few minutes is back almost immediately; one that reports
twice a day takes longer.

So an empty list shortly after a restart is normal — it means nothing has
transmitted yet. Devices you have already added are unaffected: they are stored
with the hub and come back with their entities and history on every start.
Ignored devices are stored too, and stay ignored.

## Deleting Devices

To remove a device you no longer want, open it under **Settings → Devices &
Services → rtl_433 → the device → Delete**.

Deleting removes the device and its entities from Home Assistant, but it does not
stop the transmitter. The device returns to the discovered list the next time it
transmits, so you can add it back. To keep it out of the list, ignore it
instead.

## Post-Connection Registration

Only devices heard after the integration connects count as live sightings. On
connect, the rtl_433 server replays its recent backlog. The integration uses
frame timestamps to tell that replay apart from live traffic: backlog frames
refresh the values of devices you have already added, but they never put a
device on the discovered list, so a reconnect does not fill it with everything
that transmitted while Home Assistant was away.

A device you have not added appears the first time it transmits after the
connection. This assumes the rtl_433 server and Home Assistant clocks are roughly
in sync.

## Replacing a Device That Changed Id

Many battery-powered sensors pick a new random transmitter id every time their
batteries are changed. rtl_433 identifies a device by that id, so the sensor
comes back as a brand-new device with new entities and no history, while the
original stops updating and eventually goes unavailable.

You meet this problem from the new device's side: something you did not add has
appeared on the discovered list, and it is really a sensor you already have. So
that is where the fix starts.

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
