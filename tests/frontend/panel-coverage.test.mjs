// Node's own test runner (`node --test`), no dependencies and no build step --
// the same constraint the panel itself is written under.
//
// What is under test is the one thing the union necessarily hides. A sensor two
// receivers both hear becomes one device with one set of entities, which is the
// whole point -- so *which* receiver hears it, how strongly and how recently has
// nowhere left to show itself: `rssi` and `snr` are mapped
// `enabled_by_default: false` and stay that way (Clarification #23), so a
// default install has no entity carrying any of it.
//
// The panel therefore renders it directly, from the aggregator's own state: on
// the union add-device page as "heard by Attic (-62 dB) / Garage (-89 dB)", and
// on the coverage page as a row per receiver. Both are strings assembled from
// values that are routinely absent -- a decoder that emits no level, a server
// started without `-M level`, a receiver that has never heard the device at all
// -- and every one of those absences has to read as an absence rather than as a
// measurement of nothing.
import { test } from "node:test";
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

// The module defines a custom element, so it needs the class to extend. It
// guards `customElements` itself; `HTMLElement` is the one global it cannot
// avoid naming at evaluation time.
globalThis.HTMLElement = class {};

const HERE = dirname(fileURLToPath(import.meta.url));
const { STRINGS, formatFallback, formatSignal, formatHeardBy, coverageAge } =
  await import(
    resolve(HERE, "../../custom_components/rtl_433/frontend/rtl_433-panel.js")
  );

/** Localize as the panel does with no frontend to ask: the built-in English. */
const t = (key, args) => formatFallback(STRINGS[key], args);

/** The titles a location's two receivers resolve to, by subentry id. */
const TITLES = { attic: "Attic", garage: "Garage" };
const titleFor = (receiverId) => TITLES[receiverId] || "Unknown receiver";

/** One coverage row, as `websocket_api._coverage_row` builds it. */
function coverage(receiverId, { rssi = null, snr = null, lastSeen = null } = {}) {
  return {
    receiver_id: receiverId,
    connected: true,
    vouches: false,
    last_seen: lastSeen,
    rssi,
    snr,
  };
}

// -- One signal level ---------------------------------------------------------

test("a level is rendered with its unit through a translated template", () => {
  // Where the unit goes relative to its number is a fact about a language, so
  // the two are joined by a string a translator owns rather than here.
  assert.equal(formatSignal(t, -62), "-62.0 dB");
  assert.equal(formatSignal(t, -89.25), "-89.3 dB");
  assert.equal(formatSignal(t, 0), "0.0 dB");
});

test("no level at all is an em dash, not a zero", () => {
  // `rssi` is null for a decoder that emits no level and for a server started
  // without `-M level`. Rendering that as "0.0 dB" would claim a measurement
  // nobody made -- and 0 dB is a real, and unusually strong, reading.
  assert.equal(formatSignal(t, null), "—");
  assert.equal(formatSignal(t, undefined), "—");
});

// -- Who heard this candidate -------------------------------------------------

test("two receivers read as the comparison the second receiver was bought for", () => {
  // The sentence Clarification #23 asks for, verbatim in shape: the union page
  // shows one card for the sensor and this line is the only thing on it that
  // says two receivers heard it.
  assert.equal(
    formatHeardBy(
      t,
      [
        coverage("attic", { rssi: -62 }),
        coverage("garage", { rssi: -89 }),
      ],
      titleFor,
    ),
    "Attic (-62.0 dB) / Garage (-89.0 dB)",
  );
});

test("a receiver that reported no level is named on its own", () => {
  // Not "Garage (—)", which reads as a measurement of nothing rather than as an
  // absence of one -- and plenty of decoders never emit a level at all.
  assert.equal(
    formatHeardBy(
      t,
      [coverage("attic", { rssi: -62 }), coverage("garage")],
      titleFor,
    ),
    "Attic (-62.0 dB) / Garage",
  );
  assert.equal(
    formatHeardBy(t, [coverage("attic"), coverage("garage")], titleFor),
    "Attic / Garage",
  );
});

test("a receiver the page cannot name still appears", () => {
  // A coverage row carries a subentry id; the title comes from the location
  // list, which can be a moment behind it after a receiver is added. Dropping
  // the row would under-report coverage; naming it "Unknown receiver" does not.
  assert.equal(
    formatHeardBy(t, [coverage("shed", { rssi: -70 })], titleFor),
    "Unknown receiver (-70.0 dB)",
  );
});

test("the whole line is receivers in the order the payload gave them", () => {
  // Receiver order, which is subentry order, which is the order the receivers
  // card lists them in. A line that sorted by signal would move under the
  // cursor every time a frame arrived.
  assert.equal(
    formatHeardBy(
      t,
      [
        coverage("garage", { rssi: -89 }),
        coverage("attic", { rssi: -62 }),
      ],
      titleFor,
    ),
    "Garage (-89.0 dB) / Attic (-62.0 dB)",
  );
  // One receiver is a legal payload; the caller hides the line rather than the
  // formatter refusing it.
  assert.equal(
    formatHeardBy(t, [coverage("attic", { rssi: -62 })], titleFor),
    "Attic (-62.0 dB)",
  );
  assert.equal(formatHeardBy(t, [], titleFor), "");
});

// -- When a receiver last heard a device --------------------------------------

test("a receiver that has heard the device shows how long ago", () => {
  const now = Date.parse("2026-09-12T12:00:00Z");
  assert.equal(
    coverageAge(t, coverage("attic", { lastSeen: "2026-09-12T11:59:30Z" }), now),
    "30s ago",
  );
});

test("a receiver that has never heard it says so, rather than showing a dash", () => {
  // `rtl_433/devices/coverage` lists every running receiver, including one that
  // has never decoded this device -- "the garage does not hear it" is as much a
  // coverage answer as a weak signal is, and is usually the answer someone
  // comparing two receivers came to the page for. An em dash there would read
  // as a rendering failure.
  const now = Date.parse("2026-09-12T12:00:00Z");
  assert.equal(coverageAge(t, coverage("garage"), now), "Never heard it");
  assert.equal(
    coverageAge(t, { receiver_id: "garage" }, now),
    "Never heard it",
  );
});
