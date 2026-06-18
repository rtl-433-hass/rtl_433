---
id: 4
group: "docs-i18n"
dependencies: [1, 2, 3]
status: "completed"
created: "2026-06-04"
skills:
  - json
---
# Translations for rebind field, replace step, and repair fix flow (D)

## Objective
Add the user-facing strings for every new surface: the reconfigure radio-id field,
the `hassio_replace` discovery step, the `rebind_successful` abort, the `id_in_use`
error, and the updated `server_unreachable` fix-flow form.

## Skills Required
- `json` — Home Assistant `translations/en.json` structure.

## Acceptance Criteria
- [ ] `config.step.reconfigure` has a `radio_id` entry under both `data` and `data_description`.
- [ ] A new `config.step.hassio_replace` step exists (title, description with `{addon}`/`{host}`/`{port}` placeholders, and a `replaces` `data` label + `data_description`).
- [ ] `config.abort.rebind_successful` and `config.error.id_in_use` strings exist.
- [ ] `issues.server_unreachable.fix_flow.step.confirm` is updated to describe the rebind form and lists `data`/`data_description` for `radio_id`, `host`, `port`, `path`, `secure`.
- [ ] The file remains valid JSON.

Use your internal Todo tool to track these and keep on track.

## Technical Requirements
- File: `custom_components/rtl_433/translations/en.json`.
- Field keys must match the constants from tasks 1–3 exactly: `radio_id` (`CONF_RADIO_ID`), `replaces`, `host`, `port`, `path`, `secure`.

## Input Dependencies
- Task 1 (`radio_id` field, `reconfigure_successful`/`already_configured` reuse), Task 2 (`hassio_replace` step + `replaces` field, `rebind_successful` abort), Task 3 (`server_unreachable` fix-flow form + `id_in_use` error).

## Output Artifacts
- Completed `en.json` strings.

## Implementation Notes
<details>
<summary>Edits to en.json</summary>

**1. `config.step.reconfigure`** — add a `radio_id` field. The label should
reference the add-on's surfaced value (cross-repo contract — the add-on prints the
radio's `unique_id`):
```json
"reconfigure": {
  "title": "Reconfigure rtl_433 hub",
  "description": "Update this hub's connection target, or re-point it at a replacement radio. Nested devices and their history are preserved.",
  "data": {
    "radio_id": "New radio ID (unique_id)",
    "host": "Host",
    "port": "Port",
    "path": "WebSocket path",
    "secure": "Secure (use wss://)"
  },
  "data_description": {
    "radio_id": "The replacement radio's stable ID, shown in the add-on log/status as its unique_id. Leave unchanged to only edit the connection below.",
    "host": "Hostname or IP address of the rtl_433 server.",
    "port": "TCP port of the rtl_433 HTTP server (default 8433).",
    "path": "WebSocket endpoint path on the server (default /ws).",
    "secure": "Connect over wss:// instead of ws:// (for a TLS reverse proxy)."
  }
}
```

**2. New `config.step.hassio_replace`** (add after `hassio_confirm`):
```json
"hassio_replace": {
  "title": "Replace a radio?",
  "description": "A new radio published by the {addon} add-on at `{host}:{port}` was discovered. If it replaces a radio on an existing hub, choose that hub to keep its devices and history. Otherwise add it as a new radio.",
  "data": {
    "replaces": "This radio replaces"
  },
  "data_description": {
    "replaces": "Pick the existing hub whose radio you swapped, or \"It's a new radio\" to add it separately."
  }
}
```

**3. `config.abort`** — add `rebind_successful`:
```json
"rebind_successful": "Radio replaced. Your devices and history are preserved."
```

**4. `config.error`** — add `id_in_use`:
```json
"id_in_use": "That radio ID is already used by another configured hub."
```

**5. `issues.server_unreachable.fix_flow.step.confirm`** — replace with a
rebind-aware form:
```json
"confirm": {
  "title": "rtl_433 server unreachable",
  "description": "Home Assistant cannot reach the configured rtl_433 server. If you replaced the radio, enter the replacement's ID (its unique_id from the add-on log/status) and connection details below to re-point this hub — your devices and history are preserved. If the server is just temporarily down, leave the fields unchanged and submit to retry; the card also clears automatically once the connection is restored.",
  "data": {
    "radio_id": "New radio ID (unique_id)",
    "host": "Host",
    "port": "Port",
    "path": "WebSocket path",
    "secure": "Secure (use wss://)"
  },
  "data_description": {
    "radio_id": "The replacement radio's stable ID. Leave unchanged to keep the current radio and simply retry.",
    "host": "Hostname or IP address of the rtl_433 server.",
    "port": "TCP port of the rtl_433 HTTP server (default 8433).",
    "path": "WebSocket endpoint path on the server (default /ws).",
    "secure": "Connect over wss:// instead of ws:// (for a TLS reverse proxy)."
  }
}
```

**6. Validate:** `python -c "import json;
json.load(open('custom_components/rtl_433/translations/en.json'))"` must succeed.
</details>
