# Hub Entities

Each hub exposes diagnostic entities on the hub device so you can observe the
rtl_433 server itself.

## Connectivity

The **Connectivity** binary sensor is on while the hub's WebSocket connection is
open and off otherwise. It flips off immediately when the server announces a
shutdown instead of waiting for a silence timeout.

## SDR and Meta Diagnostics

Read-only diagnostic sensors report the receiver's current configuration:

- Center frequency.
- Sample rate.
- Conversion mode.
- Hop interval.
- Gain, where an empty value reads as `auto`.
- Frequency correction in ppm.

The configured `frequencies` and `hop_times` arrays appear as attributes on the
center-frequency sensor.

## Server Statistics

Server statistics include cumulative decoded events, OOK frames, FSK frames, and
enabled decoders. Per-protocol `stats[]` and the `since` timestamp appear as
attributes on the decoded-events sensor.

Hub observability data is fetched over HTTP from the rtl_433 server's `/cmd`
endpoint at the server root, `http(s)://host:port/cmd`, independent of the
configured WebSocket path. If a reverse proxy exposes only the WebSocket path and
not `/cmd`, these sensors degrade to `unknown` while the event stream and
connectivity sensor keep working.

## Managing SDR Settings from Home Assistant

By default a new hub adopts and manages the receiver's SDR settings. With
**Manage rtl_433 settings from Home Assistant** enabled, the hub exposes controls
under the hub device in the config entity category:

- **Center frequency** number in MHz, available only for single-frequency setups.
- **Sample rate** number in Hz.
- **Frequency correction** number in ppm.
- **Gain** number in dB paired with an **Auto gain** switch.
- **Conversion mode** select with `native`, `si`, and `customary`.
- **Hop interval** number in seconds, available only for multi-frequency hopping
  setups.

Frequency hopping must be configured in rtl_433. Home Assistant can adjust the
hop interval after rtl_433 is already running with multiple frequencies, but it
does not provide an entity for editing the frequency list.

On first connect, Home Assistant adopts the server's current settings into its
desired state. It then re-applies managed settings on every reconnect so values
survive rtl_433 restarts. If an initial frequency was configured during setup,
that value is applied once and takes priority over the adopted frequency.

Once managed, change these settings in Home Assistant rather than editing the
rtl_433 config directly. Home Assistant is the authority and will re-apply its
stored values on the next reconnect.

### Re-Syncing from rtl_433 Config

To pick up direct rtl_433 config edits:

1. Turn **Manage rtl_433 settings from Home Assistant** off. This clears Home
   Assistant's stored desired state.
2. Restart rtl_433 so it loads its config.
3. Turn the toggle back on. On the next connect, Home Assistant re-adopts the
   server's current settings.

### Requirements and Caveats

- The `/cmd` endpoint must be reachable at the server root.
- Hopping setups keep center frequency unmanaged so Home Assistant never pins a
  receiver to one frequency.
- The frequency list itself can only be set in the rtl_433 config.
- Multi-stage gain strings are not supported by the single gain control.
- Retuning does not widen the sample rate automatically; high-frequency bands may
  require manually increasing sample rate.

Turning management off removes the controls, stops Home Assistant from sending
commands, and clears its stored desired state. The receiver's settings are left
untouched.
