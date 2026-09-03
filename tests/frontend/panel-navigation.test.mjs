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
const { backAction } = await import(
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
