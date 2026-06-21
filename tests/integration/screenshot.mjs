// Playwright driver for the rtl_433 integration screenshot harness.
//
// Prereqs (handled by run-harness.sh): the rtl433 + wsbridge + HA containers are
// up, HA onboarding is seeded (ha-onboard.mjs), and the WebSocket is emitting
// JSON (verified with ws-probe.mjs).
//
// Captured shots: 06 (empty config-flow form), 09 (integration overview / docs
// home hero), 02 (device page), 11 (doorbell event entity), 03 (options menu),
// 07 (Hub settings form), 05 (Device mappings YAML), 08 (Device settings form),
// 12 (calibration step), 10 (device page with the signal-diagnostic sensors
// enabled and populated), 04 (unavailable). The doorbell / energy meter / door /
// leak devices come from ws-bridge replaying tests/fixtures.
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
//   device   - re-capture only the Device settings + calibration steps against
//              an already-running harness (hub already added); for iterating.
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
  // Capture the empty "Connect to an rtl_433 server" form (docs: installation /
  // configuration) before we type anything into it.
  await page
    .locator('ha-dialog input[name="host"], dialog input[name="host"]')
    .first()
    .waitFor({ state: "visible", timeout: 8000 })
    .catch(() => {});
  await shot(page, "06-config-user.png");
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
  // Give the fixture-replayed devices (doorbell / energy meter / door / leak,
  // emitted by ws-bridge every few seconds) a moment to register too, so the
  // integration overview used as the docs home-page hero shows the full hub.
  await page.waitForTimeout(6000);
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.waitForTimeout(2000);
  await shot(page, "09-home-hero.png");

  // --- Device page (Acurite-Tower): entities + signal diagnostics ----------
  await device.click();
  await page.waitForTimeout(3000);
  // The device page doubles as the diagnostics surface (docs: diagnostics.md):
  // it carries the Diagnostic card, the "Download diagnostics" action, and the
  // disabled-by-default signal sensors (RSSI / SNR / noise).
  await shot(page, "02-device-page.png");

  // --- Event entity (docs: event-based-devices.md) -------------------------
  // The replayed Honeywell-Doorbell decodes to a momentary event entity.
  await page.goto(`${BASE}/config/integrations/integration/rtl_433`, { waitUntil: "domcontentloaded" });
  await page.waitForTimeout(2500);
  const doorbell = page.locator("text=Honeywell-Doorbell").last();
  if (await doorbell.count()) {
    await doorbell.click();
    await page.waitForTimeout(3000);
    await shot(page, "11-event-entity.png");
  } else {
    console.log("screenshot: doorbell device not present; skipping 11-event-entity.png");
  }

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
  // Capture the Hub settings form (docs: configuration / hub-entities) showing
  // its defaults before we lower the timeout.
  await shot(page, "07-hub-settings.png");
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

  // --- Device settings + calibration steps ---------------------------------
  await captureDeviceSettings(page);

  // --- Per-device signal diagnostics ---------------------------------------
  // Run last: it enables disabled-by-default entities and reloads the hub.
  await enableAndCaptureDiagnostics(page);
}

// The per-device signal-diagnostic sensors (frequency / RSSI / SNR / noise) are
// disabled by default, so the plain device page only shows "+N disabled
// entities". For docs/diagnostics.md we enable them via the authenticated
// frontend's WebSocket API (config/entity_registry/update -> disabled_by: null),
// reload the hub so the platform re-adds them, wait for a fresh Acurite event to
// populate real values, then capture the device page. The Acurite capture
// carries freq/rssi/snr/noise (decoded with -M level), so the values are real.
async function enableAndCaptureDiagnostics(page) {
  await page.goto(`${BASE}/config/integrations/integration/rtl_433`, { waitUntil: "domcontentloaded" });
  await page.waitForTimeout(2000);
  const info = await page.evaluate(async () => {
    const hass = document.querySelector("home-assistant")?.hass;
    if (!hass) return { error: "no hass on page" };
    const ents = await hass.callWS({ type: "config/entity_registry/list" });
    // Scope to the Acurite RF device's disabled signal sensors only — never the
    // hub's SDR center-frequency sensor.
    const targets = ents.filter(
      (e) =>
        e.platform === "rtl_433" &&
        e.disabled_by &&
        /acurite/i.test(e.entity_id) &&
        /(rssi|snr|noise|frequency|freq|last_seen)/i.test(e.entity_id),
    );
    let entryId = null;
    for (const e of targets) {
      entryId = e.config_entry_id || entryId;
      await hass.callWS({
        type: "config/entity_registry/update",
        entity_id: e.entity_id,
        disabled_by: null,
      });
    }
    return { enabled: targets.map((e) => e.entity_id), entryId };
  });
  console.log("screenshot: diagnostics enable -> " + JSON.stringify(info));
  if (info?.entryId) {
    // Reload immediately rather than waiting out HA's 30s auto-reload delay.
    await page.evaluate(async (entryId) => {
      const hass = document.querySelector("home-assistant")?.hass;
      await hass?.callWS({ type: "config_entries/reload", entry_id: entryId }).catch(() => {});
    }, info.entryId);
  }
  // Poll until the newly enabled sensors actually carry a numeric value (the
  // reload + reconnect + next Acurite event can take a while) so the capture
  // never shows "Unknown". Bounded ~40s.
  const ids = (info?.enabled || []).length
    ? info.enabled
    : ["sensor.acurite_tower_12053_chc_frequency", "sensor.acurite_tower_12053_chc_rssi"];
  for (let i = 0; i < 20; i++) {
    await page.waitForTimeout(2000);
    const ready = await page.evaluate((entityIds) => {
      const hass = document.querySelector("home-assistant")?.hass;
      if (!hass) return false;
      return entityIds.every((id) => {
        const st = hass.states[id];
        return st && st.state !== "unknown" && st.state !== "unavailable";
      });
    }, ids);
    if (ready) break;
  }
  await page.goto(`${BASE}/config/integrations/integration/rtl_433`, { waitUntil: "domcontentloaded" });
  await page.waitForTimeout(2500);
  await page.locator("text=Acurite-Tower").last().click();
  await page.waitForTimeout(3000);
  await shot(page, "10-diagnostics.png");
}

// Pick an option in an HA SelectSelector(DROPDOWN), which renders as an
// ha-select (mwc-select): click the anchor to open the menu, then click the
// list item whose text matches.
async function selectPick(selectLoc, page, typeahead, optionRegex) {
  await selectLoc.click();
  await page.waitForTimeout(700);
  const opt = page
    .locator("mwc-list-item, ha-list-item, vaadin-combo-box-item")
    .filter({ hasText: optionRegex })
    .first();
  if (await opt.count()) {
    await opt.scrollIntoViewIfNeeded().catch(() => {});
    const clicked = await opt
      .click({ timeout: 4000 })
      .then(() => true)
      .catch(() => false);
    if (clicked) {
      await page.waitForTimeout(600);
      return;
    }
  }
  // Fallback: mwc-select typeahead — type the option's leading text, commit.
  await page.keyboard.type(typeahead);
  await page.waitForTimeout(400);
  await page.keyboard.press("Enter");
  await page.waitForTimeout(600);
}

// Open the Device settings step, capture the form, then drive it to the
// calibration step by selecting the replayed energy meter and commodity=energy
// and capture that too. The forms are not submitted — the shots only need the
// rendered steps.
async function captureDeviceSettings(page) {
  await openOptionsMenu(page);
  await page.locator("text=Device settings").first().click({ timeout: 8000 }).catch(() => {});
  await page.waitForTimeout(2500);
  await shot(page, "08-device-settings.png");

  // Device picker (select 0) + commodity (select 1). The motion clear-delay
  // field only appears for motion-bearing devices, which the fixtures exclude.
  const selects = page.locator("ha-dialog ha-select, dialog ha-select");
  if ((await selects.count()) >= 2) {
    await selectPick(selects.nth(0), page, "EnergyMeter", /EnergyMeter/).catch(() =>
      console.log("screenshot: energy meter not pickable; using default device"),
    );
    await selectPick(selects.nth(1), page, "Energy", /Energy/).catch(() =>
      console.log("screenshot: commodity=Energy not pickable"),
    );
    await page
      .getByRole("button", { name: /^(submit|next)$/i })
      .first()
      .click({ timeout: 8000 })
      .catch(() => {});
    await page.waitForTimeout(2500);
    await shot(page, "12-calibration.png");
  } else {
    console.log("screenshot: device-settings selects not found; skipping 12-calibration.png");
  }
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
    } else if (STAGE === "device") {
      // Iterate the Device-settings + calibration captures against an already
      // running harness (hub already added). Not part of the full pipeline.
      await captureDeviceSettings(page);
    } else if (STAGE === "diagnostics") {
      // Iterate only the enable-and-capture diagnostics step against an already
      // running harness (hub already added).
      await enableAndCaptureDiagnostics(page);
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
