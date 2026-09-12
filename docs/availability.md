# Availability

RF devices announce their presence only by transmitting, so the integration uses
a silence-based availability model. If no event for a device arrives within its
availability timeout, its entities become `unavailable`.

![Device entities showing the unavailable state after the availability timeout](images/04-unavailable-state.png)

## Transmit Cadences

How long a device can reasonably stay silent depends on the device type.

| Device type | Typical behavior |
| --- | --- |
| Periodic weather, temperature, soil, and air-quality sensors | Transmit on a regular cadence. |
| Door/window contacts, motion/PIR, buttons, doorbells, and security sensors | Transmit on events, sometimes with an occasional heartbeat. |
| Generic EV1527 door/PIR devices and parked TPMS sensors | May have no heartbeat and stay silent for days. |

Periodic devices use finite timeouts. Event-driven devices default to never
expiring because a long silence is normal and does not imply failure.

## Receiver Connection

Silence only means something while the integration is listening. If the
connection to the rtl_433 server drops, no events can arrive for any device, so a
device's last reading says nothing about whether the device is still there.

When the connection to the rtl_433 server drops, every device that receiver was
the only one hearing is marked `unavailable` straight away, regardless of its own
timeout — including event-driven devices that never expire on silence, and their
**Last seen** sensors. There is no grace period: while the socket is down the
integration cannot hear the radio at all, so continuing to show the last reading
would present stale data as current. This is the same behavior an MQTT device
gets from an availability topic and a last-will message, and what Home Assistant
integrations do generally when a connection to a receiver is lost. With a second
receiver at the location still hearing the device, it stays available — see
[Receivers in One Location](#receivers-in-one-location).

The devices come back as soon as the connection is re-established; their values
are the last ones received, and the usual silence timeouts resume from there. A
brief drop therefore shows up as a brief `unavailable` — an honest one, because
during it the integration genuinely was not listening.

If you want an automation to tolerate short blips, condition it on the receiver's
**Connectivity** binary sensor with a `for:` delay rather than reacting to each
device going unavailable.

The separate **rtl_433 server unreachable** repair issue is debounced: it waits
90 seconds before raising, so a routine server restart does not produce a
notification even though the entities reported the outage immediately.

`event` entities (buttons, doorbells, remotes) go unavailable with everything
else. This matches Zigbee2MQTT, whose event entities carry the bridge-state
availability topic alongside the per-device one, and Home Assistant's own Shelly
and ESPHome integrations. Use the `event.received` trigger rather than a plain
state trigger: it ignores transitions out of `unavailable`, so a reconnect never
replays a stale press.

One kind of entity is deliberately exempt:

- **A receiver's radio controls** (**Gain**, **Sample rate**, **Frequency
  correction**, **Hop interval**, **Conversion mode**) stay available, because
  they are settings you are writing rather than readings you are trusting. With
  **Manage this receiver's radio** on — the default — these appear as
  `number`/`select` entities. Turning it off replaces them with read-only
  diagnostic sensors, and those *are* gated on the connection along with that
  receiver's other diagnostic sensors (center frequency, frame counters, enabled
  decoders), whose values are fetched over HTTP and would otherwise freeze at
  whatever was last read.

The **Connectivity** binary sensor is the entity that reports the outage: it
reads the socket state directly and stays available throughout. The Home
Assistant log records the drop and the reconnect:

```text
INFO  rtl_433 lost the connection to ws://rtl433.local:8433/ws; reconnecting, and marking all 12 device(s) ...
INFO  rtl_433 reconnected to ws://rtl433.local:8433/ws after 184s
```

## Receivers in One Location

A location can hold several receivers, and one sensor they all hear is one Home
Assistant device. So "is this device available?" has to be answered over the
whole location rather than over one server.

A receiver **vouches** for a device when *both* of these are true **of that same
receiver**:

1. it is **connected** — its WebSocket to its rtl_433 server is up; and
2. it **heard the device** within the device's effective timeout.

**The device is available while at least one receiver vouches for it.** So a
sensor at the edge of the garden that the garage receiver keeps missing stays
available as long as the attic receiver hears it, and it goes `unavailable` only
when every receiver has either dropped its connection or stopped hearing the
device.

The two halves are deliberately checked together, per receiver, rather than
separately. "Some receiver is connected" and "some receiver heard it recently"
can both be true while nothing can actually hear the device — a connected garage
receiver that is deaf to the sensor, plus an offline attic receiver holding a
minute-old timestamp. Neither of those receivers can vouch, so the device
correctly goes unavailable.

Each receiver keeps its own timeout clock, its own connection, and its own
**Connectivity** sensor; the merge only unions what they each conclude. The
timeout itself is the location's (or the device's override) — see
[Timeout Sources](#timeout-sources).

The per-receiver **RSSI**, **SNR** and **Last seen** entities are the exception
to the union: each reads its own receiver alone. "How well does *this* receiver
hear that sensor" is meaningless while *this* receiver is deaf, so `RSSI Attic`
goes unavailable when the attic receiver does, even while the merged device is
fine on the garage receiver. That is the point of keeping them separate — see
[Per-Receiver Signal Detail](#per-receiver-signal-detail).

## Per-Receiver Signal Detail

Signal readings do not merge. Every added device carries one **RSSI**, one
**SNR** and one **Last seen** entity *per receiver*, named with the receiver
they belong to — `RSSI Attic`, `RSSI Garage` — all on the one merged device. A
single merged value would report whichever receiver's frame happened to arrive
first, which is meaningless as a signal measurement and hides the very thing a
second receiver was added to fix.

The name after the field is the receiver's own name, which starts out as
`rtl_433 (host)`. Rename the receiver to something short and the entities on
every device follow.

**RSSI** and **SNR** are disabled by default, as they have always been, so a
location does not grow two extra entities per sensor per receiver for a number
most people only glance at. To read the comparison without enabling anything,
use the panel's **Signal coverage** page, which reports the same levels and ages
straight from the receivers — see
[Device Discovery](device-discovery.md#signal-coverage). Enable the entities when
you want history, graphs, or automations on signal strength.

## Timeout Sources

The effective availability timeout is resolved in this order:

1. Per-device override from **Device settings**.
2. Location default from **Location settings**, if set.
3. Device-class default.
4. 600 second fallback.

Set a timeout to `0` to make a device never expire. This is already the automatic
default for event-driven devices.

The same resolved timeout is what every receiver in the location measures its own
silence against, so a device does not have a different deadline depending on
which server heard it.

**Location settings** asks which of the three you want outright — *Per-device-type
defaults*, *Never expire*, or *a fixed timeout* — so leaving the location on the
class defaults is a choice you make rather than a number you have to leave alone.
A fixed timeout of 600 seconds is a location default like any other, and stops at
step 2; it is not the same as the step-4 fallback, which only applies to devices
no earlier step has claimed.

## Restart Behavior

On Home Assistant restart, the last known states are restored first. The timeout
then runs from the restart time, and entities flip to unavailable only after the
restored silence window elapses without a fresh event.

## Last Seen Sensor

Every device gets a diagnostic timestamp sensor per receiver, named **Last seen**
followed by the receiver's name. It reports when *that receiver* last heard the
device, and restores its previous value across restarts.

Last seen is enabled by default for event-driven devices because they never
expire and the timestamp is their freshness signal. It is disabled by default for
periodic devices, whose availability already conveys freshness.

Unlike measurement sensors, Last seen stays available after the device falls
silent, so it can drive staleness automations. It does go unavailable while its
receiver's connection is down, because the timestamp then only records when the
integration stopped listening.

Last seen is one of the three per-receiver link fields, so a device heard by two
receivers has a **Last seen** entity for each of them — "when did *the attic*
last hear this sensor" is a different question from "when did *the garage*". For
the merged, whichever-heard-it-last answer, read the device's availability, or
the **Signal coverage** page.
