// Exercise the real Add handler without a DOM. The browser harness covers the
// form itself; these checks protect asynchronous replies arriving after a receiver
// switch or navigation, and ensure only a successful meter adoption opens it.
import { test } from "node:test";
import assert from "node:assert/strict";

globalThis.HTMLElement = class {};
// `_goUp` unwinds the prompt's own history entry with `history.back()`.
const history = { backs: 0, back() { this.backs += 1; } };
globalThis.window = { history };
let Panel;
globalThis.customElements = {
  get: () => undefined,
  define: (_name, element) => { Panel = element; },
};
await import("../../custom_components/rtl_433/frontend/rtl_433-panel.js");

const METER = "SCMplus-123";
const WATER = "SCMplus-789";
const MAPPED = "SCMplus-999";
const DOOR = "Door-456";
const settings = {
  devices: [
    { device_key: METER, label: "Gas meter", commodity: "gas", calibration: null, needs_calibration: true },
    { device_key: WATER, label: "Water meter", commodity: "water", calibration: null, needs_calibration: true },
    // A meter whose consumption field a mapping already gives a unit.
    { device_key: MAPPED, label: "Mapped meter", commodity: "gas", calibration: null, needs_calibration: false },
    { device_key: DOOR, label: "Door", commodity: "none", calibration: null, needs_calibration: false },
  ],
  commodity_units: { gas: ["m³"], water: ["L"], none: [] },
};

function panelWith(call) {
  const panel = Object.create(Panel.prototype);
  Object.assign(panel, {
    isConnected: true,
    _entryId: "receiver-a",
    _segment: "discovered",
    _added: new Map(),
    _busy: new Set(),
    _pendingAreas: new Map(),
    _settingsDevice: "old-selection",
    _calibrationAfterAdd: null,
    _calibrationQueue: [],
    _promptPushed: false,
    _render: () => {},
    _areaChoiceFor: () => null,
    _t: (key) => key,
    _describeError: (error) => error.message,
    _setBanner(text, kind) { this._banner = { text, kind }; },
    _navigate(segment, options) {
      this.navigation = { segment, ...options };
      this._segment = segment;
      // A push into a subview marks the panel as owing an unwind, as the real
      // `_navigate` does.
      if (!options?.replace && segment) {
        this._pushed = true;
      }
    },
    _call: call,
    _teardownSubscription() {},
    async _openSubscription() {},
    _renderSettingsForm() {},
    _el: fakeSettingsElements(),
  });
  return panel;
}

function fakeSettingsElements() {
  return {
    settingsSave: { hidden: false, disabled: false },
    settingsActions: { hidden: false },
    settingsProblem: { hidden: true, textContent: "" },
    settingsSkip: { hidden: true },
    settingsBody: { textContent: "" },
    settingsIntro: { textContent: "", append() {} },
  };
}

/** Answer Add for any key, a device save with its row, and settings/get. */
function addAndSettings(msg) {
  if (msg.type === "rtl_433/devices/add") {
    return { applied: msg.device_keys, skipped: [] };
  }
  if (msg.type === "rtl_433/settings/device") {
    return settings.devices.find((device) => device.device_key === msg.device_key);
  }
  return structuredClone(settings);
}

test("adding a meter opens its settings with detected calibration defaults", async () => {
  const messages = [];
  const panel = panelWith(async (msg) => {
    messages.push(msg);
    return msg.type === "rtl_433/devices/add"
      ? { applied: [METER], skipped: [] }
      : settings;
  });
  await panel._addDevice({ key: METER });
  assert.deepEqual(messages.map((msg) => msg.type), [
    "rtl_433/devices/add", "rtl_433/settings/get",
  ]);
  assert.equal(panel._added.has(METER), true);
  assert.equal(panel._settingsDevice, METER);
  assert.equal(panel._calibrationAfterAdd, METER);
  // Pushed over Discovered, so the browser's Back returns to the list.
  assert.deepEqual(panel.navigation, { segment: "device-settings", entry: "receiver-a" });
  assert.deepEqual(panel._deviceDefaults(METER), {
    device_key: METER,
    timeout_override: undefined,
    motion_clear_delay: undefined,
    commodity: "gas",
    unit: "m³",
    scale: 1,
  });
  assert.equal(panel._busy.size, 0);
});

test("adding a non-meter keeps the discovery cards on screen", async () => {
  const panel = panelWith(async (msg) => msg.type === "rtl_433/devices/add"
    ? { applied: [DOOR], skipped: [] }
    : settings);
  await panel._addDevice({ key: DOOR });
  assert.equal(panel._added.has(DOOR), true);
  assert.equal(panel.navigation, undefined);
  assert.equal(panel._calibrationAfterAdd, null);
});

test("a skipped Add never offers calibration", async () => {
  let calls = 0;
  const panel = panelWith(async () => {
    calls += 1;
    return { applied: [], skipped: [METER] };
  });
  await panel._addDevice({ key: METER });
  assert.equal(calls, 1);
  assert.equal(panel._added.size, 0);
  assert.equal(panel.navigation, undefined);
});

for (const [name, change] of [
  ["a receiver switch", (panel) => { panel._entryId = "receiver-b"; }],
  ["navigation away", (panel) => { panel._segment = ""; }],
  ["a detached panel", (panel) => { panel.isConnected = false; }],
]) {
  test(`a late calibration reply cannot interrupt ${name}`, async () => {
    let reply;
    const panel = panelWith(() => new Promise((resolve) => { reply = resolve; }));
    const offer = panel._offerCalibration(METER);
    change(panel);
    reply(settings);
    await offer;
    assert.equal(panel.navigation, undefined);
    assert.equal(panel._calibrationAfterAdd, null);
  });
}

test("a settings fetch failure keeps adoption and explains how to calibrate later", async () => {
  const panel = panelWith(async (msg) => {
    if (msg.type === "rtl_433/devices/add") {
      return { applied: [METER], skipped: [] };
    }
    throw new Error("Unavailable");
  });
  await panel._addDevice({ key: METER });
  assert.equal(panel._added.has(METER), true);
  assert.equal(panel._banner.text, "settings.adopt_failed");
  assert.equal(panel._banner.kind, "notice");
  assert.equal(panel.navigation, undefined);
  assert.equal(panel._busy.size, 0);
});


test("the prompt is its own history entry, which leaving it unwinds", async () => {
  history.backs = 0;
  const panel = panelWith(async (msg) => addAndSettings(msg));
  // Discovered was reached from the overview, so it owes that one unwind.
  panel._pushed = true;
  await panel._addDevice({ key: METER });
  panel._goUp();
  // Back, not a replace: browser Back and Skip both land on Discovered.
  assert.equal(history.backs, 1);
  assert.equal(panel._calibrationAfterAdd, null);
  // The discovery page still owes its original return to the overview.
  assert.equal(panel._pushed, true);
});

test("a prompt opened from a directly-loaded Discovered leaves it un-pushed", async () => {
  const panel = panelWith(async (msg) => addAndSettings(msg));
  panel._pushed = false;
  await panel._addDevice({ key: METER });
  // The prompt's push is its own; Discovered still has nothing behind it.
  assert.equal(panel._pushed, false);
});

// --- Issue 1: Save on the post-add prompt drops the adopted card. ---------
test("saving the post-add calibration keeps the meter's added card and area", async () => {
  const panel = panelWith(async (msg) => addAndSettings(msg));
  panel._areaChoiceFor = () => "kitchen";
  // The device has not reached `hass.devices` yet, so its area is still queued.
  panel._applyPendingAreas = () => {};
  await panel._addDevice({ key: METER });
  assert.equal(panel._calibrationAfterAdd, METER);
  panel._settingsForm = "device";
  panel._settingsData = panel._deviceDefaults(METER);

  await panel._saveSettings();

  assert.equal(panel._added.has(METER), true);
  assert.equal(panel._pendingAreas.get(METER), "kitchen");
});

test("switching receivers still clears the added cards", async () => {
  const panel = panelWith(async (msg) => addAndSettings(msg));
  panel._added.set(METER, { row: { key: METER }, deviceId: null });
  panel._pendingAreas.set(METER, "kitchen");
  await panel._subscribe();
  assert.equal(panel._added.size, 0);
  assert.equal(panel._pendingAreas.size, 0);
});

// --- Issue 2: a second meter added while the prompt is open. --------------
test("a meter added while another's prompt is open is offered next", async () => {
  const panel = panelWith(async (msg) => addAndSettings(msg));
  await panel._addDevice({ key: METER });
  assert.equal(panel._calibrationAfterAdd, METER);
  // B's Add completes while A's prompt is on screen.
  await panel._addDevice({ key: WATER });
  assert.equal(panel._calibrationAfterAdd, METER);
  assert.equal(panel._added.has(WATER), true);

  // Skipping A moves on to B rather than back to the list.
  panel._goUp();
  assert.equal(panel._calibrationAfterAdd, WATER);
  assert.equal(panel._settingsDevice, WATER);
  assert.equal(panel._segment, "device-settings");
});

test("saving one queued meter opens the next without refetching mid-reload", async () => {
  const types = [];
  const panel = panelWith(async (msg) => {
    types.push(msg.type);
    return addAndSettings(msg);
  });
  await panel._addDevice({ key: METER });
  await panel._addDevice({ key: WATER });
  panel._settingsForm = "device";
  panel._settingsData = panel._deviceDefaults(METER);
  types.length = 0;

  await panel._saveSettings();

  assert.deepEqual(types, ["rtl_433/settings/device"]);
  assert.equal(panel._calibrationAfterAdd, WATER);
  // The cache still serves the next form, so a receiver reloading after the save
  // cannot answer `not_loaded` in the user's face.
  assert.notEqual(panel._settings, null);
  assert.equal(panel._deviceSettings(WATER).label, "Water meter");
});

// --- Issue 3: a mapping already calibrates the meter. ---------------------
test("a meter whose mapping already gives it a unit is not prompted", async () => {
  const panel = panelWith(async (msg) => addAndSettings(msg));
  await panel._addDevice({ key: MAPPED });
  assert.equal(panel._added.has(MAPPED), true);
  assert.equal(panel.navigation, undefined);
  assert.equal(panel._calibrationAfterAdd, null);
});

// --- The intro and Skip button. -------------------------------------------
test("the prompt's intro survives its device vanishing from the payload", () => {
  const panel = panelWith(async () => settings);
  panel._settings = structuredClone(settings);
  panel._calibrationAfterAdd = "gone-device";
  panel._settingsDevice = "gone-device";
  assert.doesNotThrow(() => panel._buildSettingsForm("device"));
  assert.equal(panel._el.settingsIntro.textContent, "settings.device_intro");
  assert.equal(panel._el.settingsSkip.hidden, true);
  assert.equal(panel._calibrationAfterAdd, null);
});

test("the prompt shows its own intro and Skip; picking another device leaves it", () => {
  const panel = panelWith(async () => settings);
  panel._settings = structuredClone(settings);
  panel._calibrationAfterAdd = METER;
  panel._calibrationQueue = [WATER];
  panel._settingsDevice = METER;
  panel._buildSettingsForm("device");
  assert.equal(panel._el.settingsIntro.textContent, "settings.adopt_intro");
  assert.equal(panel._el.settingsSkip.hidden, false);

  panel._onSettingsChanged({ ...panel._settingsData, device_key: DOOR });
  assert.equal(panel._el.settingsIntro.textContent, "settings.device_intro");
  assert.equal(panel._el.settingsSkip.hidden, true);
  assert.equal(panel._calibrationAfterAdd, null);
  assert.deepEqual(panel._calibrationQueue, []);
});

test("switching receivers during Add cannot open the new receiver's settings", async () => {
  let reply;
  const panel = panelWith(() => new Promise((resolve) => { reply = resolve; }));
  const add = panel._addDevice({ key: METER });
  panel._entryId = "receiver-b";
  reply({ applied: [METER], skipped: [] });
  await add;
  assert.equal(panel._added.size, 0);
  assert.equal(panel.navigation, undefined);
});
