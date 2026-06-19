# Diagnostics & Debugging

## Per-Device Signal Diagnostics

When rtl_433 reports level data, each event can include per-transmission
frequency, RSSI, SNR, and noise. The integration maps these to diagnostic sensors
on the RF device:

- **Frequency** in MHz.
- **RSSI** in dB.
- **SNR** in dB.
- **Noise** in dB.

These entities are disabled by default. Enable the ones you want from the device
page or entity settings to chart reception quality and antenna placement.

Level fields are only present when rtl_433 emits them. The rtl_433 Home Assistant
add-on reports levels automatically. When running rtl_433 yourself, start it with
`-M level` or add `report_meta level` to the rtl_433 config.

If level data is not reported, the diagnostic sensors do not appear; normal event
processing is unaffected.

## Downloadable Diagnostics

Home Assistant diagnostics include the fields each hub has seen but could not map
to entities. Download diagnostics from **Settings → Devices & Services → rtl_433
→ ⋮ → Download diagnostics** and inspect `unmatched_field_keys`.

Each unmatched key is either a candidate for a one-line device-library mapping or
noise/identity data that belongs in `_skip_keys.yaml`. See the
[Adding device mappings](device-library.md#adding-device-mappings) guide.

## Debug Logging

If a device fires duplicate, spurious, or late events, enable DEBUG logging for
the integration:

```yaml
logger:
  logs:
    custom_components.rtl_433: debug
```

Every decoded event frame logs an ingestion line and, for event entities, a fired
or suppression line.

| Log pattern | Meaning |
| --- | --- |
| `rtl_433 RX <device> ... -> LIVE` | A genuine live transmission. Availability refreshes and event entities fire. |
| `... -> REPLAY (event_time<=high_water)` | An already-seen frame from reconnect replay. Sensors seed, events do not fire. |
| `... -> STALE-GAP (age>threshold)` | A frame that occurred while Home Assistant was disconnected and is too old to fire. |
| `... -> BACKLOG (pre-connection)` | A replayed frame timestamped before this connection began. |
| `rtl_433 fired <type> ...` | An event entity fired for a live transmission. |
| `rtl_433 suppressed replayed/stale ...` | A stale event that would have fired was intentionally suppressed. |
| `rtl_433 discovered new device ...` | A device was registered for the first time. |
| `rtl_433 <device> reported unmapped field(s) ...` | A field has no descriptor in the active library. |

Use these lines to attribute duplicate behavior:

- Two `LIVE` ingestion lines for one physical press means rtl_433 decoded two
  transmissions.
- `REPLAY`, `BACKLOG`, or `STALE-GAP` without a fired line means the integration
  suppressed a queued duplicate.
- One `LIVE` line and one fired line with multiple automation runs points to the
  automation configuration.
