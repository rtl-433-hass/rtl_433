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
// list, and re-broadcast the whole set on an interval. Each emit restamps the
// event's `time` to "now" so HA treats successive doorbell presses as fresh
// events (an event entity keys on the timestamp) and availability stays live.
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
    const stamp = rtlTime(new Date());
    for (const ev of fixtureEvents) {
      broadcast(JSON.stringify({ ...ev, time: stamp }));
    }
  }, FIXTURE_INTERVAL_MS);
  process.stderr.write(
    `ws-bridge: replaying ${fixtureEvents.length} fixture event(s) every ${FIXTURE_INTERVAL_MS}ms\n`,
  );
}

tail.stderr.on("data", (d) => process.stderr.write(`tail: ${d}`));
tail.on("exit", (code) => {
  process.stderr.write(`ws-bridge: tail exited (${code}); shutting down\n`);
  process.exit(1);
});

httpServer.listen(PORT, "0.0.0.0", () => {
  process.stderr.write(`ws-bridge: serving ws://0.0.0.0:${PORT}/ws\n`);
});
