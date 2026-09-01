// One-off probe: what does a custom panel's `hass` actually carry, and which
// `ha-*` elements are registered? Answers the two questions the panel's area
// picker and entity-style rows depend on, against a real frontend rather than
// against assumption.
import { chromium } from "playwright";

const BASE = process.env.HA_BASE || "http://localhost:8123";
const USERNAME = process.env.HA_USER || "harness";
const PASSWORD = process.env.HA_PASS || "harness-password-123";

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });

await page.goto(BASE, { waitUntil: "domcontentloaded" });
await page
  .locator('input[name="username"]')
  .first()
  .waitFor({ state: "visible", timeout: 30000 });
await page.locator('input[name="username"]').first().fill(USERNAME);
await page.locator('input[name="password"]').first().fill(PASSWORD);
await page.keyboard.press("Enter");
await page.waitForTimeout(5000);

// Create one area so "are areas exposed?" is distinguishable from "are there
// any areas?" -- the whole ambiguity in the bug report.
const created = await page.evaluate(async () => {
  const hass = document.querySelector("home-assistant")?.hass;
  if (!hass) return { error: "no hass" };
  try {
    const existing = await hass.callWS({ type: "config/area_registry/list" });
    if (!existing.some((a) => a.name === "Patio")) {
      await hass.callWS({ type: "config/area_registry/create", name: "Patio" });
    }
    if (!existing.some((a) => a.name === "Garage")) {
      await hass.callWS({ type: "config/area_registry/create", name: "Garage" });
    }
    return { ok: true };
  } catch (e) {
    return { error: String(e) };
  }
});
console.log("create areas ->", JSON.stringify(created));

await page.waitForTimeout(3000);

const probe = await page.evaluate(async () => {
  const hass = document.querySelector("home-assistant")?.hass;
  const out = {
    hassPresent: Boolean(hass),
    areasType: typeof hass?.areas,
    areasCount: hass?.areas ? Object.keys(hass.areas).length : null,
    areaSample: hass?.areas ? Object.values(hass.areas).slice(0, 3) : null,
    devicesType: typeof hass?.devices,
    devicesCount: hass?.devices ? Object.keys(hass.devices).length : null,
    deviceHasIdentifiers: hass?.devices
      ? Object.values(hass.devices).some((d) => Array.isArray(d.identifiers))
      : null,
    elements: {},
  };
  for (const tag of [
    "ha-icon",
    "ha-svg-icon",
    "ha-area-picker",
    "ha-select",
    "ha-textfield",
    "ha-combo-box",
  ]) {
    out.elements[tag] = Boolean(customElements.get(tag));
  }
  // Areas over the connection, which is what the panel could use instead.
  try {
    out.wsAreas = (await hass.callWS({ type: "config/area_registry/list" })).map(
      (a) => a.name,
    );
  } catch (e) {
    out.wsAreas = String(e);
  }
  return out;
});

console.log("PROBE " + JSON.stringify(probe, null, 2));
await browser.close();
