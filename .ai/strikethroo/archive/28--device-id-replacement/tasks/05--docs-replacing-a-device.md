---
id: 5
group: "documentation"
dependencies: [3]
status: "completed"
created: 2026-08-31
skills:
  - technical-writing
---
# Document the replace flow and the device serial number

## Objective

Make the battery-swap recovery discoverable: document why a sensor's id changes,
how to run the replace, and exactly what is preserved — plus the new serial
number on the device pane that lets a user identify the right candidate.

## Skills Required

`technical-writing` — user-facing documentation matching the existing voice of
`docs/`, plus a short machine-oriented note in `AGENTS.md`.

## Acceptance Criteria

- [ ] `docs/device-discovery.md` gains a **"Replacing a device that changed id"**
      section covering: why ids change on a battery swap; the step-by-step
      recovery through **Settings → Devices & Services → rtl_433 → Configure →
      Replace device**; and what is preserved (entity ids and history,
      calibration, timeout override, motion clear delay, event types) versus what
      is not (the duplicate device's own brief history).
- [ ] The same file notes that a device's decoded id now appears as the **serial
      number** on the device pane, and that it survives renaming the device.
- [ ] The section works with discovery turned off (the doc already recommends
      that for urban areas) — it states that a replacement heard by the receiver
      can be adopted even when it was never registered.
- [ ] `AGENTS.md` gains a short note in the config-entry model section that
      `device_key` is re-pointable and that
      `custom_components/rtl_433/device_replace.py` is the only sanctioned place
      to rewrite a nested device's identity.
- [ ] `COMPATIBILITY_CONTRACT.md` is **not** modified.
- [ ] No broken internal links; the docs build cleanly if a build is configured
      (`mkdocs.yml` is present at the repo root).

Use your internal Todo tool to track these and keep on track.

## Technical Requirements

- Files: `docs/device-discovery.md`, `AGENTS.md`.
- `docs/device-discovery.md` already owns the device lifecycle (Post-Connection
  Registration, Discovery Toggle, Deleting Devices) — the new section belongs
  there, after "Deleting Devices".
- Match the existing docs voice: second person, short paragraphs, **bold** for UI
  paths, no marketing tone.

## Input Dependencies

- Task 3: the final step names, menu label, and wording of the options flow — the
  docs must match the shipped strings exactly.

## Output Artifacts

- The new documentation section and the `AGENTS.md` note.

## Implementation Notes

<details>
<summary>Step-by-step implementation</summary>

1. Read `docs/device-discovery.md` in full first — it is short, and the new
   section must sit naturally alongside "Deleting Devices" and reuse its
   **Settings → Devices & Services → …** path formatting.

2. Read the shipped strings in
   `custom_components/rtl_433/translations/en.json` (`options.step.replace` and
   `options.step.replace_target`) and use the *same* menu label and field names in
   the docs. Do not paraphrase the UI.

3. Draft the section roughly as:

   ```markdown
   ## Replacing a Device That Changed Id

   Many battery-powered sensors pick a new random transmitter id every time
   their batteries are changed. rtl_433 identifies devices by that id, so the
   sensor comes back as a **new** device with new entities and no history, while
   the original stops updating.

   To move the original device onto its new id, open **Settings → Devices &
   Services → rtl_433 → Configure → Replace device**. Pick the device you want to
   keep, then pick the newly discovered device that is really the same hardware.

   The device you keep takes over the new id, and the duplicate is removed. Its
   entity ids stay the same, so history, statistics, dashboards and automations
   continue uninterrupted, and its calibration, availability timeout override,
   motion clear delay and event types are carried across. The short history the
   duplicate recorded before the replace is discarded.

   This works even with device discovery turned off: a replacement the receiver
   has heard can be adopted without ever being registered.
   ```

   Adjust the wording to match the actual shipped strings.

4. Add a short paragraph — either in this section or under the device page
   screenshot near the top — noting that a device's decoded id (with its channel
   and subtype when it has them) is shown as the **Serial number** on the device
   info card, and that unlike the device name it is not affected by renaming the
   device. This is what a user reads to confirm which candidate to pick.

5. In `AGENTS.md`, find the "Config-entry model (hub + nested devices)" section
   and add two or three sentences: nested-device identity (`device_key`) is
   re-pointable via `async_replace_device` in
   `custom_components/rtl_433/device_replace.py`; that helper is the only place
   allowed to rewrite a device's registry identifiers or entity unique_ids; and
   it re-emits the `COMPATIBILITY_CONTRACT.md` templates verbatim, so the
   contract is unchanged.

6. Do not touch `COMPATIBILITY_CONTRACT.md` — the templates did not change, and
   editing it would wrongly imply an ABI change.

7. If `mkdocs` is available, build the docs to confirm nothing broke; otherwise
   just re-read the rendered markdown for broken links and heading levels
   (`##` for top-level sections in this file).
</details>
