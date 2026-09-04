/**
 * rtl_433 discovery panel: a live view of what the receiver is hearing, with
 * per-row Add and Ignore.
 *
 * This file is deliberately plain. It is one custom element in one ES module
 * with **no imports at all**, and there is no build step anywhere in this
 * repository -- the file HACS downloads is the file the browser runs. That is a
 * constraint, not an accident, and it is worth stating why:
 *
 * - Home Assistant's frontend internals are not a public API. Its Lit version
 *   and its internal module paths are free to change in any release, and a
 *   panel that *imports* them breaks silently on upgrade for everyone. So this
 *   file imports nothing: it touches `hass.connection` for messaging, CSS
 *   custom properties for theming, and the `ha-*` elements only ever **by tag
 *   name**, the way `ha-icon` is used below.
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
 *   step) for a single table. If this panel ever outgrows one hand-written
 *   file, that is a decision to take deliberately rather than to drift into.
 *
 * The backend contract lives in `custom_components/rtl_433/websocket_api.py`:
 * `rtl_433/hubs` names the receivers, `rtl_433/devices/subscribe` pushes one
 * hub's `{pending, ignored}` state whenever it changes, and
 * `rtl_433/devices/add` / `.../ignore` / `.../unignore` are the three actions.
 * None of the adopt/ignore *logic* is reimplemented here; every button is one
 * command call, so the panel and the options flow cannot diverge.
 */

/**
 * Frame keys the "latest values" column suppresses.
 *
 * These are frame metadata rather than readings: the model and id are already
 * the row's key, the signal figures have their own column, and the rest
 * describe the radio instead of the device. Dropping them is what makes the
 * column readable at a glance -- it exists to answer "is this the sensor on my
 * patio?", and a temperature answers that where a modulation scheme does not.
 */
const HIDDEN_FIELDS = new Set([
  "time",
  "model",
  "id",
  "mic",
  "protocol",
  "rssi",
  "snr",
  "noise",
  "freq",
  "freq1",
  "freq2",
  "mod",
  "duration",
]);

/**
 * How often the rendered relative timestamps ("12s ago") are recomputed.
 *
 * The subscription only pushes when the payload changes, so on an idle hub
 * "2s ago" would otherwise stay on screen indefinitely and quietly lie about
 * how long it has been since anything was heard. Re-rendering is cheap because
 * rendering reconciles the existing rows rather than rebuilding them.
 */
const CLOCK_INTERVAL_MS = 15000;

/** The columns the header offers to sort by, and the value each one sorts on. */
const SORTERS = {
  model: (row) => row.model || "",
  key: (row) => row.key || "",
  count: (row) => row.count || 0,
  // A hub started without `-M level` reports no signal at all. Sorting those as
  // negative infinity keeps them together at the weak end of the list rather
  // than scattering them through the real readings.
  signal: (row) =>
    row.signal === null || row.signal === undefined ? -Infinity : row.signal,
  last_seen: (row) => Date.parse(row.last_seen) || 0,
};

/** Format a signal level for the table, or an em dash when there is none. */
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

/** Format an ISO timestamp in the viewer's own locale, for a cell tooltip. */
function formatExact(iso) {
  const parsed = Date.parse(iso);
  return Number.isNaN(parsed) ? "" : new Date(parsed).toLocaleString();
}

/** Render a frame's readings as one compact `key: value` line. */
function formatFields(fields) {
  if (!fields) {
    return "";
  }
  return Object.keys(fields)
    .filter((name) => !HIDDEN_FIELDS.has(name))
    .map((name) => {
      const value = fields[name];
      const shown =
        typeof value === "number" ? Math.round(value * 100) / 100 : value;
      return `${name}: ${shown}`;
    })
    .join(" · ");
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
    // `null` until the first payload lands, which distinguishes "still loading"
    // from "loaded, and there is genuinely nothing here" -- different things to
    // tell someone staring at an empty table.
    this._data = null;
    this._unsubscribe = null;
    this._clock = null;

    this._sortColumn = "last_seen";
    this._sortDescending = true;
    this._showIgnored = false;

    // Device keys with a command in flight, so their buttons can be disabled.
    this._busy = new Set();
    this._banner = null;
    this._status = "";

    // key -> <tr>, so a push updates the rows already on screen instead of
    // replacing them. Rebuilding the table wholesale on every update would
    // throw away focus, text selection and (in a long list) scroll position.
    this._pendingRows = new Map();
    this._ignoredRows = new Map();
  }

  /**
   * Receive the Home Assistant connection object.
   *
   * **Home Assistant sets this on every state change in the whole instance** --
   * many times a second on a busy one -- so this setter must stay a cheap
   * assignment. Re-rendering, or far worse re-subscribing, here would turn
   * every light switch in the house into work on this page and hammer the
   * WebSocket. All it does is capture the reference and let `_start` decide,
   * once, whether there is now enough to begin.
   */
  set hass(hass) {
    this._hass = hass;
    this._start();
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

  /** Show a message above the table: an error, or a neutral notice. */
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

  /**
   * Run one action against one device key.
   *
   * The buttons are disabled while the call is in flight and re-enabled
   * whatever happens, so a slow hub cannot be double-clicked into two
   * adoptions and a failure never leaves a dead control on screen. The row
   * itself disappears (or moves to the ignored list) when the backend pushes
   * the new membership, not here -- the panel shows what the hub says, never
   * what it hopes.
   */
  async _act(commandType, deviceKey, skippedMessage) {
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
      }
    } catch (error) {
      this._setBanner(describeError(error), "error");
    } finally {
      this._busy.delete(deviceKey);
      this._render();
    }
  }

  _sortedPending() {
    const rows =
      this._data && this._data.pending ? this._data.pending.slice() : [];
    const value = SORTERS[this._sortColumn] || SORTERS.last_seen;
    const direction = this._sortDescending ? -1 : 1;
    rows.sort((left, right) => {
      const a = value(left);
      const b = value(right);
      if (a < b) {
        return -direction;
      }
      if (a > b) {
        return direction;
      }
      // A stable tiebreak on the key stops rows swapping places under the
      // cursor every time two devices tie on sighting count or signal.
      return left.key < right.key ? -1 : left.key > right.key ? 1 : 0;
    });
    return rows;
  }

  _onSort(column) {
    if (this._sortColumn === column) {
      this._sortDescending = !this._sortDescending;
    } else {
      this._sortColumn = column;
      // The numeric and time columns are interesting from the top: strongest
      // signal, most sightings, most recently heard. Text is not.
      this._sortDescending = column !== "model" && column !== "key";
    }
    this._render();
  }

  // -- DOM -----------------------------------------------------------------

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
      pendingCard: root.querySelector(".pending-card"),
      pendingBody: root.querySelector(".pending-body"),
      pendingEmpty: root.querySelector(".pending-empty"),
      pendingCount: root.querySelector(".pending-count"),
      ignoredToggle: (() => {
        const toggle = haButton("", "ghost ignored-toggle");
        toggle.hidden = true;
        root.querySelector(".list-actions").append(toggle);
        return toggle;
      })(),
      ignoredCard: root.querySelector(".ignored-card"),
      ignoredBody: root.querySelector(".ignored-body"),
    };

    for (const header of root.querySelectorAll("th[data-sort]")) {
      header.addEventListener("click", () => this._onSort(header.dataset.sort));
      header.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          this._onSort(header.dataset.sort);
        }
      });
    }

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

    for (const header of this.shadowRoot.querySelectorAll("th[data-sort]")) {
      const active = header.dataset.sort === this._sortColumn;
      header.classList.toggle("sorted", active);
      header.setAttribute(
        "aria-sort",
        active ? (this._sortDescending ? "descending" : "ascending") : "none"
      );
      header.querySelector(".arrow").textContent = active
        ? this._sortDescending
          ? "▼"
          : "▲"
        : "";
    }

    const pending = this._sortedPending();
    const loaded = this._data !== null;
    this._el.pendingCard.hidden = !loaded || pending.length === 0;
    this._el.pendingEmpty.hidden = !loaded || pending.length > 0;
    this._el.pendingCount.textContent = loaded ? String(pending.length) : "";

    this._reconcile(
      this._el.pendingBody,
      pending,
      this._pendingRows,
      (row) => this._createPendingRow(row),
      (element, row) => this._updatePendingRow(element, row, now)
    );

    const ignored = this._data && this._data.ignored ? this._data.ignored : [];
    this._el.ignoredToggle.hidden = !loaded || ignored.length === 0;
    this._el.ignoredToggle.textContent = `${
      this._showIgnored ? "Hide" : "Show"
    } ignored devices (${ignored.length})`;
    this._el.ignoredCard.hidden = !this._showIgnored || ignored.length === 0;

    this._reconcile(
      this._el.ignoredBody,
      ignored,
      this._ignoredRows,
      (row) => this._createIgnoredRow(row),
      (element, row) => this._updateIgnoredRow(element, row)
    );
  }

  /**
   * Bring `container`'s children in line with `items`, keyed by `item.key`.
   *
   * Rows that already exist are updated and moved rather than recreated, which
   * is what keeps a button's focus and the page's scroll position across a
   * live push -- the whole point of subscribing rather than reloading.
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

  _createPendingRow(row) {
    const element = document.createElement("tr");
    element.innerHTML = `
      <td class="model"></td>
      <td class="key mono"></td>
      <td class="num count"></td>
      <td class="num signal"></td>
      <td class="age"></td>
      <td><div class="fields mono"></div></td>
      <td class="actions"></td>`;
    element
      .querySelector(".actions")
      .append(
        haButton("Add", "primary add", "accent"),
        haButton("Ignore", "ghost ignore")
      );
    const parts = {
      model: element.querySelector(".model"),
      key: element.querySelector(".key"),
      count: element.querySelector(".count"),
      signal: element.querySelector(".signal"),
      age: element.querySelector(".age"),
      fields: element.querySelector(".fields"),
      add: element.querySelector(".add"),
      ignore: element.querySelector(".ignore"),
    };
    element.parts = parts;
    parts.add.addEventListener("click", () =>
      this._act(
        "rtl_433/devices/add",
        row.key,
        `${row.key} is no longer pending — it may already have been added.`
      )
    );
    parts.ignore.addEventListener("click", () =>
      this._act(
        "rtl_433/devices/ignore",
        row.key,
        `${row.key} was already ignored.`
      )
    );
    return element;
  }

  _updatePendingRow(element, row, now) {
    const parts = element.parts;
    this._text(parts.model, row.model || "Unknown");
    this._text(parts.key, row.key);
    this._text(parts.count, String(row.count));
    this._text(parts.signal, formatSignal(row.signal));
    this._text(parts.age, formatAge(row.last_seen, now));
    parts.age.title = `First seen ${formatExact(
      row.first_seen
    )}\nLast seen ${formatExact(row.last_seen)}`;
    const fields = formatFields(row.fields);
    this._text(parts.fields, fields);
    parts.fields.title = fields;
    const busy = this._busy.has(row.key);
    parts.add.disabled = busy;
    parts.ignore.disabled = busy;
  }

  _createIgnoredRow(row) {
    const element = document.createElement("tr");
    element.innerHTML = `
      <td class="model"></td>
      <td class="key mono"></td>
      <td class="actions"></td>`;
    element
      .querySelector(".actions")
      .append(haButton("Un-ignore", "ghost unignore"));
    const parts = {
      model: element.querySelector(".model"),
      key: element.querySelector(".key"),
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

  _updateIgnoredRow(element, row) {
    const parts = element.parts;
    // A device is usually ignored while still pending, long before anything is
    // stored about it, so its model is often simply not known yet.
    this._text(parts.model, row.model || "Unknown");
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
        Adding one creates its device and entities; ignoring one hides it for
        good. Un-ignoring is not retroactive &mdash; an un-ignored device comes
        back on its next transmission.
      </div>
    </div>
    <label class="hub-picker" hidden>
      <span>Receiver</span>
    </label>
  </div>

  <div class="banner-slot"></div>
  <div class="status" hidden></div>

  <div class="card pending-card" hidden>
    <div class="card-head">
      Pending <span class="pill pending-count"></span>
    </div>
    <div class="scroll">
      <table>
        <thead>
          <tr>
            <th class="sortable" data-sort="model" tabindex="0" role="button">Model <span class="arrow"></span></th>
            <th class="sortable" data-sort="key" tabindex="0" role="button">Device key <span class="arrow"></span></th>
            <th class="sortable num" data-sort="count" tabindex="0" role="button">Sightings <span class="arrow"></span></th>
            <th class="sortable num" data-sort="signal" tabindex="0" role="button">Signal <span class="arrow"></span></th>
            <th class="sortable" data-sort="last_seen" tabindex="0" role="button">Last seen <span class="arrow"></span></th>
            <th>Latest values</th>
            <th></th>
          </tr>
        </thead>
        <tbody class="pending-body"></tbody>
      </table>
    </div>
  </div>

  <div class="empty pending-empty" hidden>
    Nothing pending. Everything this receiver has heard is either added or
    ignored &mdash; trigger a new device so it transmits and it will appear here
    on its own.
  </div>

  <div class="list-actions"></div>

  <div class="card ignored-card" hidden>
    <div class="card-head">Ignored</div>
    <div class="scroll">
      <table>
        <thead>
          <tr><th>Model</th><th>Device key</th><th></th></tr>
        </thead>
        <tbody class="ignored-body"></tbody>
      </table>
    </div>
  </div>
`;

/*
 * Every colour is a Home Assistant theme custom property with a light-theme
 * fallback. The fallbacks matter: they are what the panel looks like if a theme
 * (or a future frontend) stops defining one of these, and a missing colour
 * should degrade to "readable" rather than to black text on a black card.
 * Nothing here is hard-coded to a palette, so dark themes follow automatically.
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
   * the hidden property silently does nothing. The picker is the only one --
   * and without this it stayed on screen for a single receiver, which is
   * exactly the useless one-option control _renderHubPicker means to hide.
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
  .status, .empty {
    padding: 24px;
    text-align: center;
    color: var(--secondary-text-color, #727272);
  }
  .empty { max-width: 62ch; margin: 0 auto; }
  .card {
    margin-bottom: 16px;
    background: var(--card-background-color, #ffffff);
    border: 1px solid var(--divider-color, #e0e0e0);
    border-radius: var(--ha-card-border-radius, 12px);
    overflow: hidden;
  }
  .card-head {
    padding: 12px 16px;
    font-size: 16px;
    border-bottom: 1px solid var(--divider-color, #e0e0e0);
  }
  .pill {
    display: inline-block;
    min-width: 20px;
    padding: 0 8px;
    margin-left: 4px;
    border-radius: 10px;
    font-size: 12px;
    text-align: center;
    background: var(--primary-color, #03a9f4);
    color: var(--text-primary-color, #ffffff);
  }
  .scroll { overflow-x: auto; }
  table { width: 100%; border-collapse: collapse; }
  th, td {
    padding: 8px 16px;
    text-align: left;
    white-space: nowrap;
    border-bottom: 1px solid var(--divider-color, #e0e0e0);
  }
  tbody tr:last-child td { border-bottom: none; }
  th {
    font-size: 12px;
    font-weight: 500;
    letter-spacing: 0.02em;
    text-transform: uppercase;
    color: var(--secondary-text-color, #727272);
  }
  th.num, td.num { text-align: right; }
  th.sortable { cursor: pointer; user-select: none; }
  th.sortable:hover, th.sorted { color: var(--primary-color, #03a9f4); }
  th.sortable:focus-visible {
    outline: 2px solid var(--primary-color, #03a9f4);
    outline-offset: -2px;
  }
  .arrow { font-size: 10px; }
  .mono {
    font-family: var(--ha-font-family-code, ui-monospace, Menlo, Consolas, monospace);
    font-size: 13px;
  }
  .fields {
    max-width: 340px;
    overflow: hidden;
    text-overflow: ellipsis;
    color: var(--secondary-text-color, #727272);
  }
  td.actions { text-align: right; }
  button {
    font: inherit;
    cursor: pointer;
    padding: 6px 14px;
    margin-left: 8px;
    border: 1px solid transparent;
    border-radius: 4px;
  }
  button:disabled { opacity: 0.5; cursor: default; }
  ha-button[hidden] { display: none; }
  .list-actions { display: flex; gap: 8px; flex-wrap: wrap; }
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
`;

// Guarded because a panel module can be evaluated more than once in a
// long-lived frontend session, and `customElements.define` throws on a name
// that is already taken.
if (!customElements.get("rtl-433-panel")) {
  customElements.define("rtl-433-panel", Rtl433Panel);
}
