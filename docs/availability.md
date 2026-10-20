# Availability

RF devices announce their availability only by transmitting, so the integration
uses a silence-based availability model. If no event for a device arrives within
its availability timeout, its entities become `unavailable`.

![Device entities showing the unavailable state after the availability timeout](images/04-unavailable-state.png)

## Transmission Patterns

How long a device can reasonably stay silent depends on the device type.

| Device type | Typical behavior |
| --- | --- |
| Periodic weather, temperature, soil, and air-quality sensors | Transmit at regular intervals. |
| Door/window contacts, motion/PIR, buttons, doorbells, and security sensors | Transmit on events, sometimes with an occasional heartbeat. |
| Generic EV1527 door/PIR devices and parked TPMS sensors | May have no heartbeat and stay silent for days. |

Periodic devices use finite timeouts. Event-driven devices default to never
expiring because a long silence is normal and does not imply failure.

## Receiver Connection

When a receiver disconnects, devices that depend on it become `unavailable`
immediately. This includes event-driven devices and that receiver's **Last seen**
sensors. A device stays available if another connected receiver at the location
has received it within its timeout. See
[Receivers in One Location](#receivers-in-one-location).

When the connection returns, devices show their last received values and the
usual silence timeouts resume.

If you want an automation to tolerate short blips, condition it on the receiver's
**Connectivity** binary sensor with a `for:` delay rather than reacting to each
device going unavailable.

The **rtl_433 server unreachable** repair notification waits 90 seconds, so a
routine server restart does not produce a notification.

For `event` entities (buttons, doorbells, remotes), use the `event.received`
trigger. It ignores transitions out of `unavailable`, so reconnecting does not
trigger an automation for an old press.

A receiver's radio controls (**Gain**, **Sample rate**, **Frequency correction**,
**Hop interval**, **Conversion mode**) stay available so you can change settings
while disconnected. With **Manage this receiver's radio** on, these appear as
`number`/`select` entities. Turning it off replaces them with read-only diagnostic
sensors, which become unavailable when that receiver disconnects.

The **Connectivity** binary sensor is the entity that reports the outage: it
reads the socket state directly and stays available throughout. The Home
Assistant log records the drop and the reconnect:

```text
INFO  rtl_433 lost the connection to ws://rtl433.local:8433/ws; reconnecting, and marking all 12 device(s) ...
INFO  rtl_433 reconnected to ws://rtl433.local:8433/ws after 184s
```

## Receivers in One Location

A location can hold several receivers. A sensor received by more than one of
them appears as one Home Assistant device.

A receiver can keep a device available when both of these are true:

1. Its WebSocket connection to rtl_433 is open.
2. It has received the device within its effective timeout, or the device is set
   to never expire.

The device stays available while at least one receiver meets both conditions.
A connected receiver that has never received the device cannot keep it available.
Neither can a disconnected receiver, even if it received a recent transmission.

Each receiver tracks when it last received the device. All use the same timeout,
set for the location or overridden for the device. See
[Timeout Sources](#timeout-sources).

**RSSI**, **SNR** and **Last seen** are tracked separately for each receiver.
For example, **RSSI Attic** becomes unavailable when the attic receiver disconnects,
even if the garage receiver keeps the device available. See
[Per-Receiver Signal Detail](#per-receiver-signal-detail).

## Per-Receiver Signal Detail

Each added device has separate **RSSI**, **SNR** and **Last seen** entities for
each receiver. Their names include the receiver, for example **RSSI Attic** and
**RSSI Garage**. This lets you compare reception from different places.

A receiver starts with the name `rtl_433 (host)`. Renaming it also updates these
entity names.

**RSSI** and **SNR** are disabled by default. Use the panel's **Signal coverage**
page to compare levels and ages without enabling entities. Enable the entities
when you want history, graphs, or automations on signal strength. See
[Device Discovery](device-discovery.md#signal-coverage).

## Timeout Sources

The effective availability timeout is resolved in this order:

1. Per-device override from **Device settings**.
2. Location default from **Location settings**, if set.
3. Device-class default.
4. 600 second fallback.

Set a timeout to `0` to make a device never expire. This is already the automatic
default for event-driven devices.

Every receiver uses the same resolved timeout for the device.

**Location settings** offers *Per-device-type defaults*, *Never expire*, or
*a fixed timeout*. Choosing a fixed timeout of 600 seconds applies that location
default at step 2, including to event-driven devices. The 600-second fallback at
step 4 applies only when no earlier rule supplies a timeout.

## Restart Behavior

On Home Assistant restart, the last known states are restored first. The timeout
then runs from the restart time, and entities become unavailable if no
new event arrives within the timeout.

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
