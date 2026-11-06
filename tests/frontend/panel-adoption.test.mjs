// Exercise the real Add handler without a DOM. The browser harness covers the
// form itself; these checks protect asynchronous replies arriving after a hub
// switch or navigation, and ensure only a successful meter adoption opens it.
import { test } from "node:test";
import assert from "node:assert/strict";

globalThis.HTMLElement = class {};
let Panel;
globalThis.customElements = {
  get: () => undefined,
  define: (_name, element) => { Panel = element; },
};
await import("../../custom_components/rtl_433/frontend/rtl_433-panel.js");

const METER = "SCMplus-123";
const DOOR = "Door-456";
const settings = {
  devices: [
    { device_key: METER, label: "Gas meter", commodity: "gas", calibration: null },
    { device_key: DOOR, label: "Door", commodity: "none", calibration: null },
  ],
  commodity_units: { gas: ["m³"], none: [] },
};

function panelWith(call) {
  const panel = Object.create(Panel.prototype);
  Object.assign(panel, {
    isConnected: true,
    _entryId: "hub-a",
    _segment: "discovered",
    _added: new Map(),
    _busy: new Set(),
    _pendingAreas: new Map(),
    _settingsDevice: "old-selection",
    _calibrationAfterAdd: null,
    _render: () => {},
    _areaChoiceFor: () => null,
    _t: (key) => key,
    _describeError: (error) => error.message,
    _setBanner(text, kind) { this._banner = { text, kind }; },
    _navigate(segment, options) { this.navigation = { segment, ...options }; },
    _call: call,
  });
  return panel;
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
  assert.deepEqual(panel.navigation, { segment: "device-settings", entry: "hub-a", replace: true });
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
  ["a receiver switch", (panel) => { panel._entryId = "hub-b"; }],
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


test("leaving the post-add form restores discovery without stacking history", () => {
  const panel = panelWith(async () => settings);
  panel._calibrationAfterAdd = METER;
  panel._pushed = true;
  panel._goUp();
  assert.deepEqual(panel.navigation, { segment: "discovered", replace: true });
  assert.equal(panel._calibrationAfterAdd, null);
  // The discovery page still owes its original return to the overview.
  assert.equal(panel._pushed, true);
});

test("switching receivers during Add cannot open the new receiver's settings", async () => {
  let reply;
  const panel = panelWith(() => new Promise((resolve) => { reply = resolve; }));
  const add = panel._addDevice({ key: METER });
  panel._entryId = "hub-b";
  reply({ applied: [METER], skipped: [] });
  await add;
  assert.equal(panel._added.size, 0);
  assert.equal(panel.navigation, undefined);
});
