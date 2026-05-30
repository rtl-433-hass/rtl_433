// Playwright driver for the rtl_433 integration screenshot harness.
//
// Prereqs (handled by run-harness.sh): the rtl433 + wsbridge + HA containers are
// up, HA onboarding is seeded (ha-onboard.mjs), and the WebSocket is emitting
// JSON (verified with ws-probe.mjs).
//
// Stages (STAGE env var):
//   add      - log in, add the rtl_433 hub via the config flow (host=wsbridge),
//              then — in the new hub model — the RF device appears AUTOMATICALLY
//              as a nested device (gated by the per-hub discovery toggle, which
//              defaults on). There is NO discovery card to accept. Navigate to
//              the integration's devices, open the nested device, and capture the
//              device page; then open the options flow MENU (Hub settings /
//              Device settings / Device mappings), capture it, open Hub settings,
//              set a low availability timeout (15s) so the unavailable stage is
//              fast, capture the hub-settings form too, then re-open the menu,
//              open Device mappings, pre-fill the YAML editor with an example
//              override and capture it.
//   unavail  - (after run-harness.sh stops the rtl433 replay and waits past the
//              timeout) capture the device page with all entities Unavailable.
//   full     - add, then unavail (the orchestrator stops replay in between).
//
// Every capture is gated on a selector/state where practical, never a blind long
// sleep. Output goes to ../../screenshots. Selectors were validated against HA
// 2026.5.x; the config-flow form is an ha-form (inputs by name), per-entry
// options open via the gear icon on the integration's entries page, and the
// options flow is now a menu (async_show_menu with Hub/Device settings).

import { chromium } from "playwright";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import { mkdirSync } from "node:fs";

const __dirname = dirname(fileURLToPath(import.meta.url));
const SHOTS = resolve(__dirname, "../../screenshots");
mkdirSync(SHOTS, { recursive: true });

const BASE = process.env.HA_BASE || "http://localhost:8123";
const USERNAME = process.env.HA_USER || "harness";
const PASSWORD = process.env.HA_PASS || "harness-password-123";
// The wsbridge service name resolves over the compose network; it serves the
// rtl_433 events on /ws (see README "Known limitation").
const RTL_HOST = process.env.RTL_HOST || "wsbridge";
const RTL_PORT = process.env.RTL_PORT || "8433";
const RTL_PATH = process.env.RTL_PATH || "/ws";
// Low timeout so the unavailable stage is fast to demonstrate.
const SHORT_TIMEOUT = process.env.SHORT_TIMEOUT || "15";
const STAGE = process.env.STAGE || "full";

// Example override pre-filled into the Device-mappings YAML editor for the
// screenshot. Mirrors the documented "User overrides" example (docs/
// device-library.md): adds an unmapped field and re-classifies battery_ok as a
// low-battery binary problem sensor. Content only — the shot does not save it.
const EXAMPLE_MAPPINGS = `custom_field_C:
  platform: sensor
  device_class: temperature
  unit_of_measurement: "°C"
  state_class: measurement
  name: Custom Probe
  value_transform: { round: 1 }
  object_suffix: TC

battery_ok:
  platform: binary_sensor
  device_class: battery
  name: Battery
  payload: { on: "0", off: "1" }
  entity_category: diagnostic
  object_suffix: B
`;

const shot = async (page, name) => {
  await page.screenshot({ path: resolve(SHOTS, name) });
  console.log(`screenshot: ${resolve(SHOTS, name)}`);
};

async function login(page) {
  await page.goto(BASE, { waitUntil: "domcontentloaded" });
  await page.locator('input[name="username"]').first().waitFor({ state: "visible", timeout: 30000 });
  await page.locator('input[name="username"]').first().fill(USERNAME);
  await page.locator('input[name="password"]').first().fill(PASSWORD);
  await page.keyboard.press("Enter");
  await page.waitForTimeout(4000);
}

async function addHubAndCapture(page) {
  // --- Add the hub via the config flow -------------------------------------
  await page.goto(`${BASE}/config/integrations/dashboard/add?domain=rtl_433`, {
    waitUntil: "domcontentloaded",
  });
  await page.waitForTimeout(2500);
  // "Do you want to set up rtl_433?" confirm dialog.
  await page.getByRole("button", { name: /^ok$/i }).click({ timeout: 5000 }).catch(() => {});
  await page.waitForTimeout(2500);
  // ha-form user step: host/port/path inputs by name.
  const fill = async (key, value) => {
    const f = page.locator(`ha-dialog input[name="${key}"], dialog input[name="${key}"]`);
    if (await f.count()) await f.first().fill(String(value));
  };
  await fill("host", RTL_HOST);
  await fill("port", RTL_PORT);
  await fill("path", RTL_PATH);
  await page.getByRole("button", { name: /submit|next|finish/i }).first().click().catch(() => {});
  // Coordinator validates the WS connection; allow time.
  await page.waitForTimeout(5000);
  // Close the post-create "area assign" dialog if present.
  await page.getByRole("button", { name: /finish|close/i }).first().click({ timeout: 3000 }).catch(() => {});
  await page.waitForTimeout(1500);

  // --- Nested device page (NEW model) --------------------------------------
  // There is no discovery card. With discovery on (default), the RF device shows
  // up automatically as a nested device under the hub. Poll the integration's
  // device list until it appears, then open it and capture the entities.
  await page.goto(`${BASE}/config/integrations/integration/rtl_433`, { waitUntil: "domcontentloaded" });
  const device = page.locator("text=Acurite-Tower").last();
  // The coordinator must observe an event and the platform must add the nested
  // device + entities; bounded poll (~40s) on the device link appearing.
  for (let i = 0; i < 20 && (await device.count()) === 0; i++) {
    await page.waitForTimeout(2000);
    await page.reload({ waitUntil: "domcontentloaded" });
  }
  await device.click();
  await page.waitForTimeout(3000);
  await shot(page, "02-device-page.png");

  // --- Options flow MENU (Hub settings / Device settings / Device mappings) -
  await openOptionsMenu(page);
  await shot(page, "03-options-flow.png");

  // --- Hub settings form: lower the availability timeout, then submit -------
  // Open Hub settings from the menu, set a low timeout (so the unavailable stage
  // is fast), and submit. Capturing the menu above already satisfies the docs;
  // this also exercises the live-options update path. The Hub-settings form has
  // a discovery checkbox (input[type=checkbox]) and the availability-timeout
  // field (input[type=number]); we only change the latter.
  await page.locator("text=Hub settings").first().click({ timeout: 8000 }).catch(() => {});
  await page.waitForTimeout(2500);
  const tf = page.locator("ha-dialog input[type=number], dialog input[type=number]").first();
  await tf.waitFor({ state: "visible", timeout: 8000 }).catch(() => {});
  if (await tf.count()) {
    await tf.fill(SHORT_TIMEOUT);
    // Blur so the ha-form commits the new value before we submit.
    await tf.press("Tab");
  }
  await page.getByRole("button", { name: /^submit$/i }).first().click({ timeout: 8000 }).catch(() => {});
  await page.waitForTimeout(2500);

  // --- Device mappings editor (NEW: UI-editable per-hub overrides) ----------
  // Re-open the menu, open Device mappings, pre-fill the YAML editor with an
  // example override, and capture it. We do NOT submit — saving validates and
  // reloads the hub; the screenshot only needs the editor showing real content.
  await openOptionsMenu(page);
  await captureMappings(page);
}

// Open the per-entry options flow, which lands on the menu step
// (Hub settings / Device settings / Device mappings). The menu items render as
// list rows (not button-role), so callers wait on / click by text.
async function openOptionsMenu(page) {
  await page.goto(`${BASE}/config/integrations/integration/rtl_433`, { waitUntil: "domcontentloaded" });
  await page.waitForTimeout(2500);
  const hubHeader = page.locator(`text=rtl_433 (${RTL_HOST})`).first();
  const box = await hubHeader.boundingBox();
  // The gear (options/Configure) icon sits at the right edge of the hub header.
  if (box) {
    await page.mouse.click(1243, box.y + box.height / 2);
  } else {
    // Fallback: open Configure from a kebab/Configure button if the header
    // layout shifts.
    await page.getByRole("button", { name: /configure/i }).first().click({ timeout: 5000 }).catch(() => {});
  }
  await page
    .locator("text=Hub settings")
    .first()
    .waitFor({ state: "visible", timeout: 8000 })
    .catch(() => {});
  await page.waitForTimeout(1500);
}

// From the open options menu, enter the Device mappings step and pre-fill the
// native YAML editor (ObjectSelector -> ha-yaml-editor -> ha-code-editor, a
// CodeMirror contenteditable). We seed it via clipboard paste: CodeMirror
// inserts pasted text verbatim, whereas typed Enter keys would auto-indent and
// mangle the YAML. Permissions are granted on the context in run().
async function captureMappings(page) {
  await page.locator("text=Device mappings").first().click({ timeout: 8000 }).catch(() => {});
  await page.waitForTimeout(2500);
  const editor = page
    .locator("ha-dialog ha-code-editor .cm-content, dialog ha-code-editor .cm-content")
    .first();
  await editor.waitFor({ state: "visible", timeout: 8000 }).catch(() => {});
  if (await editor.count()) {
    await editor.click();
    await page.keyboard.press("Control+a");
    await page.evaluate((text) => navigator.clipboard.writeText(text), EXAMPLE_MAPPINGS);
    await page.keyboard.press("Control+v");
    // Let CodeMirror re-render the pasted document before the capture.
    await page.waitForTimeout(1500);
  }
  await shot(page, "05-mapping-overrides.png");
}

async function captureUnavailable(page) {
  await page.goto(`${BASE}/config/integrations/integration/rtl_433`, { waitUntil: "domcontentloaded" });
  await page.waitForTimeout(2500);
  await page.locator("text=Acurite-Tower").last().click();
  await page.waitForTimeout(3000);
  await shot(page, "04-unavailable-state.png");
}

async function run() {
  const browser = await chromium.launch({ args: ["--no-sandbox"] });
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  // Needed to seed the Device-mappings YAML editor via clipboard paste.
  await context.grantPermissions(["clipboard-read", "clipboard-write"], { origin: BASE });
  const page = await context.newPage();
  try {
    await login(page);
    if (STAGE === "unavail") {
      await captureUnavailable(page);
    } else if (STAGE === "add") {
      await addHubAndCapture(page);
    } else {
      // full: add stage only; run-harness.sh stops replay then re-invokes unavail.
      await addHubAndCapture(page);
    }
  } finally {
    await browser.close();
  }
}

run().catch((e) => {
  console.error("screenshot.mjs error:", e.stack || e);
  process.exit(1);
});
