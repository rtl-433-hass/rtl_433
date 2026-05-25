---
id: 1
summary: "Native Home Assistant integration for rtl_433 over its WebSocket API, with per-instance config entries, Battery-Notes-style device discovery, a data-driven device-mapping library, and configurable availability timeouts."
created: 2026-05-25
---

# Plan: rtl_433 WebSocket Home Assistant Integration

## Original Work Order

> We are creating a Home Assistant integration for the rtl_433 websocket API. Key features:
>
> Support for multiple integration config entries to support multiple rtl_433 instances.
> Device discovery similar to Battery Notes: When a new device is discovered, it is shown in HA's device discovery.
> The ability in each addon instance to turn off new device discovery.
> To start: use the same logic as the example code in the rtl_433/examples directory for mapping signals to devices.
> Global, and per-device availability configurations. Since devices may go offline just by not sending a signal, we need to have a timeout to mark a device as unavailable.
>
> Development and testing:
> - following the setup in deviantintegral/flameconnect_ha for CI, automated testing, renovate, and so on
> - conventional commits
> - No need for a separate python library dependency since we are just parsing json and then mapping to HA devices.
> - Full unit test coverage with fixtures for mapping messages to devices.
> - Use playwright-cli plus the Home Assistant docker container to create screenshots of the integration.
> - Use the rtl_433 docker container plus sample data to mock sending real data to Home Assistant for integration tests and creating screenshots for documentation.
> - Treat device mappings as a device library - it shouldn't just be one giant python file. It needs to be easy for contributors and AIs to add support for new devices.

## Plan Clarifications

| # | Question | User guidance | Resolution adopted in this plan |
|---|----------|---------------|---------------------------------|
| 1 | When a discovered device is added, how is it modeled in HA? | "The important thing is that a new entry shows up at the top of the Integrations list." (Asked whether this matches Battery Notes.) | Mirror Battery Notes exactly. A per-instance **hub** config entry runs the WebSocket client; each newly observed device triggers `discovery_flow.async_create_flow()` with `SOURCE_INTEGRATION_DISCOVERY`, producing a "discovered" card at the top of Settings → Devices & Services. Accepting creates a **per-device config entry** that owns that device's entities; dismissing creates a `SOURCE_IGNORE` entry so it is not re-surfaced. |
| 2 | How should the device-mapping library be organized? | "Investigate if instead of executable python code, a csv or yaml file can define mappings… users could in the UI add their own mappings. Other possibility: read the rtl_433 source code and dynamically generate mappings." | Investigation result: rtl_433 has **no centralized machine-readable field registry** — 200+ C decoders embed field/unit definitions individually, and HA semantics (`device_class`, `state_class`) do not exist in the source at all. Source-generation is therefore rejected as brittle. Adopt a **data-driven YAML mapping library** (multiple themed files) seeded by porting the curated `rtl_433_mqtt_hass.py` `mappings` table, loaded by a thin Python loader. A documented drop-in user-override YAML file is included. A full in-UI mapping editor is explicitly **out of scope** (YAGNI) but the data-driven design keeps it possible later. |
| 3 | flameconnect_ha could not be read (private/404). How to source the CI/test/Renovate setup? | "I gave you the wrong name, its https://github.com/deviantintegral/flame_connect_ha" | Repo located and inspected. The CI/tooling and `custom_components` structure are mirrored from `flame_connect_ha` (see Background for the concrete file inventory). |
| 4 | Greenfield identifiers, or compatibility with the existing rtl_433-over-MQTT naming? | "If there is a good reason to greenfield this, I want to know it." | **Greenfield, with reasons.** (a) The MQTT-discovery unique_ids (e.g. `Acurite-606TXN-1-T`) carry no instance scoping; with multiple rtl_433 instances the same `model`+`id` would collide. (b) HA keys entities by `(platform, unique_id)`; entities created here live under the `rtl_433` platform, not `mqtt`, so identical unique_id strings would **not** transfer history from the MQTT integration regardless. String-matching therefore buys no real migration benefit while constraining the design. We adopt a clean, instance-scoped unique_id scheme. |
| 5 | The work order says "each addon instance" — is the deliverable a Home Assistant Supervisor add-on or a HACS custom integration? | "HACS integration — noting that we will create a new addon later to make setup easier." | **HACS custom integration** (`custom_components/rtl_433`); one hub config entry per rtl_433 server, so "addon instance" maps to a hub config entry. A separate Supervisor add-on that bundles rtl_433 for easier setup is acknowledged as **future work and explicitly out of scope** for this plan. |
| 6 | Where does the "sample data" for the rtl_433 Docker replay and screenshots come from? (None exists in-repo.) | "Can we use test data from https://github.com/merbanan/rtl_433_tests ?" | **Yes — via a pinned git submodule.** Reference `merbanan/rtl_433_tests` (`.cu8` files under `tests/GROUP/DEVICE/SET`) as a **pinned git submodule** with a shallow/sparse checkout of only the needed device directories — no captures are copied into the repo, which avoids vendoring the large corpus and sidesteps its lack of an explicit license. The end-to-end integration/screenshot path replays these real captures through rtl_433 Docker. Fast, offline **unit tests** of the mapping library instead run against a small set of **project-authored JSON event fixtures** modeled on the documented rtl_433 field vocabulary, so no upstream-derived data is committed. |
| 7 | Should the hub connection support authentication/TLS, or stay plain `ws://`? | "ws:// + allow wss URL" | Connect over plain **`ws://`** by default; the config flow accepts a full host/port/path so a **`wss://` reverse proxy** works. **No** username/password/token or TLS-certificate handling is built into the integration (rtl_433 exposes no native auth); secured remote access is delegated to a user-provided proxy. |
| 8 | When a hub config entry is deleted, what happens to its per-device config entries (and their devices/entities)? | "Cascade-remove children" | **Cascade removal.** Each per-device entry records its parent hub's entry id; deleting a hub unloads and removes its child device entries along with their HA devices and entities (via `async_remove_entry`/unload), so no orphans remain. |
| 9 | How are entities created for mapped fields that first appear only after a device entry already exists (intermittent battery/rain/gust)? | "Create dynamically as seen" | **Dynamic creation.** Each device entry persists the set of field keys it has ever observed; when a message carries a newly mapped field, the matching entity is created on the fly via the platform's `async_add_entities` callback, and the expanded field set is persisted so the entity is recreated on subsequent restarts. |
| 10 | After an HA restart, before a device transmits again, what should its entities show? | "Restore, then apply timeout" | **Restore then time out.** Entities use `RestoreEntity` to show their last known value/state on startup; the availability watchdog still applies, so a device with no message within the effective timeout becomes unavailable. This avoids blank dashboards after a restart while preserving correct staleness handling. |
| 11 | How is CI verified, and what branching/PR model should the run use? | "I will set up a remote for you. For this initial work, work directly on main — no PRs." | A GitHub **remote will be provided by the user**, so GitHub Actions (hassfest/HACS/lint/conventional-commit/pytest matrix) run there and Success Criteria #7–#8 are verified on the remote. For this initial build the run works **directly on `main` with no PRs**; the `PRE_PHASE` feature-branch step is **skipped** and per-task commits land on `main`. |
| 12 | With one commit per task, how are parallel tasks in a phase kept from colliding in git? | "Parallel, file-disjoint" | Task generation must schedule concurrently-running tasks so they touch **non-overlapping files**; each task creates its own commit independently. This preserves phase parallelism while keeping clean, conflict-free per-task commits. |
| 13 | Which rtl_433 Docker image, and how is a steady WebSocket feed kept alive when `-r` exits after one pass? | "Pin hertzg/rtl_433" + "Can we use stdin / a named pipe to keep the rtl_433 process open with -r?" | Pin **`hertzg/rtl_433`** (verified arm64-capable) at a fixed tag/digest. rtl_433 can read from a **pipe/stdin with a format prefix** (e.g. `cu8:-` or `cu8:/path/fifo`) plus an explicit `-s <rate>`; a writer loop continuously feeds a captured sample into a **named pipe (FIFO)** so the single rtl_433 process — and its `-F http` WebSocket server — stays alive and streams continuously, avoiding the reconnect churn of relaunching per pass. |
| 14 | Long Docker pulls/boots/tests can exceed a single command's time limit and stall an unattended run — add execution guidance? | "Add 'Execution Notes' bullet" | Long-running operations (image pulls, container boot, pytest, browser installs) are launched in the **background and polled for readiness**, never as one blocking command, so no step exceeds the command time limit. Captured in the new Execution Notes subsection. |

## Executive Summary

This plan delivers a native Home Assistant custom integration that consumes the rtl_433 HTTP server's WebSocket event stream (`ws://<host>:<port>/ws`, default port 8433) and turns decoded RF sensor messages into Home Assistant devices and entities. Each rtl_433 server is represented by its own "hub" config entry, so multiple instances are supported side-by-side. As the hub observes JSON events, it identifies the originating physical device (by `model` plus its identifying keys such as `id`/`channel`) and, for previously unseen devices, surfaces them through Home Assistant's discovery mechanism — appearing at the top of the Integrations list exactly as the Battery Notes integration does. Accepting a discovery creates a per-device config entry that owns the device's sensor and binary_sensor entities; the hub can have new-device discovery switched off per instance.

The signal-to-entity mapping reuses the proven logic from rtl_433's own `examples/rtl_433_mqtt_hass.py`: a registry that maps each known field name (e.g. `temperature_C`, `humidity`, `battery_ok`, `wind_avg_km_h`, `rain_mm`) to a Home Assistant entity descriptor carrying `device_class`, `unit_of_measurement`, `state_class`, display name, value transform, and a unique-id suffix. Rather than a single Python file, these mappings are expressed as a **data-driven YAML device library** split into thematic files, making it trivial for contributors and AI agents to add or correct device support without touching integration logic. Because devices may simply stop transmitting, every entity participates in an availability model: a configurable global timeout (per hub) with per-device overrides marks entities unavailable when no message arrives within the window. Entities are created as their fields are first observed (so intermittent readings still surface) and restore their last state across restarts; deleting a hub cascades to remove its devices so nothing is orphaned.

The development and testing approach is mirrored from `deviantintegral/flame_connect_ha`: conventional-commit enforcement, ruff lint/format with pre-commit, `release-please` automation, Renovate, and GitHub Actions for hassfest + HACS validation and a pytest matrix. Quality is anchored by full unit-test coverage that drives recorded JSON fixtures through the mapping library, plus a containerized integration path where the rtl_433 Docker image replays sample data into a Home Assistant Docker container and Playwright CLI captures screenshots for tests and documentation.

## Context

### Current State vs Target State

| Current State | Target State | Why? |
|---------------|--------------|------|
| Repository contains only the AI task-manager scaffolding; no integration code exists | A complete, HACS-installable `custom_components/rtl_433` integration | The work order is to create the integration from scratch |
| rtl_433 users wire sensors into HA via MQTT discovery (`rtl_433_mqtt_hass.py`) and an external MQTT broker | Direct WebSocket connection from HA to the rtl_433 HTTP server, no broker required | Removes the MQTT broker dependency; native integration with first-class config/discovery UX |
| A single rtl_433 feed maps to a flat set of MQTT-discovered entities | Multiple rtl_433 servers, each a hub config entry, each owning discovered per-device entries | The work order requires multiple instances and per-instance discovery control |
| New sensors appear automatically and silently (MQTT autoconfig) | New devices appear as explicit "discovered" cards the user can add or ignore, per Battery Notes | The work order requires Battery-Notes-style discovery and a per-instance discovery off switch |
| Field→entity mapping lives in one large Python script | A data-driven YAML device library split into themed files plus a thin loader | The work order requires a contributor/AI-friendly device library, not one giant Python file |
| Devices that stop transmitting still appear "available" with stale state | Configurable availability timeout (global + per-device) marks entities unavailable | RF devices have no link state; silence is the only offline signal |
| No project tooling | CI/test/release/renovate tooling mirrored from `flame_connect_ha`, conventional commits | The work order specifies this exact development setup |

### Background

**rtl_433 WebSocket API.** The rtl_433 HTTP output mode (`-F http`) exposes a WebSocket endpoint at `ws://<host>:<port>/ws` (default `127.0.0.1:8433`) that emits one JSON object per decoded transmission. Empty frames act as keep-alives. Each event contains a `model` plus device-identifying keys (`id`, and/or `channel`, sometimes `subtype`) and a variable set of measurement fields. The integration connects to a user-supplied host/port and treats this stream as its sole data source. No separate Python client library is required — events are parsed as JSON and mapped directly, per the work order.

**Mapping reference (`rtl_433_mqtt_hass.py`).** This upstream example defines a `mappings` table of ~80+ recognized field keys grouped by domain — temperature (`temperature_C`, `temperature_1_C`…`temperature_4_C`, `temperature_F`), humidity/moisture, pressure, wind (`wind_avg_km_h`, `wind_max_m_s`, `wind_dir_deg`, gusts), rain (`rain_mm`, `rain_rate_mm_h`, …), power/electrical (`power_W`, `energy_kWh`, `current_A`, `voltage_V`, `battery_mV`), air quality (`pm2_5_ug_m3`, `co2_ppm`), light/UV (`lux`, `uv`, `uvi`), and binary states (`battery_ok`, `tamper`, `reed_open`, `contact_open`, `alarm`, `closed`, `detect_wet`). Each entry specifies the HA `device_type` (sensor vs binary_sensor), an `object_suffix` for the unique id, and a `config` block (`device_class`, `name`, `unit_of_measurement`, `value_template`, `state_class`). A `SKIP_KEYS` set (`type`, `model`, `subtype`, `channel`, `id`, `mic`, `mod`, `freq`, `sequence_num`, …) is excluded from entity creation. This table is the seed content for the YAML device library; the mapping *semantics* are reused while the MQTT *transport* is discarded.

**Battery Notes discovery model.** Battery Notes calls `discovery_flow.async_create_flow(hass, DOMAIN, context={"source": SOURCE_INTEGRATION_DISCOVERY}, data=…)`, which routes to `async_step_integration_discovery` in the config flow and renders a discovered card at the top of the Integrations list. Accepting creates a config entry; ignoring is honored by checking for an existing `SOURCE_IGNORE` entry with the same `unique_id` so dismissed items do not re-surface. This integration applies the same pattern, with the per-device `unique_id` derived from the hub plus the device key.

**Tooling reference (`deviantintegral/flame_connect_ha`).** The repository was inspected and provides the template for this project's structure and CI:
- `.github/workflows/`: `codeql.yml`, `conventional-commits.yml`, `copilot-setup-steps.yml`, `lint.yml`, `release.yml`, `test.yml`, `validate.yml`.
- Root config: `pyproject.toml`, `renovate.json`, `.pre-commit-config.yaml`, `requirements.txt`, `requirements_dev.txt`, `requirements_test.txt`, `release-please-config.json`, `.release-please-manifest.json`, `hacs.json`, `.editorconfig`, `.gitattributes`, `.gitignore`, `.markdownlint.json`, `.prettierignore`, `.prettierrc.yml`.
- Structured `custom_components/<domain>/`: `__init__.py`, `manifest.json`, `const.py`, `data.py`, `diagnostics.py`, `repairs.py`, a `config_flow_handler/` package (with `schemas/` and `validators/` subpackages), a `coordinator/` package (`base.py`), an `entity/` package (`base.py`), and one package per platform (`sensor/`, `binary_sensor/`, etc.), plus `translations/en.json`.
- `tests/`: `conftest.py` and one test module per platform/concern.

**rtl_433 field source.** There is no single machine-readable file in rtl_433 enumerating output fields and units; definitions are embedded across 200+ C decoders and HA-specific semantics are absent. This is why the device library is seeded from the curated `rtl_433_mqtt_hass.py` table and maintained as data, rather than generated from source.

**Sample/test data source (`merbanan/rtl_433_tests`).** The repository currently contains no sample data. Realistic RF captures are sourced from the upstream `merbanan/rtl_433_tests` regression suite (`.cu8` files under `tests/GROUP/DEVICE/SET`, exercised with `rtl_433 -r <file>`), referenced as a **pinned git submodule** with a shallow/sparse checkout of only the needed device directories — no captures are copied into the repo, which avoids vendoring the large corpus and sidesteps its lack of an explicit license (see Clarification #6). The containerized screenshot path replays these captures through the pinned **`hertzg/rtl_433`** Docker image (arm64-capable); because `rtl_433 -r` consumes a file once and exits, a writer loop feeds the capture into a **named pipe (FIFO)** that rtl_433 reads with a format prefix and explicit sample rate (e.g. `cu8:/path/fifo -s 250k -F http`), keeping the process and its WebSocket server alive for a continuous stream (see Clarification #13). Fast, offline unit tests of the mapping library instead use a small set of **project-authored JSON event fixtures** modeled on the documented field vocabulary, so no upstream-derived data is committed.

## Architectural Approach

The integration is a hub-and-device custom component for Home Assistant. One config entry per rtl_433 server (the **hub**) maintains a resilient WebSocket connection and a runtime view of observed devices. Incoming JSON events are normalized into a device key and a set of field readings; the mapping library converts recognized fields into entity descriptors. Known devices have their readings dispatched to live entities; unknown devices either trigger a discovery flow (Battery-Notes style) or, when discovery is disabled for that hub, are recorded but not surfaced. Accepting a discovery creates a per-device config entry whose `sensor`/`binary_sensor` platforms instantiate entities from the device's current and historically seen fields. Availability is enforced by per-device "last seen" timestamps measured against a configurable timeout.

```mermaid
flowchart TD
    subgraph rtl["rtl_433 server(s)"]
        WS["WebSocket /ws<br/>JSON events"]
    end

    subgraph HA["Home Assistant: custom_components/rtl_433"]
        HUB["Hub config entry<br/>(per instance)"]
        COORD["WebSocket coordinator<br/>connect / reconnect / parse"]
        NORM["Event normalizer<br/>device key + field readings"]
        LIB["Device mapping library<br/>(themed YAML + loader)"]
        DISP["Dispatcher<br/>per-device signals"]
        AVAIL["Availability watchdog<br/>last-seen vs timeout"]

        subgraph DEV["Per-device config entries"]
            SENS["sensor entities"]
            BIN["binary_sensor entities"]
        end

        DISC["Discovery flow<br/>SOURCE_INTEGRATION_DISCOVERY"]
        IGN["SOURCE_IGNORE entries"]
    end

    WS --> COORD --> NORM
    NORM --> LIB
    NORM -->|known device| DISP --> SENS & BIN
    NORM -->|unknown + discovery on| DISC
    DISC -->|user adds| DEV
    DISC -->|user ignores| IGN
    NORM -->|discovery off| HUB
    LIB --> SENS & BIN
    AVAIL --> SENS & BIN
    HUB --- COORD
```

### Hub Config Entry and WebSocket Coordinator
**Objective**: Establish and maintain the connection to one rtl_433 server and act as the single ingestion point for its events.

The hub config flow collects the server host, port (default 8433), and an optional path, defaulting to the plain `ws://` scheme while also accepting a `wss://` endpoint so a user-fronted TLS reverse proxy works (see Clarification #7); it validates reachability of the WebSocket endpoint before creating the entry. The integration performs no authentication or TLS-credential handling of its own — rtl_433 exposes no native auth, and secured remote access is delegated to a proxy. A coordinator owns the WebSocket lifecycle: connect, parse JSON frames, ignore keep-alives, and reconnect with backoff on drop. The hub holds runtime state (observed device keys, last-seen timestamps, the per-hub "new device discovery" flag, and the resolved availability defaults). Multiple hubs coexist because all per-hub state is scoped to the config entry. The coordinator does not create entities directly; it normalizes events and fans them out via Home Assistant's dispatcher keyed by device, so device entries subscribe only to their own device's updates. An options flow exposes the per-instance discovery toggle and the hub-level default availability timeout.

### Event Normalization and the Device Mapping Library
**Objective**: Convert heterogeneous rtl_433 JSON into a stable device identity and a set of Home Assistant entity descriptors, in a way that is easy for contributors and AI agents to extend.

Normalization derives a deterministic device key from identity fields (`model` plus the present subset of `id`/`channel`/`subtype`) and separates measurement fields from skip-fields. The mapping library is a data-driven registry loaded from multiple **themed YAML files** (for example: temperature, humidity, pressure, wind, rain, power/electrical, air-quality, light/UV, and binary states), plus an optional misc file. Each YAML entry is keyed by the rtl_433 field name and carries the attributes needed to build an entity: target platform (sensor or binary_sensor), `device_class`, `unit_of_measurement`, `state_class`, display name, an optional value transform/rounding, an optional payload mapping for binary states (e.g. which value means "on"), and the unique-id suffix. A thin loader parses and validates these files at startup into in-memory descriptors and exposes lookup by field name. The seed content is a faithful port of the `rtl_433_mqtt_hass.py` `mappings` table and its `SKIP_KEYS`. To honor the "users can add their own mappings" intent without over-building, the loader also reads an optional user-supplied override YAML from the Home Assistant config directory, layered on top of the shipped library; a graphical mapping editor is intentionally deferred. Splitting the library by theme and expressing it as data (not code) means adding a device/field is a small, reviewable YAML change with no integration-logic risk.

### Device Discovery and Per-Device Config Entries
**Objective**: Surface newly seen devices through Home Assistant's discovery UX, on a per-instance opt-out basis, and model each accepted device as its own configurable entry.

When the hub normalizes an event for a device that has neither an existing device config entry nor a `SOURCE_IGNORE` entry, and the hub's discovery toggle is on, it calls `discovery_flow.async_create_flow()` with `SOURCE_INTEGRATION_DISCOVERY`, mirroring Battery Notes so the device appears at the top of the Integrations list. The discovery data carries the parent hub reference and the device key/metadata. The config flow's `async_step_integration_discovery` presents the device for confirmation; accepting creates a per-device config entry, while dismissing yields a `SOURCE_IGNORE` entry checked on subsequent sightings to prevent re-prompting. With the toggle off, unknown devices are tracked in hub runtime state (for diagnostics and for retroactive discovery if re-enabled) but no flow is created. Each device config entry registers a Home Assistant device and sets up the `sensor` and `binary_sensor` platforms, which build entities from the device's mapped fields and update on dispatcher signals from the hub. Each device entry stores its parent hub's entry id, and deleting a hub cascades: the integration unloads and removes the hub's child device entries along with their devices and entities so none are orphaned (see Clarification #8).

### Availability Model
**Objective**: Reflect the real online/offline status of RF devices that signal presence only by transmitting.

Every device tracks a last-seen timestamp updated on each event. A watchdog compares last-seen against an effective timeout and flips the device's entities to unavailable when exceeded, restoring them on the next event. The effective timeout resolves per device: a per-device override (from the device entry's options) takes precedence over the hub-level default, which itself defaults to a sensible configurable value. Because timeout needs vary widely (some sensors report every 30–60 s, others every several minutes), the value is configurable at both levels rather than hard-coded; the plan does not fix a single magic constant beyond a documented default. Across HA restarts, entities restore their last known state via `RestoreEntity`, and the watchdog applies the timeout from startup — a device shows its last value until either a fresh message arrives or the timeout elapses (see Clarification #10).

### Entities and Quality Surfaces
**Objective**: Provide correct, well-classified entities plus the standard integration quality surfaces.

A shared base entity centralizes device-info, availability wiring, dispatcher subscription, and `RestoreEntity`-based state restoration. Each device entry persists the set of field keys it has observed; platforms create entities for those fields at setup and add new ones dynamically via `async_add_entities` when a previously unseen mapped field first appears, so intermittent fields (battery, rain, gusts) gain entities without re-setup and survive restarts (see Clarifications #9, #10). The `sensor` platform covers measurement fields with proper `device_class`/`state_class`/units from the library; the `binary_sensor` platform covers boolean states (battery, tamper, contact/reed, alarm, leak). Diagnostics export (with redaction) exposes hub connection state, observed devices, last-seen times, and unmatched field keys to aid contributors in extending the library. `repairs`/`translations` follow the reference project's conventions; scope for repairs is limited to genuinely actionable issues (e.g. unreachable server) rather than speculative cases.

### Development, Testing, and Documentation Tooling
**Objective**: Match the prescribed `flame_connect_ha` engineering setup and the specified testing strategy.

Project tooling mirrors `flame_connect_ha`: conventional commits (enforced in CI), ruff + pre-commit, Renovate, `release-please` automation, `hacs.json`, split `requirements*.txt`, and GitHub Actions for CodeQL, lint, hassfest + HACS validation, and a pytest matrix — verified on the user-provided remote (see Clarification #11). Unit tests drive a small set of **project-authored JSON event fixtures** (one per representative model, modeled on the documented rtl_433 field vocabulary; see Clarification #6) through normalization and the mapping library, asserting correct device identity, entity set, classes, units, value transforms, binary payload handling, skip-field exclusion, dynamic late-field creation, restart restore, cascade removal, and availability transitions — targeting full coverage of the mapping and lifecycle paths. A containerized integration path replays real `.cu8` captures from the pinned `merbanan/rtl_433_tests` submodule through the **`hertzg/rtl_433`** Docker image into HA: a writer loop feeds the capture into a **named pipe** that rtl_433 reads (`cu8:/path/fifo -s <rate> -F http`) so its WebSocket server streams continuously, and Playwright CLI captures screenshots (discovery card, device page, options flow, unavailable state) for both integration assertions and documentation.

## Risk Considerations and Mitigation Strategies

<details>
<summary>Technical Risks</summary>
- **WebSocket endpoint/behavior variance across rtl_433 versions**: path, port, or frame conventions may differ.
    - **Mitigation**: Make host/port/path configurable with the documented `8433` / `/ws` defaults; validate during config flow; tolerate keep-alive/empty frames and malformed JSON without crashing the coordinator.
- **Unmapped or novel fields produce no entity (silent gaps)**: rtl_433 emits many model-specific keys not in the seed library.
    - **Mitigation**: Record unmatched field keys in diagnostics so contributors can see exactly what to add; the data-driven YAML library makes additions low-risk.
- **Device-key ambiguity / collisions**: some models lack stable `id`, or reuse `channel`; multiple instances can see the same `model`+`id`.
    - **Mitigation**: Derive the key from the present identity fields and scope unique_ids by hub/config entry; document the keying rules; cover edge cases (missing id, channel-only) with fixtures.
</details>

<details>
<summary>Implementation Risks</summary>
- **Discovery loops or duplicate entries**: repeated sightings could spam discovery flows.
    - **Mitigation**: Follow Battery Notes precisely — check for existing device entries and `SOURCE_IGNORE` entries before creating a flow; dedupe by unique_id; gate on the per-hub toggle.
- **Availability flapping**: bursty or delayed transmissions could toggle availability noisily.
    - **Mitigation**: Single watchdog comparison against a configurable timeout with sane defaults and per-device override; restore on any event; document tuning.
- **Scope creep into an in-UI mapping editor**: the user floated UI-editable mappings.
    - **Mitigation**: Deliver data-driven YAML plus a drop-in user override now; explicitly defer a graphical editor (YAGNI) while keeping the architecture compatible with it.
- **Config-entry/entity lifecycle bugs**: live-adding entities for late-appearing fields, persisting the seen-field set, restoring state on restart, and cascade-removing child device entries on hub deletion are all stateful and easy to get subtly wrong (duplicate entities, orphaned entries, a lost field set, or unavailable flicker).
    - **Mitigation**: Persist the per-device seen-field set in the device config entry; dedupe by entity unique_id before adding; route hub removal through an unload path that tears down children; and cover hub-removal cascade, late-field add, and restart-restore with explicit fixtures and tests (see Clarifications #8–#10).
</details>

<details>
<summary>Integration & Quality Risks</summary>
- **HACS/hassfest validation drift**: manifest, brands, and quality-scale requirements evolve.
    - **Mitigation**: Run hassfest + HACS validation in CI from the start (mirroring `flame_connect_ha`); keep `manifest.json`/`hacs.json` conformant.
- **Flaky containerized integration/screenshot tests**: timing between rtl_433 replay, HA startup, and Playwright capture.
    - **Mitigation**: Wait on explicit readiness signals (HA API up, entity present) before capture; keep deterministic sample data; separate fast unit tests from the slower container job.
- **Test-data licensing/provenance (`rtl_433_tests`)**: the upstream suite declares no explicit license, and vendoring it wholesale is both heavy and legally unclear.
    - **Mitigation**: Reference the suite as a **pinned git submodule** (shallow/sparse checkout of only the needed device directories) so no upstream files are copied into the repo, and commit only project-authored JSON fixtures for unit tests. This sidesteps the licensing question and keeps the repo light (see Clarifications #6, #13).
</details>

## Success Criteria

### Primary Success Criteria
1. Two or more rtl_433 servers can be added as separate hub config entries simultaneously, each connecting to its own `ws://host:port/ws` and ingesting events independently without unique_id collisions.
2. A newly observed device appears as a discovered card at the top of Settings → Devices & Services (Battery-Notes behavior); accepting creates a per-device config entry with correctly classified `sensor`/`binary_sensor` entities, and ignoring prevents re-prompting via a `SOURCE_IGNORE` entry.
3. Turning off "new device discovery" on a hub stops new discovery cards from appearing for that instance while existing devices continue updating.
4. Recognized fields from the seed library (temperature, humidity, pressure, wind, rain, power, air quality, light/UV, and binary states) produce entities with the correct `device_class`, `unit_of_measurement`, `state_class`, and value handling, matching the `rtl_433_mqtt_hass.py` semantics; `SKIP_KEYS` produce no entities.
5. After no events for longer than the effective timeout, a device's entities become unavailable and recover on the next event; the per-device override takes precedence over the hub default.
6. Adding support for a new device/field requires only a YAML change in the themed device library (no integration-logic edits), and a user-supplied override YAML is honored.
7. Unit tests cover the full mapping path from recorded JSON fixtures to entity descriptors and availability transitions; CI passes hassfest + HACS validation, lint, conventional-commit, and the pytest matrix.
8. The containerized path (rtl_433 Docker replaying sample data → HA Docker → Playwright CLI) produces screenshots of the discovery card, a device page with entities, the options flow, and an unavailable-state example.
9. Deleting a hub config entry cascades: its per-device config entries, devices, and entities are all removed, leaving no orphaned items.
10. A mapped field that first appears after a device entry already exists results in a new entity being created automatically, and that entity persists across HA restarts.
11. After an HA restart, a device's entities restore their last known state and become unavailable only if no message arrives within the effective timeout.

## Self Validation

After all tasks are complete, an agent should verify the implementation against the real system, not merely re-run unit tests:

- Bring up the Home Assistant Docker container with the integration installed and a **`hertzg/rtl_433`** container replaying captures from the pinned `merbanan/rtl_433_tests` submodule — a writer loop feeds a `.cu8` into a named pipe that rtl_433 reads (`cu8:/path/fifo -s <rate> -F http`) for a continuous WebSocket stream. Use Playwright CLI to open the HA UI, add a hub config entry (plain `ws://`) pointing at the rtl_433 container, and screenshot the resulting connection state.
- After replay begins, use Playwright CLI to confirm a discovered-device card appears at the top of Settings → Devices & Services; screenshot it. Accept it and screenshot the created device page, verifying expected entities (e.g. a temperature sensor with `°C` and `measurement` state class, a battery binary_sensor) are present with correct classes/units.
- Trigger the ignore path on a second discovered device and confirm via the config entries list (UI or `curl` to the HA config-entries API) that a `SOURCE_IGNORE` entry exists and the card does not reappear after further replay.
- Add a second hub entry for a second rtl_433 source replaying an overlapping `model`+`id`; confirm via the entity registry (HA API/`curl`) that both instances' entities exist with distinct, instance-scoped unique_ids.
- Toggle "new device discovery" off on a hub via its options flow, replay an unseen device, and confirm no new discovery card appears; screenshot the options flow.
- Stop replay and wait past a deliberately short configured availability timeout; use Playwright CLI / the HA states API to confirm the device's entities report `unavailable`, then resume replay and confirm they recover. Screenshot the unavailable state.
- Add a temporary entry to a user-override mapping YAML for a previously unmapped field present in the sample data; restart and confirm via the HA API that the corresponding entity is now created, demonstrating the data-driven library and override mechanism.
- After accepting a device, delete its parent hub config entry and confirm via the HA config-entries and device/entity registries (UI or `curl`) that the device's per-device entry, device, and entities are all gone — no orphans remain.
- Use a capture whose later frames include an intermittent mapped field (e.g. `battery_ok` or a rain field) absent from the first frames; confirm via the HA states API that a new entity for that field appears only once the field is first observed, and that it is still present after restarting the HA container.
- Restart the HA container mid-replay and confirm the device's entities restore their last known state on startup; then stop replay past the configured timeout and confirm they become unavailable, recovering when replay resumes.

## Documentation

- **README.md**: purpose, HACS installation, configuring a hub (host/port), discovery behavior and the per-instance discovery toggle, availability timeout configuration (global + per-device), and a gallery of the Playwright-generated screenshots.
- **Device library contributor guide**: the YAML mapping schema (one entry per field key: platform, device_class, unit, state_class, name, value transform, binary payload, unique-id suffix), where the themed files live, how to add/correct a mapping, how to read diagnostics' unmatched-keys to find gaps, and how the user-override file works.
- **AGENTS.md / AI-facing docs**: machine-oriented description of the device-library format and the add-a-mapping workflow so AI agents can extend support safely, plus how to run unit tests and the containerized screenshot path.
- **CONTRIBUTING / commit conventions**: conventional commits and the `release-please` flow, mirroring the reference project.

## Resource Requirements

### Development Skills
- Home Assistant custom-integration development: config flows (including `async_step_integration_discovery`), options flows, coordinators, dispatcher, entity platforms, device/entity registry, availability, diagnostics.
- Async Python and WebSocket client handling (reconnect/backoff, resilient JSON parsing).
- Familiarity with rtl_433's JSON event vocabulary and the `rtl_433_mqtt_hass.py` mapping semantics.
- Test engineering with `pytest-homeassistant-custom-component` and fixture design.
- CI/release tooling: GitHub Actions, ruff, pre-commit, Renovate, release-please, hassfest/HACS validation.
- Container orchestration (Docker for rtl_433 + HA) and Playwright CLI automation.

### Technical Infrastructure
- Home Assistant Docker image (arm64) and the pinned **`hertzg/rtl_433`** Docker image (arm64), driven via a named-pipe FIFO feed so the `-F http` WebSocket server streams continuously.
- The `merbanan/rtl_433_tests` suite as a **pinned git submodule** (shallow/sparse checkout of representative device directories) for real captures, plus a small set of project-authored JSON fixtures for unit tests.
- Playwright CLI for screenshot capture.
- GitHub Actions runners for CodeQL, lint, validate (hassfest + HACS), test matrix, conventional-commits, and release.
- No third-party Python runtime dependency for parsing/mapping (stdlib JSON + a YAML parser already available in the HA environment), per the work order.

## Integration Strategy

The component installs under `custom_components/rtl_433/` and is distributed via HACS as a custom repository. It integrates with Home Assistant exclusively through documented extension points (config/options flows, discovery flow, coordinator + dispatcher, entity platforms, device/entity registry, diagnostics) and depends only on a reachable rtl_433 HTTP server's WebSocket endpoint — no MQTT broker and no bespoke Python client library. It is deliberately independent of the existing rtl_433-over-MQTT path; the two can run concurrently, and migration is treated as out of scope per the greenfield decision.

## Notes

- **Greenfield decision is intentional and reasoned** (see Clarification #4): instance-scoped unique_ids are required for multi-instance safety, and HA cannot transfer history across platforms by string-matching unique_ids, so MQTT-name compatibility is not pursued.
- **Source-generated mappings rejected** (see Clarification #2): rtl_433 has no centralized machine-readable field registry and no HA semantics in source; the YAML library seeded from `rtl_433_mqtt_hass.py` is the maintainable choice.
- **In-UI mapping editor deferred**: the data-driven YAML library plus a drop-in user-override file satisfies the near-term "users can add mappings" intent; a graphical editor is out of scope unless requested later.
- **No backwards compatibility required**: confirmed greenfield; there is no prior version of this integration to preserve.
- **Setup add-on deferred** (see Clarification #5): the deliverable is the HACS custom integration only. A Home Assistant Supervisor add-on that bundles rtl_433 to ease setup may follow later and is explicitly out of scope here.
- **Test data sourced from `merbanan/rtl_433_tests`** (see Clarification #6): referenced as a pinned git submodule (shallow/sparse, not vendored) for the real `.cu8` captures replayed via the `hertzg/rtl_433` Docker image; unit tests use project-authored JSON fixtures instead, so no upstream-derived data is committed and the suite's lack of an explicit license is sidestepped.
- **Connection security** (see Clarification #7): plain `ws://` by default with `wss://` accepted for proxied TLS; no in-integration auth.
- **Commit cadence: one commit per task (overrides per-phase default).** During blueprint execution for this plan, create a separate descriptive conventional commit for each completed task rather than a single commit per phase. This supersedes the default per-phase commit behavior described in `config/hooks/POST_PHASE.md` for this plan only. Each task's changes must be committed individually as the task completes — including when the blueprint schedules multiple tasks in parallel within a phase, commit each task's work as its own commit rather than batching the phase into one commit. To keep parallel per-task commits conflict-free, task generation must schedule concurrently-running tasks so they touch non-overlapping files (see Clarification #12).
- This plan is a PRD only; task and phase decomposition happens in the task-generation step.

### Execution Notes

Operational directives for executing this plan end-to-end unattended, consumed by task generation and the blueprint executor:

- **Branch / PR model**: Work **directly on `main` with no PRs** for this initial build. **Skip** the `PRE_PHASE` feature-branch creation step (`create-feature-branch.cjs`); per-task commits land on `main`. CI is verified on the user-provided remote (see Clarifications #11, #14).
- **Per-task commits, file-disjoint parallelism**: One conventional commit per task; tasks scheduled in the same phase must touch **non-overlapping files** so parallel commits never conflict (see Clarification #12 and the commit-cadence note above).
- **Long-running operations**: Launch Docker image pulls, container boot, the pytest suite, and Playwright browser installs in the **background and poll for readiness** — never as a single blocking command — so no step exceeds the command time limit (see Clarification #14).
- **rtl_433 replay**: Use the pinned **`hertzg/rtl_433`** (arm64) image and keep it alive with a **named-pipe FIFO** writer loop feeding `cu8:<fifo> -s <rate> -F http`, giving a continuous WebSocket stream (see Clarification #13).
- **Test data**: `merbanan/rtl_433_tests` is a **pinned git submodule** (shallow/sparse, only needed device dirs); no captures are copied into the repo, and unit-test fixtures are project-authored JSON (see Clarification #6).
- **Environment is verified capable**: Docker 29.x + compose, Python 3.13, Node 24, git/gh, passwordless sudo, and egress to GitHub/ghcr/Docker Hub/PyPI/Playwright CDN are all present; HA and `hertzg/rtl_433` both publish arm64 images.

## Task Dependency Diagram

```mermaid
graph TD
    T01["T01: Repo tooling & config"]
    T02["T02: GitHub Actions CI"]
    T03["T03: Package skeleton"]
    T04["T04: Device mapping YAML library"]
    T05["T05: Mapping loader"]
    T06["T06: Normalizer & WS coordinator"]
    T07["T07: Config/options/discovery flow"]
    T08["T08: sensor & binary_sensor"]
    T09["T09: Lifecycle wiring + diagnostics + repairs"]
    T10["T10: Unit tests + fixtures"]
    T11["T11: Container & screenshot harness"]
    T12["T12: Documentation"]

    T03 --> T05
    T04 --> T05
    T03 --> T06
    T03 --> T07
    T06 --> T07
    T05 --> T08
    T06 --> T08
    T07 --> T08
    T06 --> T09
    T07 --> T09
    T08 --> T09
    T05 --> T10
    T06 --> T10
    T07 --> T10
    T08 --> T10
    T09 --> T10
    T09 --> T11
    T09 --> T12
    T11 --> T12
```

No circular dependencies. T01 and T02 are independent tooling tracks with no downstream code dependencies (verified on the remote CI).

## Execution Blueprint

**Validation Gates:**
- Reference: `config/hooks/POST_PHASE.md`

> Commit cadence for this plan: **one conventional commit per task** (overrides the per-phase default; see Notes / Clarification #12). Parallel tasks within a phase are file-disjoint so per-task commits never conflict. The `PRE_PHASE` feature-branch step is **skipped** — work lands directly on `main` (Clarification #11).

### ✅ Phase 1: Foundations
**Parallel Tasks:**
- ✔️ Task 01: Repository tooling & root configuration (files: root config) — completed
- ✔️ Task 02: GitHub Actions CI workflows (files: `.github/workflows/`) — completed
- ✔️ Task 03: Integration package skeleton (files: `custom_components/rtl_433/{manifest.json,const.py,__init__.py,translations/}`) — completed
- ✔️ Task 04: Device mapping YAML library + contributor guide (files: `custom_components/rtl_433/device_library/`, `docs/device-library.md`) — completed

### ✅ Phase 2: Mapping loader & ingestion
**Parallel Tasks:**
- ✔️ Task 05: Mapping library loader (depends on: 03, 04) (files: `mapping.py`) — completed
- ✔️ Task 06: Event normalizer & WebSocket coordinator (depends on: 03) (files: `normalizer.py`, `coordinator/`) — completed

### ✅ Phase 3: Config & discovery flow
**Parallel Tasks:**
- ✔️ Task 07: Config, options & discovery flow (depends on: 03, 06) (files: `config_flow.py`, `translations/en.json`) — completed

### ✅ Phase 4: Entities
**Parallel Tasks:**
- ✔️ Task 08: sensor & binary_sensor platforms + base entity (depends on: 05, 06, 07) (files: `entity.py`, `sensor.py`, `binary_sensor.py`) — completed

### ✅ Phase 5: Lifecycle wiring
**Parallel Tasks:**
- ✔️ Task 09: Integration wiring, diagnostics, repairs (depends on: 06, 07, 08) (files: `__init__.py`, `diagnostics.py`, `repairs.py`) — completed

### ✅ Phase 6: Testing
**Parallel Tasks:** (run sequentially by the coordinator — see Noteworthy Events: Task 10 first to validate/harden the integration, then Task 11 against validated code)
- ✔️ Task 10: Unit tests + JSON fixtures (depends on: 05, 06, 07, 08, 09) (files: `tests/` unit + `tests/fixtures/`) — completed (33 tests, ~89% coverage)
- ✔️ Task 11: Containerized integration & screenshot harness (depends on: 09) (files: `tests/integration/`, `.gitmodules`, `docs/images/`) — completed (full end-to-end run; 4 screenshots captured)

### Phase 7: Documentation
**Parallel Tasks:**
- Task 12: README, AGENTS.md, CONTRIBUTING (depends on: 09, 11) (files: `README.md`, `AGENTS.md`, `CONTRIBUTING.md`)

### Post-phase Actions
After each phase: ensure ruff lint passes on changed files, verify each task produced its own conventional commit on `main`, then mark the phase ✅ and its tasks ✔️/`completed` in this document.

### Execution Summary
- Total Phases: 7
- Total Tasks: 12

### Change Log
- 2026-05-25: Generated 12 tasks and the execution blueprint (7 phases) from the PRD.
- 2026-05-25: Added commit cadence directive (one commit per task, overriding the per-phase default) for this plan.
- 2026-05-25: Refinement session — resolved deliverable type (HACS integration only; bundling add-on deferred), test-data source (`merbanan/rtl_433_tests` `.cu8` subset → JSON fixtures + Docker replay, with licensing caveat), and connection security (`ws://` default, `wss://` accepted, no in-integration auth). Updated Clarifications (#5–#7), Background, hub config-flow, testing/tooling, risks, Self Validation, and Resource Requirements accordingly.
- 2026-05-25: Refinement session — resolved config-entry/entity lifecycle: hub deletion cascade-removes child device entries (#8), entities are created dynamically as mapped fields are first seen and persisted across restarts (#9), and entities restore last state on restart then apply the availability timeout (#10). Updated Clarifications (#8–#10), Executive Summary, the discovery/per-device, availability, and entities sections, Implementation Risks, Success Criteria (#9–#11), and Self Validation accordingly.
- 2026-05-25: Feasibility refinement (autonomous-run readiness) — test data switched to a pinned git submodule with project-authored unit fixtures (#6); work directly on `main`, no PRs, skip the feature-branch hook, CI verified on a user-provided remote (#11); parallel tasks must be file-disjoint for clean per-task commits (#12); pinned `hertzg/rtl_433` arm64 image kept alive via a named-pipe FIFO for a continuous stream (#13); long operations run in the background and polled (#14). Added an Execution Notes subsection and updated Clarifications (#11–#14), Background, testing/tooling, Implementation Risks, Self Validation, and Resource Requirements.
