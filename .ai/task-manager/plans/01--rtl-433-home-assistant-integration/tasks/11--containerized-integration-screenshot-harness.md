---
id: 11
group: "testing"
dependencies: [9]
status: "completed"
created: 2026-05-25
skills:
  - docker
  - playwright
---
# Containerized Integration & Screenshot Harness

## Objective
Build the containerized end-to-end path: a pinned `hertzg/rtl_433` Docker container replays real `.cu8` captures (from the `merbanan/rtl_433_tests` repo, referenced as a pinned, shallow/sparse git submodule) through a named-pipe FIFO so its `-F http` WebSocket server streams continuously into a Home Assistant Docker container running the integration, and Playwright CLI captures screenshots of the discovery card, a device page, the options flow, and an unavailable state.

## Skills Required
- `docker` — compose, FIFO/named-pipe streaming, container readiness polling
- `playwright` — CLI-driven browser automation and screenshots

## Acceptance Criteria
- [ ] `merbanan/rtl_433_tests` is added as a **pinned git submodule** (`.gitmodules`) with a shallow/sparse checkout configured to fetch only the needed device directories (do not vendor the whole corpus).
- [ ] A `tests/integration/` (or `integration/`) directory contains: a `docker-compose.yml` (or scripts) bringing up `hertzg/rtl_433` (pinned tag/digest, arm64) and a Home Assistant container with the integration mounted, plus a FIFO writer loop script that feeds a `.cu8` into the pipe (`cu8:/path/fifo -s <rate> -F http`) keeping rtl_433's WebSocket server alive.
- [ ] A Playwright CLI script (Node) drives the HA UI: add a hub config entry (plain `ws://`) pointing at the rtl_433 container, then capture screenshots of: the discovery card at the top of Settings → Devices & Services, the accepted device page with entities, the options flow, and an unavailable-state example.
- [ ] All long-running steps (image pulls, container boot, browser install, replay) are launched in the **background and polled for readiness** — never one blocking command (plan Execution Notes / Clarification #14). Readiness is gated on explicit signals (HA API up, entity present) before capture.
- [ ] A README/runbook in the integration dir documents how to run the harness locally and how the FIFO keep-alive works.
- [ ] Captured screenshots are written to a `screenshots/` (or docs/images) directory for use by the documentation task.
- [ ] The harness runs to completion in this environment and produces the screenshots OR, if a specific step cannot complete here, the blocker is documented precisely in the runbook and the plan's execution summary (do not silently claim success).
- [ ] A single conventional commit (e.g. `test: add containerized integration and screenshot harness`).

Use your internal Todo tool to track these and keep on track.

## Technical Requirements
- Pin `hertzg/rtl_433` to a fixed tag or digest; confirm arm64. Pin the HA container (`ghcr.io/home-assistant/home-assistant:stable` or a fixed version) — confirm arm64.
- Submodule: `git submodule add https://github.com/merbanan/rtl_433_tests <path>` then configure sparse-checkout for only the device dirs needed (e.g. an Acurite set). Pin to a specific commit.
- FIFO approach (Clarification #13): `mkfifo`; a writer loop `while true; do cat capture.cu8 > fifo; done`; rtl_433 reads `cu8:/path/fifo -s 250k -F http`. This keeps one rtl_433 process and its WebSocket server alive for a continuous stream.
- Playwright CLI: install via `npx playwright install --with-deps chromium` (background + poll). Drive the HA onboarding/login and config-flow UI; wait on selectors/HA REST API readiness before screenshotting.
- Mount the integration into the HA container's `config/custom_components/rtl_433`.

## Input Dependencies
- Task 9: a functional integration to install into the HA container.
- (Conceptually) the YAML library + fixtures, but the replay uses real `.cu8` captures via rtl_433, not the unit fixtures.

## Output Artifacts
- The integration/screenshot harness and generated screenshots — satisfies Success Criteria #8 and produces images for Task 12 (docs). Also supports the plan's Self Validation steps.

## Implementation Notes
<details>
<summary>Detailed implementation guidance</summary>

1. **Submodule** (keep it light):
   - `git submodule add --depth 1 https://github.com/merbanan/rtl_433_tests tests/integration/rtl_433_tests`
   - Configure sparse checkout to only a couple of device dirs. Pin the submodule to a specific commit (`git -C <submodule> checkout <sha>`), then `git add` the gitlink + `.gitmodules`.
   - If sparse-checkout of a submodule is awkward, document the exact dirs needed and fetch shallowly. Avoid committing any `.cu8` into the main repo.
2. **rtl_433 container with FIFO**:
   - Use a small wrapper (entrypoint script or compose `command`) that `mkfifo /tmp/rtl.fifo`, starts `rtl_433 -r cu8:/tmp/rtl.fifo -s 250k -F http -M level` in the background, and runs `while true; do cat /data/<capture>.cu8 > /tmp/rtl.fifo; sleep 1; done`.
   - Expose port 8433. Verify the WebSocket emits JSON by `curl`/`websocat` against `/ws` (or check logs) before proceeding.
3. **HA container**:
   - Mount `./custom_components/rtl_433` → `/config/custom_components/rtl_433`. Pre-seed `/config/configuration.yaml` minimal. Boot, wait for the HA API (`GET /api/` returns 401/200) before driving the UI.
   - Onboarding: either script the onboarding API to create an owner + long-lived token, or use the UI via Playwright.
4. **Playwright**:
   - `npx playwright install --with-deps chromium` (background + poll for completion).
   - Script: log in, go to Settings → Devices & Services, add the `rtl_433` integration (host = rtl_433 container, port 8433, path `/ws`), wait for the discovery card, screenshot it; accept; screenshot the device page; open options; screenshot; stop replay / wait past a short timeout; screenshot unavailable.
5. **Background + poll everything**: use `run_in_background` for `docker pull`, `docker compose up`, `npx playwright install`, and the replay loop; poll with short non-blocking checks (curl/docker ps/log grep) in a bounded loop. Never issue a single multi-minute blocking command.
6. **Honesty**: if the HA container onboarding or Playwright cannot be fully driven in this environment, capture as far as possible, and clearly document the exact failing step + reproduction in the runbook and report it back — do not fabricate screenshots.
7. Own only `tests/integration/` (+ `.gitmodules`, submodule gitlink, `screenshots/`). Disjoint from Task 10's `tests/` unit files.
8. Commit `test:`.
</details>
