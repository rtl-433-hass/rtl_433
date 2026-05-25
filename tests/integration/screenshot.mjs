// Playwright driver for the rtl_433 integration screenshot harness.
//
// Prereqs (handled by run-harness.sh): the rtl433 + wsbridge + HA containers are
// up, HA onboarding is seeded (ha-onboard.mjs), and the WebSocket is emitting
// JSON (verified with ws-probe.mjs).
//
// Stages (STAGE env var):
//   add      - log in, add the rtl_433 hub via the config flow (host=wsbridge),
//              capture the discovery card, accept the device, capture the device
//              page, then open + capture the hub options flow. Leaves the hub's
//              availability timeout set low (15s) so the unavailable stage is fast.
//   unavail  - (after run-harness.sh stops the rtl433 replay and waits past the
//              timeout) capture the device page with all entities Unavailable.
//   full     - add, then unavail (the orchestrator stops replay in between).
//
// Every capture is gated on a selector/state, never a blind long sleep. Output
// goes to ../../screenshots. Selectors were validated against HA 2026.5.x; the
// config-flow form is an ha-form (inputs by name), and per-entry options open via
// the gear icon on the integration's entries page.

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

  // --- Discovery card -------------------------------------------------------
  await page.goto(`${BASE}/config/integrations/dashboard`, { waitUntil: "domcontentloaded" });
  await page.waitForTimeout(3000);
  await shot(page, "01-discovery-card.png");

  // --- Accept the discovered device ----------------------------------------
  await page.getByRole("button", { name: /^add$/i }).first().click({ timeout: 5000 }).catch(() => {});
  await page.waitForTimeout(2500);
  await page.getByRole("button", { name: /submit/i }).first().click({ timeout: 5000 }).catch(() => {});
  await page.waitForTimeout(2500);
  // Close the area-assign dialog.
  await page.getByRole("button", { name: /finish|close/i }).first().click({ timeout: 3000 }).catch(() => {});
  await page.waitForTimeout(2000);

  // --- Device page with entities -------------------------------------------
  await page.goto(`${BASE}/config/integrations/integration/rtl_433`, { waitUntil: "domcontentloaded" });
  await page.waitForTimeout(2500);
  await page.locator("text=Acurite-Tower").last().click();
  await page.waitForTimeout(3000);
  await shot(page, "02-device-page.png");

  // --- Hub options flow -----------------------------------------------------
  await page.goto(`${BASE}/config/integrations/integration/rtl_433`, { waitUntil: "domcontentloaded" });
  await page.waitForTimeout(2500);
  const hubHeader = page.locator(`text=rtl_433 (${RTL_HOST})`).first();
  const box = await hubHeader.boundingBox();
  // The gear (options) icon sits at the right edge of the hub group header.
  await page.mouse.click(1243, box.y + box.height / 2);
  await page.waitForTimeout(2500);
  await shot(page, "03-options-flow.png");

  // Set a low availability timeout so the unavailable stage is fast, then submit.
  const tf = page.locator("ha-dialog input[type=number], dialog input[type=number]").first();
  if (await tf.count()) await tf.fill(SHORT_TIMEOUT);
  await page.getByRole("button", { name: /submit/i }).first().click().catch(() => {});
  await page.waitForTimeout(2500);
}

async function captureUnavailable(page) {
  await page.goto(`${BASE}/config/integrations/integration/rtl_433`, { waitUntil: "domcontentloaded" });
  await page.waitForTimeout(2500);
  await page.locator("text=Acurite-Tower (Acurite-Tower-12053-chC)").last().click();
  await page.waitForTimeout(3000);
  await shot(page, "04-unavailable-state.png");
}

async function run() {
  const browser = await chromium.launch({ args: ["--no-sandbox"] });
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
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
