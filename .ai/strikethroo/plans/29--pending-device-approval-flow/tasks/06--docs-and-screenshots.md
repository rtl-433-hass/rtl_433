---
id: 6
group: "documentation"
dependencies: [4, 5]
status: "pending"
created: 2026-08-31
skills:
  - mkdocs-documentation
  - playwright
---
# Rewrite the discovery docs and capture screenshots of the new flow

## Objective

Document observe-then-approve and show it: rewrite
`docs/device-discovery.md` around the pending list, capture real screenshots of
the new options-flow steps with the existing container harness, and bring
`AGENTS.md` and the other docs in line with the removed toggle and notification.

## Skills Required

- `mkdocs-documentation` — the MkDocs Material site under `docs/`.
- `playwright` — the containerised screenshot harness in `tests/integration/`.

## Acceptance Criteria

- [ ] `docs/device-discovery.md` describes: that every heard device becomes a
      pending candidate and nothing is added automatically; how to add devices
      from `Settings → Devices & Services → rtl_433 → Configure → Add discovered
      devices`; how to ignore and un-ignore; that the pending list is in-memory
      and empty after a restart; and that deleting a device returns it to
      pending on its next transmission.
- [ ] The "Discovery Toggle" section is gone.
- [ ] New screenshots are captured from a live instance and embedded: the
      options menu showing the new entries, the populated "Add discovered
      devices" form, and the "Ignored devices" step.
- [ ] Any existing screenshot that shows the removed discovery toggle is
      recaptured.
- [ ] Every image has descriptive alt text, matching the style of the existing
      docs.
- [ ] `AGENTS.md` no longer describes the discovery toggle, the auto-add path, or
      the new-device notification, and describes the pending/adopt/ignore model
      instead.
- [ ] `docs/diagnostics.md` is corrected if it documents `discovery_enabled` in
      the diagnostics payload.
- [ ] `mkdocs build --strict` succeeds.
- [ ] The screenshots are genuinely captured from the running harness, not
      cropped from old images or hand-composed.

Use your internal Todo tool to track these and keep on track.

## Technical Requirements

- Harness: `tests/integration/` — `./run-harness.sh full`, `screenshot.mjs`,
  `docker-compose.yml`, and the runbook in `tests/integration/README.md`.
- Images live in `docs/images/` and are numbered sequentially
  (`02-device-page.png` … `14-hub-noise.png`); continue that numbering.
- The harness's `ws-bridge` replays recorded RF captures, so under the new
  behaviour the pending list populates on its own — no fixture faking required.

## Input Dependencies

- Tasks 4 and 5: the implementation must be test-verified before it is
  screenshotted, so the images do not capture behaviour that later changes.

## Output Artifacts

- A rewritten discovery page, new `docs/images/*.png`, and an accurate
  `AGENTS.md`.

## Implementation Notes

<details>
<summary>Detailed implementation guidance</summary>

### Capturing the screenshots

Read `tests/integration/README.md` first — it carries the prerequisites and the
orchestrator steps. The short version:

1. `cd tests/integration && ./run-harness.sh full` brings up Home Assistant plus
   rtl_433 replaying captures through the Node WebSocket bridge.
2. The captures transmit several distinct devices, so within a minute the hub's
   pending list holds a handful of candidates — that is the state to screenshot.
3. Extend `screenshot.mjs` with steps that navigate to
   `Settings → Devices & Services → rtl_433 → Configure`, capture the menu, open
   "Add discovered devices", capture the populated form, then (after adding one
   device and ignoring another) open "Ignored devices" and capture that.

Follow the selector and navigation style already in `screenshot.mjs`; do not
introduce a second automation approach alongside it.

If the harness cannot be run in the execution environment (no Docker), **stop and
report that** rather than substituting hand-made or reused images. A fabricated
screenshot of a flow is worse than a documented gap — say plainly which images
could not be captured and leave the prose complete.

### Rewriting the page

Structure to aim for:

- **How discovery works** — every device the server decodes is heard, but nothing
  is added to Home Assistant until you add it. Weak signals and neighbours'
  devices show up here; that is expected and is why nothing is automatic.
- **Adding devices** — the path through Configure, what each row's model / key /
  sighting count / signal level means, and that adding several at once is one
  submit. Screenshot.
- **Ignoring devices** — what Ignore does, that it persists across restarts, and
  where to undo it. Screenshot.
- **The pending list is temporary** — it lives in memory and is empty after a
  restart or a reload; devices reappear as they transmit. This is deliberate.
- **Deleting a device** — deleting removes it from Home Assistant; it returns to
  the pending list on its next transmission, so ignore it if you want it gone for
  good.
- **Post-connection registration** — keep the existing section; it still applies
  to which frames count as a live sighting.

Keep the existing voice: short sentences, second person, no marketing tone. Match
the surrounding pages' heading depth so the MkDocs nav stays consistent.

### `AGENTS.md`

Search it for `discovery`, `notification`, and `SIGNAL_NEW_DEVICE`. The
guardrails section names the dispatcher signals and the discovery toggle;
the architecture notes describe the auto-add path. Both need updating to describe
the pending/adopt/ignore model and the fact that `new_device_callback` now fires
only for adopted devices and explicit adoption.

</details>
