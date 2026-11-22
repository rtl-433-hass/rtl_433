// Node's own test runner (`node --test`), no dependencies and no build step --
// the same constraint the panel itself is written under.
//
// What is under test is the panel's side of its translations, which has two
// halves that fail invisibly.
//
// The first is drift. `translations/en.json` is the source of truth and the
// panel carries the same English inline, for the case where Home Assistant
// cannot hand it any strings at all. A key renamed in one and not the other is
// a page that renders perfectly in English and blank in every other language --
// or the reverse -- and nothing about either file looks wrong on its own.
//
// The second is the fallback formatter. It only runs when the frontend could
// not supply a localize, which is to say never, in development, on a healthy
// instance -- so left unexercised it would be broken by the time it is needed.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

// The module defines a custom element, so it needs the class to extend. It
// guards `customElements` itself; `HTMLElement` is the one global it cannot
// avoid naming at evaluation time.
globalThis.HTMLElement = class {};

const HERE = dirname(fileURLToPath(import.meta.url));
const COMPONENT = resolve(HERE, "../../custom_components/rtl_433");
const PANEL = resolve(COMPONENT, "frontend/rtl_433-panel.js");

const {
  STRINGS,
  VIEWS,
  TIMEOUT_MODES,
  formatFallback,
  pluralCategory,
  translate,
  formatAge,
} = await import(PANEL);

/** The key `translate` actually asks a localizer for. */
const QUALIFIED = "component.rtl_433.config_panel.";

/** A localizer that knows exactly `resources`, answering "" like the real one. */
function localizer(resources) {
  return (key, args) => {
    const template = resources[key];
    return template === undefined ? "" : formatFallback(template, args);
  };
}

/** The shipped English translations, as Home Assistant loads them. */
const english = JSON.parse(
  readFileSync(resolve(COMPONENT, "translations/en.json"), "utf-8")
);

/** The panel's source, for the key-usage sweep further down. */
const source = readFileSync(PANEL, "utf-8");

/** Flatten a translations section the way Home Assistant keys it: `a.b.c`. */
function flatten(node, prefix = "") {
  const out = {};
  for (const [key, value] of Object.entries(node)) {
    const path = `${prefix}${key}`;
    if (value && typeof value === "object") {
      Object.assign(out, flatten(value, `${path}.`));
    } else {
      out[path] = value;
    }
  }
  return out;
}

/** Localize as the panel does with no frontend to ask: the built-in English. */
const t = (key, args) => formatFallback(STRINGS[key], args);

// -- The two copies of the English say the same thing -------------------------

test("the built-in English matches translations/en.json key for key", () => {
  // `config_panel` is the section Home Assistant reserves for a config panel's
  // own words, and the only one hassfest lets an integration shape freely.
  // Losing it is the merge accident this file exists to catch, and it would
  // make every assertion below vacuous.
  assert.ok(english.config_panel, "translations/en.json has no `config_panel`");
  const shipped = flatten(english.config_panel);
  // The two key-set differences first, because a key added to one side and not
  // the other is the usual drift and this names exactly which -- where a whole
  // -object comparison would print all ~88 pairs and leave the reader to spot
  // it. The deepEqual behind them then catches a changed *value*.
  assert.deepEqual(
    Object.keys(STRINGS).filter((key) => !(key in shipped)).sort(),
    [],
    "keys in the panel's built-in English with no entry in en.json",
  );
  assert.deepEqual(
    Object.keys(shipped).filter((key) => !(key in STRINGS)).sort(),
    [],
    "keys in en.json's config_panel section the panel has no English for",
  );
  assert.deepEqual(STRINGS, shipped);
});

// -- Every key the panel asks for is a key that exists ------------------------

test("every string the panel looks up by name is translated", () => {
  // Source-driven rather than a hand-kept list, in the same spirit as
  // tests/test_translations.py: a `_t` added without its string fails here
  // without anyone remembering to extend a fixture.
  const asked = new Set();
  for (const match of source.matchAll(/_t\(\s*"([^"]+)"/g)) {
    asked.add(match[1]);
  }
  // Sanity: a regex that stopped matching would make this pass on nothing.
  assert.ok(asked.size > 30, `only ${asked.size} literal lookups found`);
  assert.deepEqual(
    [...asked].filter((key) => !defines(key)).sort(),
    [],
    "the panel asks for these keys and nothing defines them",
  );
});

/**
 * Whether `STRINGS` answers for `key`, the way `pluralCandidates` resolves it.
 *
 * A counted string is a group rather than a string, so it is defined by having
 * an `other` member -- the form every language has and the one a lookup lands
 * on last.
 */
function defines(key) {
  return key in STRINGS || `${key}.other` in STRINGS;
}

// The English plural groups themselves are guarded in tests/test_translations.py,
// which owns `en.json` -- and the test above ties `STRINGS` to it key for key,
// so asserting them here again would be the same rule kept in two languages.

// -- Which answer wins --------------------------------------------------------

test("a localizer that knows the key is what the page shows", () => {
  const dutch = localizer({ [`${QUALIFIED}card.add`]: "Toevoegen" });
  assert.equal(translate([dutch], "card.add"), "Toevoegen");
});

test("the key is asked for under the integration's config_panel section", () => {
  // The prefix is the whole reason a lookup resolves at all, and getting it
  // wrong is invisible: every key misses, every string falls back, and the page
  // renders perfectly in English in every language.
  const asked = [];
  translate([(key) => asked.push(key) && ""], "card.add");
  assert.deepEqual(asked, ["component.rtl_433.config_panel.card.add"]);
});

test("the first localizer with an answer wins, and the order is fixed", () => {
  // hass.localize before the captured one: the first follows a change of
  // language and the second is a snapshot from load time.
  const current = localizer({ [`${QUALIFIED}card.add`]: "current" });
  const snapshot = localizer({ [`${QUALIFIED}card.add`]: "snapshot" });
  assert.equal(translate([current, snapshot], "card.add"), "current");
  // A localizer with no answer is skipped rather than accepted as a blank.
  assert.equal(translate([localizer({}), snapshot], "card.add"), "snapshot");
});

test("no localizer at all still renders the page, in English", () => {
  // The case this whole fallback exists for: a frontend that has moved
  // `loadBackendTranslation`, or a request that failed. `undefined` and `null`
  // are what `hass && hass.localize` yields before there is a hass.
  assert.equal(translate([undefined, null], "card.add"), "Add");
  assert.equal(translate([], "common.save"), "Save");
  assert.equal(
    translate([localizer({})], "action.already_ignored", { device: "Foo-1" }),
    "Foo-1 was already ignored.",
  );
});

// -- Counted strings ----------------------------------------------------------

test("a counted string asks for its own plural form", () => {
  const polish = localizer({
    [`${QUALIFIED}overview.device_count.few`]: "{count} urządzenia",
    [`${QUALIFIED}overview.device_count.other`]: "{count} urządzeń",
  });
  const count = (n) =>
    translate([polish], "overview.device_count", { count: n }, "pl");
  assert.equal(count(2), "2 urządzenia");
  assert.equal(count(5), "5 urządzeń");
});

test("a plural form nobody translated falls back to the plural", () => {
  // Polish needs "few"; a translator who filled in only the two English forms
  // should leave a slightly wrong sentence, not an empty one.
  const partial = localizer({
    [`${QUALIFIED}overview.device_count.one`]: "{count} urządzenie",
    [`${QUALIFIED}overview.device_count.other`]: "{count} urządzeń",
  });
  assert.equal(
    translate([partial], "overview.device_count", { count: 2 }, "pl"),
    "2 urządzeń",
  );
});

test("counted strings fall back to English like any other", () => {
  assert.equal(
    translate([], "overview.device_count", { count: 1 }, "en"),
    "1 device",
  );
  assert.equal(
    translate([], "overview.entity_count", { count: 7 }, "en"),
    "7 entities",
  );
});

test("any string handed a count may be pluralized by a translation", () => {
  // The point of folding the plural lookup into `translate`: "12s ago" is one
  // sentence in English and needs agreement in Polish, and a translator can
  // split it into forms without the panel knowing. Before this, only the three
  // keys a call site had routed through a separate helper could ever do that.
  const polish = localizer({
    [`${QUALIFIED}age.seconds.few`]: "{count} sekundy temu",
    [`${QUALIFIED}age.seconds.other`]: "{count} sekund temu",
  });
  const at = (n) => translate([polish], "age.seconds", { count: n }, "pl");
  assert.equal(at(2), "2 sekundy temu");
  assert.equal(at(5), "5 sekund temu");
  // And the English, which is a single un-split sentence, still resolves.
  assert.equal(translate([], "age.seconds", { count: 5 }, "en"), "5s ago");
});

test("a plain key still wins over its own plural forms", () => {
  // A translation that kept one sentence gets that sentence, even though the
  // forms are looked for: the direct key is tried first.
  const dutch = localizer({
    [`${QUALIFIED}age.seconds`]: "{count}s geleden",
    [`${QUALIFIED}age.seconds.other`]: "nooit",
  });
  assert.equal(
    translate([dutch], "age.seconds", { count: 5 }, "nl"),
    "5s geleden",
  );
});

test("a non-numeric count is not treated as a counted string", () => {
  // `{device}`-style arguments must not send the lookup hunting for forms.
  assert.equal(
    translate([], "action.already_ignored", { device: "Foo-1", count: "x" }),
    "Foo-1 was already ignored.",
  );
});

// -- Which plural form a count takes ------------------------------------------

test("English splits at one and nowhere else", () => {
  assert.equal(pluralCategory("en", 1), "one");
  assert.equal(pluralCategory("en", 0), "other");
  assert.equal(pluralCategory("en", 2), "other");
  assert.equal(pluralCategory("en", 21), "other");
});

test("a language with more than two forms gets more than two", () => {
  // The reason this is `Intl.PluralRules` and not `count === 1`: Polish sorts
  // 1, 2 and 5 into three different forms. A panel that hard-coded English
  // would be wrong on two of the three.
  const forms = [1, 2, 5].map((count) => pluralCategory("pl", count));
  assert.equal(forms[0], "one");
  assert.equal(new Set(forms).size, 3, `pl 1/2/5 gave ${forms.join(", ")}`);
});

test("an unusable language falls back rather than throwing", () => {
  // A render that throws is a blank page; the wrong plural form is a blemish.
  assert.equal(pluralCategory("not a language tag", 1), "one");
  assert.equal(pluralCategory("", 1), "one");
  assert.equal(pluralCategory(undefined, 2), "other");
});

test("a count that is not a number is the plural form", () => {
  // `result.cleared` comes off the wire, so it can be anything at all.
  assert.equal(pluralCategory("en", NaN), "other");
  assert.equal(pluralCategory("en", undefined), "other");
});

test("the keys the panel builds rather than writes are translated too", () => {
  // Three families are assembled at runtime, so the sweep above cannot see
  // them: the toolbar title per view, the availability-timeout modes, and a
  // label and description per settings field.
  //
  // The first two sweep the panel's own tables rather than a copy of them, so
  // a view or a mode added without its string fails here -- which a hand-kept
  // list beside them could not do.
  for (const [segment, view] of Object.entries(VIEWS)) {
    assert.ok(view.title in STRINGS, `view "${segment}" title ${view.title}`);
  }
  assert.ok(TIMEOUT_MODES.length, "no timeout modes to check");
  for (const mode of TIMEOUT_MODES) {
    assert.ok(`settings.timeout_mode.${mode}` in STRINGS, mode);
  }
  // Every name `_settingsSchema` can put in a schema. A field whose label is
  // missing renders as its own raw name, which is a control the user cannot
  // read rather than one they cannot see.
  for (const field of [
    "availability_mode",
    "availability_timeout",
    "manage_settings",
    "device_key",
    "timeout_override",
    "motion_clear_delay",
    "commodity",
    "unit",
    "scale",
    "mappings",
  ]) {
    assert.ok(defines(`settings.data.${field}`), field);
  }
});

test("the keys chosen inside a lookup are translated too", () => {
  // Four pairs are picked by a conditional *inside* the `_t(...)` call -- a
  // receiver's connection state and a merged device's availability -- so the
  // source sweep above cannot see them: it matches a literal immediately after
  // `_t(`, and these have an expression there. Both halves of each pair have to
  // exist, and the failure if one does not is a blank word mid-sentence.
  for (const key of [
    "overview.receiver_connected",
    "overview.receiver_disconnected",
    "coverage.available",
    "coverage.unavailable",
  ]) {
    assert.ok(defines(key), key);
  }
});

test("every view the panel can show has a title, and each is distinct", () => {
  // The toolbar is the only thing naming the page, so two views sharing a title
  // is two screens a user cannot tell apart -- which is exactly what "Receiver
  // settings" was before the location and receiver forms were split.
  const titles = Object.values(VIEWS).map((view) => view.title);
  assert.equal(new Set(titles).size, titles.length, titles.join(", "));
  const rendered = titles.map((title) => STRINGS[title]);
  assert.equal(new Set(rendered).size, rendered.length, rendered.join(", "));
});

// -- The fallback formatter ---------------------------------------------------

test("a string with nothing in it comes back unchanged", () => {
  assert.equal(formatFallback("Clear discovered devices"), "Clear discovered devices");
});

test("a placeholder takes the argument named on it", () => {
  assert.equal(
    formatFallback("{device} was already ignored.", { device: "Acurite-609TXC/194" }),
    "Acurite-609TXC/194 was already ignored.",
  );
  // Two of them, and one used twice over, which the replace banner does.
  assert.equal(
    formatFallback("{a}-{b}-{a}", { a: "x", b: "y" }),
    "x-y-x",
  );
});

test("an argument that was not supplied leaves nothing behind", () => {
  // Better an empty gap than the literal "{error}" or the word "undefined" in
  // front of someone trying to read an error message.
  assert.equal(formatFallback("before {missing} after"), "before  after");
  assert.equal(formatFallback("{n}", { n: null }), "");
  // Zero is a value, not an absence.
  assert.equal(formatFallback("{n}", { n: 0 }), "0");
});

test("the counted strings read as sentences once filled in", () => {
  // The real ones, so a reworded string that lost its placeholder is caught
  // here rather than on a page reading "Cleared  discovered devices".
  assert.equal(
    formatFallback(STRINGS["discovered.cleared.one"], { count: 1 }),
    "Cleared 1 discovered device. They reappear as they transmit.",
  );
  assert.equal(
    formatFallback(STRINGS["discovered.cleared.other"], { count: 3 }),
    "Cleared 3 discovered devices. They reappear as they transmit.",
  );
  assert.equal(
    formatFallback(STRINGS["overview.device_count.other"], { count: 12 }),
    "12 devices",
  );
});

test("anything that is not a string formats as nothing", () => {
  // A missing key reads as `undefined` here, and a key that names something on
  // Object.prototype -- `constructor`, `toString` -- reads as a function. Both
  // have to end up as the empty string `hass.localize` would have answered
  // with, rather than as "undefined" or a printed function body on the page.
  assert.equal(formatFallback(undefined), "");
  assert.equal(formatFallback(STRINGS["no.such.key"]), "");
  assert.equal(formatFallback(STRINGS.constructor), "");
});

test("something that is not a placeholder is left where it is", () => {
  // Not something the English above contains; the point is that a stray brace
  // degrades to showing its own characters rather than to eating the rest of
  // the sentence.
  assert.equal(formatFallback("a {b"), "a {b");
  assert.equal(formatFallback("{ spaced }", { spaced: "x" }), "{ spaced }");
});

// -- Which age unit a timestamp falls in --------------------------------------

test("each threshold moves to the next unit and no sooner", () => {
  const now = Date.parse("2026-09-12T12:00:00Z");
  const at = (seconds) =>
    formatAge(t, new Date(now - seconds * 1000).toISOString(), now);
  assert.equal(at(2), "2s ago");
  assert.equal(at(59), "59s ago");
  assert.equal(at(60), "1m ago");
  assert.equal(at(3599), "59m ago");
  assert.equal(at(3600), "1h ago");
  assert.equal(at(86399), "23h ago");
  assert.equal(at(86400), "1d ago");
});

test("a timestamp in the future reads as just now rather than negative", () => {
  // Clock skew between the receiver and the browser is ordinary, and "-3s ago"
  // reads as a bug in the panel rather than as a clock that is a little off.
  const now = Date.parse("2026-09-12T12:00:00Z");
  assert.equal(formatAge(t, "2026-09-12T12:00:03Z", now), "0s ago");
});

test("an unparseable timestamp is an em dash, not an age", () => {
  assert.equal(formatAge(t, "not a date", Date.now()), "—");
  assert.equal(formatAge(t, undefined, Date.now()), "—");
});
