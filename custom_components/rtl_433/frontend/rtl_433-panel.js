/**
 * rtl_433 discovery panel: a live view of what the receiver is hearing, as one
 * card per candidate device with Add and Ignore on the card itself.
 *
 * This file is deliberately plain. It is one custom element in one ES module
 * with **no imports at all**, and there is no build step anywhere in this
 * repository -- the file HACS downloads is the file the browser runs. That is a
 * constraint, not an accident, and it is worth stating why:
 *
 * - Home Assistant's frontend internals are not a public API. The `ha-*`
 *   elements, its Lit version, and its internal module paths are all free to
 *   change in any release, and a panel that imports them breaks silently on
 *   upgrade for everyone. So this file touches the smallest surface that is
 *   actually stable in practice: `hass.connection` for messaging, `hass.areas`
 *   and `hass.devices` for the two registries it reads, and CSS custom
 *   properties for theming. When something here does break, it degrades to a
 *   broken *page* -- the options flow still adds devices.
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
 * does.
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

/** The integration domain, as it appears in a device registry identifier. */
const DOMAIN = "rtl_433";

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
    // `null` until the first payload lands, which distinguishes "still loading"
    // from "loaded, and there is genuinely nothing here" -- different things to
    // tell someone staring at an empty page.
    this._data = null;
    this._unsubscribe = null;
    this._clock = null;

    this._showIgnored = false;

    // The card the replace dialog was opened from, and the device chosen in it.
    // Held on the panel rather than on the card because the dialog is one
    // element for the whole page: a modal is singular by definition, and one
    // per card would be dozens of hidden dialogs in the tree.
    this._replaceFor = null;
    this._replaceChoice = "";

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

  connectedCallback() {
    // `hass` may have been set before the element was in the document, in which
    // case `_start` deferred; and an element that is detached and re-attached
    // needs its subscription back (see `disconnectedCallback`).
    this._start();
    if (this._started && !this._unsubscribe && this._entryId) {
      this._subscribe();
    }
    this._startClock();
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
    const entryId = this._entryId;
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
    this._status = "Loading…";
    this._render();

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
      if (this._entryId === entryId) {
        this._status = "";
        this._setBanner(describeError(error), "error");
      }
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
    this._act(
      "rtl_433/devices/add",
      row.key,
      `${row.key} is no longer pending — it may already have been added.`,
      () => {
        // Snapshot the row: it is about to leave the pending list, and this is
        // the only copy of what the card should keep showing.
        this._added.set(row.key, { row, deviceId: null });
        const areaId = this._areaChoiceFor(row.key);
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
      hubSelect: root.querySelector(".hub-select"),
      banner: root.querySelector(".banner"),
      status: root.querySelector(".status"),
      grid: root.querySelector(".grid"),
      empty: root.querySelector(".pending-empty"),
      ignoredToggle: root.querySelector(".ignored-toggle"),
      ignoredGrid: root.querySelector(".ignored-grid"),
      dialog: root.querySelector(".replace-dialog"),
      dialogIntro: root.querySelector(".replace-intro"),
      dialogList: root.querySelector(".replace-list"),
      dialogCancel: root.querySelector(".replace-cancel"),
      dialogConfirm: root.querySelector(".replace-confirm"),
    };

    this._el.hubSelect.addEventListener("change", () => {
      this._entryId = this._el.hubSelect.value;
      this._subscribe();
    });

    this._el.ignoredToggle.addEventListener("click", () => {
      this._showIgnored = !this._showIgnored;
      this._render();
    });

    this._el.dialogCancel.addEventListener("click", () => this._closeReplace());
    this._el.dialogConfirm.addEventListener("click", () => this._confirmReplace());
    // Esc and the backdrop both close a native dialog on their own; this keeps
    // the panel's own state from surviving a close it did not initiate.
    this._el.dialog.addEventListener("close", () => {
      this._replaceFor = null;
      this._replaceChoice = "";
    });
  }

  _renderHubPicker() {
    // A single receiver is the common case, and a picker with one option is
    // just a control that does nothing.
    this._el.hubPicker.hidden = this._hubs.length < 2;
    this._el.hubSelect.textContent = "";
    for (const hub of this._hubs) {
      const option = document.createElement("option");
      option.value = hub.entry_id;
      option.textContent = hub.loaded ? hub.title : `${hub.title} (not loaded)`;
      option.selected = hub.entry_id === this._entryId;
      this._el.hubSelect.append(option);
    }
  }

  _render() {
    if (!this._el) {
      return;
    }
    const now = Date.now();

    if (this._banner) {
      this._el.banner.textContent = this._banner.text;
      this._el.banner.className = `banner ${this._banner.kind}`;
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
    this._el.empty.hidden = !loaded || cards.length > 0;

    this._reconcile(
      this._el.grid,
      cards,
      this._deviceCards,
      (card) => this._createDeviceCard(card),
      (element, card) => this._updateDeviceCard(element, card, now, areasChanged)
    );

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
        <button class="ghost replace" type="button">Replace</button>
        <button class="ghost ignore" type="button">Ignore</button>
        <button class="primary add" type="button">Add</button>
      </div>`;

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
   * A native `<dialog>` opened with `showModal()`, not a hand-built overlay and
   * not `ha-dialog`. The platform gives focus trapping, Esc-to-close, a
   * backdrop and correct stacking above everything else on the page for free,
   * and `ha-dialog`'s own API has already moved under this integration once --
   * which is exactly the kind of breakage this file is written to avoid.
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
      const option = document.createElement("label");
      option.className = "replace-option";

      const input = document.createElement("input");
      input.type = "radio";
      input.name = "replace-target";
      input.value = target.key;
      input.addEventListener("change", () => {
        this._replaceChoice = target.key;
        this._el.dialogConfirm.disabled = false;
      });

      const text = document.createElement("span");
      text.className = "replace-option-text";
      const name = document.createElement("span");
      name.className = "replace-option-name";
      name.textContent = target.model || "Unknown model";
      const key = document.createElement("span");
      key.className = "replace-option-key mono";
      key.textContent = target.key;
      text.append(name, key);

      option.append(input, text);
      this._el.dialogList.append(option);
    }

    this._el.dialogConfirm.disabled = true;
    this._el.dialog.showModal();
  }

  _closeReplace() {
    if (this._el.dialog.open) {
      this._el.dialog.close();
    }
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
      <div class="device-actions">
        <button class="ghost unignore" type="button">Un-ignore</button>
      </div>`;
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
  <div class="header">
    <div>
      <h1>Discovered devices</h1>
      <div class="subtitle">
        Devices this receiver has heard that are not in Home Assistant yet.
        Adding one creates its device and entities; ignoring one hides it until
        you un-ignore it. An un-ignored device comes back on its next
        transmission.
      </div>
    </div>
    <label class="hub-picker" hidden>
      <span>Receiver</span>
      <select class="hub-select"></select>
    </label>
  </div>

  <div class="banner" hidden></div>
  <div class="status" hidden></div>

  <div class="grid" hidden></div>

  <div class="empty pending-empty" hidden>
    Nothing pending. Everything this receiver has heard is either added or
    ignored &mdash; trigger a new device so it transmits and it will appear here
    on its own.
  </div>

  <button class="ghost ignored-toggle" type="button" hidden></button>

  <div class="grid ignored-grid" hidden></div>

  <dialog class="replace-dialog">
    <form method="dialog" class="replace-form">
      <h2 class="replace-title">Replace a device</h2>
      <p class="replace-intro"></p>
      <div class="replace-list" role="radiogroup" aria-label="Device to replace"></div>
      <div class="replace-actions">
        <button class="ghost replace-cancel" type="button">Cancel</button>
        <button class="primary replace-confirm" type="button" disabled>Replace</button>
      </div>
    </form>
  </dialog>
`;

/*
 * Every colour is a Home Assistant theme custom property with a light-theme
 * fallback. The fallbacks matter: they are what the panel looks like if a theme
 * (or a future frontend) stops defining one of these, and a missing colour
 * should degrade to "readable" rather than to black text on a black card.
 * Nothing here is hard-coded to a palette, so dark themes follow automatically.
 *
 * The two heading colours carry the card's whole state. Blue is a candidate the
 * receiver has heard and Home Assistant has not adopted; green is one adopted
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
  .header {
    display: flex;
    flex-wrap: wrap;
    gap: 16px;
    align-items: flex-start;
    justify-content: space-between;
    margin-bottom: 16px;
  }
  h1 { margin: 0 0 4px; font-size: 22px; font-weight: 400; }
  .subtitle { color: var(--secondary-text-color, #727272); max-width: 68ch; }
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
  .hub-select {
    font: inherit;
    font-size: 14px;
    padding: 6px 8px;
    color: var(--primary-text-color, #212121);
    background: var(--card-background-color, #ffffff);
    border: 1px solid var(--divider-color, #e0e0e0);
    border-radius: 4px;
  }
  .banner {
    padding: 12px 16px;
    margin-bottom: 16px;
    border-radius: 8px;
    border-left: 4px solid var(--error-color, #db4437);
    background: var(--card-background-color, #ffffff);
    color: var(--primary-text-color, #212121);
  }
  .banner.notice { border-left-color: var(--warning-color, #ffa600); }
  .status, .empty {
    padding: 24px;
    text-align: center;
    color: var(--secondary-text-color, #727272);
  }
  .empty { max-width: 62ch; margin: 0 auto; }

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
  button.primary {
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
  .ignored-toggle { margin: 0 0 16px; }

  /*
   * The replace dialog. A native <dialog>, so the backdrop, the stacking and
   * the focus trap are the platform's; only the surface needs dressing, in the
   * same tokens as the cards.
   */
  .replace-dialog {
    padding: 0;
    border: none;
    border-radius: var(--ha-card-border-radius, 12px);
    background: var(--card-background-color, #ffffff);
    color: var(--primary-text-color, #212121);
    max-width: 480px;
    width: calc(100vw - 32px);
  }
  .replace-dialog::backdrop { background: rgba(0, 0, 0, 0.5); }
  .replace-form {
    margin: 0;
    padding: 20px;
    font-family: var(--ha-font-family-body, Roboto, system-ui, sans-serif);
    font-size: 14px;
  }
  .replace-title { margin: 0 0 8px; font-size: 20px; font-weight: 400; }
  .replace-intro {
    margin: 0 0 16px;
    color: var(--secondary-text-color, #727272);
  }
  .replace-list {
    max-height: 45vh;
    overflow-y: auto;
    margin-bottom: 16px;
  }
  .replace-option {
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
  .replace-actions {
    display: flex;
    justify-content: flex-end;
    gap: 8px;
  }
`;

// Guarded because a panel module can be evaluated more than once in a
// long-lived frontend session, and `customElements.define` throws on a name
// that is already taken.
if (!customElements.get("rtl-433-panel")) {
  customElements.define("rtl-433-panel", Rtl433Panel);
}
