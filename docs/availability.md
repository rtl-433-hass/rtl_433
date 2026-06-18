# Availability

RF devices announce their presence only by transmitting, so the integration uses
a silence-based availability model. If no event for a device arrives within its
availability timeout, its entities become `unavailable`.

## Transmit Cadences

How long a device can reasonably stay silent depends on the device type.

| Device type | Typical behavior |
| --- | --- |
| Periodic weather, temperature, soil, and air-quality sensors | Transmit on a regular cadence. |
| Door/window contacts, motion/PIR, buttons, doorbells, and security sensors | Transmit on events, sometimes with an occasional heartbeat. |
| Generic EV1527 door/PIR devices and parked TPMS sensors | May have no heartbeat and stay silent for days. |

Periodic devices use finite timeouts. Event-driven devices default to never
expiring because a long silence is normal and does not imply failure.

## Timeout Sources

The effective availability timeout is resolved in this order:

1. Per-device override from **Device settings**.
2. Hub default from **Hub settings**, if set.
3. Device-class default.
4. 600 second fallback.

Set a timeout to `0` to make a device never expire. This is already the automatic
default for event-driven devices.

Both the hub default and per-device overrides apply live without reloading the
hub or reconnecting the WebSocket.

## Restart Behavior

On Home Assistant restart, the last known states are restored first. The timeout
then runs from the restart time, and entities flip to unavailable only after the
restored silence window elapses without a fresh event.

## Last Seen Sensor

Every device gets a diagnostic timestamp sensor named **Last seen**. It reports
when the device was last heard from and restores its previous value across
restarts.

Last seen is enabled by default for event-driven devices because they never
expire and the timestamp is their freshness signal. It is disabled by default for
periodic devices, whose availability already conveys freshness.

Unlike measurement sensors, Last seen stays available after the device falls
silent, so it can drive staleness automations.

## Event Entities and Replay Suppression

Momentary RF devices such as remotes, doorbells, and key fobs are native Home
Assistant `event` entities. Each genuine transmission fires one event whose type
is the transmitted value. Event entities stay available between presses.

On reconnect or Home Assistant restart, rtl_433 replays recent event history.
Momentary events that occurred while Home Assistant was disconnected are not
re-fired, so an old doorbell press cannot trigger automations late. Their latest
readings still seed ordinary sensors.

## Motion and Occupancy

Detect-only PIR sensors that send a trip but never an off become occupancy
`binary_sensor` entities. The integration synthesizes the off state after a clear
delay, defaulting to 90 seconds. The delay can be tuned per device in **Device
settings**.

See [Motion / occupancy](device-library.md#motion-occupancy) for the mapping
schema.
