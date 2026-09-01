---
id: 4
group: "documentation"
dependencies: [3]
status: "pending"
created: 2026-09-01
skills:
  - mkdocs-documentation
  - playwright
---
# Document the panel and the WebSocket API, with a screenshot

## Objective

Present the panel as the primary way to add discovered devices, document the five
WebSocket commands, and capture the panel live from the container harness.

## Skills Required

- `mkdocs-documentation` — the MkDocs Material site under `docs/`.
- `playwright` — the containerised screenshot harness in `tests/integration/`.

## Acceptance Criteria

- [ ] `docs/device-discovery.md` presents the panel as the primary path — where
      to find it from the integration card, what the table shows, and the per-row
      Add / Ignore / Un-ignore actions — with the options flow kept as the
      alternative, not deleted.
- [ ] A screenshot of the populated panel, captured live, is embedded with
      descriptive alt text.
- [ ] `docs/websocket-api.md` documents all five commands and the subscription:
      parameters, response shape, admin requirement, and an example payload.
- [ ] `AGENTS.md` describes the shared adoption service, the WebSocket layer, the
      panel and its once-per-run registration guard, and the no-build-step
      constraint.
- [ ] `mkdocs build --strict` succeeds.
- [ ] The screenshot is genuinely captured from the running harness — not
      hand-composed, cropped, or reused.

Use your internal Todo tool to track these and keep on track.

## Technical Requirements

- Harness: `tests/integration/` — `./run-harness.sh full`, `screenshot.mjs`.
  The RF captures are already fetched at `tests/integration/rtl_433_tests`; do
  **not** re-run `scripts/fetch_captures.sh`.
- Images live in `docs/images/`, numbered sequentially — `16-ignored-devices.png`
  is the current highest.
- `docs/websocket-api.md` already exists; match its established structure rather
  than inventing a new one.

## Input Dependencies

- Task 3: the implementation must be test-verified before it is screenshotted.

## Output Artifacts

- Updated docs and a new panel screenshot.

## Implementation Notes

<details>
<summary>Detailed implementation guidance</summary>

### The screenshot

Extend `tests/integration/screenshot.mjs` in its existing style. Navigate to the
panel (it is registered at `/rtl_433` and reachable from the integration card),
wait for the table to populate from the replayed captures, and capture it with
several devices visible and their sighting counts legible. That is the shot that
shows why the panel beats the form.

Note that `run-harness.sh full` already drives the options-flow approval steps and
**adds five of the six devices**, which would leave the panel nearly empty. Either
capture the panel *before* that stage runs, or add devices to the replay set, so
the shot shows a genuinely populated table. Read the current `shots` stage before
deciding.

If a shot cannot be captured, **stop and report exactly what is missing and why**.
Do not hand-compose or reuse an image.

### The discovery page

The page was rewritten last plan and reads well; this is an edit, not another
rewrite. Add the panel as the primary route under "Adding Devices" and keep the
options-flow instructions as the alternative for a non-admin user or a browser
where the panel does not load. Be honest about the admin requirement.

Preserve the existing voice: short sentences, second person, no marketing tone.

### `docs/websocket-api.md`

Read the file first and follow its existing structure. For each command give the
message shape, the response shape, and one realistic example. Document that all
commands require an admin user, and that `rtl_433/devices/subscribe` sends the
current list immediately on subscribe and then pushes on change — including the
fact that repeat sightings are coalesced, so a client must not assume one message
per transmission.

### `AGENTS.md`

Add the new architecture to the integration overview: `adoption.py` as the single
implementation behind all three surfaces, `websocket_api.py`, and the panel with
its registration guard. State the no-build-step constraint plainly — it is a
decision a future agent could easily undo by reaching for a bundler.

</details>
