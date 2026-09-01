// Playwright driver for the rtl_433 integration screenshot harness.
//
// Prereqs (handled by run-harness.sh): the rtl433 + wsbridge + HA containers are
// up, HA onboarding is seeded (ha-onboard.mjs), and the WebSocket is emitting
// JSON (verified with ws-probe.mjs).
//
// Captured shots: 06 (empty config-flow form), 17 (the discovery panel, live and
// populated), 03 (options menu), 15 (the populated "Add discovered devices"
// form), 16 (the "Ignored devices" step),
// 09 (integration overview / docs home hero), 02 (device page), 11 (doorbell
// event entity), 07 (Hub settings form), 05 (Device mappings YAML), 08 (Device
// settings form), 12 (calibration step), 10 (device page with the
// signal-diagnostic sensors enabled and populated), 14 (the hub device's
// Diagnostic card with the receiver-noise sensors), 04 (unavailable). The
// doorbell / energy meter / door / leak devices come from ws-bridge replaying
// tests/fixtures.
//
// Stages (STAGE env var):
//   add      - log in, add the rtl_433 hub via the config flow (host=wsbridge).
//              Nothing is added to Home Assistant automatically: the heard
//              devices sit on the coordinator's in-memory pending list until
//              they are added from the options flow, so the run captures the
//              options MENU, the populated "Add discovered devices" form and the
//              "Ignored devices" step, and adds the devices the later shots need
//              (approveDevices). It then opens the nested device and captures
//              the device page; opens Hub settings, sets a low availability
//              timeout (15s) so the unavailable stage is fast and captures the
//              form; then Device mappings with an example override, the Device
//              settings pair, and the per-device signal diagnostics.
//   approve  - re-capture only the options menu / add-devices / ignored-devices
//              shots against an already-running harness; for iterating.
//   panel    - re-capture only the discovery panel against an already-running
//              harness (hub added, devices still pending); for iterating.
//   unavail  - (after run-harness.sh stops the rtl433 replay and waits past the
//              timeout) capture the device page with all entities Unavailable.
//   device   - re-capture only the Device settings + calibration steps against
//              an already-running harness (hub already added); for iterating.
//   hubnoise - re-capture only the hub Diagnostic card (receiver-noise sensors)
//              against an already-running harness; for iterating.
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

  // --- Discovery panel -----------------------------------------------------
  // Captured BEFORE the approval stage, which adds five of the six replayed
  // devices: a panel screenshot taken afterwards would show one row and none of
  // the reason the panel exists.
  await capturePanel(page);

  // --- Approve the heard devices -------------------------------------------
  // Nothing is added automatically: every device the server decodes lands on the
  // coordinator's in-memory pending list and reaches Home Assistant only when it
  // is added from the options flow. This captures the options menu, the
  // populated "Add discovered devices" form, and the "Ignored devices" step, and
  // leaves the hub holding the devices the later shots need.
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

  // --- Hub settings form: lower the availability timeout, then submit -------
  // (The options menu itself was captured as 03-options-flow.png during the
  // approval stage, when its two approval entries had devices behind them.)
  await openOptionsMenu(page);
  // Open Hub settings from the menu, set a low timeout (so the unavailable stage
  // is fast), and submit. This also exercises the live-options update path. The
  // Hub-settings form carries the availability-timeout field (input[type=number])
  // and the managed-settings checkbox; we only change the former.
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
  // The hub's receiver-noise capture is a separate step (STAGE=hubnoise): it
  // needs the orchestrator to restart the decoder first — see captureHubNoise.
}

// Tick rows in one of an approval step's multi-selects. A SelectSelector in
// LIST + multiple mode renders each option as an <ha-checkbox> whose light-DOM
// text is the option label, with the real <input type=checkbox> inside the
// checkbox's shadow root; the whole form sits several shadow roots deep inside
// the dialog. So the rows are found by walking the shadow trees from the
// <ha-selector-select> whose `label` matches the field ("Add these devices",
// "Ignore these devices", "Stop ignoring these devices") — scoping by field
// rather than by ordinal, because the add-devices step renders every candidate
// twice, once per action. Clicking the inner input is what fires the change
// event the selector listens for; clicking the host does not.
//
// Rows are ticked ONE PER CALL with a pause between them (tickPendingRows).
// The selector rebuilds its whole value from its previous value on every change,
// and that value is only refreshed on the next render, so a burst of clicks in a
// single synchronous pass would submit just the last one — the same reason a
// person ticking boxes by hand never hits this.
async function tickPendingRow(page, pattern, fieldLabel) {
  return page.evaluate(
    ({ pattern, fieldLabel }) => {
      const deep = (root, out = []) => {
        for (const el of root.querySelectorAll("*")) {
          out.push(el);
          if (el.shadowRoot) deep(el.shadowRoot, out);
        }
        return out;
      };
      const field = deep(document).find(
        (el) =>
          el.localName === "ha-selector-select" &&
          String(el.label || "").includes(fieldLabel),
      );
      if (!field) return { error: `no field labelled ${fieldLabel}` };
      const boxes = [...deep(field), ...(field.shadowRoot ? deep(field.shadowRoot) : [])].filter(
        (el) => el.localName === "ha-checkbox",
      );
      const rows = boxes.map((el) => ({ el, label: el.textContent.trim() }));
      const row = rows.find((candidate) => candidate.label.includes(pattern));
      if (!row) return { error: `no row matching ${pattern}`, rows: rows.map((r) => r.label) };
      const input = row.el.shadowRoot?.querySelector("input");
      if (!input) return { error: `no checkbox input for ${pattern}` };
      if (!input.checked) input.click();
      return { ticked: row.label };
    },
    { pattern, fieldLabel },
  );
}

// Read back which rows of a field are ticked, so a submit is never made on an
// assumption: the selector rebuilds its value on every change, and a value that
// did not stick shows up here as an unchecked row.
async function checkedRows(page, fieldLabel) {
  return page.evaluate(
    ({ fieldLabel }) => {
      const deep = (root, out = []) => {
        for (const el of root.querySelectorAll("*")) {
          out.push(el);
          if (el.shadowRoot) deep(el.shadowRoot, out);
        }
        return out;
      };
      const field = deep(document).find(
        (el) =>
          el.localName === "ha-selector-select" &&
          String(el.label || "").includes(fieldLabel),
      );
      if (!field) return { error: `no field labelled ${fieldLabel}` };
      return [...deep(field), ...(field.shadowRoot ? deep(field.shadowRoot) : [])]
        .filter((el) => el.localName === "ha-checkbox")
        .filter((el) => el.shadowRoot?.querySelector("input")?.checked)
        .map((el) => el.textContent.trim());
    },
    { fieldLabel },
  );
}

async function tickPendingRows(page, patterns, fieldLabel) {
  const results = [];
  for (const pattern of patterns) {
    results.push(await tickPendingRow(page, pattern, fieldLabel));
    // Let the selector re-render with the new value before the next tick.
    await page.waitForTimeout(700);
  }
  return results;
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

// Drive the two approval steps and capture them:
//
//   03-options-flow.png     the options menu, led by the two approval entries
//   15-add-devices.png      the populated "Add discovered devices" form
//   16-ignored-devices.png  the "Ignored devices" step
//
// Nothing reaches the Home Assistant device registry without this stage: the
// coordinator records every device it hears into an in-memory pending list, and
// the add-devices step is the only route out of it. The stage adds the devices
// the later shots need, ignores the leak detector so the ignored-devices step
// has something real to show, then un-ignores and adds it again — the documented
// round trip, and it leaves the hub holding every replayed device.
async function approveDevices(page) {
  // The Acurite capture decodes continuously and ws-bridge re-emits the
  // fixtures every 8s, so a short wait gives every candidate a realistic
  // sighting count and signal level to render in its label.
  await page.waitForTimeout(45000);

  await openOptionsMenu(page);
  await shot(page, "03-options-flow.png");

  const opened = await openApprovalStep(page, "Add discovered devices", /add these devices/i);
  if (!opened) {
    console.log("screenshot: add-devices step did not render; skipping 15/16");
    return;
  }
  // The form carries two full candidate lists, so it is taller than the
  // documentation viewport. Grow the viewport for this capture only, so the shot
  // shows both actions instead of the first list and a scrollbar.
  await page.setViewportSize({ width: 1440, height: 1400 });
  await page.waitForTimeout(1500);
  await shot(page, "15-add-devices.png");

  const adds = ["Acurite-Tower", "Honeywell-Doorbell", "EnergyMeter-2000", "SCMplus", "GenericDoor-X1"];
  console.log(
    "screenshot: add rows -> " + JSON.stringify(await tickPendingRows(page, adds, "Add these devices")),
  );
  console.log(
    "screenshot: ignore rows -> " +
      JSON.stringify(await tickPendingRows(page, ["LeakDetector-9"], "Ignore these devices")),
  );
  console.log(
    "screenshot: ticked -> " +
      JSON.stringify({
        add: await checkedRows(page, "Add these devices"),
        ignore: await checkedRows(page, "Ignore these devices"),
      }),
  );
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.waitForTimeout(500);
  await submitDialog(page);
  await page.waitForTimeout(4000);

  // --- Ignored devices: capture it, then un-ignore the leak detector --------
  if (await openApprovalStep(page, "Ignored devices", /stop ignoring these devices/i)) {
    await shot(page, "16-ignored-devices.png");
    console.log(
      "screenshot: unignore rows -> " +
        JSON.stringify(await tickPendingRows(page, ["LeakDetector-9"], "Stop ignoring these devices")),
    );
    await submitDialog(page);
    await page.waitForTimeout(3000);
  } else {
    console.log("screenshot: ignored-devices step did not render; skipping 16");
  }

  // Un-ignoring is not retroactive, so the leak detector re-enters the pending
  // list on its next transmission (the fixture replay, within 8s). Add it, so
  // the hub in the overview shot holds every replayed device.
  await page.waitForTimeout(12000);
  if (await openApprovalStep(page, "Add discovered devices", /add these devices/i)) {
    console.log(
      "screenshot: re-add rows -> " +
        JSON.stringify(await tickPendingRows(page, ["LeakDetector-9"], "Add these devices")),
    );
    await submitDialog(page);
    await page.waitForTimeout(4000);
  }
}

// Open one options-flow step from the menu and wait for a field of its form to
// render. Menu items are list rows rather than buttons (see openOptionsMenu), so
// they are clicked by text; `fieldText` is a label from the step's own form,
// which is what distinguishes a rendered form from the menu it came from or from
// an abort dialog ("no devices are waiting").
async function openApprovalStep(page, menuText, fieldText) {
  await openOptionsMenu(page);
  await page.locator(`text=${menuText}`).first().click({ timeout: 8000 }).catch(() => {});
  await page.waitForTimeout(2500);
  return page
    .getByText(fieldText)
    .first()
    .waitFor({ state: "visible", timeout: 8000 })
    .then(() => true)
    .catch(() => false);
}

async function submitDialog(page) {
  await page
    .getByRole("button", { name: /^(submit|next|finish)$/i })
    .first()
    .click({ timeout: 8000 })
    .catch(() => {});
}

// The hub device page carries the hub-level diagnostic sensors. Two of them —
// Noise level and Minimum detection level (docs/hub-entities.md "Receiver
// Noise") — are fed by rtl_433's "Auto Level" log frames, which the harness
// produces for real: the decoder runs with `-Y autolevel -M noise:10` and the
// ws-bridge re-frames its `-F log` output as the structured log frames a real
// `-F http` server pushes. Resolve the hub device from the device registry
// (model "rtl_433 server"), poll until both noise sensors carry a number, then
// capture the Diagnostic card.
//
// Run this via run-harness.sh's `hubnoise` step, which restarts the decoder
// first. `-M noise` reports the noise level every 10s, but `-Y autolevel` only
// logs the *adjustment* line behind Minimum detection level while its estimate
// is still converging — a burst over the first seconds of a decoder run, then
// silence once it settles. Without that restart the sensor stays `unknown`.
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

// Drive the three-form Device settings path and capture each rendered step:
//
//   13-device-picker.png    the picker alone, showing the SCMplus gas meter
//                           annotated "— gas detected" from its MeterType
//   08-device-settings.png  that device's settings, commodity pre-filled to gas
//   12-calibration.png      the gas base-unit + scale form
//
// The picker is its own step precisely so the settings form can be pre-filled
// from the selected device, so the shots must be taken in sequence rather than
// off one combined form. Only the picker and settings forms are submitted (to
// advance); the calibration form is left unsubmitted.
async function captureDeviceSettings(page) {
  await openOptionsMenu(page);
  await page.locator("text=Device settings").first().click({ timeout: 8000 }).catch(() => {});
  await page.waitForTimeout(2500);

  const submit = () =>
    page
      .getByRole("button", { name: /^(submit|next)$/i })
      .first()
      .click({ timeout: 8000 })
      .catch(() => {});

  // Step 1 — the picker is the only field on the form.
  const picker = page.locator("ha-dialog ha-select, dialog ha-select").first();
  if (!(await picker.count())) {
    console.log("screenshot: device picker not found; skipping 13/08/12");
    return;
  }
  // Select the gas meter FIRST, then capture: the point of this shot is the
  // per-device "— <commodity> detected" annotation, and the collapsed anchor
  // shows it for the chosen device. (Screenshotting with the menu expanded is
  // not worth it — holding an mwc-select menu open across a capture reliably
  // dismisses the whole dialog.)
  await selectPick(picker, page, "SCMplus", /SCMplus/).catch(() =>
    console.log("screenshot: SCMplus meter not pickable; using default device"),
  );
  await shot(page, "13-device-picker.png");
  await submit();
  await page.waitForTimeout(2500);
  await shot(page, "08-device-settings.png");

  // Step 2 — commodity is pre-filled from MeterType, so just submit it through
  // to the calibration step. (The motion clear-delay field is absent here: the
  // selected device is not motion-bearing.)
  await submit();
  await page.waitForTimeout(2500);
  await shot(page, "12-calibration.png");
}

// Open the per-entry options flow, which lands on the menu step
// (Hub settings / Device settings / Device mappings). The menu items render as
// list rows (not button-role), so callers wait on / click by text.
async function openOptionsMenu(page) {
  await page.goto(`${BASE}/config/integrations/integration/rtl_433`, { waitUntil: "domcontentloaded" });
  await page.waitForTimeout(2500);
  // Gate on the integration card actually rendering: a fixed sleep races the
  // frontend's "Loading data" splash on a cold load, and clicking through it
  // silently produces no dialog.
  await page
    .locator(`text=rtl_433 (${RTL_HOST})`)
    .first()
    .waitFor({ state: "visible", timeout: 30000 })
    .catch(() => {});

  const menuShown = () =>
    page
      .locator("text=Hub settings")
      .first()
      .waitFor({ state: "visible", timeout: 8000 })
      .then(() => true)
      .catch(() => false);

  // The gear (options/Configure) icon sits at the right edge of the hub header.
  // On a cold page load the first click can land before the card is interactive,
  // so retry a couple of times rather than silently continuing without a dialog.
  for (let attempt = 0; attempt < 3; attempt++) {
    const hubHeader = page.locator(`text=rtl_433 (${RTL_HOST})`).first();
    const box = await hubHeader.boundingBox().catch(() => null);
    if (box) {
      await page.mouse.click(1243, box.y + box.height / 2);
    } else {
      // Fallback: open Configure from a kebab/Configure button if the header
      // layout shifts.
      await page.getByRole("button", { name: /configure/i }).first().click({ timeout: 5000 }).catch(() => {});
    }
    if (await menuShown()) break;
    await page.waitForTimeout(1500);
  }
  // Say so loudly rather than letting the caller capture whatever page it
  // landed on. This control is the only route to the options flow, so this
  // warning firing on every attempt means the options flow has no entry point in
  // the UI at all, not that a click was mistimed -- which is exactly how
  // registering the panel with `config_panel_domain` broke it once. See
  // AGENTS.md, "Approval surfaces".
  if (!(await menuShown())) {
    console.log(
      `screenshot: WARNING options menu never opened at ${page.url()} — ` +
        "every options-flow shot after this one will be wrong or skipped",
    );
  }
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
      // Iterate only the options menu / add-devices / ignored-devices captures
      // against an already running harness (hub already added, devices still
      // pending). Not part of the full pipeline.
      await approveDevices(page);
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
