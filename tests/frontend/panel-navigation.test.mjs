// Node's own test runner (`node --test`), no dependencies and no build step --
// the same constraint the panel itself is written under.
//
// What is under test is the panel's back-navigation rule, which is the part
// that broke: going up from a subview *pushed* a history entry instead of
// unwinding the one that came down, so the overview ended up stacked on top of
// the subview it came from. Its own back control then walked straight back into
// that subview and there was no way out of the panel.
//
// That failure is invisible to a screenshot: every page renders correctly, and
// only the history underneath is wrong. So the rule is a pure function and this
// checks it directly.
import { test } from "node:test";
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

// The module defines a custom element, so it needs the class to extend. It
// guards `customElements` itself; `HTMLElement` is the one global it cannot
// avoid naming at evaluation time.
globalThis.HTMLElement = class {};

const HERE = dirname(fileURLToPath(import.meta.url));
const { backAction, viewFor, pushedAfter, VIEWS, receiverFor } = await import(
  resolve(HERE, "../../custom_components/rtl_433/frontend/rtl_433-panel.js")
);

/** Every subview the panel can be on, swept from the real table. */
const SUBVIEWS = Object.keys(VIEWS).filter(Boolean);

test("a subview reached from the overview unwinds its own push", () => {
  // The regression. Anything but "unwind" here leaves the overview stacked on
  // top of the subview, which is what trapped the user in the panel.
  assert.equal(backAction("discovered", true, 5), "unwind");
  assert.equal(backAction("options", true, 3), "unwind");
  assert.equal(backAction("receiver", true, 4), "unwind");
  assert.equal(backAction("coverage", true, 6), "unwind");
  assert.equal(backAction("device-settings", true, 2), "unwind");
  assert.equal(backAction("mappings", true, 9), "unwind");
});

test("a subview opened directly replaces, having nothing to unwind", () => {
  // A bookmark, a reload or a shared link: no push was made, so unwinding would
  // leave Home Assistant altogether rather than going up a level.
  assert.equal(backAction("discovered", false, 5), "replace-up");
  assert.equal(backAction("mappings", false, 1), "replace-up");
});

test("the overview leaves the panel rather than going round again", () => {
  assert.equal(backAction("", false, 5), "leave");
  assert.equal(backAction("", true, 5), "leave");
});

test("the overview with no history behind it exits to the integration page", () => {
  assert.equal(backAction("", false, 1), "exit");
  assert.equal(backAction("", false, 0), "exit");
});

test("going up from a subview never pushes another entry", () => {
  // The property the bug violated, stated once over every subview: whatever the
  // rule decides, it is never an action that grows the history. Swept from the
  // real table, so a view added later is covered without anyone remembering.
  for (const segment of SUBVIEWS) {
    for (const pushed of [true, false]) {
      for (const length of [1, 2, 5]) {
        const action = backAction(segment, pushed, length);
        assert.ok(
          action === "unwind" || action === "replace-up",
          `back from /${segment} (pushed=${pushed}, history=${length}) ` +
            `chose ${action}, which does not go up a level`,
        );
      }
    }
  }
});

test("the overview never resolves to a within-panel move", () => {
  for (const pushed of [true, false]) {
    for (const length of [0, 1, 2, 5]) {
      const action = backAction("", pushed, length);
      assert.ok(
        action === "leave" || action === "exit",
        `back from the overview (pushed=${pushed}, history=${length}) ` +
          `chose ${action}, which stays inside the panel`,
      );
    }
  }
});

// -- Which view a path shows -------------------------------------------------

test("a known path segment picks its own view", () => {
  assert.equal(viewFor("").view, "overview");
  assert.equal(viewFor("discovered").view, "discovered");
  assert.equal(viewFor("coverage").view, "coverage");
  assert.equal(viewFor("options").form, "location");
  assert.equal(viewFor("receiver").form, "receiver");
  assert.equal(viewFor("device-settings").form, "device");
  assert.equal(viewFor("mappings").form, "mappings");
});

test("only the radio page is addressed by a receiver as well", () => {
  // The split the whole location model rests on: the availability timeout is
  // one answer for every device at a location, and the manage-radio toggle is
  // one answer per receiver. A page marked `receiver` carries a second id in
  // its URL and saves through `rtl_433/settings/receiver`; every other page
  // names a location alone. Marking the wrong one renders fine and saves the
  // wrong scope, which is why this is asserted over the whole table.
  assert.equal(viewFor("receiver").receiver, true);
  for (const [segment, view] of Object.entries(VIEWS)) {
    if (segment === "receiver") {
      continue;
    }
    assert.ok(!view.receiver, `view "${segment}" claims a receiver`);
  }
});

test("the four settings pages are four distinct forms", () => {
  // One form per page, and no two pages sharing one: the location and receiver
  // forms were a single form until the settings split, and collapsing them
  // again would quietly put a radio toggle back on a location-wide page.
  const forms = Object.values(VIEWS)
    .map((view) => view.form)
    .filter(Boolean);
  assert.deepEqual(forms.slice().sort(), [
    "device",
    "location",
    "mappings",
    "receiver",
  ]);
  assert.equal(new Set(forms).size, forms.length);
});

test("an unknown path segment falls back to the overview", () => {
  assert.equal(viewFor("nonsense").view, "overview");
  assert.equal(viewFor("devices/1").view, "overview");
});

test("a segment that names something on Object.prototype is still unknown", () => {
  // The lookup table is an object literal, so a plain `VIEWS[segment]` finds a
  // function for these three. It is truthy, so the fallback never ran, and the
  // caller read `.view` off it and got `undefined`: every view hidden and the
  // toolbar reading "undefined". Nobody would type these, but a crawler or a
  // stale link can.
  for (const segment of ["toString", "constructor", "valueOf", "__proto__"]) {
    assert.equal(viewFor(segment).view, "overview", segment);
  }
});

// -- Which receiver the radio page is editing --------------------------------

/** Two receivers of one location, as `rtl_433/receivers` reports them. */
const RECEIVERS = [
  { receiver_id: "attic", title: "rtl_433 (attic.local)" },
  { receiver_id: "garage", title: "rtl_433 (garage.local)" },
];

test("the receiver named in the URL is the one being edited", () => {
  // The page is reached from a row on the receivers card, which puts the id in
  // the path -- and a bookmark of that path has to come back to the same radio.
  assert.equal(receiverFor(RECEIVERS, "garage").receiver_id, "garage");
  assert.equal(receiverFor(RECEIVERS, "attic").receiver_id, "attic");
});

test("a link naming no receiver falls back to the first", () => {
  // A page that resolved to nothing would render a toggle with no id behind it
  // and fail on save; the first receiver is a real one to configure.
  assert.equal(receiverFor(RECEIVERS, null).receiver_id, "attic");
  assert.equal(receiverFor(RECEIVERS, undefined).receiver_id, "attic");
  assert.equal(receiverFor(RECEIVERS, "").receiver_id, "attic");
});

test("a link naming a receiver that is gone falls back rather than failing", () => {
  // A receiver removed since the link was made, or a link from another
  // location's page. Either way the save would land on nothing.
  assert.equal(receiverFor(RECEIVERS, "shed").receiver_id, "attic");
});

test("a location with no receivers has nothing to edit", () => {
  assert.equal(receiverFor([], "attic"), null);
  assert.equal(receiverFor(undefined, "attic"), null);
});

// -- Whether the back arrow still owes an unwind -----------------------------

test("the first path is an arrival, not a move", () => {
  // A bookmarked subview has no overview behind it, so going back from it would
  // leave the panel. `seen` false is what says "this is where we came in".
  assert.equal(pushedAfter("", "options", false, false), false);
  assert.equal(pushedAfter("", "", false, false), false);
});

test("moving from the overview into a subview owes an unwind", () => {
  assert.equal(pushedAfter("", "options", false, true), true);
  assert.equal(pushedAfter("", "discovered", false, true), true);
});

test("landing on the overview clears the debt", () => {
  assert.equal(pushedAfter("options", "", true, true), false);
  assert.equal(pushedAfter("discovered", "", false, true), false);
});

test("browser Forward back into a subview owes an unwind again", () => {
  // The sequence that was wrong: overview, click a row (pushed), Back (cleared
  // by popstate), Forward. The panel is on a subview that really does own a
  // history entry, and only this last step decides whether the back arrow
  // unwinds it or replaces it -- replacing it left two overview entries
  // stacked, so the user had to press Back twice to leave.
  let pushed = false;
  pushed = pushedAfter("", "device-settings", pushed, true); // row click
  assert.equal(pushed, true);
  pushed = pushedAfter("device-settings", "", pushed, true); // Back
  assert.equal(pushed, false);
  pushed = pushedAfter("", "device-settings", pushed, true); // Forward
  assert.equal(pushed, true);
  assert.equal(backAction("device-settings", pushed, 3), "unwind");
});

test("moving between two subviews changes nothing either way", () => {
  assert.equal(pushedAfter("options", "mappings", true, true), true);
  assert.equal(pushedAfter("options", "mappings", false, true), false);
});
