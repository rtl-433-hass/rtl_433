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
#   LOG_FILE     rtl_433 log output file in the shared volume (Auto Level lines)
#   NOISE_SECS   -M noise interval in seconds (periodic noise reports)
#   SILENCE_BYTES  bytes of RF silence fed between capture passes (default 1MB
#                  = 2s at 250k, cu8); 0 disables the silence gap
#   FIFO         path of the named pipe (default /tmp/rtl.fifo)
#   LOOP_SLEEP   seconds to pause between replay passes (default 1)

set -eu

CAPTURE="${CAPTURE:-/data/capture.cu8}"
RATE="${RATE:-250k}"
EVENTS_FILE="${EVENTS_FILE:-/shared/events.jsonl}"
LOG_FILE="${LOG_FILE:-/shared/rtl433.log}"
NOISE_SECS="${NOISE_SECS:-10}"
SILENCE_BYTES="${SILENCE_BYTES:-1000000}"
SILENCE="${SILENCE:-/tmp/silence.cu8}"
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
mkdir -p "$(dirname "$LOG_FILE")"
: > "$LOG_FILE"

# RF silence played between capture passes (see the writer loop). cu8 samples are
# unsigned 8-bit I/Q pairs where 0x80 is zero amplitude, so a run of 0x80 bytes is
# a genuinely quiet input rather than a fabricated log line.
rm -f "$SILENCE"
if [ "$SILENCE_BYTES" -gt 0 ]; then
    head -c "$SILENCE_BYTES" /dev/zero | tr '\000' '\200' > "$SILENCE"
fi

echo "rtl433-entrypoint: replaying $CAPTURE at -s $RATE -> $EVENTS_FILE (JSON lines)"

# Start the single, long-lived decoder writing JSON events to the shared file.
#   -r cu8:<fifo>      read raw I/Q (unsigned 8-bit complex) from the FIFO
#   -s <rate>          sample rate (required when reading a headerless pipe)
#   -F json:<file>     append decoded events as JSON lines (bridge tails this)
#   -F log:<file>      append rtl_433's own log messages ("<src>: <msg>" lines).
#                      The decoded-event JSON output carries NO log messages, so
#                      the pulse detector's "Auto Level" noise lines — the only
#                      place rtl_433 exposes its noise floor, and the source of
#                      the hub's noise sensors — are only visible here. The
#                      ws-bridge tails this file and re-frames those lines the
#                      way a real `-F http` server sends them (see ws-bridge.mjs).
#   -M level           include signal-level metadata in events
#   -Y autolevel       auto-adjust the pulse detector's minimum level; logs
#                      "Estimated noise level is X dB, adjusting minimum
#                      detection level to Y dB" on every shift over 1 dB
#   -M noise:<secs>    periodic noise report ("Current noise level ...")
rtl_433 -r "cu8:${FIFO}" -s "$RATE" -F "json:${EVENTS_FILE}" -F "log:${LOG_FILE}" \
    -M level -Y autolevel -M "noise:${NOISE_SECS}" &
RTL_PID=$!

# Open the FIFO for writing AFTER the reader exists, and hold the fd open across
# passes so rtl_433 never sees EOF (the keep-alive). fd 3 stays open for the loop.
exec 3>"$FIFO"

# Clean shutdown.
trap 'kill "$RTL_PID" 2>/dev/null || true; rm -f "$FIFO"; exit 0' INT TERM

# Writer loop: re-feed the capture forever, each pass followed by a stretch of RF
# silence (cu8 zero-amplitude samples, byte 0x80). Back-to-back capture passes
# would keep the receiver permanently "loud": the noise estimate creeps up to the
# replayed burst level and settles, so `-Y autolevel` never sees a shift over
# 1 dB and never logs an adjustment (the source of the hub's minimum-detection-
# level sensor). The silence gap is also what a real receiver mostly hears — it
# makes the noise floor genuinely move, so both "Auto Level" message forms are
# emitted from real measurements. If the decoder dies, stop so the container
# exits and the orchestrator notices.
while kill -0 "$RTL_PID" 2>/dev/null; do
    cat "$CAPTURE" >&3
    if [ -s "$SILENCE" ]; then
        cat "$SILENCE" >&3
    fi
    sleep "$LOOP_SLEEP"
done

echo "rtl433-entrypoint: rtl_433 process exited; stopping writer loop" >&2
wait "$RTL_PID" 2>/dev/null || true
