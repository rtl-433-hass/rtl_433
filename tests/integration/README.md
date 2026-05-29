# Containerized integration & screenshot harness

This directory contains an end-to-end harness that drives the `rtl_433` Home
Assistant integration against **real RF captures**, with no SDR hardware, and
captures documentation screenshots with Playwright.

```text
 rtl_433_tests (.cu8)         hertzg/rtl_433                ws-bridge (Node)            Home Assistant
 ┌────────────────┐  FIFO    ┌──────────────┐  JSON lines  ┌──────────────┐  ws://…/ws ┌──────────────┐
 │ Acurite-592TXR │ ───────▶ │ rtl_433 -r   │ ───────────▶ │ tail + relay │ ─────────▶ │ rtl_433      │
 │  capture.cu8   │ keep-    │ cu8:fifo     │ /shared/     │ on /ws       │            │ integration  │
 └────────────────┘ alive    │ -F json:file │ events.jsonl │              │            │ (coordinator)│
                             └──────────────┘              └──────────────┘            └──────────────┘
```

## What it proves

A single `rtl_433` process replays a real Acurite capture continuously; the
integration connects over a WebSocket, discovers the device, creates entities
with correct device classes/units, and flips them to `unavailable` when the
stream stops. Playwright captures these screenshots (see `../../screenshots/`):

| File | Shows |
| --- | --- |
| `01-discovery-card.png` | The discovered-device card at the top of Settings → Devices & Services |
| `02-device-page.png` | The accepted device page: Temperature `26.7 °C`, Humidity `74.0%`, Battery `100%`, signal diagnostics |
| `03-options-flow.png` | The hub options flow menu (Hub settings / Device settings / Device mappings) |
| `04-unavailable-state.png` | The same device after the stream stops — all entities `Unavailable` |
| `05-mapping-overrides.png` | The **Device mappings** step: the YAML editor pre-filled with an example per-hub override |

## Prerequisites

- Docker + Docker Compose (tested on Docker 29.x, Compose v5), `arm64` or `amd64`
- Node 22+ (for the Playwright driver, the bridge, and the WS probe)
- Network egress to GitHub, ghcr.io, Docker Hub, and the Playwright CDN

One-time setup:

```bash
# 1. Fetch the pinned, sparse test-capture submodule (only the Acurite dirs).
git submodule update --init tests/integration/rtl_433_tests

# 2. Install Node deps (Playwright + ws) and the Chromium browser.
cd tests/integration
npm ci                                  # installs playwright + ws (pinned)
npx playwright install chromium         # browser binary only
sudo npx playwright install-deps chromium   # system libs (see note below)
```

> **Playwright system libs on Debian 13 / trixie:** `--with-deps` may fail
> because a couple of font packages (`ttf-unifont`, `ttf-ubuntu-font-family`)
> have no candidate. Install the real dependencies directly instead:
>
> ```bash
> sudo apt-get install -y libatk1.0-0 libatk-bridge2.0-0 libatspi2.0-0 \
>   libxdamage1 libxkbcommon0 libnss3 libcups2 libdrm2 libgbm1 \
>   libpango-1.0-0 libcairo2 libasound2
> ```

## Running

```bash
cd tests/integration
./run-harness.sh full      # up → onboard → screenshots → unavailable → recover
# or step by step:
./run-harness.sh up        # start containers, poll WS-JSON + HA API readiness
./run-harness.sh onboard   # seed HA owner + token via the onboarding REST API
./run-harness.sh shots     # add the hub, capture discovery/device/options
./run-harness.sh unavailable  # stop replay, wait out the timeout, capture, resume
./run-harness.sh down      # tear everything down (removes the shared volume)
```

All long-running steps run detached and are polled in bounded loops (image
pulls happen via `docker compose up`; readiness is gated on `ws-probe.mjs`
returning a decoded event and on the HA HTTP API answering `200`). Nothing is
ever a single multi-minute blocking command.

Default HA login created by the harness: **`harness` / `harness-password-123`**
(see `ha-onboard.mjs`). HA is on <http://localhost:8123>, the WebSocket stream on
`ws://localhost:8433/ws`.

## How the FIFO keep-alive works

`rtl_433 -r <file>` reads a capture **once** and exits, which would end the
stream after a single pass. To keep one decoder process — and a continuous event
stream — alive (plan Clarification #13), `rtl433-entrypoint.sh`:

1. Creates a named pipe: `mkfifo /tmp/rtl.fifo`.
2. Starts **one** long-lived decoder reading the FIFO:
   `rtl_433 -r cu8:/tmp/rtl.fifo -s 250k -F json:/shared/events.jsonl -M level`.
   The reader opens the FIFO **first** and blocks waiting for a writer.
3. Opens the FIFO for writing on fd 3 (`exec 3>fifo`) **after** the reader
   exists, then loops `cat <capture>.cu8 >&3; sleep 1` forever. Holding fd 3 open
   across passes means the decoder never sees EOF, so it stays alive and keeps
   decoding the same capture on repeat.

The ordering matters: opening the FIFO for write *before* the reader exists
deadlocks (a FIFO write-open blocks until a reader connects). Reader-first,
writer-second is the working pattern.

## Known limitation — why the `ws-bridge` exists

The plan (Clarification #13) called for `rtl_433 -F http` to serve the WebSocket
that Home Assistant connects to. **That does not work when rtl_433 reads from a
file or FIFO.** Verified against `hertzg/rtl_433` (rtl_433 **v25.12**):

- With `-r cu8:<fifo> -F http`, the HTTP server **binds** port 8433 and logs
  `Serving HTTP-API on address 0.0.0.0:8433`, but it never answers a single
  request — `curl http://localhost:8433/` (and `/events`, `/stream`, `/ws`) hang
  and return **0 bytes**, while `-F json` simultaneously shows events being
  decoded just fine.
- Root cause is in upstream `src/rtl_433.c`: file/`-r` input runs in **test
  mode** (`if (cfg->in_files.len) { … exit(0); }`) which decodes and exits
  **before** reaching the live loop `while (!exit_async) mg_mgr_poll(...)` that
  pumps the mongoose HTTP/WebSocket event loop. So `-F http` only streams when
  rtl_433 is driven by a live SDR device, not from a file/FIFO.

To keep the rest of the plan intact (real captures, FIFO keep-alive, the actual
HA integration, real discovery/availability), the harness emits `-F json:<file>`
into a shared volume and a tiny Node **`ws-bridge`** (`ws-bridge.mjs`) tails that
file and re-broadcasts each event on `ws://0.0.0.0:8433/ws` — exactly the frame
shape the integration's coordinator expects from a real `-F http` server. The
bridge is a faithful transport stand-in **for the harness only**; it is not part
of, and not required by, the shipped integration.

If you want to exercise rtl_433's *native* `-F http` server end-to-end, you must
feed it a **live-style** input — e.g. run an `rtl_tcp` replay server and point
rtl_433 at it as a device (`-d rtl_tcp:…`) instead of `-r`. That is a larger
change and was out of scope for this harness.

## Pinned versions

| Component | Pin |
| --- | --- |
| rtl_433 image | `hertzg/rtl_433@sha256:bcfd12afa59efc1ae8316ac21757b5e4161d4a42baaa91f609b4bcca9525dcfd` (rtl_433 25.12, arm64) |
| Home Assistant | `ghcr.io/home-assistant/home-assistant@sha256:ceb1202133a5a036e8b03e20a10eb113186cc2f871968323c6fc6c3fc4205716` (2026.5.4, arm64) |
| Node (bridge) | `node@sha256:968df39aedcea65eeb078fb336ed7191baf48f972b4479711397108be0966920` (node:22-alpine, arm64) |
| Captures submodule | `merbanan/rtl_433_tests` @ `1244ba1f79a9f1bd93fcd989dd2101b0f0c6cbc4`, sparse: `tests/acurite/Acurite_592TXR`, `tests/acurite/Acurite_606TX` |
| Playwright | `1.49.1` (see `package.json`) |

The Acurite-592TXR capture (`acurite-592txr-003.cu8`, sampled at 250k) decodes as
model **`Acurite-Tower`** with `temperature_C`, `humidity`, and `battery_ok` —
covering a temperature sensor (°C / `measurement`), a humidity sensor, and a
battery indicator in one device.

## Files

| File | Purpose |
| --- | --- |
| `docker-compose.yml` | The three services (rtl433, wsbridge, homeassistant), pinned by digest |
| `rtl433-entrypoint.sh` | FIFO keep-alive replay + `-F json:<file>` output |
| `ws-bridge.mjs` | Tails the JSON-lines file, serves `/ws` (see Known limitation) |
| `ws-probe.mjs` | Bounded readiness probe: connects to `/ws`, exits 0 on a decoded event |
| `ha-config/configuration.yaml` | Minimal HA seed config (debug logging for the integration) |
| `ha-onboard.mjs` | Seeds HA onboarding (owner + token) via the REST API |
| `screenshot.mjs` | Playwright driver: login, add hub, capture the four screenshots |
| `run-harness.sh` | Orchestrator with background+poll readiness gating |
| `rtl_433_tests/` | Pinned, sparse git submodule with the `.cu8` captures (not vendored) |
