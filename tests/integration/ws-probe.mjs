// Connect to the rtl_433 -F http WebSocket and exit 0 if a decoded device event
// (a JSON object with a "model" field) arrives within a short window; else exit
// 1. Used by run-harness.sh as a bounded readiness probe — no long blocking.
//
// Uses Node's built-in WebSocket (Node >= 22), so no extra dependency.

const url = process.argv[2] || "ws://localhost:8433/ws";
const TIMEOUT_MS = 8000;

const ws = new WebSocket(url);
let done = false;

const finish = (code, msg) => {
  if (done) return;
  done = true;
  if (msg) process.stderr.write(msg + "\n");
  try {
    ws.close();
  } catch {}
  process.exit(code);
};

const timer = setTimeout(() => finish(1, "ws-probe: no model event within timeout"), TIMEOUT_MS);

ws.addEventListener("open", () => {
  process.stderr.write(`ws-probe: connected to ${url}\n`);
});

ws.addEventListener("message", (ev) => {
  const text = typeof ev.data === "string" ? ev.data : "";
  if (!text.trim()) return; // keep-alive
  try {
    const obj = JSON.parse(text);
    if (obj && typeof obj === "object" && obj.model) {
      clearTimeout(timer);
      process.stderr.write(`ws-probe: got event model=${obj.model}\n`);
      finish(0);
    }
  } catch {
    // ignore non-JSON frames
  }
});

ws.addEventListener("error", (e) => {
  finish(1, `ws-probe: ws error: ${e.message || e.type || e}`);
});

ws.addEventListener("close", () => {
  if (!done) finish(1, "ws-probe: closed before a model event");
});
