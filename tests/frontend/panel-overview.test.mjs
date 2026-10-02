// Node's own test runner (`node --test`), no dependencies and no build step --
// the same constraint the panel itself is written under.
//
// What is under test is the overview's claim to be about the *integration*
// rather than about one receiver. Both halves of that failed quietly:
//
// - the Devices and Entities rows linked to Home Assistant's registry pages
//   filtered to one config entry, so a user with two receivers clicked "7
//   devices" and landed on a list of four, and
// - the card above them read "Online" off the one receiver the panel happens to
//   subscribe to, which says nothing at all about the second one.
//
// Neither shows up in a screenshot of a one-receiver instance, which is every
// screenshot, so the rules are pure functions and these check them directly.
import { test } from "node:test";
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

// The module defines a custom element, so it needs the class to extend. It
// guards `customElements` itself; `HTMLElement` is the one global it cannot
// avoid naming at evaluation time.
globalThis.HTMLElement = class {};

const HERE = dirname(fileURLToPath(import.meta.url));
const { overviewStatus, configPagePath, STRINGS } = await import(
  resolve(HERE, "../../custom_components/rtl_433/frontend/rtl_433-panel.js")
);

// -- One receiver: the wording it has always had ------------------------------

test("a single connected receiver is Online", () => {
  assert.deepEqual(overviewStatus([true]), {
    connected: true,
    key: "status.online",
    args: {},
  });
});

test("a single receiver that is down says Problem, not one of one", () => {
  // Counting to "1 of 1 receivers offline" would be a strange way to tell a
  // user with one radio that their radio is down.
  assert.deepEqual(overviewStatus([false]), {
    connected: false,
    key: "status.problem",
    args: {},
  });
});

test("a receiver whose state has not arrived is still connecting", () => {
  // The page opens knowing nothing. A card that flashed a problem on the way in
  // would cry wolf on every visit.
  assert.deepEqual(overviewStatus([null]), {
    connected: false,
    key: "status.connecting",
    args: {},
  });
});

test("no receivers at all is connecting, not online", () => {
  // The state between the panel starting and `rtl_433/hubs` answering. An empty
  // list must never fall through to "everything is fine".
  assert.equal(overviewStatus([]).key, "status.connecting");
  assert.equal(overviewStatus([]).connected, false);
});

// -- Several receivers: the case the card used to misreport --------------------

test("every receiver connected is Online", () => {
  assert.deepEqual(overviewStatus([true, true, true]), {
    connected: true,
    key: "status.online",
    args: {},
  });
});

test("one receiver down is counted rather than hidden behind the other", () => {
  // The regression. With the subscribed receiver up, this card used to read
  // "Online" while a second radio was unreachable.
  assert.deepEqual(overviewStatus([true, false]), {
    connected: false,
    key: "status.receivers_offline",
    args: { count: 1, total: 2 },
  });
  // Order is not what decides it: the subscribed receiver is not always first.
  assert.deepEqual(overviewStatus([false, true]).args, { count: 1, total: 2 });
});

test("all of several receivers down counts all of them", () => {
  assert.deepEqual(overviewStatus([false, false]).args, { count: 2, total: 2 });
});

test("a receiver known to be down outweighs one not heard from yet", () => {
  // "Connecting…" beside a receiver that is genuinely offline would bury the
  // fact the user needs. The unknown one still counts towards the total,
  // because it is a receiver they have configured.
  assert.deepEqual(overviewStatus([false, null]), {
    connected: false,
    key: "status.receivers_offline",
    args: { count: 1, total: 2 },
  });
});

test("one receiver still connecting holds the whole card back", () => {
  // Nothing is known to be wrong, but "Online" would be a claim about a
  // receiver nobody has heard from.
  assert.equal(overviewStatus([true, null]).key, "status.connecting");
  assert.equal(overviewStatus([true, null]).connected, false);
});

test("only an all-connected card draws the green mark", () => {
  // `connected` is what picks the icon and the badge colour, so it has to agree
  // with the words beside it in every case -- including the ones where the
  // headline is a count rather than a state.
  for (const states of [[true], [true, true]]) {
    assert.equal(overviewStatus(states).connected, true, String(states));
  }
  for (const states of [[], [null], [false], [true, false], [false, null]]) {
    assert.equal(overviewStatus(states).connected, false, String(states));
  }
});

test("every headline it can return is a string the panel has", () => {
  // These keys are returned rather than written at the `_t` call site, so the
  // source sweep in panel-strings.test.mjs cannot see them: a rename here would
  // otherwise put a blank headline on the card.
  const cases = [[], [null], [true], [false], [true, false], [true, true]];
  for (const states of cases) {
    const { key } = overviewStatus(states);
    assert.ok(
      key in STRINGS || `${key}.other` in STRINGS,
      `${key} (from ${JSON.stringify(states)}) has no English`,
    );
  }
});

// -- Where the rows link ------------------------------------------------------

test("the registry links are filtered to the location, not to one receiver", () => {
  // The bug, stated as the URL it produces: a link scoped to anything but the
  // location entry made the count and the page it opens disagree. A receiver is
  // a subentry of the location, so the location's entry covers every radio in
  // it -- which is exactly the set the count beside the link is taken from.
  for (const which of ["devices", "entities"]) {
    const path = configPagePath(which, "01LOCATION");
    assert.equal(
      path,
      `/config/${which}/dashboard?historyBack=1&config_entry=01LOCATION`,
    );
    assert.ok(!path.includes("domain="), path);
  }
});

test("the links come back to this panel rather than to their own tab", () => {
  // `historyBack=1` is what Home Assistant's registry pages read to put the
  // caller behind their back arrow.
  assert.ok(configPagePath("devices", "01LOCATION").includes("historyBack=1"));
});
