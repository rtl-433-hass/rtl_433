// WebSocket bridge that re-serves rtl_433's decoded JSON events on /ws.
//
// WHY THIS EXISTS (see README "Known limitation"): rtl_433's native `-F http`
// WebSocket server only pumps its mongoose event loop in the LIVE-SDR code path.
// When rtl_433 reads from a file or FIFO (`-r cu8:...`) it runs in "test mode",
// which decodes and then exits WITHOUT ever entering the HTTP event loop
// (src/rtl_433.c: the `if (cfg->in_files.len)` block returns/exit(0) before the
// `while (!exit_async) mg_mgr_poll()` loop). So the `-F http` /ws endpoint binds
// the port but never broadcasts replayed events.
//
// This bridge keeps the plan's FIFO keep-alive replay intact, but takes rtl_433's
// `-F json` STDOUT (which DOES stream continuously from a FIFO) and rebroadcasts
// each event to every connected WebSocket client on path /ws — i.e. exactly the
// frames the Home Assistant integration's coordinator expects from a real
// rtl_433 `-F http` server. It is a faithful stand-in for screenshot/integration
// purposes only.
//
// Tails a newline-delimited rtl_433 JSON event file (rtl_433 writes it with
// `-F json:<file>` into a shared volume); serves ws://0.0.0.0:PORT/ws.

import { createServer } from "node:http";
import { spawn } from "node:child_process";
import { createInterface } from "node:readline";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { WebSocketServer } from "ws";

const PORT = Number(process.env.WS_PORT || 8433);
// File that rtl_433 appends JSON events to (shared volume). The bridge tails it.
const EVENTS_FILE = process.env.EVENTS_FILE || "/shared/events.jsonl";
// File that rtl_433 appends its own log messages to (`-F log:<file>`, shared
// volume). Tailed and re-framed as structured log frames — see the log section.
const LOG_FILE = process.env.LOG_FILE || "/shared/rtl433.log";

// Optional: replay project-authored JSON fixtures alongside the live Acurite
// capture so the screenshot harness sees more device types (a doorbell event
// entity, an energy meter for the calibration step, a door + leak sensor) than
// the single .cu8 capture provides. FIXTURE_FILES is a comma-separated list of
// filenames inside FIXTURE_DIR; each file is the same JSON-array shape used by
// the pytest fixtures (tests/fixtures/*.json). Disabled when FIXTURE_FILES is
// empty. See README "Replaying extra fixtures for screenshots".
const FIXTURE_DIR = process.env.FIXTURE_DIR || "/fixtures";
const FIXTURE_FILES = (process.env.FIXTURE_FILES || "")
  .split(",")
  .map((s) => s.trim())
  .filter(Boolean);
const FIXTURE_INTERVAL_MS = Number(process.env.FIXTURE_INTERVAL_MS || 8000);
// Gap between the individual events of one fixture round. The events MUST NOT
// share a timestamp: the client library classifies a frame stamped at or before
// its high-water mark as an already-seen replay, and a replayed frame is never a
// device's first live sighting — so a whole round emitted in one burst would
// leave only its first device visible to Home Assistant. Real transmitters are
// independent, so spacing the round out is also the more faithful replay.
// Keep FIXTURE_STEP_MS * events < FIXTURE_INTERVAL_MS so rounds do not overlap.
const FIXTURE_STEP_MS = Number(process.env.FIXTURE_STEP_MS || 1200);

const httpServer = createServer((req, res) => {
  // A trivial UI/health endpoint so curl and HA reachability checks see a 200.
  if (req.url === "/" || req.url === "/health") {
    res.writeHead(200, { "Content-Type": "text/plain" });
    res.end("rtl_433 ws-bridge: connect to /ws for JSON events\n");
    return;
  }
  res.writeHead(404);
  res.end();
});

// Serve WS only on /ws to mirror the rtl_433 -F http endpoint.
const wss = new WebSocketServer({ server: httpServer, path: "/ws" });
const clients = new Set();
wss.on("connection", (ws) => {
  clients.add(ws);
  ws.on("close", () => clients.delete(ws));
  ws.on("error", () => clients.delete(ws));
});

const broadcast = (line) => {
  for (const ws of clients) {
    if (ws.readyState === ws.OPEN) {
      try {
        ws.send(line);
      } catch {
        clients.delete(ws);
      }
    }
  }
};

// Tail the shared events file with `tail -F` (follows across truncation/rotation
// and waits for the file to appear). Each line is one rtl_433 JSON event.
let count = 0;
const tail = spawn("tail", ["-n", "0", "-F", EVENTS_FILE]);
const rl = createInterface({ input: tail.stdout });
process.stderr.write(`ws-bridge: tailing ${EVENTS_FILE}\n`);
rl.on("line", (line) => {
  const text = line.trim();
  if (!text || text[0] !== "{") return; // skip non-JSON log lines
  try {
    JSON.parse(text);
  } catch {
    return;
  }
  count++;
  if (count % 25 === 0) {
    process.stderr.write(`ws-bridge: relayed ${count} events, ${clients.size} client(s)\n`);
  }
  broadcast(text);
});
// --- Optional fixture replay ------------------------------------------------
// Load the configured fixtures once, flatten their arrays into a flat event
// list, and re-broadcast the whole set on an interval, one event every
// FIXTURE_STEP_MS. Each emit restamps the event's `time` to "now" so HA treats
// successive doorbell presses as fresh events (an event entity keys on the
// timestamp), availability stays live, and no two events of a round share a
// timestamp (see FIXTURE_STEP_MS).
const pad = (n) => String(n).padStart(2, "0");
const rtlTime = (d) =>
  `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ` +
  `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;

const fixtureEvents = [];
for (const name of FIXTURE_FILES) {
  try {
    const parsed = JSON.parse(readFileSync(join(FIXTURE_DIR, name), "utf-8"));
    const list = Array.isArray(parsed) ? parsed : [parsed];
    fixtureEvents.push(...list);
    process.stderr.write(`ws-bridge: loaded ${list.length} event(s) from ${name}\n`);
  } catch (e) {
    process.stderr.write(`ws-bridge: failed to load fixture ${name}: ${e.message}\n`);
  }
}
if (fixtureEvents.length) {
  setInterval(() => {
    fixtureEvents.forEach((ev, index) => {
      // Stamped at emit time, not round time, so every event of the round
      // carries a distinct, strictly increasing timestamp.
      setTimeout(
        () => broadcast(JSON.stringify({ ...ev, time: rtlTime(new Date()) })),
        index * FIXTURE_STEP_MS,
      );
    });
  }, FIXTURE_INTERVAL_MS);
  process.stderr.write(
    `ws-bridge: replaying ${fixtureEvents.length} fixture event(s) every ` +
      `${FIXTURE_INTERVAL_MS}ms, ${FIXTURE_STEP_MS}ms apart\n`,
  );
}

// --- rtl_433 log frames -----------------------------------------------------
// A real rtl_433 `-F http` server forwards its own log messages to every
// connected WebSocket client as {"time","src","lvl","msg"} frames (>= 23.11).
// That channel is the ONLY place rtl_433 exposes its receiver noise floor — the
// pulse detector's "Auto Level" messages, which the integration parses into the
// hub's noise-level / minimum-detection-level sensors. The decoded-event JSON
// output carries no log messages at all, so the bridge tails rtl_433's
// `-F log:<file>` output (plain "<src>: <msg>" lines) and re-frames each line
// exactly as the HTTP server would, keeping the transport stand-in complete.
//
// The text log output drops the numeric level, so it is restored per source:
// the "Auto Level" messages are emitted at LOG_WARNING, everything else the
// harness produces is informational. Only src/msg are load-bearing downstream.
const LOG_LINE_RE = /^([A-Z][\w /-]*): (.+)$/;
const LOG_LEVEL_BY_SRC = { "Auto Level": 4 };
const DEFAULT_LOG_LEVEL = 5;

let logCount = 0;
const logTail = spawn("tail", ["-n", "0", "-F", LOG_FILE]);
const logRl = createInterface({ input: logTail.stdout });
process.stderr.write(`ws-bridge: tailing ${LOG_FILE} for log frames\n`);
logRl.on("line", (line) => {
  const match = LOG_LINE_RE.exec(line.trim());
  if (!match) return; // banner/continuation lines carry no "<src>: <msg>" shape
  const [, src, msg] = match;
  logCount++;
  if (src === "Auto Level") {
    process.stderr.write(`ws-bridge: log frame -> ${src}: ${msg}\n`);
  }
  broadcast(
    JSON.stringify({
      time: rtlTime(new Date()),
      src,
      lvl: LOG_LEVEL_BY_SRC[src] ?? DEFAULT_LOG_LEVEL,
      msg,
    }),
  );
});
logTail.stderr.on("data", (d) => process.stderr.write(`tail(log): ${d}`));
// Log frames are supplementary: if this tail dies the event stream is still
// valid, so unlike the events tail it does not take the bridge down.
logTail.on("exit", (code) =>
  process.stderr.write(`ws-bridge: log tail exited (${code}) after ${logCount} frame(s)\n`),
);

tail.stderr.on("data", (d) => process.stderr.write(`tail: ${d}`));
tail.on("exit", (code) => {
  process.stderr.write(`ws-bridge: tail exited (${code}); shutting down\n`);
  process.exit(1);
});

httpServer.listen(PORT, "0.0.0.0", () => {
  process.stderr.write(`ws-bridge: serving ws://0.0.0.0:${PORT}/ws\n`);
});
