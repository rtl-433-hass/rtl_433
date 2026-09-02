// Playwright driver for the rtl_433 integration screenshot harness.
//
// Prereqs (handled by run-harness.sh): the rtl433 + wsbridge + HA containers are
// up, HA onboarding is seeded (ha-onboard.mjs), and the WebSocket is emitting
// JSON (verified with ws-probe.mjs).
//
// Captured shots: 06 (empty config-flow form), 17 (the panel, live and
// populated), 16 (the ignored-devices section), 09 (integration overview / docs
// home hero), 02 (device page), 11 (doorbell event entity), 07 (Receiver
// settings dialog), 05 (Device mappings editor), 08 (Device settings dialog),
// 10 (device page with the signal-diagnostic sensors enabled and populated),
// 14 (the hub device's Diagnostic card with the receiver-noise sensors),
// 04 (unavailable). The doorbell / energy meter / door / leak devices come from
// ws-bridge replaying tests/fixtures.
//
// **Everything but the config flow is now driven through the panel.** The hub
// entry registers it with `config_panel_domain`, so Configure on the entry opens
// `/rtl_433` and there is no options dialog to drive: adding, ignoring,
// un-ignoring, receiver settings, device settings and device mappings are all
// controls inside the panel's shadow root, reached through `inPanel` below. A
// stage that finds no dialog says so loudly rather than capturing whatever page
// it landed on -- a settings form with no entry point is exactly the break this
// harness exists to catch.
//
// Stages (STAGE env var):
//   add      - log in, add the rtl_433 hub via the config flow (host=wsbridge).
//              Nothing is added to Home Assistant automatically: the heard
//              devices sit on the coordinator's in-memory pending list until
//              somebody clicks Add, so the run captures the panel, ignores the
//              leak detector to capture the ignored section, then un-ignores it
//              and adds every device the later shots need (approveDevices). It
//              then captures the integration overview and the device page; opens
//              Receiver settings, sets a low availability timeout (15s) so the
//              unavailable stage is fast and captures the dialog; then Device
//              mappings with an example override, Device settings against the
//              gas meter, and the per-device signal diagnostics.
//   approve  - re-capture only the approval / ignored-devices shots against an
//              already-running harness; for iterating.
//   panel    - re-capture only the panel itself against an already-running
//              harness (hub added, devices still pending); for iterating.
//   unavail  - (after run-harness.sh stops the rtl433 replay and waits past the
//              timeout) capture the device page with all entities Unavailable.
//   device   - re-capture only the Device settings dialog against an
//              already-running harness (hub already added); for iterating.
//   hub      - re-capture only the Receiver settings and Device mappings
//              dialogs against an already-running harness; for iterating.
//   hubnoise - re-capture only the hub Diagnostic card (receiver-noise sensors)
//              against an already-running harness; for iterating.
//   full     - add, then unavail (the orchestrator stops replay in between).
//
// Every capture is gated on a selector/state where practical, never a blind long
// sleep. Output goes to ../../screenshots. Selectors were validated against HA
// 2026.5.x; the config-flow form is an ha-form (inputs by name), and everything
// else is the panel's own markup, which this repository owns.

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

  // --- Discovery panel -----------------------------------------------------
  // Captured BEFORE the approval stage, which adds five of the six replayed
  // devices: a panel screenshot taken afterwards would show one row and none of
  // the reason the panel exists.
  await capturePanel(page);

  // --- Approve the heard devices -------------------------------------------
  // Nothing is added automatically: every device the server decodes lands on the
  // coordinator's in-memory pending list and reaches Home Assistant only when a
  // person adds it. This drives the cards, captures the ignored-devices section,
  // and leaves the hub holding the devices the later shots need.
  await approveDevices(page);

  // --- Integration overview (docs home-page hero) --------------------------
  await page.goto(`${BASE}/config/integrations/integration/rtl_433`, { waitUntil: "domcontentloaded" });
  const device = page.locator("text=Acurite-Tower").last();
  // The entity platforms build each adopted device from the same dispatcher
  // signal a live first sighting used; bounded poll (~40s) on the device link.
  for (let i = 0; i < 20 && (await device.count()) === 0; i++) {
    await page.waitForTimeout(2000);
    await page.reload({ waitUntil: "domcontentloaded" });
  }
  // Give the adopted fixture devices (doorbell / energy meter / door / leak) a
  // moment to finish registering too, so the integration overview used as the
  // docs home-page hero shows the full hub.
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

  // --- Receiver settings: lower the availability timeout, then save --------
  // Captured from the panel's own dialog, which is where these settings live
  // now: the config entry's Configure control opens the panel, so there is no
  // options form to drive. Lowering the timeout here is also what makes the
  // later unavailable stage fast.
  await captureHubSettings(page);

  // --- Device mappings editor ----------------------------------------------
  // Pre-fill the editor with an example override and capture it. NOT saved --
  // storing overrides reloads the hub, and the screenshot only needs the editor
  // showing real content.
  await captureMappings(page);

  // --- Device settings + calibration steps ---------------------------------
  await captureDeviceSettings(page);

  // --- Per-device signal diagnostics ---------------------------------------
  // Run last: it enables disabled-by-default entities and reloads the hub.
  await enableAndCaptureDiagnostics(page);
  // The hub's receiver-noise capture is a separate step (STAGE=hubnoise): it
  // needs the orchestrator to restart the decoder first — see captureHubNoise.
}

// Run `fn` with the panel element as its argument, inside the page.
//
// The panel sits several shadow roots deep (home-assistant -> … ->
// ha-panel-custom) and everything it shows lives inside its own shadow root, so
// every interaction with it has to start by walking the trees to find it. The
// walker is passed as source text because `page.evaluate` serializes the
// function it is given and closures do not survive that.
const PANEL_FINDER = `
  () => {
    const walk = (root, out = []) => {
      for (const el of root.querySelectorAll("*")) {
        out.push(el);
        if (el.shadowRoot) walk(el.shadowRoot, out);
      }
      return out;
    };
    return walk(document).find((el) => el.localName === "rtl-433-panel") || null;
  }
`;

function inPanel(page, body, arg) {
  return page.evaluate(
    ([finder, source, value]) => {
      const panel = eval(finder)();
      if (!panel) return { error: "panel not found" };
      return eval(source)(panel, value);
    },
    [PANEL_FINDER, body, arg],
  );
}

// Open the panel and wait for it to have rendered at least one candidate card.
async function openPanel(page, { cards = 1, tries = 30 } = {}) {
  await page.goto(`${BASE}/rtl_433`, { waitUntil: "domcontentloaded" });
  for (let i = 0; i < tries; i++) {
    const ready = await inPanel(
      page,
      `(panel) => panel.shadowRoot.querySelectorAll(".device-card").length`,
    );
    if (typeof ready === "number" && ready >= cards) return true;
    await page.waitForTimeout(2000);
  }
  console.log("screenshot: WARNING panel never rendered its cards");
  return false;
}

// Click one of the panel's settings buttons and wait for its dialog.
//
// The dialog only appears once `rtl_433/settings/get` has answered, and after a
// save that reloaded the hub that can take a couple of seconds -- so this polls
// rather than sleeping, and says so loudly if the dialog never opens. That
// warning is the signal that the settings forms have lost their entry point,
// which is the failure mode this whole page exists to avoid.
async function openPanelSettings(page, buttonClass) {
  await inPanel(
    page,
    `(panel, cls) => panel.shadowRoot.querySelector(cls).click()`,
    buttonClass,
  );
  for (let i = 0; i < 20; i++) {
    const open = await inPanel(
      page,
      `(panel) => Boolean(panel.shadowRoot.querySelector(".settings-dialog").open)`,
    );
    if (open === true) {
      // Let the form finish laying out before a capture.
      await page.waitForTimeout(800);
      return true;
    }
    await page.waitForTimeout(1000);
  }
  console.log(`screenshot: WARNING ${buttonClass} never opened its dialog`);
  return false;
}

async function closePanelSettings(page) {
  await inPanel(
    page,
    `(panel) => panel.shadowRoot.querySelector(".settings-dialog").close()`,
  );
  await page.waitForTimeout(500);
}

// Click a named button on the card for `key` ("add" / "ignore").
async function clickCardButton(page, key, action) {
  return inPanel(
    page,
    `(panel, arg) => {
      const cards = [...panel.shadowRoot.querySelectorAll(".device-card")];
      const card = cards.find((c) =>
        (c.querySelector(".device-key")?.textContent || "").includes(arg.key),
      );
      if (!card) return "no card";
      const button = card.querySelector("." + arg.action);
      if (!button || button.hidden) return "no button";
      button.click();
      return "clicked";
    }`,
    { key, action },
  );
}

// Read the discovery panel's own state out of its shadow root.
//
// The panel (`custom_components/rtl_433/frontend/rtl_433-panel.js`) is a plain
// custom element that Home Assistant loads as an ES module and hands `hass` to
// as a property, and everything it shows comes from the WebSocket API rather
// than from the page. Both of those are asserted here rather than assumed: the
// element is otherwise covered only by a Python registration test, so this is
// the one place a real browser proves the module loaded, `hass` arrived, and the
// table filled from `rtl_433/devices/subscribe`.
//
// It sits several shadow roots deep (home-assistant -> … -> ha-panel-custom),
// so it is found by walking the shadow trees, the same way the options-flow
// helpers above find their form fields.
async function readPanel(page) {
  return page.evaluate(() => {
    const deep = (root, out = []) => {
      for (const el of root.querySelectorAll("*")) {
        out.push(el);
        if (el.shadowRoot) deep(el.shadowRoot, out);
      }
      return out;
    };
    const panel = deep(document).find((el) => el.localName === "rtl-433-panel");
    if (!panel) {
      return { found: false };
    }
    const root = panel.shadowRoot;
    const cell = (row, selector) =>
      (row.querySelector(selector)?.textContent || "").trim();
    return {
      found: true,
      // `hass` is set as a property by the frontend for a non-iframe panel; no
      // `hass` means no connection and an empty page.
      hass: Boolean(panel.hass),
      status: (root.querySelector(".status")?.textContent || "").trim(),
      banner: root.querySelector(".banner")?.hidden
        ? ""
        : (root.querySelector(".banner")?.textContent || "").trim(),
      rows: [...root.querySelectorAll(".grid:not(.ignored-grid) .device-card")].map(
        (card) => ({
          model: cell(card, ".device-model"),
          key: cell(card, ".device-key"),
          count: cell(card, ".stat-count"),
          signal: cell(card, ".stat-signal"),
          added: card.classList.contains("added"),
          // The readings are the part of the card that has to come back from
          // the library rather than from the frame, so they are worth reporting
          // in the harness log: a card showing "temperature_C" instead of
          // "Temperature" means the descriptor lookup silently failed.
          readings: [...card.querySelectorAll(".reading")].map((reading) => ({
            name: cell(reading, ".reading-name"),
            value: cell(reading, ".reading-value"),
          })),
          buttons: [...card.querySelectorAll("button")]
            .filter((b) => !b.hidden)
            .map((b) => b.textContent.trim()),
        }),
      ),
      // The panel inherits the frontend's theme through CSS custom properties
      // only, so the computed colours are the check that dark mode really
      // reaches it.
      colors: {
        background: getComputedStyle(panel).backgroundColor,
        text: getComputedStyle(panel).color,
      },
    };
  });
}

// Capture the discovery panel with a genuinely populated grid:
//
//   17-discovery-panel.png  the live pending list, one card per heard device
//                           with its sighting count, signal level, its latest
//                           readings named as Home Assistant entities, an area
//                           picker and per-card Add / Ignore buttons
//
// Run BEFORE approveDevices(): that stage adds five of the six replayed devices,
// and a panel with one card would show none of the reason the panel exists.
//
// The bounded poll here doubles as the live-update check. Nothing is reloaded
// between the two reads: the counts move because the backend pushed a new
// payload down the open subscription, which is the behaviour the page is for.
async function capturePanel(page) {
  await page.goto(`${BASE}/rtl_433`, { waitUntil: "domcontentloaded" });
  // The Acurite capture decodes continuously and ws-bridge re-emits the fixtures
  // every 8s, so a bounded wait (~100s) gives every candidate a realistic
  // sighting count to render. Six devices are expected; five is enough to make
  // the point if one fixture round is slow.
  let state = await readPanel(page);
  for (let i = 0; i < 50 && (state.rows || []).length < 6; i++) {
    await page.waitForTimeout(2000);
    state = await readPanel(page);
  }
  console.log("screenshot: panel -> " + JSON.stringify(state));
  if (!state.found || !state.rows.length) {
    console.log("screenshot: panel did not render cards; skipping 17-discovery-panel.png");
    return;
  }

  // Live update, no reload: re-read after a further fixture round and report the
  // per-key sighting counts before and after.
  const before = Object.fromEntries(state.rows.map((r) => [r.key, r.count]));
  await page.waitForTimeout(20000);
  const later = await readPanel(page);
  const after = Object.fromEntries(later.rows.map((r) => [r.key, r.count]));
  console.log(
    "screenshot: panel live update (no reload) -> " +
      JSON.stringify({ before, after }),
  );

  // Dump what the commands themselves return, from the same live hub, so the
  // payloads quoted in docs/websocket-api.md are transcribed from a real
  // response rather than composed by hand. `callWS` goes over the frontend's
  // own authenticated connection -- the same one the panel uses.
  const api = await page.evaluate(async () => {
    const hass = document.querySelector("home-assistant")?.hass;
    if (!hass) return { error: "no hass on page" };
    const hubs = await hass.callWS({ type: "rtl_433/hubs" });
    const entryId = hubs.hubs?.[0]?.entry_id;
    const pending = entryId
      ? await hass.callWS({ type: "rtl_433/devices/pending", entry_id: entryId })
      : null;
    return { hubs, pending };
  });
  console.log("screenshot: api -> " + JSON.stringify(api));

  // Widen for the capture only. The grid lays out in columns of at least 320px,
  // so at the documentation viewport six candidates stack into a tall narrow
  // column that photographs as a list rather than as the grid a real screen
  // shows. Nothing is unreachable at either width; it just does not photograph.
  await page.setViewportSize({ width: 1680, height: 900 });
  await page.waitForTimeout(1000);
  await shot(page, "17-discovery-panel.png");
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.waitForTimeout(500);

  // Dark theme: Home Assistant's default theme setting follows the browser, so
  // emulating the media query is the same switch a user's system makes. Captured
  // as a verification artifact (the docs image is the light one) because the
  // panel takes every colour from theme custom properties and nothing else.
  await page.emulateMedia({ colorScheme: "dark" });
  await page.reload({ waitUntil: "domcontentloaded" });
  let dark = await readPanel(page);
  for (let i = 0; i < 15 && !(dark.rows || []).length; i++) {
    await page.waitForTimeout(2000);
    dark = await readPanel(page);
  }
  console.log("screenshot: panel dark -> " + JSON.stringify(dark.colors || {}));
  await shot(page, "panel-dark-theme.png");
  await page.emulateMedia({ colorScheme: "light" });
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.waitForTimeout(2000);
}

// Add the heard devices from their cards, and capture the ignored section:
//
//   16-ignored-devices.png  the ignored device, with the control that undoes it
//
// Nothing reaches the Home Assistant device registry without this stage: the
// coordinator records every device it hears into an in-memory pending list, and
// a person clicking Add is the only route out of it. The stage ignores the leak
// detector first so the ignored section has something real to show, captures it,
// then un-ignores and adds it -- the documented round trip -- leaving the hub
// holding every replayed device.
//
// Driven through the cards rather than a form because that is now the only
// surface: the config entry's Configure control opens this page.
async function approveDevices(page) {
  // The Acurite capture decodes continuously and ws-bridge re-emits the fixtures
  // every 8s, so a short wait gives every candidate a realistic sighting count.
  await page.waitForTimeout(45000);
  if (!(await openPanel(page, { cards: 5 }))) {
    console.log("screenshot: no cards to approve; skipping 16-ignored-devices.png");
    return;
  }

  console.log(
    "screenshot: ignore -> " +
      (await clickCardButton(page, "LeakDetector-9", "ignore")),
  );
  await page.waitForTimeout(3000);

  // Reveal the ignored section and capture it. The toggle names its own count,
  // so a shot with it open is also the proof the ignore landed.
  const revealed = await inPanel(
    page,
    `(panel) => {
      const toggle = panel.shadowRoot.querySelector(".ignored-toggle");
      if (!toggle || toggle.hidden) return "no toggle";
      toggle.click();
      return toggle.textContent.trim();
    }`,
  );
  console.log("screenshot: ignored toggle -> " + JSON.stringify(revealed));
  await page.waitForTimeout(1500);
  await shot(page, "16-ignored-devices.png");

  // Undo it, the way the documentation says to.
  console.log(
    "screenshot: unignore -> " +
      (await inPanel(
        page,
        `(panel) => {
          const card = [...panel.shadowRoot.querySelectorAll(".ignored-grid .device-card")][0];
          if (!card) return "no ignored card";
          card.querySelector(".unignore").click();
          return "clicked";
        }`,
      )),
  );
  // Un-ignoring is not retroactive: the device comes back on its next
  // transmission, so wait for a fixture round before expecting its card.
  await page.waitForTimeout(20000);

  for (const key of [
    "Acurite-Tower",
    "Honeywell-Doorbell",
    "EnergyMeter-2000",
    "SCMplus",
    "GenericDoor-X1",
    "LeakDetector-9",
  ]) {
    console.log(
      `screenshot: add ${key} -> ` + (await clickCardButton(page, key, "add")),
    );
    await page.waitForTimeout(2500);
  }
  await page.waitForTimeout(4000);
}

async function captureHubNoise(page) {
  await page.goto(`${BASE}/config/integrations/integration/rtl_433`, { waitUntil: "domcontentloaded" });
  await page.waitForTimeout(2000);
  const hub = await page.evaluate(async () => {
    const hass = document.querySelector("home-assistant")?.hass;
    if (!hass) return { error: "no hass on page" };
    const devices = await hass.callWS({ type: "config/device_registry/list" });
    const device = devices.find((d) => d.model === "rtl_433 server");
    const ents = await hass.callWS({ type: "config/entity_registry/list" });
    const noise = ents
      .filter((e) => e.device_id === device?.id && /(noise_level|min_level|minimum)/i.test(e.entity_id))
      .map((e) => e.entity_id);
    return { deviceId: device?.id, noise };
  });
  console.log("screenshot: hub device -> " + JSON.stringify(hub));
  if (!hub?.deviceId) {
    console.log("screenshot: hub device not found; skipping 14-hub-noise.png");
    return;
  }
  // The periodic report is every 10s and a reconnect re-arms it; poll ~60s.
  for (let i = 0; i < 30; i++) {
    const ready = await page.evaluate((ids) => {
      const hass = document.querySelector("home-assistant")?.hass;
      if (!hass || !ids.length) return false;
      return ids.every((id) => {
        const st = hass.states[id];
        return st && st.state !== "unknown" && st.state !== "unavailable";
      });
    }, hub.noise || []);
    if (ready) break;
    await page.waitForTimeout(2000);
  }
  const states = await page.evaluate((ids) => {
    const hass = document.querySelector("home-assistant")?.hass;
    return ids.map((id) => `${id}=${hass?.states[id]?.state}`);
  }, hub.noise || []);
  console.log("screenshot: hub noise states -> " + JSON.stringify(states));
  await page.goto(`${BASE}/config/devices/device/${hub.deviceId}`, { waitUntil: "domcontentloaded" });
  await page.waitForTimeout(3000);
  // Capture the Diagnostic card itself rather than the whole page: the hub's
  // sensors sit below the fold, and HA's device page scrolls an inner container
  // that ignores scripted scrollIntoView. An element screenshot also keeps the
  // shot readable at docs width. `has:` matches through shadow DOM.
  const card = page
    .locator("ha-card")
    .filter({ has: page.getByText("Diagnostic", { exact: true }) })
    .first();
  if (!(await card.count())) {
    console.log("screenshot: Diagnostic card not found; capturing full page instead");
    await shot(page, "14-hub-noise.png");
    return;
  }
  await card.screenshot({ path: resolve(SHOTS, "14-hub-noise.png") });
  console.log(`screenshot: ${resolve(SHOTS, "14-hub-noise.png")}`);
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

// Capture the receiver-settings dialog and lower the availability timeout.
//
//   07-hub-settings.png  the receiver's own settings: default availability
//                        timeout and the managed-settings toggle
//
// The timeout is lowered to SHORT_TIMEOUT as a side effect, which is what makes
// the later unavailable stage finish in under a minute instead of ten.
async function captureHubSettings(page) {
  await openPanel(page, { cards: 0 });
  if (!(await openPanelSettings(page, ".open-hub-settings"))) {
    return;
  }
  await shot(page, "07-hub-settings.png");
  console.log(
    "screenshot: hub settings -> " +
      JSON.stringify(
        await inPanel(
          page,
          `(panel, timeout) => {
            const root = panel.shadowRoot;
            const field = root.querySelector('.settings-body input[type="number"]');
            const was = field.value;
            field.value = timeout;
            root.querySelector(".settings-save").click();
            return { was, now: timeout };
          }`,
          SHORT_TIMEOUT,
        ),
      ),
  );
  // Saving the hub form does not reload the entry (the timeout applies live), but
  // the panel re-subscribes regardless; give it a moment to settle.
  await page.waitForTimeout(4000);
}

// Capture the device-settings dialog against the replayed gas meter:
//
//   08-device-settings.png  one device's overrides, with the commodity
//                           pre-filled from its decoded MeterType and the
//                           base-unit + scale controls it reveals
//
// One dialog rather than the three forms this used to take. The picker, the
// per-device overrides and the calibration are on the same surface now, and the
// fields rebuild when the picked device changes -- which is what the options
// flow needed a separate step for.
async function captureDeviceSettings(page) {
  await openPanel(page, { cards: 0 });
  if (!(await openPanelSettings(page, ".open-device-settings"))) {
    return;
  }
  const picked = await inPanel(
    page,
    `(panel) => {
      const picker = panel.shadowRoot.querySelector(".settings-body select");
      if (!picker) return "no picker";
      const option = [...picker.options].find((o) => o.value.includes("SCMplus"));
      if (!option) return "no SCMplus option";
      picker.value = option.value;
      picker.dispatchEvent(new Event("change"));
      return option.textContent.trim();
    }`,
  );
  console.log("screenshot: device settings -> " + JSON.stringify(picked));
  await page.waitForTimeout(1000);
  await shot(page, "08-device-settings.png");
  // Not saved: the shot only needs the form, and storing a calibration would
  // reload the hub and change the device page shots that follow.
  await closePanelSettings(page);
}

// Capture the device-mappings editor, pre-filled with a real example:
//
//   05-mapping-overrides.png  the YAML editor holding two overrides
//
// A plain <textarea>, so the document is assigned rather than typed -- there is
// no editor here to auto-indent it into something that no longer parses, which
// is what the previous CodeMirror-based editor required a clipboard paste to
// avoid. NOT saved: storing overrides reloads the hub.
async function captureMappings(page) {
  await openPanel(page, { cards: 0 });
  if (!(await openPanelSettings(page, ".open-mappings"))) {
    return;
  }
  await inPanel(
    page,
    `(panel, text) => {
      panel.shadowRoot.querySelector(".settings-body textarea").value = text;
    }`,
    EXAMPLE_MAPPINGS,
  );
  await page.waitForTimeout(800);
  await shot(page, "05-mapping-overrides.png");
  await closePanelSettings(page);
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
  const page = await context.newPage();
  try {
    await login(page);
    if (STAGE === "unavail") {
      await captureUnavailable(page);
    } else if (STAGE === "device") {
      // Iterate the Device-settings + calibration captures against an already
      // running harness (hub already added). Not part of the full pipeline.
      await captureDeviceSettings(page);
    } else if (STAGE === "hubnoise") {
      // Iterate only the hub device page / receiver-noise capture against an
      // already running harness (hub already added).
      await captureHubNoise(page);
    } else if (STAGE === "panel") {
      // Iterate only the discovery-panel capture against an already running
      // harness (hub added, devices still pending). Not part of the full
      // pipeline.
      await capturePanel(page);
    } else if (STAGE === "approve") {
      // Iterate only the approval + ignored-devices captures against an already
      // running harness (hub already added, devices still pending). Not part of
      // the full pipeline.
      await approveDevices(page);
    } else if (STAGE === "hub") {
      // Iterate only the receiver-settings and device-mappings dialogs against
      // an already running harness. Not part of the full pipeline.
      await captureHubSettings(page);
      await captureMappings(page);
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
