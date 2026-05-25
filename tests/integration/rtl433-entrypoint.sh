#!/bin/sh
# rtl_433 FIFO keep-alive entrypoint for the containerized screenshot harness.
#
# Problem: `rtl_433 -r <file>` reads a capture exactly once and then exits, which
# would tear down output after a single pass. Home Assistant needs a *continuous*
# stream so its coordinator stays connected and devices keep reporting (otherwise
# the availability watchdog flips them to unavailable).
#
# Solution (plan Clarification #13): create a named pipe (FIFO), start ONE
# long-lived rtl_433 process reading from the FIFO with an explicit format prefix
# and sample rate, and a writer loop that re-feeds the capture into the FIFO
# forever. The reader (rtl_433) opens the FIFO FIRST and blocks; the writer then
# opens it and keeps an fd open across passes, so rtl_433 never sees EOF and the
# single process stays alive emitting decoded JSON events continuously.
#
# IMPORTANT (see README "Known limitation"): rtl_433's native `-F http` /ws
# WebSocket server does NOT broadcast events when reading from a file/FIFO — that
# code path ("test mode") never enters the mongoose HTTP event loop. So instead
# of `-F http`, we emit `-F json:<file>` into a shared volume and a small Node
# `ws-bridge` service re-serves those events on ws://.../ws (the endpoint HA's
# coordinator expects). The FIFO keep-alive itself works exactly as the plan
# describes; only the transport to HA changed from native -F http to the bridge.
#
# Environment variables (set by docker-compose):
#   CAPTURE      absolute path to the .cu8 capture to replay (in the container)
#   RATE         sample rate passed to rtl_433 (e.g. 250k) — match the capture
#   EVENTS_FILE  JSON-lines output file in the shared volume the bridge tails
#   FIFO         path of the named pipe (default /tmp/rtl.fifo)
#   LOOP_SLEEP   seconds to pause between replay passes (default 1)

set -eu

CAPTURE="${CAPTURE:-/data/capture.cu8}"
RATE="${RATE:-250k}"
EVENTS_FILE="${EVENTS_FILE:-/shared/events.jsonl}"
FIFO="${FIFO:-/tmp/rtl.fifo}"
LOOP_SLEEP="${LOOP_SLEEP:-1}"

if [ ! -f "$CAPTURE" ]; then
    echo "rtl433-entrypoint: capture not found at $CAPTURE" >&2
    echo "rtl433-entrypoint: mount the rtl_433_tests submodule and set CAPTURE" >&2
    exit 1
fi

# Fresh FIFO and events file on each start.
rm -f "$FIFO"
mkfifo "$FIFO"
mkdir -p "$(dirname "$EVENTS_FILE")"
: > "$EVENTS_FILE"

echo "rtl433-entrypoint: replaying $CAPTURE at -s $RATE -> $EVENTS_FILE (JSON lines)"

# Start the single, long-lived decoder writing JSON events to the shared file.
#   -r cu8:<fifo>      read raw I/Q (unsigned 8-bit complex) from the FIFO
#   -s <rate>          sample rate (required when reading a headerless pipe)
#   -F json:<file>     append decoded events as JSON lines (bridge tails this)
#   -M level           include signal-level metadata in events
rtl_433 -r "cu8:${FIFO}" -s "$RATE" -F "json:${EVENTS_FILE}" -M level &
RTL_PID=$!

# Open the FIFO for writing AFTER the reader exists, and hold the fd open across
# passes so rtl_433 never sees EOF (the keep-alive). fd 3 stays open for the loop.
exec 3>"$FIFO"

# Clean shutdown.
trap 'kill "$RTL_PID" 2>/dev/null || true; rm -f "$FIFO"; exit 0' INT TERM

# Writer loop: re-feed the capture forever. If the decoder dies, stop so the
# container exits and the orchestrator notices.
while kill -0 "$RTL_PID" 2>/dev/null; do
    cat "$CAPTURE" >&3
    sleep "$LOOP_SLEEP"
done

echo "rtl433-entrypoint: rtl_433 process exited; stopping writer loop" >&2
wait "$RTL_PID" 2>/dev/null || true
