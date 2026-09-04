// Node's own test runner (`node --test`), no dependencies and no build step --
// the same constraint the panel itself is written under.
//
// What is under test is the hub form's availability-timeout rule. The stored
// timeout has three states and only one of them is a number, so the form asks
// which of the three you mean and sends a value to match. Getting the mapping
// wrong is invisible on screen -- the page renders, the save succeeds, and a
// doorbell quietly starts going unavailable ten minutes after it last rang. So
// the mapping is a pair of pure functions and this checks them directly.
import { test } from "node:test";
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

// The module defines a custom element, so it needs the class to extend. It
// guards `customElements` itself; `HTMLElement` is the one global it cannot
// avoid naming at evaluation time.
globalThis.HTMLElement = class {};

const HERE = dirname(fileURLToPath(import.meta.url));
const { timeoutMode, timeoutValue } = await import(
  resolve(HERE, "../../custom_components/rtl_433/frontend/rtl_433-panel.js")
);

// The backend's DEFAULT_AVAILABILITY_TIMEOUT. Written out rather than imported
// because the point of these tests is that it is no longer special.
const PLAIN_DEFAULT = 600;

// -- Which mode a stored timeout opens the form in ---------------------------

test("nothing stored opens on the per-device-type defaults", () => {
  assert.equal(timeoutMode(null), "defaults");
  // A payload that omitted the key entirely, rather than sending null for it.
  assert.equal(timeoutMode(undefined), "defaults");
});

test("a stored zero opens on never-expire", () => {
  // 0 is falsy, and every "is it set?" test that reaches for truthiness reads
  // this as unset -- which flips a receiver the user told never to expire back
  // onto timeouts.
  assert.equal(timeoutMode(0), "never");
});

test("a stored number opens on a fixed timeout", () => {
  assert.equal(timeoutMode(30), "custom");
  assert.equal(timeoutMode(1), "custom");
  assert.equal(timeoutMode(86400), "custom");
});

test("the plain default is a stored number like any other", () => {
  // The regression this whole change is about. 600 used to mean "unset", so it
  // was the one value a ten-minute default makes it natural to type and the one
  // value that could not be stored. It reads back as a real timeout now.
  assert.equal(timeoutMode(PLAIN_DEFAULT), "custom");
});

// -- What the form sends back ------------------------------------------------

test("the defaults mode sends null, whatever is in the seconds field", () => {
  // The field keeps its value while hidden so switching back to custom offers
  // something sensible; that value must not leak into the save.
  assert.equal(timeoutValue("defaults", null), null);
  assert.equal(timeoutValue("defaults", PLAIN_DEFAULT), null);
  assert.equal(timeoutValue("defaults", 42), null);
});

test("the never mode sends zero", () => {
  assert.equal(timeoutValue("never", null), 0);
  assert.equal(timeoutValue("never", 42), 0);
});

test("the custom mode sends the seconds beside it", () => {
  assert.equal(timeoutValue("custom", 30), 30);
  assert.equal(timeoutValue("custom", 0), 0);
  // Storable now, which is the whole point.
  assert.equal(timeoutValue("custom", PLAIN_DEFAULT), PLAIN_DEFAULT);
});

test("a custom timeout with no number falls back to the defaults", () => {
  // A cleared field is mid-edit, not a choice. Falling back to a number of its
  // own would pin a timeout onto every device on the receiver without anyone
  // asking for one; falling back to null leaves the hub as it was.
  assert.equal(timeoutValue("custom", null), null);
  assert.equal(timeoutValue("custom", undefined), null);
  assert.equal(timeoutValue("custom", Number.NaN), null);
  assert.equal(timeoutValue("custom", -1), null);
});

test("an unknown mode is read as the defaults, never as a timeout", () => {
  // Nothing produces one today. If something ever does, the safe reading is the
  // state that expires nothing unexpectedly.
  assert.equal(timeoutValue("", 42), null);
  assert.equal(timeoutValue(undefined, 42), null);
});

// -- The two together --------------------------------------------------------

test("opening the form and saving it untouched stores what was there", () => {
  // The property that matters on every one of these pages: a user who opens
  // Receiver settings to flip the manage-settings toggle, and touches nothing
  // else, must not change the availability behaviour by doing so.
  for (const stored of [null, 0, 1, 30, PLAIN_DEFAULT, 3600]) {
    const mode = timeoutMode(stored);
    const seconds = mode === "custom" ? stored : PLAIN_DEFAULT;
    assert.equal(
      timeoutValue(mode, seconds),
      stored === undefined ? null : stored,
      `a stored ${stored} did not survive an untouched save`,
    );
  }
});
