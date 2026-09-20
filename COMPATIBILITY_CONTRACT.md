# rtl_433 HACS ↔ Core Compatibility Contract (ABI)

**Status: FROZEN.** This document is a byte-level Application Binary Interface (ABI)
between the full HACS build (`custom_components/rtl_433/`) and the future minimal
Home Assistant Core build. Both builds ship under the **same domain** (`rtl_433`,
`const.py:16`) and therefore read and write the **same** config entries, entity
registry, and device registry. The three identity surfaces below MUST be produced
**byte-identical** by both builds. Changing any of them is a breaking change that
requires a coordinated migration shipped in **both** builds simultaneously.

The minimal Core `rtl_433` integration MUST:
- construct every `unique_id` and device `identifiers` tuple using the **exact**
  templates in §2 and §3;
- carry the same config-entry **major** `VERSION` (§1) and a migration path that is
  a **superset-tolerant, non-downgrading subset** of §1 — i.e. it MUST tolerate
  options and `minor_version` values written by the full build (which will normally
  be *ahead* of core's), and MUST NOT downgrade an entry;
- lead on the major: the full build only writes major `N+1` once a released core
  build already reads it (see
  [Majors are a one-way door](#majors-are-a-one-way-door-the-precondition-for-version--3));
- never mutate these formats without a coordinated migration in both builds.

All facts below were transcribed from the current source; line citations are to
the state of the tree at authoring time.

---

## 1. Config-entry `version` / `minor_version` scheme + migration ladder

### Declared version (`config_flow.py:139-140`)

```python
VERSION = 2
MINOR_VERSION = 8
```

New entries are created at `version=2, minor_version=8`.

### Invariants
- Migrations are **monotonic and non-destructive**: each step is guarded and only
  moves forward.
- The minimal Core build **MUST tolerate** options and `minor_version` values
  written by the full build (superset tolerance).
- **No migration may downgrade.** A future schema (`version > 2`) is explicitly
  rejected, returning `False` (unsupported) rather than mutating the entry.
- **The major is a one-way door; the minor is not.** `MINOR_VERSION` may advance in
  this build ahead of core (core tolerates any v2 minor). `VERSION` may not: it only
  advances once a released core build already reads the new major, because a
  higher-major entry is refused *above* the integration and strands the user if the
  custom component is removed. See below.

### `async_migrate_entry` ladder (`migration.py:417-594`)

Entry point rejects future schemas first:

- **`migration.py:454-456`** — `if entry.version > 2: return False` (downgrade from
  a future schema is unsupported). This is defence in depth rather than the live
  guard: Home Assistant already rejects a stored major above the *loaded* handler's
  `VERSION` before it resolves `async_migrate_entry` at all, so while this build
  declares `VERSION = 2` the branch is only reachable by calling the function
  directly (as the tests do). It is the *loaded* build's `VERSION` that decides
  whether an entry can be read — see
  [Majors are a one-way door](#majors-are-a-one-way-door-the-precondition-for-version--3).

**Version 1 → 2** (`migration.py:458-472`): the 0.1.0 per-device-entry model → the
hub model.
- `migration.py:459-469` — a legacy **device** entry (`CONF_ENTRY_TYPE ==
  ENTRY_TYPE_DEVICE`) processed on its own re-homes its registry objects to the
  parent hub (`CONF_HUB_ENTRY_ID`), then sets `version=2, minor_version=2` and
  returns `True`. The hub later folds and removes it.
- `migration.py:471-472` — a **hub** entry consolidates all child device entries
  into `entry.data[CONF_DEVICES]` (via `_migrate_hub_entry`), re-homing their
  registry objects before removal. Then it falls through the minor ladder below.

**Minor-version ladder** — each step guarded by `if (entry.minor_version or 1) < N`:

- **→ minor 2** (`migration.py:474-486`): seed this hub's
  `entry.data[CONF_USER_MAPPINGS]` from any pre-existing legacy
  `<config>/rtl_433_mappings.yaml` (read once in the executor; never modified or
  deleted). Sets `version=2, minor_version=2`.
- **→ minor 3** (`migration.py:488-493`): disable any already-created per-device
  "Last seen" sensors (unique-id tail `:last_seen`), which now ship
  disabled-by-default. Sets `minor_version=3`.
- **→ minor 4** (`migration.py:495-516`): drop a hub `CONF_AVAILABILITY_TIMEOUT`
  option still pinned to the legacy global default
  (`LEGACY_DEFAULT_AVAILABILITY_TIMEOUT == 600`, `const.py:174`) so the new
  device-class-aware defaults apply; a user-set non-default value is preserved.
  Sets `minor_version=4`.
- **→ minor 5** (`migration.py:518-525`): rewrite already-persisted doorbell
  `event_types` from the raw `"0"`/`"1"` strings to the standardized
  `"ring"`/`"secret_knock"` types. Removes no entity; the doorbell unique_id /
  `object_suffix` is unchanged. Sets `minor_version=5`.
- **→ minor 6** (`migration.py:527-535`): re-enable the "Last seen" sensor for
  event-driven devices (which now never expire) — only instances the integration
  disabled at minor 3, not ones the user disabled. Sets `minor_version=6`.
- **→ minor 7** (`migration.py:537-561`): repeat the minor-4 cleanup — strip a hub
  `CONF_AVAILABILITY_TIMEOUT` still equal to the legacy default (600) that the
  options flow used to re-persist on save. The options flow no longer writes the
  sentinel, so this heal is final. Sets `minor_version=7`.
- **→ minor 8** (`migration.py:563-591`): strip the retired `discovery_enabled` key
  from `entry.data` and `entry.options` (discovery stopped being a toggle; a heard
  device now waits in the pending list until the user adopts it). Deliberately
  narrow — `entry.data[CONF_DEVICES]`, every per-device override and every
  calibration survive byte-for-byte, and `data`/`options` are only rewritten when
  the key was actually present. Sets `minor_version=8`.
- **`migration.py:594`** — `return True`.

> Note: the ladder is written to be idempotent and order-tolerant — a legacy device
> entry migrated before its hub only re-homes its own registry objects and bumps to
> minor 2; the hub folds and removes it later. Either ordering converges.

### Majors are a one-way door: the precondition for `VERSION = 3`

> **Rule.** The full HACS build MUST NOT write `version = N+1` until a **released**
> Core build already *reads* major `N+1`. Today that means **`VERSION` stays 2.**
> Minors are free and may run ahead of core; majors may not.

Because both builds share the domain, Home Assistant loads
`custom_components/rtl_433/` in preference to the core integration. The supported
direction of travel is therefore **core → full build**: installing the HACS build
over a core-managed entry is fine (it migrates the entry forward), and *removing*
the HACS build hands the same entry back to core. That handback is the only place
the version ladder can strand a user, and it is not symmetric.

`ConfigEntry.async_migrate_handler` (HA's `config_entries.py`) compares the
**stored** major against the **loaded** handler's `VERSION` *before* it looks for
the integration's migration hook:

```python
same_major_version = self.version == handler.VERSION
if same_major_version and self.minor_version == handler.MINOR_VERSION:
    return True

if self.version > handler.VERSION:
    self.logger.error(
        "Config entry %s for %s has version %s which is higher than the"
        " current version %s", ...
    )
    return False          # <- before component.async_migrate_entry is resolved
```

So if this build ever writes major 3 while core still declares `VERSION = 2`:

- uninstalling the HACS build leaves every entry in `migration_error`;
- core's own `async_migrate_entry` is **never called**, so the core build cannot
  even raise a repair or log a helpful "reinstall/upgrade the custom component
  first" message — the refusal happens above the integration;
- the user's only recovery is reinstalling the custom component (or hand-editing
  `.storage/core.config_entries`). That is an unrecoverable-by-UI state, which is
  why the major is treated as frozen rather than merely coordinated.

**Minors are free**, because a same-major mismatch is explicitly tolerated by the
snippet above:

- a core build **with** `async_migrate_entry` runs its ladder; every step is guarded
  `if (entry.minor_version or 1) < N`, so an entry written at a *higher* minor
  no-ops and returns `True`;
- a core build **without** any `async_migrate_entry` at all returns `True` on the
  `same_major_version` branch (`supports_migrate` is False but the major matches).

Raising `MINOR_VERSION` ahead of core is therefore safe as long as each new step
stays additive and guarded, and any `data`/`options` key it introduces is tolerated
by core (the superset-tolerance requirement at the top of this document).
`tests/test_migration_roundtrip.py` covers the current `2.8` entry explicitly, and
`test_the_declared_major_version_stays_2` fails if the major is bumped without
updating this section.

**Checklist for a future major bump** — every item must already be true before the
bump merges here:

1. the core `rtl_433` handler declares `VERSION = 3` and its `async_migrate_entry`
   handles `2.x → 3`;
2. that core version is **released** (not merely merged) and is recorded as the
   minimum Home Assistant version this build supports;
3. the identical `2.x → 3` migration ships in both builds;
4. §1 here, `CONTRACT_VERSION` in `tests/test_migration_roundtrip.py`, and the
   guard test are all updated in the same PR.

Until then, express new schema needs as a **guarded minor step**, not a major bump.

---

## 2. Entity `unique_id` formats

All entity `unique_id`s are scoped by the parent hub's config-entry id, so two hubs
observing the same model+id never collide. **`hub_entry_id` is passed as
`entry.entry_id`** at every call site (see §4), so the two names denote the same
value.

| Entity kind | Format | Source |
|---|---|---|
| Per-device field entity (sensor / binary_sensor / event) | `f"{hub_entry_id}:{device_key}:{object_suffix}"` | `entity.py:164` |
| Hub SDR control (number / select / switch) | `f"{hub_entry_id}:hub:{object_suffix}"` | `entity.py:350` |
| Hub connectivity binary_sensor | `f"{hub_entry_id}:hub:connectivity"` | `binary_sensor.py:164` |

Component provenance:
- `hub_entry_id` == the config entry's `entry_id` (`entry.entry_id`).
- `device_key` — the deterministic per-device identity `<model-token>-<id>[-ch..][-st..]`
  (`const.py:83-85`, `CONF_DEVICE_KEY`), stored as the key of
  `entry.data[CONF_DEVICES]`.
- `object_suffix` — the field/control descriptor's stable object suffix
  (`descriptor.object_suffix` for device fields at `entity.py:164`;
  `setting.object_suffix` for hub controls at `entity.py:350`).

Corroborating construction site: the platform builder assembles the same device
unique_id independently as
`f"{entry.entry_id}:{device_key}:{descriptor.object_suffix}"` (`entity.py:551`),
confirming `hub_entry_id == entry.entry_id`.

Migration sweeps depend on these tails and MUST stay valid:
- device unique-id shape `{hub_entry_id}:{device_key}:{object_suffix}`
  (`migration.py:120-121`, `137`);
- `:motion` tail — legacy `event.*_motion` cleanup (`migration.py:133-141`);
- `:last_seen` tail — Last-seen enable/disable sweeps (`migration.py:251`, `292`).

---

## 3. Device-registry identifier tuples

`DOMAIN == "rtl_433"` (`const.py:16`).

| Device | Identifier tuple | Source |
|---|---|---|
| Hub device | `(DOMAIN, entry.entry_id)` | `__init__.py:175`, `__init__.py:219`; hub entities `(DOMAIN, hub_entry_id)` at `entity.py:297` |
| Per-device (nested) | `(DOMAIN, f"{hub_entry_id}:{device_key}")` | `entity.py:165`; linked to the hub by `via_device_id`, resolved from `(DOMAIN, hub_entry_id)` at `entity.py:173` |
| Phantom `unknown` (legacy cleanup target only) | `(DOMAIN, f"{entry.entry_id}:{PHANTOM_DEVICE_KEY}")` | `migration.py:106` |

`PHANTOM_DEVICE_KEY == "unknown"` — **defined in `migration.py:64`, not `const.py`**
(intentionally not exported; the v2 model never creates this device, and the
idempotent cleanup at `migration.py:88-109` removes any pre-fix instance). The Core
build only needs this tuple to reproduce the same cleanup; it MUST NOT create a
phantom device.

---

## 4. Critical invariant: `hub_entry_id == entry.entry_id`

Both spellings appear in the code and refer to the **same string**:
- `entity.py:556` and `entity.py:570` pass `entry.entry_id` as the `hub_entry_id`
  argument into `entity_cls(...)` / `per_device_factory(...)`.
- `entity.py:383` passes `entry.entry_id` as `hub_entry_id` for hub controls.
- `binary_sensor.py:184` passes `entry.entry_id` for the hub connectivity entity.
- The hub device is registered with `(DOMAIN, entry.entry_id)` (`__init__.py:175`),
  while hub-attached entities declare `(DOMAIN, hub_entry_id)` (`entity.py:297`) —
  identical because of the above.

Therefore the entry-scoped identifiers (`entry.entry_id`) and the
`hub_entry_id`-scoped identifiers are one and the same scope. The minimal Core build
MUST use `entry.entry_id` wherever these templates reference `hub_entry_id`.

---

## Change control

Any change to a `version`/`minor_version` value or migration step (§1), a
`unique_id` template (§2), or a device `identifiers` tuple / its hub link (§3) is a
**breaking ABI change**. It requires:
1. a forward-only, non-downgrading migration, and
2. the identical change and migration shipped in **both** the HACS build and the
   minimal Core build at the same time.

A change to the config-entry **major** `VERSION` carries one further, stricter
requirement: the released core build must already read the new major *before* this
build starts writing it, because core refuses a higher-major entry above the
integration and the user cannot recover from the UI. See
[Majors are a one-way door](#majors-are-a-one-way-door-the-precondition-for-version--3).

Until then, these three surfaces are **FROZEN**.
