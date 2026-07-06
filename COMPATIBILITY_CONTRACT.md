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
- carry the same config-entry `VERSION` / `MINOR_VERSION` (§1) and a migration
  path that is a **superset-tolerant, non-downgrading subset** of §1 — i.e. it
  MUST tolerate options / `minor_version` values written by the full build, and
  MUST NOT downgrade an entry;
- never mutate these formats without a coordinated migration in both builds.

All facts below were transcribed from the current source; line citations are to
the state of the tree at authoring time.

---

## 1. Config-entry `version` / `minor_version` scheme + migration ladder

### Declared version (`config_flow.py:137-138`)

```python
VERSION = 2
MINOR_VERSION = 7
```

New entries are created at `version=2, minor_version=7`.

### Invariants
- Migrations are **monotonic and non-destructive**: each step is guarded and only
  moves forward.
- The minimal Core build **MUST tolerate** options and `minor_version` values
  written by the full build (superset tolerance).
- **No migration may downgrade.** A future schema (`version > 2`) is explicitly
  rejected, returning `False` (unsupported) rather than mutating the entry.

### `async_migrate_entry` ladder (`migration.py:410-554`)

Entry point rejects future schemas first:

- **`migration.py:445-447`** — `if entry.version > 2: return False` (downgrade from
  a future schema is unsupported).

**Version 1 → 2** (`migration.py:449-463`): the 0.1.0 per-device-entry model → the
hub model.
- `migration.py:449-460` — a legacy **device** entry (`CONF_ENTRY_TYPE ==
  ENTRY_TYPE_DEVICE`) processed on its own re-homes its registry objects to the
  parent hub (`CONF_HUB_ENTRY_ID`), then sets `version=2, minor_version=2` and
  returns `True`. The hub later folds and removes it.
- `migration.py:462-463` — a **hub** entry consolidates all child device entries
  into `entry.data[CONF_DEVICES]` (via `_migrate_hub_entry`), re-homing their
  registry objects before removal. Then it falls through the minor ladder below.

**Minor-version ladder** — each step guarded by `if (entry.minor_version or 1) < N`:

- **→ minor 2** (`migration.py:465-477`): seed this hub's
  `entry.data[CONF_USER_MAPPINGS]` from any pre-existing legacy
  `<config>/rtl_433_mappings.yaml` (read once in the executor; never modified or
  deleted). Sets `version=2, minor_version=2`.
- **→ minor 3** (`migration.py:479-484`): disable any already-created per-device
  "Last seen" sensors (unique-id tail `:last_seen`), which now ship
  disabled-by-default. Sets `minor_version=3`.
- **→ minor 4** (`migration.py:486-507`): drop a hub `CONF_AVAILABILITY_TIMEOUT`
  option still pinned to the legacy global default
  (`LEGACY_DEFAULT_AVAILABILITY_TIMEOUT == 600`, `const.py:174`) so the new
  device-class-aware defaults apply; a user-set non-default value is preserved.
  Sets `minor_version=4`.
- **→ minor 5** (`migration.py:509-516`): rewrite already-persisted doorbell
  `event_types` from the raw `"0"`/`"1"` strings to the standardized
  `"ring"`/`"secret_knock"` types. Removes no entity; the doorbell unique_id /
  `object_suffix` is unchanged. Sets `minor_version=5`.
- **→ minor 6** (`migration.py:518-526`): re-enable the "Last seen" sensor for
  event-driven devices (which now never expire) — only instances the integration
  disabled at minor 3, not ones the user disabled. Sets `minor_version=6`.
- **→ minor 7** (`migration.py:528-552`): repeat the minor-4 cleanup — strip a hub
  `CONF_AVAILABILITY_TIMEOUT` still equal to the legacy default (600) that the
  options flow used to re-persist on save. The options flow no longer writes the
  sentinel, so this heal is final. Sets `minor_version=7`.
- **`migration.py:554`** — `return True`.

> Note: the ladder is written to be idempotent and order-tolerant — a legacy device
> entry migrated before its hub only re-homes its own registry objects and bumps to
> minor 2; the hub folds and removes it later. Either ordering converges.

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
| Per-device (nested) | `(DOMAIN, f"{hub_entry_id}:{device_key}")` | `entity.py:182`; linked to the hub via `via_device=(DOMAIN, hub_entry_id)` at `entity.py:186` |
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
`unique_id` template (§2), or a device `identifiers`/`via_device` tuple (§3) is a
**breaking ABI change**. It requires:
1. a forward-only, non-downgrading migration, and
2. the identical change and migration shipped in **both** the HACS build and the
   minimal Core build at the same time.

Until then, these three surfaces are **FROZEN**.
