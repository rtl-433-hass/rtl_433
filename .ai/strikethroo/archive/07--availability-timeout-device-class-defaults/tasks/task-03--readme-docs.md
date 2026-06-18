---
id: 3
group: "docs"
dependencies: [1]
status: "completed"
created: "2026-05-31"
skills: ["technical-writing"]
---

# README: document regimes, class defaults, never-expire

## Objective

Update `README.md` so users understand the two device cadence regimes, the new device-class-aware default timeouts, and the `0` = never-expire option, with representative device cadences cited.

## Skills Required

- `technical-writing` (Markdown docs)

## Output

Edits to `README.md` in the availability-timeout section(s).

## Implementation Notes

<details>
<summary>Detailed implementation guidance</summary>

**Verified current state:** `README.md` already describes the availability timeout with the 600s/10-minute default and explains Hub settings vs Device settings (per-device override). Find those passages (search for "availability", "600", "timeout", "unavailable").

**Steps — update the README to explain:**

1. **Two regimes.** rtl_433 devices split into:
   - Periodic transmitters (weather/temp/soil/air): transmit on a fixed cadence — e.g. Acurite temp ~16s, Ecowitt/Fine Offset WH51 soil ~72s, Ecowitt WH41 air quality ~10min. A short timeout (600s) suits these.
   - Event-driven devices (door/window contact, motion/PIR, security): only transmit on an event plus an occasional supervision heartbeat — e.g. Honeywell 5800 contacts ~70–90min, GE/Interlogix motion ~1h. Cheap generic EV1527 door/PIR sensors and parked TPMS send NO heartbeat at all and can be silent for days.

2. **New class-aware defaults.** State that, when no explicit timeout is set, the integration now picks a default based on the device's Home Assistant device class: event-driven binary-sensor classes (door, window, opening/contact, motion) default to ~2 hours (7200s), while periodic sensors keep the 600s default. Note this applies automatically, including to existing installs that never customized the hub timeout; any explicit per-device or hub value you set is preserved.

3. **Never-expire.** Document that setting the timeout to `0` (hub default or per-device override) means the device is never marked unavailable — recommended for heartbeat-less generic door/PIR sensors and for TPMS that go silent when parked.

4. **Resolution order.** Briefly: per-device override → hub default (if you set one) → device-class default → 600s fallback.

Keep the tone and formatting consistent with the surrounding README. Tables are welcome for the cadence examples. Do not duplicate the whole section — edit/extend the existing one.

</details>
