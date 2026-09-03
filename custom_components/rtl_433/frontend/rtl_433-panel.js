/**
 * rtl_433 panel: a live view of what the receiver is hearing, as one card per
 * candidate device with Add, Ignore and Replace on the card itself -- and the
 * receiver's own settings, since this page *is* the integration's configuration
 * page and there is nowhere else for them to be.
 *
 * This file is deliberately plain. It is one custom element in one ES module
 * with **no imports at all**, and there is no build step anywhere in this
 * repository -- the file HACS downloads is the file the browser runs. That is a
 * constraint, not an accident, and it is worth stating why:
 *
 * - Home Assistant's frontend internals are not a public API. Its Lit version
 *   and its internal module paths are free to change in any release, and a
 *   panel that *imports* them breaks silently on upgrade for everyone. So this
 *   file imports nothing: it touches `hass.connection` for messaging,
 *   `hass.areas` and `hass.devices` for the two registries it reads, CSS custom
 *   properties for theming, and the `ha-*` elements only ever **by tag name**.
 * - Borrowing those elements by tag name is what makes the panel look and
 *   behave like the rest of Settings -- the same fields, the same buttons, the
 *   same focus rings -- instead of like a page that reimplements them and gets
 *   the details subtly wrong. It is also *more* robust than hand-rolling, not
 *   less, because the low-level widgets are the ones that churn: `ha-textfield`
 *   and `ha-select`'s list-item children have both already been replaced
 *   upstream. `haControl` below therefore prefers the highest-level element
 *   that will do the job (`ha-form` over a hand-assembled field) and falls back
 *   to the native control when an element is not registered, so a frontend that
 *   has moved on degrades to plain-but-working rather than to an inert box.
 * - A bundler would mean a second release pipeline (npm, a lockfile, a publish
 *   step) for a single screen. If this panel ever outgrows one hand-written
 *   file, that is a decision to take deliberately rather than to drift into.
 *
 * The backend contract lives in `custom_components/rtl_433/websocket_api.py`:
 * `rtl_433/hubs` names the receivers, `rtl_433/devices/subscribe` pushes one
 * hub's `{pending, ignored}` state whenever it changes, and
 * `rtl_433/devices/add` / `.../ignore` / `.../unignore` are the three actions,
 * and `rtl_433/devices/replace` re-points an existing device onto a candidate.
 * None of the adopt/ignore/replace *logic* is reimplemented here; every button
 * is one command call, so this panel cannot drift from what the integration
 * does. The same holds for the three settings pages: `rtl_433/settings/get`
 * answers with everything they render -- including which units are valid for
 * which commodity -- and `.../hub`, `.../device` and `.../mappings` store it.
 * A form here knows how to lay a control out and nothing about what a value
 * means, which is why the panel cannot store a setting the integration would
 * refuse.
 *
 * **Why cards rather than a table.** A candidate is judged on evidence that is
 * not columnar: how often it has been heard, how strong it was, and above all
 * *what it reports*. A row can hold one truncated line of that; a card holds
 * the readings laid out the way the device's own page will lay them out after
 * adoption, which is the actual question ("is this the sensor on my patio?").
 * The card also has somewhere to put an area picker and, once added, the link
 * to the device that was created.
 */

/**
 * How often the rendered relative timestamps ("12s ago") are recomputed.
 *
 * The subscription only pushes when the payload changes, so on an idle hub
 * "2s ago" would otherwise stay on screen indefinitely and quietly lie about
 * how long it has been since anything was heard. Re-rendering is cheap because
 * rendering reconciles the existing cards rather than rebuilding them.
 */
const CLOCK_INTERVAL_MS = 15000;

/**
 * How long to keep waiting for a hub that is mid-reload, and how often to look.
 *
 * Saving a calibration, a mapping override or the manage-settings toggle
 * reloads the hub, and for a moment afterwards it is genuinely not loaded --
 * the subscription this page depends on cannot be opened against it. That is a
 * wait, not a failure, and reporting it as one would put an error banner in
 * front of the user at the exact moment their save succeeded. Ten seconds is
 * far longer than a reload takes and still short enough that a hub which is
 * really unreachable says so rather than spinning.
 */
const RELOAD_RETRY_MS = 1000;
const RELOAD_RETRY_LIMIT = 10;

/** The integration domain, as it appears in a device registry identifier. */
const DOMAIN = "rtl_433";

/**
 * The panel's views, keyed by the path segment that addresses each one.
 *
 * Real paths rather than in-page state, because these are pages: the browser's
 * back button, a bookmark and a reload all have to land where the user left
 * off, which is what Home Assistant's own Zigbee and Z-Wave subpages do. The
 * empty key is the overview.
 */
const VIEWS = {
  "": { view: "overview", title: "rtl_433" },
  discovered: { view: "discovered", title: "Discovered devices" },
  options: { view: "settings", title: "Receiver settings", form: "hub" },
  "device-settings": {
    view: "settings",
    title: "Device settings",
    form: "device",
  },
  mappings: { view: "settings", title: "Device mappings", form: "mappings" },
};

/** mdiDevices, mdiShape: the overview's row icons. */
const ICON_DEVICES =
  "M3 6h18V4H3a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h4v-2H3V6m10 6H9v1.78c-.61.55-1 1.33-1 2.22s.39 1.67 1 2.22V20h4v-1.78c.61-.55 1-1.33 1-2.22s-.39-1.67-1-2.22V12m-2 5.5a1.5 1.5 0 0 1 0-3 1.5 1.5 0 0 1 0 3M22 8h-6a1 1 0 0 0-1 1v10a1 1 0 0 0 1 1h6a1 1 0 0 0 1-1V9a1 1 0 0 0-1-1m-1 10h-4v-8h4v8Z";
const ICON_ENTITIES =
  "m12 2 5.5 9h-11L12 2M17.5 13a4.5 4.5 0 1 1 0 9 4.5 4.5 0 0 1 0-9M3 13.5h8v8H3v-8Z";
/** mdiCog, mdiTune, mdiCodeBraces: the settings rows. */
const ICON_RECEIVER =
  "M12 15.5A3.5 3.5 0 0 1 8.5 12A3.5 3.5 0 0 1 12 8.5a3.5 3.5 0 0 1 3.5 3.5a3.5 3.5 0 0 1-3.5 3.5m7.43-2.53c.04-.32.07-.64.07-.97c0-.33-.03-.66-.07-1l2.11-1.63c.19-.15.24-.42.12-.64l-2-3.46c-.12-.22-.39-.31-.61-.22l-2.49 1c-.52-.39-1.06-.73-1.69-.98l-.37-2.65A.506.506 0 0 0 14 2h-4c-.25 0-.46.18-.5.42l-.37 2.65c-.63.25-1.17.59-1.69.98l-2.49-1c-.22-.09-.49 0-.61.22l-2 3.46c-.13.22-.07.49.12.64L4.57 11c-.04.34-.07.67-.07 1c0 .33.03.65.07.97l-2.11 1.66c-.19.15-.25.42-.12.64l2 3.46c.12.22.39.3.61.22l2.49-1.01c.52.4 1.06.74 1.69.99l.37 2.65c.04.24.25.42.5.42h4c.25 0 .46-.18.5-.42l.37-2.65c.63-.26 1.17-.59 1.69-.99l2.49 1.01c.22.08.49 0 .61-.22l2-3.46c.12-.22.07-.49-.12-.64l-2.11-1.66Z";
const ICON_DEVICE_SETTINGS =
  "M3 17v2h6v-2H3M3 5v2h10V5H3m10 16v-2h8v-2h-8v-2h-2v6h2M7 9v2H3v2h4v2h2V9H7m14 4v-2H11v2h10m-6-4h2V7h4V5h-4V3h-2v6Z";
const ICON_MAPPINGS =
  "M8 3a2 2 0 0 0-2 2v4a2 2 0 0 1-2 2H3v2h1a2 2 0 0 1 2 2v4a2 2 0 0 0 2 2h2v-2H8v-5a2 2 0 0 0-1-1.73V13a2 2 0 0 0 1-1.73V5h2V3H8m8 0a2 2 0 0 1 2 2v4a2 2 0 0 0 2 2h1v2h-1a2 2 0 0 0-2 2v4a2 2 0 0 1-2 2h-2v-2h2v-5a2 2 0 0 1 1-1.73V13a2 2 0 0 1-1-1.73V5h-2V3h2Z";

/** mdiPlus, for the floating action button. */
const ICON_PLUS = "M19 13h-6v6h-2v-6H5v-2h6V5h2v6h6v2Z";

/** mdiCheckCircle / mdiAlertCircle: the status card's two states. */
const ICON_ONLINE =
  "M12 2a10 10 0 1 0 10 10A10 10 0 0 0 12 2m-2 15-5-5 1.41-1.41L10 14.17l7.59-7.59L19 8l-9 9Z";
const ICON_OFFLINE =
  "M13 13h-2V7h2m0 10h-2v-2h2M12 2A10 10 0 0 0 2 12a10 10 0 0 0 10 10 10 10 0 0 0 10-10A10 10 0 0 0 12 2Z";

/**
 * What the back control should do from where the panel currently is.
 *
 * Pure, and exported, so the rule can be tested without a browser: it is the
 * part that was wrong, and it is wrong in a way no screenshot shows -- the
 * page looks right and the history underneath it is not.
 *
 * Entering a subview pushes an entry, so going back up has to *unwind* that
 * push rather than push again. Pushing again is what broke it: the overview
 * ended up stacked on top of the subview it came from, so the overview's own
 * back control walked straight back into that subview, and there was no way
 * out of the panel.
 *
 * Arriving at a subview directly -- a bookmark, a reload, a shared link -- has
 * no push to unwind, so going up replaces instead. Either way exactly one entry
 * represents "the panel", and back from the overview leaves it.
 *
 * @param {string} segment      the path segment showing, "" for the overview
 * @param {boolean} pushed      whether this panel pushed to reach `segment`
 * @param {number} historyLength `window.history.length`
 * @returns {"unwind"|"replace-up"|"leave"|"exit"}
 */
export function backAction(segment, pushed, historyLength) {
  if (segment) {
    return pushed ? "unwind" : "replace-up";
  }
  return historyLength > 1 ? "leave" : "exit";
}

/** mdiArrowLeft, for the toolbar's back control and its native fallback. */
const BACK_ARROW_PATH = "M20 11H7.8l5.6-5.6L12 4l-8 8 8 8 1.4-1.4L7.8 13H20v-2z";

/** Format a signal level for a card, or an em dash when there is none. */
function formatSignal(value) {
  if (value === null || value === undefined) {
    return "—";
  }
  return `${value.toFixed(1)} dB`;
}

/** Format an ISO timestamp as a coarse age relative to `now` (epoch ms). */
function formatAge(iso, now) {
  const then = Date.parse(iso);
  if (Number.isNaN(then)) {
    return "—";
  }
  const seconds = Math.max(0, Math.round((now - then) / 1000));
  if (seconds < 60) {
    return `${seconds}s ago`;
  }
  if (seconds < 3600) {
    return `${Math.floor(seconds / 60)}m ago`;
  }
  if (seconds < 86400) {
    return `${Math.floor(seconds / 3600)}h ago`;
  }
  return `${Math.floor(seconds / 86400)}d ago`;
}

/**
 * Formatter for the exact timestamps in a card's tooltip.
 *
 * Built once at module scope rather than per call. `toLocaleString()`
 * constructs a fresh `Intl.DateTimeFormat` every time it runs, and this is
 * called for every card of every render -- on a hub with dozens of candidates
 * that is a lot of formatter construction to produce a string nobody may ever
 * hover over.
 */
const EXACT_TIME_FORMAT = new Intl.DateTimeFormat(undefined, {
  dateStyle: "medium",
  timeStyle: "medium",
});

/** Format an ISO timestamp in the viewer's own locale, for a tooltip. */
function formatExact(iso) {
  const parsed = Date.parse(iso);
  return Number.isNaN(parsed) ? "" : EXACT_TIME_FORMAT.format(new Date(parsed));
}

/**
 * Render one reading's state.
 *
 * There is nothing to format: `display` arrives finished, built in the payload
 * from the same descriptor, the same unit and the same translated vocabulary
 * the entity itself will use. That is deliberate. Every formatting rule this
 * function used to hold was a rule invented here because the payload did not
 * carry one, and each was wrong in a way only a real device page revealed --
 * "On" where core says "Wet", "99" where the state is "99.0", a unit spaced
 * where core joins it.
 */
function formatReadingValue(reading) {
  return reading.display === null || reading.display === undefined
    ? "—"
    : reading.display;
}

/**
 * Turn whatever a rejected call threw into something a person can read.
 *
 * `sendMessagePromise` rejects with the backend's `{code, message}`, but a
 * dropped connection rejects with an `Error` and a programming mistake could
 * reject with anything at all. A button that fails silently is the worst
 * outcome on this page, so every shape has to end up as *some* sentence.
 */
function describeError(error) {
  if (!error) {
    return "Unknown error";
  }
  if (typeof error === "string") {
    return error;
  }
  if (error.message) {
    return error.message;
  }
  if (error.code) {
    return error.code;
  }
  return String(error);
}

/**
 * Create `tag` if the frontend has registered it, else `fallback()`.
 *
 * The registration check is the whole point. An unknown element is inert until
 * its definition arrives, which is harmless for decoration (`ha-icon` renders
 * nothing and reserves its space) but not for a control the user has to
 * operate: an un-upgraded `<ha-select>` has no value and no popup, so a form
 * built on one would look finished and do nothing. Checking first means the
 * page either gets Home Assistant's control or a working native one.
 */
function haControl(tag, fallback) {
  return customElements.get(tag) ? document.createElement(tag) : fallback();
}

/**
 * One radio row, preferring Home Assistant's own.
 *
 * `ha-formfield` is the element core pairs a control with its label, and it is
 * what puts the ripple, the hit area and the label-clicks-the-control behaviour
 * on the row. Its label is plain text, so a row that wants two lines (a model
 * over a device key) passes them as a node and lets the field wrap it.
 */
function haRadio(name, value, labelNode) {
  const radio = haControl("ha-radio", () => {
    const native = document.createElement("input");
    native.type = "radio";
    return native;
  });
  radio.name = name;
  radio.value = value;
  radio.className = "replace-radio";

  const field = haControl("ha-formfield", () => document.createElement("label"));
  field.className = "replace-option";
  if (field.localName === "ha-formfield") {
    // `ha-formfield` slots its control; the two-line text goes in the label
    // slot so the whole row stays one click target.
    labelNode.slot = "label";
    field.append(radio, labelNode);
  } else {
    field.append(radio, labelNode);
  }
  return { field, radio };
}

/**
 * A button, preferring Home Assistant's own.
 *
 * `appearance`/`variant` are `ha-button`'s current styling API. The classes are
 * kept on whichever element comes back: they are how the rest of this file (and
 * the screenshot harness) finds its buttons, and how the native fallback picks
 * up the `.primary`/`.ghost` rules in STYLES.
 */
function haButton(label, className, appearance = "outlined") {
  const button = haControl("ha-button", () => {
    const native = document.createElement("button");
    native.type = "button";
    return native;
  });
  button.className = className;
  button.textContent = label;
  if (button.localName === "ha-button") {
    button.appearance = appearance;
    button.variant = "brand";
  }
  return button;
}

class Rtl433Panel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });

    this._hass = null;
    // Guards one-time start-up. `hass` arrives repeatedly (see the setter), so
    // "have we begun?" has to be tracked separately from "do we have a hass?".
    this._started = false;

    this._hubs = [];
    this._entryId = null;
    // Whether this panel pushed a history entry to reach the view it is on,
    // and therefore owes an unwind when it goes back up.
    this._pushed = false;
    // The brands access token, fetched once for the status card's logo.
    this._brandToken = null;
    // The path segment this panel is showing, from `route`. Empty is the
    // overview.
    this._segment = "";
    this._narrow = false;
    // `null` until the first payload lands, which distinguishes "still loading"
    // from "loaded, and there is genuinely nothing here" -- different things to
    // tell someone staring at an empty page.
    this._data = null;
    this._unsubscribe = null;
    this._clock = null;
    // A scheduled re-attempt at subscribing to a hub that was mid-reload, and
    // how many have been made. Both are reset by every deliberate subscribe.
    this._retry = null;
    this._retries = 0;

    this._showIgnored = false;

    // The card the replace dialog was opened from, and the device chosen in it.
    // Held on the panel rather than on the card because the dialog is one
    // element for the whole page: a modal is singular by definition, and one
    // per card would be dozens of hidden dialogs in the tree.
    this._replaceFor = null;
    this._replaceChoice = "";

    // The settings payload (`rtl_433/settings/get`), which form is open, and
    // that form's live controls. Fetched when a dialog is first opened rather
    // than on load: most visits to this page are about discovery and never
    // touch a setting, and the payload walks every adopted device to build it.
    this._settings = null;
    this._settingsForm = null;
    // The open form's values, keyed by the names its schema uses.
    this._settingsData = null;
    // Which device the settings dialog is showing. Remembered across opens
    // because editing several devices in a row is the normal way to use it,
    // and re-picking from a list of thirty each time is not.
    this._settingsDevice = "";

    // Device keys with a command in flight, so their buttons can be disabled.
    this._busy = new Set();
    this._banner = null;
    this._status = "";

    // Devices adopted from this panel, in this session:
    // `key -> {row, deviceId}`.
    //
    // An adopted device leaves the pending list immediately, so without this
    // its card would simply vanish at the moment of the click -- the one moment
    // the user is looking straight at it, and with the device page it just
    // created still one unexplained navigation away. Keeping the snapshot lets
    // the card stay exactly where it was and turn green, which is also what
    // makes "you added these five" readable at a glance.
    this._added = new Map();

    // `key -> area_id` for adds whose area has not been applied yet.
    //
    // Adoption writes the hub's device map; the device *registry* entry only
    // appears once the platforms have built the entities, which is a later turn
    // of the event loop. Rather than poll for it, these are drained whenever a
    // new `hass` arrives (see the setter) -- the registry landing is itself one
    // of the things that produces a new `hass`.
    this._pendingAreas = new Map();

    // key -> card element, so a push updates the cards already on screen
    // instead of replacing them. Rebuilding wholesale on every update would
    // throw away focus, an open area dropdown, and the page's scroll position.
    this._deviceCards = new Map();
    this._ignoredCards = new Map();

    // The `hass.areas` object the pickers were last handed. The frontend
    // replaces it only when the registry changes, so comparing by identity is
    // both cheaper and more accurate than rebuilding a signature per render.
    this._lastAreas = null;
  }

  /**
   * Receive the Home Assistant connection object.
   *
   * **Home Assistant sets this on every state change in the whole instance** --
   * many times a second on a busy one -- so this setter must stay a cheap
   * assignment. Re-rendering, or far worse re-subscribing, here would turn
   * every light switch in the house into work on this page and hammer the
   * WebSocket.
   *
   * The one piece of work it does do is drain `_pendingAreas`, and only when
   * that map is non-empty -- which is true for a few hundred milliseconds after
   * an add and never otherwise. That is deliberate: a new `hass` is exactly the
   * signal that a registry may have changed, so this is the cheapest correct
   * place to notice that the device now exists.
   */
  set hass(hass) {
    this._hass = hass;
    this._start();
    if (this._pendingAreas.size) {
      this._applyPendingAreas();
    }
  }

  get hass() {
    return this._hass;
  }

  /**
   * Take the path Home Assistant is showing.
   *
   * `ha-panel-custom` assigns `route` as `{prefix, path}` on every navigation
   * inside the panel, so this is where a subview URL -- typed, bookmarked,
   * reloaded or arrived at with the browser's back button -- becomes the view
   * on screen. It arrives before `hass` sometimes and after it others, so it
   * only records the segment; `_render` is what draws it.
   */
  set route(route) {
    this._segment = ((route && route.path) || "").replace(/^\//, "");
    if (this._el) {
      this._render();
    }
  }

  get route() {
    return { prefix: `/${DOMAIN}`, path: `/${this._segment || ""}` };
  }

  /** Narrow is handed down for the elements that lay themselves out by it. */
  set narrow(narrow) {
    this._narrow = Boolean(narrow);
  }

  get narrow() {
    return this._narrow;
  }

  /**
   * Move to another view by changing the URL.
   *
   * `pushState` plus a `location-changed` event is Home Assistant's own
   * navigation contract: the frontend router listens for it and re-assigns
   * `route`. The view is also set directly rather than waiting for that round
   * trip, so a frontend that stops re-assigning `route` still navigates -- the
   * URL and the view are kept in step by whichever of the two arrives.
   */
  _navigate(segment, { replace = false } = {}) {
    const path = segment ? `/${DOMAIN}/${segment}` : `/${DOMAIN}`;
    if (window.location.pathname !== path) {
      if (replace) {
        window.history.replaceState(null, "", path);
      } else {
        window.history.pushState(null, "", path);
        // Only a move *into* a subview is an entry this panel has to unwind
        // later; the rows that do it all live on the overview.
        if (segment) {
          this._pushed = true;
        }
      }
      window.dispatchEvent(
        new CustomEvent("location-changed", { detail: { replace } })
      );
    }
    this._segment = segment;
    this._render();
  }

  /**
   * Leave a subview for the overview, without stacking an entry on top of it.
   *
   * Used by the back arrow and by a form's Cancel and Save alike: all three
   * mean the same thing, and all three were pushing.
   */
  _goUp() {
    if (this._pushed) {
      this._pushed = false;
      // `popstate` re-reads the URL and renders, so this needs no follow-up.
      window.history.back();
      return;
    }
    this._navigate("", { replace: true });
  }

  /** The view definition for the current path, falling back to the overview. */
  _viewFor() {
    return VIEWS[this._segment] || VIEWS[""];
  }

  connectedCallback() {
    // `hass` may have been set before the element was in the document, in which
    // case `_start` deferred; and an element that is detached and re-attached
    // needs its subscription back (see `disconnectedCallback`).
    this._start();
    if (this._started && !this._unsubscribe && this._entryId) {
      this._subscribe();
    }
    this._startClock();
    // The frontend router re-assigns `route` for navigations it makes, but a
    // browser back button between two of this panel's own paths does not
    // always reach it. Reading the URL back on `popstate` keeps the view and
    // the address bar in step either way.
    this._onPopState = () => {
      const prefix = `/${DOMAIN}`;
      if (window.location.pathname.startsWith(prefix)) {
        this._segment = window.location.pathname
          .slice(prefix.length)
          .replace(/^\//, "");
        // Landing back on the overview means any entry this panel pushed has
        // been unwound by the browser, so there is nothing left to owe.
        if (!this._segment) {
          this._pushed = false;
        }
        if (this._el) {
          this._render();
        }
      }
    };
    window.addEventListener("popstate", this._onPopState);
  }

  /**
   * Tear the subscription down when the panel leaves the document.
   *
   * Navigating away from a panel detaches the element and nothing else cleans
   * up after it: a leaked subscription keeps the backend rendering and pushing
   * payloads at a page that no longer exists, for the life of the browser
   * session, once per visit. The interval clock is stopped for the same reason.
   */
  disconnectedCallback() {
    this._teardownSubscription();
    this._stopClock();
    if (this._onPopState) {
      window.removeEventListener("popstate", this._onPopState);
      this._onPopState = null;
    }
  }

  /** Begin once, as soon as there is both a `hass` and a document to render into. */
  _start() {
    if (this._started || !this._hass || !this.isConnected) {
      return;
    }
    this._started = true;
    this._buildDom();
    this._status = "Loading…";
    this._render();
    this._startClock();
    this._loadHubs();
    this._loadBrandLogo();
  }

  _startClock() {
    if (this._clock === null) {
      this._clock = window.setInterval(() => {
        if (this._data) {
          this._render();
        }
      }, CLOCK_INTERVAL_MS);
    }
  }

  _stopClock() {
    if (this._clock !== null) {
      window.clearInterval(this._clock);
      this._clock = null;
    }
  }

  /** Send one command and return its result. */
  _call(message) {
    return this._hass.connection.sendMessagePromise(message);
  }

  /** Show a message above the cards: an error, or a neutral notice. */
  _setBanner(text, kind) {
    this._banner = text ? { text, kind: kind || "error" } : null;
    this._render();
  }

  async _loadHubs() {
    let result;
    try {
      result = await this._call({ type: "rtl_433/hubs" });
    } catch (error) {
      this._status = "";
      this._setBanner(describeError(error), "error");
      return;
    }
    this._hubs = result.hubs || [];
    if (!this._hubs.length) {
      this._status = "No rtl_433 hubs are configured.";
      this._render();
      return;
    }
    // Prefer a loaded hub: with one healthy receiver and one mid-reload, the
    // healthy one is the one worth opening on.
    const initial = this._hubs.find((hub) => hub.loaded) || this._hubs[0];
    this._entryId = initial.entry_id;
    this._renderHubPicker();
    this._subscribe();
  }

  _teardownSubscription() {
    if (this._retry !== null) {
      window.clearTimeout(this._retry);
      this._retry = null;
    }
    if (!this._unsubscribe) {
      return;
    }
    const unsubscribe = this._unsubscribe;
    this._unsubscribe = null;
    // The unsubscribe is async and rejects if the socket has already gone --
    // which is exactly the case where there is nothing left to clean up.
    Promise.resolve()
      .then(unsubscribe)
      .catch(() => {});
  }

  async _subscribe() {
    this._teardownSubscription();
    this._retries = 0;
    this._data = null;
    this._banner = null;
    // The green cards describe adoptions made against *this* hub, so they are
    // meaningless once the panel is pointed at another one.
    this._added.clear();
    // Left behind, a queued key can never drain: `_deviceFor` builds its
    // identifier from the *current* entry, so an entry from the old hub would
    // never match, and the `hass` setter would scan the whole device registry
    // on every state change in the instance for the life of the page.
    this._pendingAreas.clear();
    // The settings payload describes *a* hub, and every device key in it
    // belongs to that one. Carried across a switch it would offer the previous
    // receiver's devices under this receiver's name.
    this._settings = null;
    this._settingsDevice = "";
    this._status = "Loading…";
    this._render();
    await this._openSubscription();
  }

  /**
   * Open the subscription, waiting out a hub that is mid-reload.
   *
   * Split from `_subscribe` so a retry re-attempts only the *connection*. The
   * resets above describe "the user pointed this page at a hub", which happens
   * once; this can happen several times for that one intent, and clearing the
   * status on each attempt would flicker the page while it waits.
   */
  async _openSubscription() {
    const entryId = this._entryId;

    let unsubscribe;
    try {
      unsubscribe = await this._hass.connection.subscribeMessage(
        (payload) => {
          // A push from a hub the user has since switched away from must not
          // paint over the hub they are now looking at.
          if (this._entryId !== entryId) {
            return;
          }
          this._data = payload;
          this._status = "";
          this._render();
        },
        { type: "rtl_433/devices/subscribe", entry_id: entryId }
      );
    } catch (error) {
      if (this._entryId !== entryId) {
        return;
      }
      // A hub that is reloading is a wait, not a failure -- and it is the
      // *expected* answer for a second or so after saving a setting that
      // requires a reload. Reporting it would show an error banner at the one
      // moment the user has just succeeded at something.
      if (error && error.code === "not_loaded" && this._retries < RELOAD_RETRY_LIMIT) {
        this._retries += 1;
        this._status = "Waiting for the receiver to reload…";
        this._render();
        this._retry = window.setTimeout(() => {
          this._retry = null;
          if (this._entryId === entryId && this.isConnected) {
            this._openSubscription();
          }
        }, RELOAD_RETRY_MS);
        return;
      }
      this._status = "";
      this._setBanner(describeError(error), "error");
      return;
    }

    // Awaiting gave the user time to switch hubs or navigate away; either way
    // this subscription is already unwanted, so close it instead of storing it.
    if (this._entryId !== entryId || !this.isConnected) {
      Promise.resolve()
        .then(unsubscribe)
        .catch(() => {});
      return;
    }
    this._unsubscribe = unsubscribe;
  }

  // -- Registry lookups ------------------------------------------------------

  /**
   * The device registry entry adoption created for `key`, or `null`.
   *
   * Matched on the identifier the entities are built with
   * (`entity.py`: `(DOMAIN, f"{hub_entry_id}:{device_key}")`), which is the only
   * stable join between a pending candidate and the device it becomes.
   *
   * `hass.devices` is read defensively: it is a documented part of the frontend
   * `hass` object, but this file's whole posture is that frontend internals may
   * move, and the cost of it being absent should be a missing link rather than
   * a page that throws on every render.
   */
  _deviceFor(key) {
    const devices = this._hass && this._hass.devices;
    if (!devices) {
      return null;
    }
    const identifier = `${this._entryId}:${key}`;
    for (const device of Object.values(devices)) {
      if (!device.identifiers) {
        continue;
      }
      for (const pair of device.identifiers) {
        if (pair[0] === DOMAIN && pair[1] === identifier) {
          return device;
        }
      }
    }
    return null;
  }

  /**
   * Assign the requested area to every add whose device has since appeared.
   *
   * Runs off the `hass` setter, so "has the device been created yet?" is
   * answered by the registry update that created it rather than by a timer. A
   * key is dropped from the queue as soon as its call is *issued*: a failure
   * here means the device exists but kept its default area, which is a mild,
   * visible, user-fixable outcome, and retrying forever on a permission error
   * would be worse than leaving it alone.
   */
  _applyPendingAreas() {
    for (const [key, areaId] of [...this._pendingAreas]) {
      const device = this._deviceFor(key);
      if (!device) {
        continue;
      }
      this._pendingAreas.delete(key);
      if (device.area_id === areaId) {
        continue;
      }
      this._call({
        type: "config/device_registry/update",
        device_id: device.id,
        area_id: areaId,
      }).catch((error) => {
        this._setBanner(
          `${key} was added, but its area could not be set: ${describeError(
            error
          )}`,
          "notice"
        );
      });
    }
  }

  // -- Actions ---------------------------------------------------------------

  /**
   * Run one action against one device key.
   *
   * The buttons are disabled while the call is in flight and re-enabled
   * whatever happens, so a slow hub cannot be double-clicked into two adoptions
   * and a failure never leaves a dead control on screen.
   *
   * `onApplied` runs only when the backend confirms this key was one it acted
   * on. A key that comes back skipped was already adopted, already ignored, or
   * no longer a candidate at all -- so turning its card green on the strength of
   * the click alone would show the user an adoption they did not make.
   */
  async _act(commandType, deviceKey, skippedMessage, onApplied) {
    this._busy.add(deviceKey);
    this._banner = null;
    this._render();
    try {
      const result = await this._call({
        type: commandType,
        entry_id: this._entryId,
        device_keys: [deviceKey],
      });
      if (result.skipped && result.skipped.length) {
        this._setBanner(skippedMessage, "notice");
      } else if (onApplied) {
        onApplied();
      }
    } catch (error) {
      this._setBanner(describeError(error), "error");
    } finally {
      this._busy.delete(deviceKey);
      this._render();
    }
  }

  /** Add one candidate, keeping its card on screen in the adopted state. */
  _addDevice(row) {
    // Read the area *before* sending the command. Adding a device makes the
    // backend push an updated pending list, and that push arrives ahead of the
    // command's own reply -- rendering it removes this card and forgets its
    // picker. Reading the area in the reply handler therefore always found
    // nothing, and the area the user chose was silently dropped.
    const areaId = this._areaChoiceFor(row.key);
    this._act(
      "rtl_433/devices/add",
      row.key,
      `${row.key} is no longer pending — it may already have been added.`,
      () => {
        // Snapshot the row: it is about to leave the pending list, and this is
        // the only copy of what the card should keep showing.
        this._added.set(row.key, { row, deviceId: null });
        if (areaId) {
          this._pendingAreas.set(row.key, areaId);
          this._applyPendingAreas();
        }
      }
    );
  }

  /**
   * The cards to show, newest discovery first.
   *
   * Sorted on `first_seen` rather than on last contact so a card holds its
   * place: a device that transmits every thirty seconds would otherwise climb
   * back to the top over and over, moving every other card under the cursor.
   * Newest-first because the reason to open this page is usually a device just
   * triggered to make it appear, and that one should not be at the bottom of a
   * long list.
   *
   * Adopted cards are merged in over the pending list at their original
   * position, so pressing Add moves nothing on screen -- the card the user is
   * looking at simply changes colour.
   */
  _cards() {
    const cards = new Map();
    const pending = this._data && this._data.pending ? this._data.pending : [];
    for (const row of pending) {
      cards.set(row.key, { key: row.key, row, added: false });
    }
    for (const [key, added] of this._added) {
      cards.set(key, { key, row: added.row, added: true });
    }
    // Decorated before the sort so each timestamp is parsed once rather than
    // once per comparison.
    for (const card of cards.values()) {
      card.firstSeen = Date.parse(card.row.first_seen) || 0;
    }
    return [...cards.values()].sort((left, right) => {
      const delta = right.firstSeen - left.firstSeen;
      if (delta) {
        return delta;
      }
      // A stable tiebreak on the key stops cards swapping places under the
      // cursor when two devices are first heard in the same millisecond.
      return left.key < right.key ? -1 : left.key > right.key ? 1 : 0;
    });
  }

  // -- DOM -------------------------------------------------------------------

  /**
   * The replace dialog, preferring Home Assistant's own.
   *
   * `ha-dialog` brings the chrome a user recognises: the header with its close
   * control, the scrim, the card shape, and the full-screen treatment on a
   * phone. A hand-rolled `<dialog>` gets none of that and has to approximate all
   * of it in CSS, which is what made this one read as somebody else's UI.
   *
   * Its actions go in the `footer` slot -- checked against a running frontend,
   * along with the two behaviours below, because neither is guessable:
   *
   * * dismissing it emits `closed` (`wa-hide` / `wa-after-hide` are the Web
   *   Awesome element underneath talking to itself), and
   * * `open` stays **true** after a dismiss. It has to be set back to false, or
   *   the next `open = true` is a no-op and the dialog never reappears.
   *
   * The native `<dialog>` stays as the fallback. It is genuinely good at this --
   * the platform gives focus trapping, Esc and a backdrop for free -- so where
   * `ha-dialog` is missing the page keeps a working dialog rather than losing
   * the feature.
   */
  _buildReplaceDialog(slot) {
    const dialog = haControl("ha-dialog", () => {
      const native = document.createElement("dialog");
      native.innerHTML = `
        <form method="dialog" class="replace-form">
          <h2 class="replace-title">Replace a device</h2>
          <p class="replace-intro"></p>
          <div class="replace-list" role="radiogroup" aria-label="Device to replace"></div>
          <div class="replace-actions"></div>
        </form>`;
      return native;
    });
    dialog.className = "replace-dialog";

    if (dialog.localName === "ha-dialog") {
      dialog.hass = this._hass;
      // The heading is the element's own, so the fallback's <h2> is not needed.
      dialog.headerTitle = "Replace a device";
      const intro = document.createElement("p");
      intro.className = "replace-intro";
      const list = document.createElement("div");
      list.className = "replace-list";
      list.setAttribute("role", "radiogroup");
      list.setAttribute("aria-label", "Device to replace");
      const actions = document.createElement("div");
      actions.className = "replace-actions";
      actions.slot = "footer";
      dialog.append(intro, list, actions);
    }

    slot.append(dialog);
    return dialog;
  }

  /** Show the replace dialog, whichever element is standing in for it. */
  _showReplaceDialog() {
    const dialog = this._el.dialog;
    if (dialog.localName === "ha-dialog") {
      dialog.open = true;
      return;
    }
    dialog.showModal();
  }

  /**
   * The toolbar's back control, preferring Home Assistant's own.
   *
   * `ha-icon-button` takes an SVG `path` rather than markup, and core's own
   * back arrow is `ha-icon-button-arrow-prev` -- which is preferred when it is
   * registered because it also carries the right label in the user's language.
   * The hand-drawn arrow stays as the last fallback.
   */
  _buildBack(slot) {
    const prev = customElements.get("ha-icon-button-arrow-prev")
      ? document.createElement("ha-icon-button-arrow-prev")
      : null;
    if (prev) {
      prev.className = "back";
      prev.hass = this._hass;
      slot.append(prev);
      return prev;
    }
    const button = haControl("ha-icon-button", () => {
      const native = document.createElement("button");
      native.type = "button";
      native.innerHTML =
        '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="' +
        BACK_ARROW_PATH +
        '"/></svg>';
      return native;
    });
    button.className = "icon-button back";
    button.setAttribute("aria-label", "Back");
    if (button.localName === "ha-icon-button") {
      button.path = BACK_ARROW_PATH;
      button.label = "Back";
    }
    slot.append(button);
    return button;
  }

  /**
   * One navigation row, preferring `ha-md-list-item`.
   *
   * This is the row core uses for a link into a subpage -- an icon, a headline,
   * an optional supporting line and a chevron -- and it is what makes these
   * read as pages to open rather than buttons to press. The class is kept on
   * whichever element comes back so the rest of this file (and the screenshot
   * harness) can still find and click it.
   */
  _navRow({ className, icon, headline, supporting, onClick }) {
    const item = haControl("ha-md-list-item", () => {
      const native = document.createElement("button");
      native.type = "button";
      return native;
    });
    item.className = className;
    if (item.localName === "ha-md-list-item") {
      item.type = "button";
    }

    if (icon) {
      const glyph = haControl("ha-svg-icon", () =>
        document.createElement("span")
      );
      glyph.slot = "start";
      glyph.className = "row-icon";
      if (glyph.localName === "ha-svg-icon") {
        glyph.path = icon;
      }
      item.append(glyph);
    }

    const head = document.createElement("span");
    head.slot = "headline";
    head.className = "row-headline";
    head.textContent = headline;
    item.append(head);

    if (supporting !== undefined) {
      const sub = document.createElement("span");
      sub.slot = "supporting-text";
      sub.className = "row-supporting";
      sub.textContent = supporting;
      item.append(sub);
    }

    // The chevron is decoration: it says "this opens something" and the row
    // itself is the control, so it is never the click target.
    const chevron = haControl("ha-icon-next", () =>
      document.createElement("span")
    );
    chevron.slot = "end";
    chevron.className = "row-chevron";
    item.append(chevron);

    item.addEventListener("click", onClick);
    return item;
  }

  /**
   * The floating action button that opens the discovery page.
   *
   * Zigbee and Z-Wave both put the one thing you came to do in a floating
   * button rather than a row, and adopting a device is that thing here.
   *
   * `ha-fab` is not registered in this frontend -- checked, not assumed -- so
   * this is built from `ha-button`, which is, using the pill shape and accent
   * weighting core gives the same control. The positioning is this file's,
   * because a floating button has to be placed by whatever owns the scroll
   * area, and here that is the panel.
   */
  _buildFab(slot) {
    const fab = haControl("ha-fab", () =>
      haButton("Add or replace device", "fab-button", "accent")
    );
    fab.classList.add("panel-fab");
    if (fab.localName === "ha-fab") {
      fab.label = "Add or replace device";
      fab.extended = true;
    } else if (fab.localName === "ha-button") {
      fab.size = "large";
      fab.pill = true;
      const plus = haControl("ha-svg-icon", () =>
        document.createElement("span")
      );
      plus.slot = "start";
      if (plus.localName === "ha-svg-icon") {
        plus.path = ICON_PLUS;
      }
      fab.prepend(plus);
    }
    fab.addEventListener("click", () => this._navigate("discovered"));
    slot.append(fab);
    return fab;
  }

  /** A card with a heading and a list of rows inside it. */
  _buildCard(slot, className, rows, heading) {
    const card = haControl("ha-card", () => document.createElement("div"));
    card.className = className;
    if (heading) {
      if (card.localName === "ha-card") {
        card.header = heading;
      } else {
        const title = document.createElement("h2");
        title.className = "card-heading";
        title.textContent = heading;
        card.append(title);
      }
    }
    const list = haControl("ha-md-list", () => document.createElement("div"));
    list.className = "card-list";
    for (const row of rows) {
      list.append(row);
    }
    card.append(list);
    slot.append(card);
    return card;
  }

  /**
   * The receiver's status, as core's integration pages lead with it.
   *
   * The connection state is the one fact worth the top of the page: everything
   * below it is meaningless while the receiver is unreachable, and "is it
   * connected?" is the first question anyone opens this page with.
   */
  _buildStatusCard(slot) {
    const card = haControl("ha-card", () => document.createElement("div"));
    card.className = "status-card";
    const row = document.createElement("div");
    row.className = "status-row";
    const badge = document.createElement("div");
    badge.className = "status-badge";
    const glyph = haControl("ha-svg-icon", () =>
      document.createElement("span")
    );
    glyph.className = "status-icon";
    badge.append(glyph);
    const text = document.createElement("div");
    text.className = "status-text";
    const headline = document.createElement("div");
    headline.className = "status-headline";
    const supporting = document.createElement("div");
    supporting.className = "status-supporting";
    text.append(headline, supporting);

    // The brand mark sits opposite the state, which is where Zigbee and Z-Wave
    // put theirs. Hidden until it actually loads: an integration whose brand
    // images are missing should show a card with no logo, never a broken-image
    // glyph, and the request needs a token that may not arrive.
    const logo = document.createElement("img");
    logo.className = "status-logo";
    logo.alt = "";
    logo.hidden = true;
    logo.addEventListener("error", () => {
      logo.hidden = true;
    });
    logo.addEventListener("load", () => {
      logo.hidden = false;
    });

    row.append(badge, text, logo);
    card.append(row);
    slot.append(card);
    return { card, glyph, badge, headline, supporting, logo };
  }

  /**
   * Point the status card's logo at this integration's own brand image.
   *
   * `/api/brands/integration/<domain>/<image>` serves a *custom* integration's
   * in-repo `brand/` directory before it falls back to the brands CDN, which is
   * what makes this work for an integration that is not in core's brands
   * repository -- the files in `custom_components/rtl_433/brand/` are the ones
   * that come back.
   *
   * The view is not behind the frontend's normal bearer auth, because an `<img>`
   * cannot carry a header; it takes a short-lived token instead, which is what
   * `brands/access_token` hands out and what Home Assistant's own frontend uses
   * for the same images. The token rotates, so a card left open long enough
   * will eventually 403 -- and then the logo hides itself rather than breaking
   * the card, which is the same posture as every other borrowed thing here.
   */
  async _loadBrandLogo() {
    const logo = this._el.status0 && this._el.status0.logo;
    if (!logo || this._brandToken) {
      return;
    }
    let token;
    try {
      token = (await this._call({ type: "brands/access_token" })).token;
    } catch (error) {
      // A frontend without the brands component, or an older core: the card is
      // complete without a logo, so this is not worth a banner.
      return;
    }
    this._brandToken = token;
    logo.src = `/api/brands/integration/${DOMAIN}/icon.png?token=${encodeURIComponent(
      token
    )}`;
  }

  /**
   * The overview's two lists: what this receiver has, and what can be changed.
   *
   * Devices and entities link out to the registry pages filtered to this
   * integration, which is where Home Assistant already lists them -- there is
   * nothing this panel could add by drawing that list again. There is no
   * groups row because rtl_433 has no grouping, and no map because a receiver
   * has no topology to draw.
   */
  _buildOverview(root) {
    const made = {};
    made.status0 = this._buildStatusCard(root.querySelector(".status-slot"));
    made.rowDevices = this._navRow({
      className: "nav-devices",
      icon: ICON_DEVICES,
      headline: "Devices",
      supporting: "",
      onClick: () => this._openConfigPage("devices"),
    });
    made.rowEntities = this._navRow({
      className: "nav-entities",
      icon: ICON_ENTITIES,
      headline: "Entities",
      supporting: "",
      onClick: () => this._openConfigPage("entities"),
    });
    this._buildCard(
      root.querySelector(".network-slot"),
      "network-card",
      [made.rowDevices, made.rowEntities],
      "My network"
    );
    made.fab = this._buildFab(root.querySelector(".fab-slot"));

    made.openHub = this._navRow({
      className: "open-hub-settings",
      icon: ICON_RECEIVER,
      headline: "Receiver settings",
      supporting: "Availability timeout and whether Home Assistant manages the receiver",
      onClick: () => this._navigate("options"),
    });
    made.openDevice = this._navRow({
      className: "open-device-settings",
      icon: ICON_DEVICE_SETTINGS,
      headline: "Device settings",
      supporting: "Per-device timeout overrides and utility-meter calibration",
      onClick: () => this._navigate("device-settings"),
    });
    made.openMappings = this._navRow({
      className: "open-mappings",
      icon: ICON_MAPPINGS,
      headline: "Device mappings",
      supporting: "YAML overrides for how fields become entities",
      onClick: () => this._navigate("mappings"),
    });
    this._buildCard(
      root.querySelector(".page-actions"),
      "settings-card",
      [made.openHub, made.openDevice, made.openMappings],
      null
    );
    return made;
  }

  /**
   * Open one of Home Assistant's own registry pages, scoped to this hub.
   *
   * The config-entry filter is the same one the integration page's own
   * "N devices" link uses, so this lands on exactly that list.
   */
  _openConfigPage(which) {
    if (!this._entryId) {
      return;
    }
    const path = `/config/${which}/dashboard?historyBack=1&config_entry=${this._entryId}`;
    window.history.pushState(null, "", path);
    window.dispatchEvent(
      new CustomEvent("location-changed", { detail: { replace: false } })
    );
  }

  /**
   * The problem banner, preferring `ha-alert`.
   *
   * `ha-alert` is the element every core panel uses to say something went
   * wrong, so borrowing it gets the icon, the colour and the wording weight
   * that a user has already learnt to read, for free.
   */
  _buildBanner(slot) {
    const banner = haControl("ha-alert", () => document.createElement("div"));
    banner.className = "banner";
    banner.hidden = true;
    slot.append(banner);
    return banner;
  }

  /**
   * The receiver picker, preferring `ha-select`.
   *
   * `ha-select` carries its own floating label, so when it is available the
   * skeleton's `<span>Receiver</span>` caption is dropped rather than stacked
   * on top of a second one.
   */
  _buildHubSelect(picker) {
    const select = haControl("ha-select", () =>
      document.createElement("select")
    );
    select.className = "hub-select";
    if (select.localName === "ha-select") {
      select.label = "Receiver";
      picker.textContent = "";
    }
    picker.append(select);
    return select;
  }

  /**
   * Build the shadow tree once.
   *
   * Shadow DOM so this panel's CSS cannot leak into the rest of Home Assistant
   * (and so the frontend's cannot leak in here); the tree is built from a fixed
   * skeleton and then only ever *updated*, never re-created.
   */
  _buildDom() {
    const style = document.createElement("style");
    style.textContent = STYLES;

    const root = document.createElement("div");
    root.className = "wrap";
    root.innerHTML = SKELETON;

    this.shadowRoot.append(style, root);

    this._el = {
      hubPicker: root.querySelector(".hub-picker"),
      hubSelect: this._buildHubSelect(root.querySelector(".hub-picker")),
      banner: this._buildBanner(root.querySelector(".banner-slot")),
      status: root.querySelector(".status"),
      grid: root.querySelector(".grid"),
      searching: root.querySelector(".searching"),
      searchingHint: root.querySelector(".searching-hint"),
      searchingSpinner: root.querySelector(".searching-spinner"),
      back: this._buildBack(root.querySelector(".back-slot")),
      ignoredGrid: root.querySelector(".ignored-grid"),
      dialog: this._buildReplaceDialog(root.querySelector(".replace-slot")),
      dialogIntro: root.querySelector(".replace-intro"),
      dialogList: root.querySelector(".replace-list"),
      dialogActions: root.querySelector(".replace-actions"),
      dialogCancel: null,
      dialogConfirm: null,
      openHub: null,
      openDevice: null,
      openMappings: null,
      title: root.querySelector(".toolbar-title"),
      viewOverview: root.querySelector(".view-overview"),
      viewDiscovered: root.querySelector(".view-discovered"),
      viewSettings: root.querySelector(".view-settings"),
      settingsIntro: root.querySelector(".settings-intro"),
      settingsProblem: root.querySelector(".settings-problem"),
      settingsBody: root.querySelector(".settings-body"),
      settingsCancel: null,
      settingsSave: null,
    };
    Object.assign(this._el, this._buildOverview(root));
    this._el.searchingSpinner.append(
      haControl("ha-spinner", () => {
        // No spinner element: the heading already says what is happening, so
        // the fallback is nothing rather than a hand-rolled animation.
        const nothing = document.createElement("span");
        nothing.hidden = true;
        return nothing;
      })
    );
    // Cancel is `plain` and Save `accent`: core's weighting for a dialog's
    // dismiss-versus-commit pair.
    this._el.settingsCancel = haButton(
      "Cancel",
      "ghost settings-cancel",
      "plain"
    );
    this._el.settingsSave = haButton(
      "Save",
      "primary settings-save",
      "accent"
    );
    root
      .querySelector(".settings-actions")
      .append(this._el.settingsCancel, this._el.settingsSave);

    // The two list actions are built rather than templated so they can be Home
    // Assistant's buttons. They are appended in the order they read on the page,
    // and both start hidden: one has nothing to reveal until there are ignored
    // devices, the other nothing to clear until something has been heard.
    this._el.ignoredToggle = haButton("", "ghost ignored-toggle");
    this._el.ignoredToggle.hidden = true;
    this._el.clear = haButton(
      "Clear discovered devices",
      "ghost clear-devices"
    );
    this._el.clear.hidden = true;
    root
      .querySelector(".list-actions")
      .append(this._el.ignoredToggle, this._el.clear);

    // Built rather than templated so they can be Home Assistant's own buttons.
    // Cancel is `plain` and Replace `accent`: that is the weighting core gives a
    // dialog's dismiss-versus-commit pair, and Replace starts disabled because
    // the dialog opens with nothing chosen.
    this._el.dialogCancel = haButton("Cancel", "ghost replace-cancel", "plain");
    this._el.dialogConfirm = haButton(
      "Replace",
      "primary replace-confirm",
      "accent"
    );
    this._el.dialogConfirm.disabled = true;
    this._el.dialogActions.append(this._el.dialogCancel, this._el.dialogConfirm);

    // Which event a select fires on a pick is exactly the sort of detail that
    // has changed upstream before, so every plausible one is listened for and
    // the handler is made idempotent instead: it returns unless the value it
    // reads is genuinely new, which also covers a popup closed without a pick.
    const onPick = () => {
      const picked = this._el.hubSelect.value;
      if (!picked || picked === this._entryId) {
        return;
      }
      this._entryId = picked;
      this._subscribe();
    };
    for (const name of ["change", "input", "selected", "closed"]) {
      this._el.hubSelect.addEventListener(name, onPick);
    }

    this._el.ignoredToggle.addEventListener("click", () => {
      this._showIgnored = !this._showIgnored;
      this._render();
    });

    this._el.back.addEventListener("click", () => this._goBack());
    this._el.clear.addEventListener("click", () => this._clearDevices());
    this._el.dialogCancel.addEventListener("click", () => this._closeReplace());
    this._el.dialogConfirm.addEventListener("click", () => this._confirmReplace());
    // Esc and the backdrop both close a native dialog on their own; this keeps
    // the panel's own state from surviving a close it did not initiate.
    // `close` is the native element's; `closed` is ha-dialog's. Listening for
    // both keeps one handler for either, and the handler forces `open` back to
    // false because ha-dialog leaves it true after a dismiss -- without that the
    // next open is a no-op and the dialog never returns.
    const onClosed = () => {
      if (this._el.dialog.localName === "ha-dialog") {
        this._el.dialog.open = false;
      }
      this._replaceFor = null;
      this._replaceChoice = "";
    };
    for (const name of ["close", "closed"]) {
      this._el.dialog.addEventListener(name, onClosed);
    }

    this._el.settingsCancel.addEventListener("click", () => {
      this._goUp();
    });
    this._el.settingsSave.addEventListener("click", () => this._saveSettings());
  }

  _renderHubPicker() {
    // A single receiver is the common case, and a picker with one option is
    // just a control that does nothing.
    this._el.hubPicker.hidden = this._hubs.length < 2;
    const options = this._hubs.map((hub) => ({
      value: hub.entry_id,
      label: hub.loaded ? hub.title : `${hub.title} (not loaded)`,
    }));
    const select = this._el.hubSelect;
    if (select.localName === "ha-select") {
      // `ha-select` is driven by its `options` property; the list-item children
      // its predecessor took are no longer read.
      select.options = options;
      select.value = this._entryId;
      return;
    }
    select.textContent = "";
    for (const { value, label } of options) {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = label;
      option.selected = value === this._entryId;
      select.append(option);
    }
  }

  _render() {
    if (!this._el) {
      return;
    }
    const now = Date.now();
    const view = this._viewFor();

    // One view is on screen at a time, and the toolbar names it -- which is
    // what makes the back arrow read as "up a level" rather than "leave".
    this._el.viewOverview.hidden = view.view !== "overview";
    this._el.viewDiscovered.hidden = view.view !== "discovered";
    this._el.viewSettings.hidden = view.view !== "settings";
    this._el.title.textContent = view.title;

    if (view.form) {
      // Fetching the payload is asynchronous, so this is fired and forgotten:
      // it re-renders on its own when the form is built.
      this._openSettings(view.form);
    } else if (this._settingsForm) {
      this._settingsForm = null;
      this._settingsData = null;
      // Dropped so the next visit builds a fresh form: the schema it needs
      // depends on the payload, which is re-fetched after every save.
      this._el.settingsFormEl = null;
      this._el.settingsBody.textContent = "";
      this._el.settingsProblem.hidden = true;
    }

    if (this._banner) {
      this._el.banner.textContent = this._banner.text;
      this._el.banner.className = `banner ${this._banner.kind}`;
      if (this._el.banner.localName === "ha-alert") {
        // `ha-alert` names the two states "error" and "warning"; this file has
        // always called the softer one a notice.
        this._el.banner.alertType =
          this._banner.kind === "notice" ? "warning" : "error";
      }
      this._el.banner.hidden = false;
    } else {
      this._el.banner.hidden = true;
    }

    this._el.status.textContent = this._status;
    this._el.status.hidden = !this._status;

    // The frontend swaps this object only when the area registry changes, so
    // an identity compare answers "did areas move?" without touching its
    // contents on every push, banner change and clock tick.
    const areas = this._hass ? this._hass.areas : null;
    const areasChanged = areas !== this._lastAreas;
    this._lastAreas = areas;

    const cards = this._cards();
    const loaded = this._data !== null;
    this._el.grid.hidden = !loaded || cards.length === 0;
    // The search line stays put whether or not anything has been heard: this
    // receiver is always listening, and saying so is the honest state. Only the
    // "they will show up here" hint drops away once they have, where it would
    // be describing something the user can already see.
    this._el.searchingHint.hidden = cards.length > 0;

    this._reconcile(
      this._el.grid,
      cards,
      this._deviceCards,
      (card) => this._createDeviceCard(card),
      (element, card) => this._updateDeviceCard(element, card, now, areasChanged)
    );

    this._el.clear.hidden = !loaded || cards.length === 0;
    // Every settings command names a hub, so the rows wait for one to resolve.
    for (const row of [
      this._el.openHub,
      this._el.openDevice,
      this._el.openMappings,
      this._el.rowDevices,
      this._el.rowEntities,
      this._el.fab,
    ]) {
      row.disabled = !this._entryId;
    }
    this._renderOverview();

    const ignored = this._data && this._data.ignored ? this._data.ignored : [];
    this._el.ignoredToggle.hidden = !loaded || ignored.length === 0;
    this._el.ignoredToggle.textContent = `${
      this._showIgnored ? "Hide" : "Show"
    } ignored devices (${ignored.length})`;
    this._el.ignoredGrid.hidden = !this._showIgnored || ignored.length === 0;

    this._reconcile(
      this._el.ignoredGrid,
      ignored,
      this._ignoredCards,
      (row) => this._createIgnoredCard(row),
      (element, row) => this._updateIgnoredCard(element, row)
    );
  }

  /**
   * Bring `container`'s children in line with `items`, keyed by `item.key`.
   *
   * Cards that already exist are updated and moved rather than recreated, which
   * is what keeps a button's focus, an open dropdown and the page's scroll
   * position across a live push -- the whole point of subscribing rather than
   * reloading.
   */
  _reconcile(container, items, cache, create, update) {
    const seen = new Set();
    let previous = null;
    for (const item of items) {
      seen.add(item.key);
      let element = cache.get(item.key);
      if (!element) {
        element = create(item);
        cache.set(item.key, element);
      }
      update(element, item);
      const expected = previous ? previous.nextSibling : container.firstChild;
      if (expected !== element) {
        container.insertBefore(element, expected);
      }
      previous = element;
    }
    for (const [key, element] of cache) {
      if (!seen.has(key)) {
        element.remove();
        cache.delete(key);
      }
    }
  }

  /** Set `textContent` only on a real change, leaving the DOM alone otherwise. */
  _text(node, value) {
    if (node.textContent !== value) {
      node.textContent = value;
    }
  }

  /** Set `title` only on a real change -- the tooltip twin of `_text`. */
  _title(node, value) {
    if (node.title !== value) {
      node.title = value;
    }
  }

  _createDeviceCard(card) {
    const element = document.createElement("div");
    element.className = "device-card";
    element.innerHTML = `
      <div class="device-head">
        <div class="device-model"></div>
        <div class="device-key mono"></div>
      </div>
      <div class="device-body">
        <div class="stats">
          <div class="stat">
            <span class="stat-label">Sightings</span>
            <span class="stat-value stat-count"></span>
          </div>
          <div class="stat">
            <span class="stat-label">Signal</span>
            <span class="stat-value stat-signal"></span>
          </div>
          <div class="stat">
            <span class="stat-label">Last seen</span>
            <span class="stat-value stat-age"></span>
          </div>
        </div>
        <div class="readings"></div>
        <div class="area"></div>
      </div>
      <div class="device-actions">
        <a class="device-link" hidden>Open device</a>
      </div>`;
    element
      .querySelector(".device-actions")
      .append(
        haButton("Replace", "ghost replace"),
        haButton("Ignore", "ghost ignore"),
        haButton("Add", "primary add", "accent")
      );

    const parts = {
      model: element.querySelector(".device-model"),
      key: element.querySelector(".device-key"),
      count: element.querySelector(".stat-count"),
      signal: element.querySelector(".stat-signal"),
      age: element.querySelector(".stat-age"),
      readings: element.querySelector(".readings"),
      area: element.querySelector(".area"),
      link: element.querySelector(".device-link"),
      add: element.querySelector(".add"),
      ignore: element.querySelector(".ignore"),
      replace: element.querySelector(".replace"),
      // Keyed like `_deviceCards`, so the readings reconcile through the same
      // helper the cards do rather than through a second implementation.
      readingRows: new Map(),
    };
    element.parts = parts;
    // `_cards()` builds a fresh descriptor object every render, so the handlers
    // below must read the *current* one off the element rather than close over
    // the one this card was created from -- that one's `row` freezes at the
    // moment the card first appeared, and Add would snapshot a sighting count,
    // signal and set of readings minutes out of date.
    element.card = card;

    this._buildAreaControl(parts);
    parts.add.addEventListener("click", () => this._addDevice(element.card.row));
    parts.replace.addEventListener("click", () =>
      this._openReplaceDialog(element.card.row)
    );
    parts.ignore.addEventListener("click", () =>
      this._act(
        "rtl_433/devices/ignore",
        element.card.key,
        `${element.card.key} was already ignored.`
      )
    );
    return element;
  }

  _updateDeviceCard(element, card, now, areasChanged) {
    const parts = element.parts;
    const row = card.row;
    element.card = card;

    // The model and key are the card's identity, promoted into the coloured
    // heading: without an interview step there is nothing else to lead with,
    // and between them they are what a user matches against the box in their
    // hand.
    this._text(parts.model, row.model || "Unknown model");
    this._text(parts.key, row.key);
    element.classList.toggle("added", card.added);

    this._text(parts.count, String(row.count));
    this._text(parts.signal, formatSignal(row.signal));
    this._text(parts.age, formatAge(row.last_seen, now));
    this._title(
      parts.age,
      `First seen ${formatExact(row.first_seen)}\nLast seen ${formatExact(
        row.last_seen
      )}`
    );

    this._renderReadings(parts, row.readings || []);

    // `areasChanged` alone would only ever populate the cards that existed on
    // the render the area registry last changed on. A candidate heard while the
    // panel is open -- the whole point of subscribing -- is created on a render
    // where the registry has not moved, so its `<select>` would stay empty and
    // the user could not give the new device an area at all. An empty select is
    // therefore always (re)built: `_renderAreaOptions` writes "No area" first,
    // so a populated one is never empty, even with no areas configured.
    if (areasChanged) {
      this._refreshAreaControl(parts);
    }

    // Resolved once and kept on the snapshot: a device id never changes, and
    // re-scanning the registry for every green card on every render is a lot of
    // work to rebuild an href that is already known.
    let device = null;
    if (card.added) {
      const added = this._added.get(card.key);
      if (added && added.deviceId) {
        device = { id: added.deviceId };
      } else if ((device = this._deviceFor(card.key)) && added) {
        added.deviceId = device.id;
      }
    }
    // Once the device exists it owns its own area, and the card's picker would
    // be a second control claiming to set the same thing while actually only
    // recording an intent that has already been carried out.
    parts.area.hidden = card.added;
    if (parts.areaControl) {
      parts.areaControl.disabled = card.added;
    }

    parts.link.hidden = !device;
    if (device) {
      parts.link.href = `/config/devices/device/${device.id}`;
    }

    const busy = this._busy.has(card.key);
    parts.add.hidden = card.added;
    parts.ignore.hidden = card.added;
    // Replace needs something to replace. On a hub whose first device this is,
    // the button would open a dialog with an empty list, so it is not offered.
    parts.replace.hidden = card.added || !this._replaceTargets().length;
    parts.add.disabled = busy;
    parts.ignore.disabled = busy;
    parts.replace.disabled = busy;
  }

  /**
   * Render a frame's readings as the rows a device page would show.
   *
   * Icon, name, value -- the same three columns, in the same order, as an entity
   * row on a device page. The payload has already ordered them the way that page
   * orders them (readings first, then diagnostics), and resolved each icon from
   * Home Assistant's own device-class table, so this method only lays them out.
   *
   * They are one list rather than the device page's two cards because the split
   * exists there to give each group its own "Add to dashboard" action, and there
   * is no dashboard to add to from a device that does not exist yet.
   *
   * Keyed reconciliation again, for the same reason as the cards: a device that
   * reports the same six fields every thirty seconds should update six values,
   * not rebuild six rows.
   */
  _renderReadings(parts, readings) {
    this._reconcile(
      parts.readings,
      readings,
      parts.readingRows,
      () => this._createReadingRow(),
      (element, reading) => this._updateReadingRow(element, reading)
    );
  }

  /** One entity row: icon, name, value. */
  _createReadingRow() {
    const row = document.createElement("div");
    row.className = "reading";
    // `ha-icon` is created by tag name and never imported. If the frontend has
    // not registered it yet the browser upgrades the element when it does, and
    // if it never does the element stays inert at its reserved size -- a row
    // with a blank icon column rather than a broken layout.
    row.innerHTML = `<ha-icon class="reading-icon"></ha-icon><span class="reading-name"></span><span class="reading-value"></span>`;
    row.iconEl = row.querySelector(".reading-icon");
    row.nameEl = row.querySelector(".reading-name");
    row.valueEl = row.querySelector(".reading-value");
    return row;
  }

  _updateReadingRow(row, reading) {
    // The icon is set as an attribute, not a property: an element that has not
    // been upgraded yet has no property to set, but the attribute is read
    // whenever it is.
    if (reading.icon) {
      if (row.iconEl.getAttribute("icon") !== reading.icon) {
        row.iconEl.setAttribute("icon", reading.icon);
      }
    } else {
      row.iconEl.removeAttribute("icon");
    }
    this._text(row.nameEl, reading.name);
    this._text(row.valueEl, formatReadingValue(reading));
  }

  /**
   * Build one card's area control, preferring Home Assistant's own picker.
   *
   * `ha-area-picker` is the control the device page itself uses to set an area,
   * so borrowing it is what makes this field look and behave like the one the
   * user will meet a minute later -- with the same search, the same "add a new
   * area", and the same 56px outlined field. It is used by tag name and never
   * imported: nothing here reaches into a frontend module path, and the element
   * is only touched if the frontend has already registered it.
   *
   * When it has not, the card falls back to a native `<select>` populated from
   * `hass.areas`. That is plainer, but it is a working control rather than an
   * empty box, and this file's whole posture is that frontend internals may move
   * and the page should degrade rather than break.
   */
  /**
   * Build one card's area field.
   *
   * `ha-area-picker` is the control Home Assistant's own device page uses to
   * set an area, so borrowing it is what makes this field look and behave like
   * the one the user meets a minute later -- the same search, the same "add a
   * new area", the same outlined field. It is used *by tag name* and never
   * imported: nothing here reaches into a frontend module path.
   *
   * Creating it before the frontend has registered it is safe. An unknown
   * element is inert until the definition arrives and the browser then upgrades
   * it in place, and Lit re-applies properties set beforehand -- so the picker
   * fills itself in whenever it becomes available. If it never does, the card
   * keeps a blank field and the rest of it still works.
   */
  _buildAreaControl(parts) {
    const picker = document.createElement("ha-area-picker");
    picker.hass = this._hass;
    picker.label = "Area";
    parts.area.append(picker);
    parts.areaControl = picker;
  }

  /**
   * Hand an existing picker a fresh `hass` so it sees a changed registry.
   *
   * Only when the registry actually moved: assigning the property schedules a
   * Lit update, and this runs for every card on screen.
   */
  _refreshAreaControl(parts) {
    if (parts.areaControl) {
      parts.areaControl.hass = this._hass;
    }
  }

  /**
   * The area chosen on one card, or `undefined`.
   *
   * Read off the control at the moment of the click rather than mirrored into a
   * Map as it changes: the control already holds this fact, and cards are
   * reconciled rather than recreated, so it survives every live push. A second
   * copy could only ever be a way for the two to disagree.
   */
  _areaChoiceFor(key) {
    const element = this._deviceCards.get(key);
    const control = element && element.parts.areaControl;
    return control ? control.value : undefined;
  }

  /**
   * Fill the overview: the connection state, and what is behind each row.
   *
   * The counts come from the registries rather than from the panel's own
   * payload, because they are counts of what Home Assistant *has* -- which is
   * exactly what the rows link to. A count the page could not work out is left
   * off rather than guessed at, so a row never claims "0 devices" while the
   * subscription is still connecting.
   */
  _renderOverview() {
    const status = this._el.status0;
    const connected = this._data !== null && !this._banner;
    status.glyph.className = "status-icon";
    if (status.glyph.localName === "ha-svg-icon") {
      status.glyph.path = connected ? ICON_ONLINE : ICON_OFFLINE;
    }
    status.badge.classList.toggle("offline", !connected);
    status.headline.textContent = connected
      ? "Online"
      : this._data === null
        ? "Connecting…"
        : "Problem";
    const devices = this._entryDeviceCount();
    status.supporting.textContent =
      devices === null ? "" : `${devices} ${devices === 1 ? "device" : "devices"}`;

    this._setRowCount(this._el.rowDevices, devices, "device", "devices");
    this._setRowCount(
      this._el.rowEntities,
      this._entryEntityCount(),
      "entity",
      "entities"
    );
  }

  /** Put "N things" on a row's supporting line, or nothing when unknown. */
  _setRowCount(row, count, one, many) {
    const line = row.querySelector(".row-supporting");
    if (!line) {
      return;
    }
    line.textContent =
      count === null ? "" : `${count} ${count === 1 ? one : many}`;
    line.hidden = count === null;
  }

  /**
   * The device ids this hub owns, from the device registry.
   *
   * `hass.devices` is keyed by device id and each entry lists the config
   * entries it belongs to, so this is a filter rather than a lookup. The
   * receiver's own device is *included*, because the row this count sits on
   * opens Home Assistant's device list filtered to this entry -- and that list
   * includes it. A count that disagreed with what clicking it shows would read
   * as a bug in the count.
   */
  _entryDeviceIds() {
    const devices = this._hass && this._hass.devices;
    if (!devices || !this._entryId) {
      return null;
    }
    return Object.values(devices)
      .filter(
        (device) =>
          device.config_entries &&
          device.config_entries.includes(this._entryId)
      )
      .map((device) => device.id);
  }

  _entryDeviceCount() {
    const ids = this._entryDeviceIds();
    return ids === null ? null : ids.length;
  }

  /**
   * How many entities this hub owns, counted through its devices.
   *
   * Deliberately not `entity.config_entry_id`: the registry the frontend hands
   * a panel is the *display* registry, and it carries `device_id`, `platform`
   * and `entity_category` but no config entry at all -- so filtering on one
   * silently counted zero. Every entity this integration creates is attached
   * to one of its devices, so the device set is the reliable way in.
   */
  _entryEntityCount() {
    const entities = this._hass && this._hass.entities;
    const ids = this._entryDeviceIds();
    if (!entities || ids === null) {
      return null;
    }
    const owned = new Set(ids);
    return Object.values(entities).filter((entity) =>
      owned.has(entity.device_id)
    ).length;
  }

  /**
   * Leave the panel the way the user arrived at it.
   *
   * The panel is the rtl_433 entry's configuration page, so it is always
   * reached from somewhere -- the integration page, or a link. `history.back()`
   * returns there rather than guessing a destination, which is what makes the
   * control correct on a phone, where the sidebar is closed and this is the
   * only way out.
   */
  _goBack() {
    // Inside the panel, back means up a level: a subview returns to the
    // overview rather than out of the integration entirely, which is what the
    // arrow means on core's own subpages.
    switch (backAction(this._segment, this._pushed, window.history.length)) {
      case "unwind":
        this._pushed = false;
        window.history.back();
        return;
      case "replace-up":
        this._navigate("", { replace: true });
        return;
      case "leave":
        window.history.back();
        return;
      default:
        // Opened directly by URL with nothing behind it. The integration's own
        // page is where this panel belongs under.
        window.location.assign("/config/integrations/integration/rtl_433");
    }
  }

  /**
   * Forget every candidate, so the list refills from live traffic.
   *
   * Offered without a confirmation because there is nothing to confirm: the
   * pending list is memory-only and every device cleared returns on its next
   * transmission. What the user is discarding is a working set, not a decision
   * -- ignoring is the decision, and this does not touch it.
   */
  async _clearDevices() {
    this._banner = null;
    this._el.clear.disabled = true;
    try {
      const result = await this._call({
        type: "rtl_433/devices/clear",
        entry_id: this._entryId,
      });
      // The green cards describe adoptions, not candidates, but they are the
      // same screenful: leaving them behind a cleared list would look like the
      // clear had half worked.
      this._added.clear();
      this._setBanner(
        `Cleared ${result.cleared} discovered ${
          result.cleared === 1 ? "device" : "devices"
        }. They reappear as they transmit.`,
        "notice"
      );
    } catch (error) {
      this._setBanner(describeError(error), "error");
    } finally {
      this._el.clear.disabled = false;
      this._render();
    }
  }

  // -- Settings ---------------------------------------------------------------

  /**
   * Open one of the three settings forms.
   *
   * The payload behind all three is fetched once and reused, because the three
   * pages are one screenful between them and the alternative is a round trip
   * per visit. It is re-fetched after every save so the next visit shows what
   * was stored rather than what was typed -- the two differ exactly where the
   * backend cleared something, which is the case the user most needs to see.
   */
  async _openSettings(kind) {
    if (!this._entryId || this._settingsForm === kind) {
      return;
    }
    this._el.settingsProblem.hidden = true;
    if (!this._settings) {
      // The form cannot be drawn until the payload lands, and the view is
      // already on screen by now -- so the body says so rather than sitting
      // blank, and Save is withheld until there is something to save.
      this._el.settingsBody.textContent = "Loading…";
      this._el.settingsSave.disabled = true;
      try {
        this._settings = await this._call({
          type: "rtl_433/settings/get",
          entry_id: this._entryId,
        });
      } catch (error) {
        this._el.settingsBody.textContent = "";
        this._el.settingsProblem.textContent = describeError(error);
        this._el.settingsProblem.hidden = false;
        return;
      } finally {
        this._el.settingsSave.disabled = false;
      }
      // Navigated away while the payload was in flight.
      if (this._viewFor().form !== kind) {
        return;
      }
    }
    this._buildSettingsForm(kind);
  }

  /**
   * Build one field: a label, a control, and an optional hint beneath it.
   *
   * Returns the control so the caller can read it back on save. Every form here
   * is a handful of these, so a builder is cheaper than a template per form and
   * keeps the markup and the read-back next to each other -- the two things
   * that drift when a field is added.
   */
  _field(parent, { label, control, hint, hidden }) {
    const wrapper = document.createElement("div");
    wrapper.className = control.type === "checkbox" ? "field checkbox" : "field";
    wrapper.hidden = Boolean(hidden);
    const id = `field-${Math.random().toString(36).slice(2)}`;
    control.id = id;
    const labelEl = document.createElement("label");
    labelEl.setAttribute("for", id);
    labelEl.textContent = label;
    // A checkbox reads as "[x] label", everything else as "label / control".
    if (control.type === "checkbox") {
      wrapper.append(control, labelEl);
    } else {
      wrapper.append(labelEl, control);
    }
    if (hint) {
      const hintEl = document.createElement("span");
      hintEl.className = "hint";
      hintEl.textContent = hint;
      wrapper.append(hintEl);
    }
    parent.append(wrapper);
    control.fieldEl = wrapper;
    return control;
  }

  /** A `<select>` pre-set to `value`, from `[value, label]` pairs. */
  _select(options, value) {
    const select = document.createElement("select");
    for (const [optionValue, optionLabel] of options) {
      const option = document.createElement("option");
      option.value = optionValue;
      option.textContent = optionLabel;
      option.selected = optionValue === value;
      select.append(option);
    }
    return select;
  }

  /** A number input, blank when `value` is null (which means "not set"). */
  _numberInput(value, min) {
    const input = document.createElement("input");
    input.type = "number";
    if (min !== undefined) {
      input.min = String(min);
    }
    input.value = value === null || value === undefined ? "" : String(value);
    return input;
  }

  /** The settings row for one device key, from the last fetched payload. */
  _deviceSettings(deviceKey) {
    return (
      this._settings.devices.find((device) => device.device_key === deviceKey) ||
      null
    );
  }

  /**
   * The fields of one settings form, in `ha-form`'s selector vocabulary.
   *
   * One schema per form, and it is the *only* description of these fields:
   * `ha-form` renders it, the native fallback renders the same list, and the
   * save reads its names straight off `_settingsData`. A field added here shows
   * up in all three without being repeated, which is the drift the previous
   * builder-plus-read-back pair kept inviting.
   *
   * Selector names are Home Assistant's own (`number`, `boolean`, `select`), so
   * the controls that appear are the controls core would draw for the same
   * field -- which is the whole point of going through `ha-form` rather than
   * assembling `ha-input` and `ha-select` here by hand.
   */
  _settingsSchema(kind) {
    const settings = this._settings;
    if (kind === "hub") {
      return [
        {
          name: "availability_timeout",
          selector: { number: { min: 0, mode: "box" } },
        },
        { name: "manage_settings", selector: { boolean: {} } },
      ];
    }
    if (kind === "mappings") {
      // Deliberately not an `object` selector: that one parses the YAML and
      // hands back a structure, so saving it would rewrite the user's file
      // without their comments or their key order. The backend takes the raw
      // text, so the raw text is what the editor holds.
      return [{ name: "mappings", yaml: true }];
    }

    const device = this._deviceSettings(this._settingsData.device_key);
    const schema = [
      {
        name: "device_key",
        selector: {
          select: {
            mode: "dropdown",
            options: settings.devices.map((entry) => ({
              value: entry.device_key,
              label: entry.label,
            })),
          },
        },
      },
      {
        name: "timeout_override",
        selector: { number: { min: 0, mode: "box" } },
      },
    ];
    if (!device) {
      return schema;
    }
    // Only for devices with a field that actually auto-clears; anywhere else
    // this would be a control with nothing behind it.
    if (device.motion) {
      schema.push({
        name: "motion_clear_delay",
        selector: { number: { min: 1, mode: "box" } },
      });
    }
    schema.push({
      name: "commodity",
      selector: {
        select: {
          mode: "dropdown",
          options: settings.commodities.map((value) => ({
            value,
            label: value,
          })),
        },
      },
    });
    // The unit list is per commodity: offering a unit Home Assistant will not
    // convert for the chosen commodity produces a sensor the Energy dashboard
    // silently refuses, so the list is narrowed rather than validated later.
    // A commodity with no units is not calibrated at all, and then neither
    // field belongs on the form -- which is why the schema is recomputed on
    // every change rather than fields being hidden after the fact.
    const units = settings.commodity_units[this._settingsData.commodity] || [];
    if (units.length) {
      schema.push({
        name: "unit",
        selector: {
          select: {
            mode: "dropdown",
            options: units.map((value) => ({ value, label: value })),
          },
        },
      });
      schema.push({
        name: "scale",
        selector: { number: { min: 0, step: "any", mode: "box" } },
      });
    }
    return schema;
  }

  /** The label and hint for one field name. */
  _settingsCopy(name) {
    const defaults = this._settings.defaults;
    const copy = {
      availability_timeout: [
        "Availability timeout (seconds)",
        `Leave at ${defaults.availability_timeout} to use the per-device-type ` +
          "defaults, which is what keeps event-driven devices — doorbells, " +
          "motion, contacts — from going unavailable on silence. Use 0 to " +
          "never expire.",
      ],
      manage_settings: [
        "Manage the receiver's own settings",
        "Adds the frequency, gain and sample-rate controls, and lets Home " +
          "Assistant apply them. Turning this off leaves the receiver exactly " +
          "as configured elsewhere.",
      ],
      device_key: ["Device", ""],
      timeout_override: [
        "Availability timeout override (seconds)",
        "Blank uses the receiver's timeout. 0 means never expire.",
      ],
      motion_clear_delay: [
        "Motion clear delay (seconds)",
        "How long after a detection this device is reported clear. Blank uses " +
          `${defaults.motion_clear_delay} seconds.`,
      ],
      commodity: [
        "Utility meter commodity",
        "Setting a commodity turns this device's counter into an " +
          "Energy-dashboard sensor. “none” leaves it as the library " +
          "describes it.",
      ],
      unit: ["Base unit", "One unit of what the counter counts."],
      scale: [
        "Scale",
        "Multiplier on the raw counter, to reach one base unit.",
      ],
      mappings: ["Overrides", ""],
    };
    return copy[name] || [name, ""];
  }

  /** The values the open form starts from. */
  _settingsDefaults(kind) {
    const settings = this._settings;
    if (kind === "hub") {
      return {
        availability_timeout: settings.hub.availability_timeout,
        manage_settings: Boolean(settings.hub.manage_settings),
      };
    }
    if (kind === "mappings") {
      return { mappings: settings.mappings };
    }
    const key =
      this._settingsDevice ||
      (settings.devices.length ? settings.devices[0].device_key : null);
    return this._deviceDefaults(key);
  }

  /** The values for one device, which change wholesale when the picker moves. */
  _deviceDefaults(deviceKey) {
    const device = this._deviceSettings(deviceKey);
    if (!device) {
      return { device_key: deviceKey };
    }
    const calibration = device.calibration;
    const commodity = calibration ? calibration.commodity : device.commodity;
    const units = this._settings.commodity_units[commodity] || [];
    return {
      device_key: deviceKey,
      timeout_override: device.timeout_override,
      motion_clear_delay: device.motion_clear_delay,
      commodity,
      unit: calibration && calibration.commodity === commodity
        ? calibration.unit
        : units[0],
      scale: calibration ? calibration.scale : 1,
    };
  }

  _buildSettingsForm(kind) {
    const body = this._el.settingsBody;
    body.textContent = "";
    this._el.settingsIntro.textContent = "";
    this._el.settingsSave.hidden = false;
    this._settingsForm = kind;

    if (kind === "hub") {
      this._el.settingsIntro.textContent =
        "Settings for this receiver as a whole. Individual devices can override the timeout.";
    } else if (kind === "device") {
      if (!this._settings.devices.length) {
        this._el.settingsIntro.textContent =
          "No devices have been added yet. Add one from this page first, and its settings will appear here.";
        this._el.settingsSave.hidden = true;
        this._settingsData = {};
        return;
      }
      this._el.settingsIntro.textContent =
        "Overrides for one device. Blank means “use the receiver's setting”.";
    } else {
      // The one intro that needs a link, so it is built rather than assigned.
      this._el.settingsIntro.textContent =
        "YAML overrides for how this receiver's fields become entities. Clearing the editor removes them all. ";
      const link = document.createElement("a");
      link.href = this._settings.mappings_docs_url;
      link.target = "_blank";
      link.rel = "noreferrer noopener";
      link.textContent = "Documentation";
      this._el.settingsIntro.append(link);
    }

    this._settingsData = this._settingsDefaults(kind);
    this._renderSettingsForm();
  }

  /**
   * Draw the open form from its schema, preferring `ha-form`.
   *
   * Re-entrant on purpose: the device form's fields depend on the device and
   * the commodity chosen *in* it, so a change re-renders from the new schema
   * rather than reaching in to hide fields. `ha-form` keeps focus across that
   * because it diffs its own rows.
   */
  _renderSettingsForm() {
    const kind = this._settingsForm;
    const body = this._el.settingsBody;
    const schema = this._settingsSchema(kind);
    const yamlField = schema.find((field) => field.yaml);

    if (yamlField) {
      body.textContent = "";
      this._buildYamlField(body, yamlField.name);
      return;
    }

    if (customElements.get("ha-form")) {
      let form = this._el.settingsFormEl;
      if (!form || !form.isConnected) {
        form = document.createElement("ha-form");
        form.className = "settings-form";
        // `computeLabel`/`computeHelper` are how `ha-form` asks for the words;
        // it takes the whole field object, so the name is read off it.
        form.computeLabel = (field) => this._settingsCopy(field.name)[0];
        form.computeHelper = (field) => this._settingsCopy(field.name)[1];
        form.addEventListener("value-changed", (event) =>
          this._onSettingsChanged(event.detail.value)
        );
        body.append(form);
        this._el.settingsFormEl = form;
      }
      form.hass = this._hass;
      form.schema = schema;
      form.data = this._settingsData;
      return;
    }

    // No `ha-form`: the same schema, drawn with native controls, so the form
    // still works and still has exactly the fields the schema names.
    body.textContent = "";
    this._el.settingsFormEl = null;
    for (const field of schema) {
      this._buildNativeField(body, field);
    }
  }

  /**
   * One native control for a schema field, for the no-`ha-form` fallback.
   */
  _buildNativeField(body, field) {
    const [label, hint] = this._settingsCopy(field.name);
    const value = this._settingsData[field.name];
    const selector = field.selector || {};
    let control;
    if (selector.boolean) {
      control = document.createElement("input");
      control.type = "checkbox";
      control.checked = Boolean(value);
    } else if (selector.select) {
      control = this._select(
        selector.select.options.map((option) => [option.value, option.label]),
        value
      );
    } else {
      control = this._numberInput(value, selector.number?.min);
      if (selector.number?.step) {
        control.step = String(selector.number.step);
      }
    }
    this._field(body, { label, control, hint });
    control.addEventListener("change", () => {
      const next = { ...this._settingsData };
      next[field.name] = selector.boolean
        ? control.checked
        : selector.select
          ? control.value
          : this._readNumber(control);
      this._onSettingsChanged(next);
    });
  }

  /**
   * The mappings editor: Home Assistant's own YAML editor.
   *
   * `ha-code-editor` in `yaml` mode rather than `ha-yaml-editor`, because the
   * backend stores the text and `ha-yaml-editor` hands back a parsed structure
   * -- saving that would rewrite the file without the user's comments or key
   * order. This keeps the characters the user typed.
   */
  _buildYamlField(body, name) {
    const [label, hint] = this._settingsCopy(name);
    const editor = haControl("ha-code-editor", () => {
      const native = document.createElement("textarea");
      native.spellcheck = false;
      return native;
    });
    editor.className = "mappings-editor";
    if (editor.localName === "ha-code-editor") {
      editor.hass = this._hass;
      editor.mode = "yaml";
      editor.linewrap = true;
      editor.inDialog = true;
    }
    editor.value = this._settingsData[name] || "";
    const commit = (text) =>
      this._onSettingsChanged({ ...this._settingsData, [name]: text });
    editor.addEventListener("value-changed", (event) =>
      commit(event.detail.value)
    );
    editor.addEventListener("change", () => commit(editor.value));
    this._field(body, { label, control: editor, hint });
  }

  /**
   * Take a form's new values.
   *
   * Moving the device picker replaces the rest of the form wholesale, because
   * every default on it comes *from* the device: its stored override, whether
   * it has a field that auto-clears at all, and whether it already has a
   * calibration. Changing the commodity only re-renders, because the units it
   * offers depend on it.
   */
  _onSettingsChanged(next) {
    const previous = this._settingsData;
    if (
      this._settingsForm === "device" &&
      next.device_key !== previous.device_key
    ) {
      this._settingsDevice = next.device_key;
      this._settingsData = this._deviceDefaults(next.device_key);
      this._renderSettingsForm();
      return;
    }
    this._settingsData = next;
    if (
      this._settingsForm === "device" &&
      next.commodity !== previous.commodity
    ) {
      // A different commodity means a different unit list, and possibly no
      // unit or scale field at all.
      this._settingsData = {
        ...next,
        unit: (this._settings.commodity_units[next.commodity] || [])[0],
      };
      this._renderSettingsForm();
    }
  }

  /** Read a number field: blank is `null` (clear it), not `0`. */
  _readNumber(control) {
    if (!control || control.value.trim() === "") {
      return null;
    }
    const value = Number(control.value);
    return Number.isFinite(value) ? value : null;
  }

  /**
   * Save the open form.
   *
   * A rejected save stays on the form and reports the reason next to it: the
   * overview's banner is not where the user is standing, and navigating away on
   * a refusal would throw away what they typed. On success the payload is
   * dropped so the next visit re-reads it, the panel returns to the overview,
   * and the subscription is re-established -- a hub
   * reloads when its calibration, mappings or manage-settings toggle changes,
   * and the old subscription would then be pushing a replaced coordinator's
   * state.
   */
  async _saveSettings() {
    const kind = this._settingsForm;
    if (!kind) {
      return;
    }
    const data = this._settingsData || {};
    // A number the user cleared arrives as undefined, and every one of these
    // means "unset" rather than zero -- so they are normalised to null, which
    // is what the backend reads as "clear it".
    const number = (name) => (data[name] === undefined ? null : data[name]);
    let message;
    if (kind === "hub") {
      message = {
        type: "rtl_433/settings/hub",
        entry_id: this._entryId,
        availability_timeout:
          number("availability_timeout") ??
          this._settings.defaults.availability_timeout,
        manage_settings: Boolean(data.manage_settings),
      };
    } else if (kind === "device") {
      // The unit and scale fields only exist for a calibrated commodity, so
      // their absence from the schema is what says "send nothing" -- the same
      // fact the hidden fields used to carry.
      const calibrated = this._settingsSchema("device").some(
        (field) => field.name === "unit"
      );
      message = {
        type: "rtl_433/settings/device",
        entry_id: this._entryId,
        device_key: data.device_key,
        timeout_override: number("timeout_override"),
        motion_clear_delay: number("motion_clear_delay"),
        commodity: data.commodity,
        unit: calibrated ? data.unit : null,
        scale: calibrated ? number("scale") : null,
      };
    } else {
      message = {
        type: "rtl_433/settings/mappings",
        entry_id: this._entryId,
        yaml: data.mappings || "",
      };
    }

    this._el.settingsSave.disabled = true;
    this._el.settingsProblem.hidden = true;
    try {
      await this._call(message);
    } catch (error) {
      this._el.settingsProblem.textContent = describeError(error);
      this._el.settingsProblem.hidden = false;
      return;
    } finally {
      this._el.settingsSave.disabled = false;
    }

    this._settings = null;
    this._goUp();
    // Re-subscribe *before* the banner, not after. A hub reloads when its
    // calibration, mappings or manage-settings toggle changes, and the old
    // subscription would go on pushing a replaced coordinator's state -- but
    // `_subscribe` also clears the banner on its way past, so a "saved" set
    // first is wiped before anyone reads it.
    this._subscribe();
    this._setBanner("Settings saved.", "notice");
  }

  // -- Replace ---------------------------------------------------------------

  /**
   * The devices this candidate could be standing in for.
   *
   * Everything the hub already has, minus the candidate itself -- a device
   * cannot replace itself, and `async_replace_device` rejects that anyway.
   */
  _replaceTargets(exceptKey) {
    const devices = this._data && this._data.devices ? this._data.devices : [];
    return exceptKey ? devices.filter((d) => d.key !== exceptKey) : devices;
  }

  /**
   * Open the replace dialog for one candidate.
   *
   * The dialog element itself is built by `_buildReplaceDialog`, which prefers
   * Home Assistant's own; this method only fills it and shows it.
   *
   * Same-model devices are listed first. A battery change keeps the model, so
   * the device the user is looking for is nearly always one of those; the rest
   * stay listed because a model string can change between firmware revisions of
   * the same hardware.
   */
  _openReplaceDialog(row) {
    const targets = this._replaceTargets(row.key);
    if (!targets.length) {
      return;
    }
    this._replaceFor = row;
    this._replaceChoice = "";

    const model = row.model || "";
    const sorted = [...targets].sort((left, right) => {
      const leftMatch = model && left.model === model ? 0 : 1;
      const rightMatch = model && right.model === model ? 0 : 1;
      if (leftMatch !== rightMatch) {
        return leftMatch - rightMatch;
      }
      return left.key < right.key ? -1 : left.key > right.key ? 1 : 0;
    });

    this._text(
      this._el.dialogIntro,
      `${row.model || "This device"} (${row.key}) is new to Home Assistant. ` +
        "If it is a device you already have — the same sensor after a battery " +
        "change, say — pick it below. Its history, settings and entity ids move " +
        "across to the new transmitter id, and the candidate is merged into it."
    );

    this._el.dialogList.textContent = "";
    for (const target of sorted) {
      const text = document.createElement("span");
      text.className = "replace-option-text";
      const name = document.createElement("span");
      name.className = "replace-option-name";
      name.textContent = target.model || "Unknown model";
      const key = document.createElement("span");
      key.className = "replace-option-key mono";
      key.textContent = target.key;
      text.append(name, key);

      const { field, radio } = haRadio("replace-target", target.key, text);
      // `change` is what both the borrowed radio and the native one fire on a
      // pick, and picking is the only thing that enables Replace: the dialog
      // opens with no choice made, so there is nothing safe to default to.
      radio.addEventListener("change", () => {
        this._replaceChoice = target.key;
        this._el.dialogConfirm.disabled = false;
      });
      this._el.dialogList.append(field);
    }

    this._el.dialogConfirm.disabled = true;
    this._showReplaceDialog();
  }

  _closeReplace() {
    const dialog = this._el.dialog;
    if (!dialog.open) {
      return;
    }
    if (dialog.localName === "ha-dialog") {
      // Setting `open` false is the dismiss; the `closed` handler still runs.
      dialog.open = false;
      return;
    }
    dialog.close();
  }

  /**
   * Run the replace the dialog was opened to make.
   *
   * The card disappears on its own: the merge reloads the entry, which rebuilds
   * the pending list without this candidate in it, and that arrives as an
   * ordinary push. Nothing is hidden optimistically here -- the panel shows what
   * the hub says.
   */
  async _confirmReplace() {
    const row = this._replaceFor;
    const target = this._replaceChoice;
    if (!row || !target) {
      return;
    }
    this._closeReplace();
    this._busy.add(row.key);
    this._banner = null;
    this._render();
    try {
      await this._call({
        type: "rtl_433/devices/replace",
        entry_id: this._entryId,
        device_key: row.key,
        replaces: target,
      });
      this._setBanner(
        `${target} now uses the transmitter id ${row.key}. Its history and settings came with it.`,
        "notice"
      );
    } catch (error) {
      this._setBanner(describeError(error), "error");
    } finally {
      this._busy.delete(row.key);
      this._render();
    }
  }

  _createIgnoredCard(row) {
    const element = document.createElement("div");
    element.className = "device-card ignored";
    element.innerHTML = `
      <div class="device-head">
        <div class="device-model"></div>
        <div class="device-key mono"></div>
      </div>
      <div class="device-actions"></div>`;
    element
      .querySelector(".device-actions")
      .append(haButton("Un-ignore", "ghost unignore"));
    const parts = {
      model: element.querySelector(".device-model"),
      key: element.querySelector(".device-key"),
      unignore: element.querySelector(".unignore"),
    };
    element.parts = parts;
    parts.unignore.addEventListener("click", () =>
      this._act(
        "rtl_433/devices/unignore",
        row.key,
        `${row.key} was not on the ignore list.`
      )
    );
    return element;
  }

  _updateIgnoredCard(element, row) {
    const parts = element.parts;
    // A device is usually ignored while still pending, long before anything is
    // stored about it, so its model is often simply not known yet.
    this._text(parts.model, row.model || "Unknown model");
    this._text(parts.key, row.key);
    parts.unignore.disabled = this._busy.has(row.key);
  }
}

const SKELETON = `
  <div class="toolbar">
    <div class="back-slot"></div>
    <div class="toolbar-title">rtl_433</div>
  </div>

  <div class="banner-slot"></div>

  <div class="view view-overview">
    <label class="hub-picker" hidden>
      <span>Receiver</span>
    </label>
    <div class="status-slot"></div>
    <div class="network-slot"></div>
    <div class="page-actions"></div>
    <div class="fab-slot"></div>
  </div>

  <div class="view view-discovered" hidden>
    <div class="searching">
      <div class="searching-spinner"></div>
      <h2 class="searching-title">Searching for rtl_433 devices&hellip;</h2>
      <div class="searching-hint">
        Devices will show up here once discovered.
      </div>
    </div>

    <div class="status" hidden></div>

    <div class="grid" hidden></div>

    <div class="list-actions"></div>

    <div class="grid ignored-grid" hidden></div>
  </div>

  <div class="view view-settings" hidden>
    <p class="settings-intro"></p>
    <div class="settings-card-slot">
      <div class="settings-problem" hidden></div>
      <div class="settings-body"></div>
      <div class="settings-actions"></div>
    </div>
  </div>

  <div class="replace-slot"></div>
`;

/*
 * Every colour is a Home Assistant theme custom property with a light-theme
 * fallback. The fallbacks matter: they are what the panel looks like if a theme
 * (or a future frontend) stops defining one of these, and a missing colour
 * should degrade to "readable" rather than to black text on a black card.
 * Nothing here is hard-coded to a palette, so dark themes follow automatically.
 *
 * The two heading colours carry the card's whole state. Blue is a candidate the
 * receiver has decoded and Home Assistant has not adopted; green is one adopted
 * from this page a moment ago. They are `--info-color` and `--success-color`
 * rather than literals so a theme restyles them along with everything else.
 */
const STYLES = `
  :host {
    display: block;
    box-sizing: border-box;
    height: 100%;
    overflow: auto;
    padding: 16px;
    background: var(--primary-background-color, #fafafa);
    color: var(--primary-text-color, #212121);
    font-family: var(--ha-font-family-body, Roboto, system-ui, sans-serif);
    font-size: 14px;
    line-height: 1.4;
  }
  .wrap { max-width: 1280px; margin: 0 auto; }

  /*
   * The page's own toolbar. A panel reached from the integration page has no
   * chrome of its own -- Home Assistant draws none around a non-iframe custom
   * panel -- so on a phone, where the sidebar is closed, there would otherwise
   * be no way back out. Sized and coloured like core's own app bar so it reads
   * as part of the page rather than as content.
   */
  .toolbar {
    display: flex;
    align-items: center;
    gap: 8px;
    height: 56px;
    margin: -16px -16px 8px;
    padding: 0 8px;
    background: var(--app-header-background-color, var(--primary-color, #03a9f4));
    color: var(--app-header-text-color, var(--text-primary-color, #ffffff));
  }
  .toolbar-title { font-size: 20px; font-weight: 400; }
  .icon-button {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 40px;
    height: 40px;
    padding: 0;
    border: none;
    border-radius: 50%;
    background: transparent;
    color: inherit;
    cursor: pointer;
  }
  button.icon-button:hover { background: rgba(255, 255, 255, 0.12); }
  .icon-button svg { width: 24px; height: 24px; fill: currentColor; }
  /* The borrowed back control draws its own ripple and sizing. */
  ha-icon-button.back, ha-icon-button-arrow-prev.back {
    --mdc-icon-button-size: 40px;
    color: var(--app-header-text-color, #ffffff);
  }

  .list-actions {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin-bottom: 16px;
  }

  .hub-picker {
    display: flex;
    flex-direction: column;
    gap: 4px;
    font-size: 12px;
    color: var(--secondary-text-color, #727272);
  }
  /*
   * An author display rule beats the user agent's "[hidden] { display: none }",
   * so every element this file gives a display to needs its own hidden rule or
   * the hidden property silently does nothing. The picker, the grids and the
   * card's own flex children all need one.
   */
  .hub-picker[hidden] { display: none; }
  /*
   * Only the native fallback is styled here. ha-select arrives with Home
   * Assistant's own field -- floating label, 56px height, theme colours -- and
   * restyling it from outside would be this panel disagreeing with the rest of
   * Settings about what a field looks like.
   */
  select.hub-select {
    font: inherit;
    font-size: 14px;
    padding: 6px 8px;
    color: var(--primary-text-color, #212121);
    background: var(--card-background-color, #ffffff);
    border: 1px solid var(--divider-color, #e0e0e0);
    border-radius: 4px;
  }
  ha-select.hub-select { min-width: 240px; }
  ha-alert.banner { display: block; margin-bottom: 16px; }
  ha-alert.banner[hidden] { display: none; }
  div.banner {
    padding: 12px 16px;
    margin-bottom: 16px;
    border-radius: 8px;
    border-left: 4px solid var(--error-color, #db4437);
    background: var(--card-background-color, #ffffff);
    color: var(--primary-text-color, #212121);
  }
  div.banner.notice { border-left-color: var(--warning-color, #ffa600); }
  .status {
    padding: 24px;
    text-align: center;
    color: var(--secondary-text-color, #727272);
  }

  /*
   * auto-fill rather than auto-fit: with a single candidate, auto-fit would
   * stretch its card the full width of a desktop window, which reads as a
   * layout bug rather than as one device.
   */
  .grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
    gap: 16px;
    margin-bottom: 16px;
  }
  .grid[hidden] { display: none; }

  .device-card {
    display: flex;
    flex-direction: column;
    background: var(--card-background-color, #ffffff);
    border: 1px solid var(--divider-color, #e0e0e0);
    border-radius: var(--ha-card-border-radius, 12px);
    overflow: hidden;
  }
  .device-head {
    padding: 12px 16px;
    background: var(--info-color, #039be5);
    color: var(--text-primary-color, #ffffff);
  }
  .device-card.added .device-head {
    background: var(--success-color, #0f9d58);
  }
  .device-card.ignored .device-head {
    background: var(--disabled-text-color, #bdbdbd);
  }
  .device-model {
    font-size: 16px;
    font-weight: 500;
    overflow-wrap: anywhere;
  }
  .device-key {
    font-size: 12px;
    opacity: 0.9;
    overflow-wrap: anywhere;
  }
  .device-body {
    flex: 1;
    padding: 12px 16px;
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .stats { display: flex; flex-wrap: wrap; gap: 16px; }
  .stat { display: flex; flex-direction: column; }
  .stat-label {
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.02em;
    color: var(--secondary-text-color, #727272);
  }
  .stat-value { font-size: 15px; }

  /*
   * An entity row, laid out like the ones on a device page: a state-coloured
   * icon, the name, and the value against the right edge. No rules between
   * rows -- a device page does not draw them, and at this row height they made
   * three readings look like a table again.
   */
  .reading {
    display: flex;
    align-items: center;
    gap: 16px;
    min-height: 40px;
  }
  .reading-icon {
    flex: 0 0 24px;
    width: 24px;
    height: 24px;
    color: var(--state-icon-color, #44739e);
  }
  .reading-name {
    flex: 1;
    overflow-wrap: anywhere;
  }
  .reading-value {
    text-align: right;
    white-space: nowrap;
    color: var(--secondary-text-color, #727272);
  }

  /*
   * Pushed to the foot of the body so the pickers line up across a row of
   * cards. Cards in a grid row stretch to the tallest, and a device with one
   * reading would otherwise leave its picker stranded in mid-card with the
   * slack below it.
   */
  .area { display: block; margin-top: auto; }
  .area[hidden] { display: none; }
  /*
   * ha-area-picker brings its own label and its own 56px outlined field, so it
   * needs no styling here -- only the room to use it.
   */

  .device-actions {
    display: flex;
    align-items: center;
    justify-content: flex-end;
    gap: 8px;
    padding: 12px 16px;
    border-top: 1px solid var(--divider-color, #e0e0e0);
  }
  .device-link {
    margin-right: auto;
    color: var(--primary-color, #03a9f4);
    text-decoration: none;
    font-weight: 500;
  }
  .device-link:hover { text-decoration: underline; }
  .device-link[hidden] { display: none; }

  .mono {
    font-family: var(--ha-font-family-code, ui-monospace, Menlo, Consolas, monospace);
    font-size: 13px;
  }
  button {
    font: inherit;
    cursor: pointer;
    padding: 6px 14px;
    border: 1px solid transparent;
    border-radius: 4px;
  }
  button:disabled { opacity: 0.5; cursor: default; }
  button[hidden] { display: none; }
  ha-button[hidden] { display: none; }
  .list-actions { display: flex; gap: 8px; flex-wrap: wrap; }  button.primary {
    background: var(--primary-color, #03a9f4);
    color: var(--text-primary-color, #ffffff);
  }
  button.ghost {
    background: transparent;
    color: var(--primary-color, #03a9f4);
    border-color: var(--divider-color, #e0e0e0);
  }
  button:focus-visible {
    outline: 2px solid var(--primary-color, #03a9f4);
    outline-offset: 2px;
  }

  /*
   * Both dialogs. Native <dialog>s, so the backdrop, the stacking and the focus
   * trap are the platform's; only the surface needs dressing, in the same
   * tokens as the cards.
   */
  /*
   * Only the native dialog is styled here. The replace dialog is ha-dialog now
   * and arrives with its own surface, radius, scrim and phone treatment;
   * painting over those from out here is how a borrowed dialog ends up looking
   * like neither one thing nor the other.
   */
  dialog.panel-dialog {
    padding: 0;
    border: none;
    border-radius: var(--ha-card-border-radius, 12px);
    background: var(--card-background-color, #ffffff);
    color: var(--primary-text-color, #212121);
    max-width: 480px;
    width: calc(100vw - 32px);
  }
  dialog.panel-dialog::backdrop { background: rgba(0, 0, 0, 0.5); }
  /* The borrowed dialog only needs its body to breathe like core's do. */
  ha-dialog.replace-dialog .replace-list { margin-top: 8px; }
  .panel-dialog-form {
    margin: 0;
    padding: 20px;
    font-family: var(--ha-font-family-body, Roboto, system-ui, sans-serif);
    font-size: 14px;
  }
  .panel-dialog-title { margin: 0 0 8px; font-size: 20px; font-weight: 400; }
  .panel-dialog-intro {
    margin: 0 0 16px;
    color: var(--secondary-text-color, #727272);
  }
  .panel-dialog-intro a { color: var(--primary-color, #03a9f4); }
  .replace-list {
    max-height: 45vh;
    overflow-y: auto;
    margin-bottom: 16px;
  }
  ha-formfield.replace-option {
    display: flex;
    width: 100%;
    padding: 4px 0;
  }
  label.replace-option {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 8px 4px;
    cursor: pointer;
    border-radius: 4px;
  }
  .replace-option:hover { background: var(--secondary-background-color, #f5f5f5); }
  .replace-option-text { display: flex; flex-direction: column; }
  .replace-option-key {
    font-size: 12px;
    color: var(--secondary-text-color, #727272);
    overflow-wrap: anywhere;
  }
  .panel-dialog-actions {
    display: flex;
    justify-content: flex-end;
    gap: 8px;
  }

  /*
   * The settings row: the three forms that used to live behind the config
   * entry's Configure button and now have nowhere else to be, since this panel
   * *is* that button. Above the discovered devices rather than below them,
   * because a receiver in a busy neighbourhood puts dozens of cards between the
   * two and a setting nobody can find is a setting nobody has.
   */
  .page-actions { margin-bottom: 16px; }

  /*
   * The overview and the settings pages are a reading column, not a grid, so
   * they are held to a width a form is comfortable at rather than stretched
   * across a desktop. The discovered view is deliberately left full width --
   * it is a card grid, and it wants the room.
   */
  .view-overview, .view-settings { max-width: 800px; margin: 0 auto; }
  .settings-card-slot {
    padding: 16px;
    border-radius: var(--ha-card-border-radius, 12px);
    background: var(--card-background-color, #ffffff);
    border: 1px solid var(--divider-color, #e0e0e0);
  }
  .settings-intro {
    margin: 0 0 16px;
    max-width: 68ch;
    color: var(--secondary-text-color, #727272);
  }

  /*
   * One view on screen at a time. An author display rule beats the user
   * agent's [hidden] rule, so each of these needs its own hidden rule or the
   * hidden property silently does nothing -- the same trap the hub picker hit.
   */
  .view { display: block; }
  .view[hidden] { display: none; }

  /*
   * The overview, shaped like the integration pages core ships for Zigbee and
   * Z-Wave: a status card, then cards of rows that open somewhere. Spacing is
   * the only thing set on the cards themselves -- ha-card brings its own
   * surface, radius and elevation, and overriding those would make this panel
   * disagree with every other card in Settings.
   */
  .status-slot, .network-slot { display: block; margin-bottom: 16px; }
  .status-card, .network-card, .settings-card { display: block; }
  .status-row {
    display: flex;
    align-items: center;
    gap: 16px;
    padding: 16px;
  }
  .status-badge {
    display: flex;
    align-items: center;
    justify-content: center;
    flex: 0 0 auto;
    width: 40px;
    height: 40px;
    border-radius: 50%;
    background: color-mix(in srgb, var(--success-color, #43a047) 18%, transparent);
    color: var(--success-color, #43a047);
  }
  /*
   * A receiver that is not connected is the one thing on this page worth
   * colouring, so the badge carries the state rather than a line of text
   * needing to be read.
   */
  .status-badge.offline {
    background: color-mix(in srgb, var(--error-color, #db4437) 18%, transparent);
    color: var(--error-color, #db4437);
  }
  .status-icon { width: 24px; height: 24px; }
  /*
   * Pushed to the far end by the text block taking the slack, so the mark keeps
   * its corner whatever the state line says. Sized in ch-independent pixels
   * because it is a fixed-aspect image, not text.
   */
  .status-text { flex: 1 1 auto; min-width: 0; }
  .status-logo {
    flex: 0 0 auto;
    width: 40px;
    height: 40px;
    object-fit: contain;
  }
  .status-logo[hidden] { display: none; }
  .status-headline { font-size: 20px; font-weight: 400; }
  .status-supporting {
    font-size: 14px;
    color: var(--secondary-text-color, #727272);
  }

  .card-list { display: block; }
  .card-heading {
    margin: 0;
    padding: 16px 16px 0;
    font-size: 20px;
    font-weight: 400;
  }
  .row-icon { color: var(--secondary-text-color, #727272); }
  .row-supporting[hidden] { display: none; }
  /*
   * The fallback row, for a frontend with no ha-md-list-item: a button laid out
   * like the list row it stands in for, so the overview still reads as a list
   * of things to open.
   */
  button.nav-devices,
  button.nav-entities,
  button.open-hub-settings,
  button.open-device-settings,
  button.open-mappings {
    display: flex;
    align-items: center;
    gap: 16px;
    width: 100%;
    padding: 16px;
    border: none;
    border-radius: 0;
    background: none;
    color: inherit;
    text-align: left;
    cursor: pointer;
  }
  button.nav-devices .row-headline,
  button.nav-entities .row-headline,
  button.open-hub-settings .row-headline,
  button.open-device-settings .row-headline,
  button.open-mappings .row-headline { font-size: 16px; }

  /*
   * The search state, shaped like the one core shows while a radio is looking
   * for devices: a spinner, what it is doing, and where the results will land.
   * It stays on screen once cards appear, because the receiver does not stop
   * listening -- only the closing hint drops away, where it would be describing
   * something already visible.
   */
  .searching {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 8px;
    padding: 24px 16px 32px;
    text-align: center;
  }
  .searching-spinner { display: flex; min-height: 28px; }
  .searching-title { margin: 0; font-size: 20px; font-weight: 400; }
  .searching-hint {
    max-width: 48ch;
    color: var(--secondary-text-color, #727272);
  }
  .searching-hint[hidden] { display: none; }

  /*
   * The floating action button. Fixed to the viewport rather than the page so
   * it stays reachable while the overview scrolls, and inset far enough to
   * clear a phone's home indicator.
   */
  .panel-fab {
    position: fixed;
    right: 16px;
    bottom: 16px;
    bottom: calc(16px + env(safe-area-inset-bottom, 0px));
    z-index: 1;
  }
  ha-button.panel-fab { --ha-button-height: 56px; }
  button.fab-button {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    height: 56px;
    padding: 0 20px;
    border: none;
    border-radius: 28px;
    background: var(--primary-color, #03a9f4);
    color: var(--text-primary-color, #ffffff);
    font-size: 15px;
    cursor: pointer;
  }

  /* The settings subview's own action row, at the end of the page. */
  .settings-actions {
    display: flex;
    justify-content: flex-end;
    gap: 8px;
    margin-top: 16px;
  }

  /*
   * The native fallback's form controls, for a frontend with no ha-form. Sized
   * and coloured like Home Assistant's own so a page that has to fall back to
   * them still reads as part of the frontend. Every rule here is scoped to the
   * element it styles, so none of it reaches the borrowed controls: those
   * arrive already themed, and restyling them from outside is how a panel ends
   * up disagreeing with the rest of Settings. (No backticks in here -- this
   * comment is inside a template literal, and one would end it.)
   */
  .field { margin-bottom: 16px; }
  .field > label {
    display: block;
    margin-bottom: 4px;
    font-size: 13px;
    color: var(--secondary-text-color, #727272);
  }
  .field .hint {
    display: block;
    margin-top: 4px;
    font-size: 12px;
    color: var(--secondary-text-color, #727272);
  }
  .field input[type="number"],
  .field input[type="text"],
  .field select,
  .field textarea {
    box-sizing: border-box;
    width: 100%;
    padding: 8px;
    font-family: inherit;
    font-size: 14px;
    color: var(--primary-text-color, #212121);
    background: var(--card-background-color, #ffffff);
    border: 1px solid var(--divider-color, #e0e0e0);
    border-radius: 4px;
  }
  .field textarea {
    min-height: 220px;
    font-family: var(--ha-font-family-code, ui-monospace, monospace);
    font-size: 13px;
    resize: vertical;
    white-space: pre;
    overflow-wrap: normal;
    overflow-x: auto;
  }
  /*
   * A checkbox reads as "[x] label", with its hint on its own line beneath --
   * so the row wraps and the hint is given the whole width. Without that the
   * hint sits beside the label as a flex sibling and squeezes it into a
   * three-word-wide column, which is what it did until a screenshot showed it.
   */
  .field.checkbox {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
  }
  .field.checkbox > label {
    flex: 1 1 auto;
    margin: 0;
    color: inherit;
    font-size: 14px;
  }
  .field.checkbox .hint { flex: 1 0 100%; margin-top: 0; }
  .field[hidden] { display: none; }

  /*
   * The borrowed form is left to lay itself out. ha-form puts every row inside
   * a single wrapper of its own, so a flex gap here would apply to that one
   * child and space nothing -- and the row rhythm it already has is core's,
   * for the same fields, which is the whole reason for going through it.
   */
  ha-form.settings-form { display: block; }
  /*
   * The YAML editor needs a height: ha-code-editor sizes to its content, and
   * an empty mappings file would otherwise open as a one-line box that grows
   * under the cursor.
   */
  ha-code-editor.mappings-editor {
    display: block;
    min-height: 220px;
    border: 1px solid var(--divider-color, #e0e0e0);
    border-radius: 4px;
    overflow: hidden;
  }
  /* The label above a borrowed control is the panel's, so it keeps its own. */
  .field > ha-code-editor { margin-top: 4px; }

  /*
   * No height cap: this is a page now, so the page scrolls. Capping it was for
   * a dialog, where the form had to fit inside a box that could not grow.
   */
  .settings-body { display: block; }
  /*
   * A rejected save reports what was wrong next to the form that was refused,
   * not in the page banner: the banner is at the top of the overview, which is
   * not where the user is standing, and a form that lost its reason would look
   * like it simply did nothing.
   */
  .settings-problem {
    margin-bottom: 16px;
    padding: 8px 12px;
    border-radius: 4px;
    background: var(--error-color, #db4437);
    color: var(--text-primary-color, #ffffff);
    white-space: pre-wrap;
  }
  .settings-problem[hidden] { display: none; }
  /*
   * The page padding and a card's own padding stack. That is comfortable on a
   * desktop and wasteful on a phone, where it cost the form 66px of a 412px
   * screen, so the outer one gives way and the card keeps its own.
   */
  @media (max-width: 600px) {
    :host { padding: 8px; }
    .settings-card-slot { padding: 12px; }
  }

`;

// Guarded twice over. `customElements.define` throws on a name that is already
// taken, and a panel module can be evaluated more than once in a long-lived
// frontend session; and the registry is absent entirely outside a browser,
// where this module is imported to test the pure helpers above.
if (
  typeof customElements !== "undefined" &&
  !customElements.get("rtl-433-panel")
) {
  customElements.define("rtl-433-panel", Rtl433Panel);
}
