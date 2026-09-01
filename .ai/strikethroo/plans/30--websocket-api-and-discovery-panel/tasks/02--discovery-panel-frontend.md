---
id: 2
group: "frontend"
dependencies: [1]
status: "pending"
created: 2026-09-01
skills:
  - web-components
  - home-assistant-integration
complexity_score: 7
complexity_notes: "First frontend asset in the repository: a hand-written web component plus static-path and panel registration, with a hard no-build-step constraint and unstable host APIs."
---
# Ship the discovery panel

## Objective

Add the ZHA-style page: a custom panel, reachable from the integration card,
rendering a live table of discovered devices with per-row Add and Ignore buttons —
as one hand-written ES module with no build step.

## Skills Required

- `web-components` — custom elements, shadow DOM, ES modules, plain DOM rendering.
- `home-assistant-integration` — `panel_custom`, `http` static paths, and the
  `hass` object handed to a non-iframe panel.

## Acceptance Criteria

- [ ] `custom_components/rtl_433/frontend/rtl_433-panel.js` defines a single
      custom element and **imports nothing**.
- [ ] No `ha-*` Home Assistant component is used, and no Home Assistant internal
      module is imported.
- [ ] No npm, package.json, bundler, or build step is added anywhere.
- [ ] `__init__.py` registers a static path for that directory and calls
      `panel_custom.async_register_panel` with `config_panel_domain=DOMAIN`,
      `require_admin=True`, and `embed_iframe=False`.
- [ ] Registration happens once per Home Assistant run; setting up a second hub
      entry does not register a second panel or raise.
- [ ] The panel lists pending devices with model, key, sighting count, signal
      level, last seen, and latest field values, one row each.
- [ ] Each row has working **Add** and **Ignore** buttons.
- [ ] An ignored-devices view offers **Un-ignore**.
- [ ] The table updates live from `rtl_433/devices/subscribe` — a device heard
      while the panel is open appears without a reload.
- [ ] Columns sort by last seen, sighting count, and signal level.
- [ ] A hub selector appears when more than one hub is configured.
- [ ] Empty, loading, and error states are handled — never a blank page.
- [ ] Styling uses Home Assistant theme CSS custom properties and is legible in
      both light and dark themes.
- [ ] `uv run pytest tests/` exits 0; ruff check and format clean.

Use your internal Todo tool to track these and keep on track.

## Technical Requirements

- Read `homeassistant/components/dynalite/panel.py` in
  `.venv/lib/python3.14/site-packages/` for the exact registration calls.
- `StaticPathConfig` is imported from `homeassistant.components.http`.
- A non-iframe custom panel receives `hass`, `narrow`, `route`, and `panel` as
  **properties** set on the element by the frontend.
- Message the backend with
  `this.hass.connection.sendMessagePromise({type: "rtl_433/devices/pending", entry_id})`
  and subscribe with `this.hass.connection.subscribeMessage(cb, {type: "rtl_433/devices/subscribe", entry_id})`.

## Input Dependencies

- Task 1: the five WebSocket commands, the subscription, and the manifest
  dependencies.

## Output Artifacts

- The panel asset and its registration, asserted by task 3 and screenshotted by
  task 4.

## Implementation Notes

<details>
<summary>Detailed implementation guidance</summary>

### Registration (`__init__.py`)

```python
PANEL_URL_BASE = "/rtl_433_panel"

async def _async_register_panel(hass: HomeAssistant) -> None:
    """Serve and register the discovery panel, once per Home Assistant run.

    Guarded on the panel already existing because registration is per-run while
    this is called from per-entry setup: a second hub must not register a second
    panel. ``config_panel_domain`` is what puts the panel behind the integration's
    own entry rather than leaving it only in the sidebar, and ``embed_iframe=False``
    is what gets ``hass`` handed to the element directly, so the panel can talk to
    the WebSocket API and inherit the user's theme without importing anything.
    """
    if async_panel_exists(hass, DOMAIN):
        return
    await hass.http.async_register_static_paths([
        StaticPathConfig(PANEL_URL_BASE, str(Path(__file__).parent / "frontend"), False)
    ])
    await panel_custom.async_register_panel(
        hass=hass,
        frontend_url_path=DOMAIN,
        config_panel_domain=DOMAIN,
        webcomponent_name="rtl-433-panel",
        module_url=f"{PANEL_URL_BASE}/rtl_433-panel.js",
        embed_iframe=False,
        require_admin=True,
        sidebar_title="rtl_433",
        sidebar_icon="mdi:radio-tower",
    )
```

Check the real `async_register_static_paths` / `StaticPathConfig` signature in the
installed package rather than trusting the sketch. `cache_headers=False` is right
here — the file ships with the integration and changes on upgrade, and a cached
stale panel is a miserable bug to diagnose.

`async_panel_exists` comes from `homeassistant.components.frontend`; confirm the
import path.

### The component

One file, one class, no imports:

```js
class Rtl433Panel extends HTMLElement {
  static get properties() { ... }   // not needed without Lit; use setters
  set hass(hass) { this._hass = hass; this._maybeInit(); }
  connectedCallback() { ... }
  disconnectedCallback() { /* unsubscribe! */ }
}
customElements.define("rtl-433-panel", Rtl433Panel);
```

Points that matter:

- **`hass` is set repeatedly**, on every state change in Home Assistant. Do not
  re-render or re-subscribe on each set — capture it, and only kick off
  initialisation once. Getting this wrong makes the panel hammer the socket.
- **Unsubscribe in `disconnectedCallback`.** The subscription returns an unsub
  function; a leaked subscription survives navigation away from the panel.
- Use **shadow DOM** so the panel's CSS cannot leak into Home Assistant.
- Render with plain DOM. Rebuilding `innerHTML` on every push loses focus and
  scroll position — update rows in place, or at minimum preserve scroll.
- Theme with the host's custom properties: `--primary-text-color`,
  `--secondary-text-color`, `--card-background-color`, `--divider-color`,
  `--primary-color`, `--error-color`. Provide sane fallbacks
  (`var(--primary-text-color, #212121)`) so the panel is legible even if a
  property is missing.
- Buttons must disable while their call is in flight and re-enable on
  failure, with the error surfaced in the UI — a silent no-op button is the
  worst outcome here.
- Sorting: clicking a column header sorts by it; keep the current sort across
  live updates.

### What NOT to do

- Do not import Lit, or anything else, from a CDN or `node_modules`. The panel
  must work with no network and no build.
- Do not use `ha-card`, `ha-button`, or any other `ha-*` element. They are not a
  public API and would tie the panel to a frontend version.
- Do not add a `package.json`, a bundler config, or a minification step.
- Do not reimplement adopt/ignore logic in JS — call the task-1 commands.

### Wiring the file into the package

`custom_components/rtl_433/frontend/rtl_433-panel.js` must actually ship. Check
whether anything in packaging (`pyproject.toml`'s `[tool.setuptools] packages`,
`hacs.json`, `.gitignore`) would exclude a `.js` file under the component, and fix
it if so. A panel that works locally but is missing from the HACS download is a
silent failure.

</details>
