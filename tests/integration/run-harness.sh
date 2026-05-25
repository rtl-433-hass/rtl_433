#!/usr/bin/env bash
# Orchestrate the containerized rtl_433 -> Home Assistant -> Playwright screenshot
# harness with strict background+poll discipline (plan Clarification #14): no
# single multi-minute blocking command; every long step is launched detached and
# polled for an explicit readiness signal before the next step runs.
#
# Usage:  ./run-harness.sh            # full run
#         ./run-harness.sh up         # just bring containers up + verify WS
#         ./run-harness.sh down       # tear everything down
#
# Run from tests/integration/. Requires: docker compose, node, and a Playwright
# chromium install (npx playwright install chromium; system libs via
# `sudo npx playwright install-deps` or apt — see README).

set -euo pipefail
cd "$(dirname "$0")"

COMPOSE="docker compose"
HA_BASE="http://localhost:8123"
WS_URL="ws://localhost:8433/ws"
RTL_CONTAINER="rtl433_harness"
HA_CONTAINER="ha_harness"

log() { printf '[harness] %s\n' "$*"; }

ensure_submodule() {
  if [ ! -f rtl_433_tests/tests/acurite/Acurite_592TXR/acurite-592txr-003.cu8 ]; then
    log "submodule capture missing; initializing sparse submodule"
    git -C ../.. submodule update --init tests/integration/rtl_433_tests
  fi
}

# --- bring up containers (detached) and poll for readiness ------------------
up() {
  ensure_submodule
  log "starting containers (detached)"
  $COMPOSE up -d

  log "polling rtl_433 WebSocket for decoded JSON (up to 90s)"
  poll_ws_json 90

  log "polling Home Assistant API for readiness (up to 180s)"
  poll_ha_api 180
}

# Connect to the rtl_433 WS from inside a short-lived node and assert at least
# one JSON frame with model/temperature arrives. Bounded, non-blocking loop.
poll_ws_json() {
  local deadline=$(( $(date +%s) + ${1:-90} ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    if node ./ws-probe.mjs "$WS_URL" 2>/dev/null; then
      log "rtl_433 WebSocket emitted decoded JSON"
      return 0
    fi
    sleep 3
  done
  log "ERROR: rtl_433 WebSocket did not emit JSON within timeout"
  $COMPOSE logs --tail=40 rtl433 || true
  return 1
}

poll_ha_api() {
  local deadline=$(( $(date +%s) + ${1:-180} ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    # HA returns 200 (api_message) once core is up; 404/401 also mean it's alive.
    code=$(curl -s -o /dev/null -w '%{http_code}' "$HA_BASE/manifest.json" || echo 000)
    if [ "$code" = "200" ]; then
      log "Home Assistant HTTP is serving (manifest 200)"
      return 0
    fi
    sleep 5
  done
  log "ERROR: Home Assistant did not become ready within timeout"
  $COMPOSE logs --tail=60 homeassistant || true
  return 1
}

onboard() {
  log "seeding HA onboarding via REST API"
  HA_BASE="$HA_BASE" node ./ha-onboard.mjs
  echo
}

shots() {
  log "driving Playwright: add hub, capture discovery/device/options (sets 15s timeout)"
  HA_BASE="$HA_BASE" RTL_HOST=wsbridge RTL_PORT=8433 RTL_PATH=/ws SHORT_TIMEOUT=15 STAGE=add \
    node ./screenshot.mjs
}

# Stop the replay so the device goes silent. The options flow above lowered the
# hub availability timeout to 15s and the watchdog ticks every 30s, so ~50s after
# stopping the replay the device's entities flip to unavailable; then capture it.
unavailable() {
  log "stopping rtl_433 replay to force device silence"
  $COMPOSE stop rtl433 || true
  log "waiting 50s for the watchdog to mark the device unavailable"
  # Bounded wait; the watchdog logs 'went unavailable' in the HA container.
  end=$(( $(date +%s) + 70 ))
  while [ "$(date +%s)" -lt "$end" ]; do
    if $COMPOSE logs --since 90s homeassistant 2>/dev/null | grep -q "went unavailable"; then
      log "device marked unavailable"
      break
    fi
    sleep 5
  done
  HA_BASE="$HA_BASE" STAGE=unavail node ./screenshot.mjs
  log "resuming replay so the device recovers"
  $COMPOSE start rtl433 || true
}

down() {
  log "tearing down"
  $COMPOSE down -v || true
}

main() {
  case "${1:-full}" in
    up) up ;;
    down) down ;;
    onboard) onboard ;;
    shots) shots ;;
    unavailable) unavailable ;;
    full)
      up
      onboard
      shots
      unavailable
      log "done; screenshots in ../../screenshots"
      ;;
    *) echo "usage: $0 [full|up|down|onboard|shots|unavailable]" >&2; exit 2 ;;
  esac
}

main "$@"
