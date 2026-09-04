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
const { backAction, viewFor, pushedAfter } = await import(
  resolve(HERE, "../../custom_components/rtl_433/frontend/rtl_433-panel.js")
);

test("a subview reached from the overview unwinds its own push", () => {
  // The regression. Anything but "unwind" here leaves the overview stacked on
  // top of the subview, which is what trapped the user in the panel.
  assert.equal(backAction("discovered", true, 5), "unwind");
  assert.equal(backAction("options", true, 3), "unwind");
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
  // rule decides, it is never an action that grows the history.
  for (const segment of [
    "discovered",
    "options",
    "device-settings",
    "mappings",
  ]) {
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
  assert.equal(viewFor("options").form, "hub");
  assert.equal(viewFor("device-settings").form, "device");
  assert.equal(viewFor("mappings").form, "mappings");
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
