---
id: 5
group: "docs-i18n"
dependencies: []
status: "completed"
created: "2026-06-04"
skills:
  - technical-writing
---
# README "Replacing a radio" section (D)

## Objective
Document the end-to-end radio-replacement procedure for users, mirroring the
paired add-on plan's §5 so the two repos line up.

## Skills Required
- `technical-writing` — user-facing README prose.

## Acceptance Criteria
- [ ] `README.md` gains a "Replacing a radio" section (heading text contains "Replacing a radio").
- [ ] It describes: swap the dongle, stamp a fresh serial in the add-on and read the new radio's `unique_id` + `host:port`, then in HA either use the **unreachable repair card** ("re-point this hub") or **hub → Reconfigure** and enter the new radio ID; notes that devices/history are preserved.
- [ ] The section fits the existing README's tone/structure (placed near other hub/setup or troubleshooting content).

Use your internal Todo tool to track these and keep on track.

## Technical Requirements
- File: `README.md`.

## Input Dependencies
None (procedure is described from the plan; no internal symbols needed).

## Output Artifacts
- README docs section.

## Implementation Notes
<details>
<summary>Guidance</summary>

Find an appropriate location (after hub setup / near any troubleshooting or
discovery section — `grep -n "^##" README.md` to pick a spot). Keep it concise.
Suggested content:

```markdown
## Replacing a radio

If your RTL-SDR dongle dies, you can swap in a replacement without losing any of
your decoded devices, their history, or your automations — the hub config entry
is re-pointed at the new radio in place.

1. Remove the dead dongle and plug in the replacement (any USB port).
2. In the **rtl_433 add-on**, stamp the replacement with a fresh serial if needed
   (`force_randomize_serial` / `randomize_default_serial`), restart it, and note
   the new radio's **ID (`unique_id`)** and **host:port** from the add-on
   log/status.
3. In **Home Assistant**, either:
   - open the **"rtl_433 server unreachable"** repair card (it appears once the
     old radio stops responding) and enter the replacement's radio ID and
     connection details to re-point the hub; **or**
   - go to the rtl_433 hub → **Reconfigure** and enter the new **radio ID**
     (plus host/port if they changed). If discovery already created a duplicate
     hub for the new radio, the reconfigure adopts its identity and removes the
     duplicate.
4. All decoded sensors, their history, and your automations are preserved,
   because the hub keeps the same internal entry.
```

Match heading depth and wording style to the surrounding README.
</details>
