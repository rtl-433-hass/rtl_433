# rtl_433 HACS ↔ Core Compatibility Contract (ABI)

**Status: FROZEN — revision 2.** This document is a byte-level Application Binary
Interface (ABI) between the full HACS build (`custom_components/rtl_433/`) and the
future minimal Home Assistant Core build. Both builds ship under the **same domain**
(`rtl_433`, `const.py:18`) and therefore read and write the **same** config entries,
entity registry, and device registry. The identity surfaces below MUST be produced
**byte-identical** by both builds. Changing any of them is a breaking change that
requires a coordinated migration shipped in **both** builds simultaneously.

The minimal Core `rtl_433` integration MUST:
- construct every `unique_id` and device `identifiers` tuple using the **exact**
  templates in §2 and §3;
- carry the same config-entry `VERSION` / `MINOR_VERSION` (§1) and a migration
  path that is a **superset-tolerant, non-downgrading subset** of §1 — i.e. it
  MUST tolerate options / `minor_version` values written by the full build, and
  MUST NOT downgrade an entry;
- model the two identity levels of §4 (a config entry is a **location**; a config
  **subentry** is a receiver) and honour the registry-ownership rule in §5;
- treat `receiver` as a reserved identity token (§6);
- never mutate these formats without a coordinated migration in both builds.

All facts below were transcribed from the current source; line citations are to
the state of the tree at authoring time.

---

## Revision history

| Revision | Change |
|---|---|
| 1 | Original freeze of the v2 "hub" model: one config entry == one rtl_433 server, `VERSION = 2`, `hub_entry_id == entry.entry_id`. |
| 2 | **The coordinated break.** A config entry becomes a **location** holding one **receiver subentry** per rtl_433 server, and a decoded RF device is **unioned** across that location's receivers. `VERSION = 3`; the future-schema guard rises to `> 3`; receiver-owned entities and the three per-receiver link fields gain a receiver segment; the location device, the receiver device and the merged device are three distinct registry rows; §4's `hub_entry_id == entry.entry_id` invariant is replaced by the two-level location/receiver invariant; §5 (registry ownership) and §6 (identity grammar) are added. |

Revision 2 is the change `Change control` below contemplates: it is forward-only,
it never downgrades an entry, and it is shipped together with the migration that
performs it. **The Core build must adopt revision 2 directly** — see
[`CORE_UPSTREAM.md`](CORE_UPSTREAM.md); it must never land the revision-1 shape
first.

---

## 1. Config-entry `version` / `minor_version` scheme + migration ladder

### Declared version (`config_flow.py:435-436`)

```python
VERSION = 3
MINOR_VERSION = 1
```

New entries are created at `version=3, minor_version=1`. A v3 entry is a
**location**; its rtl_433 servers live in receiver **subentries** (§4).

### Invariants
- Migrations are **monotonic and non-destructive**: each step is guarded and only
  moves forward.
- The minimal Core build **MUST tolerate** options and `minor_version` values
  written by the full build (superset tolerance).
- **No migration may downgrade.** A future schema (`version > 3`) is explicitly
  rejected, returning `False` (unsupported) rather than mutating the entry.
- **`version = 3` is a hard minimum for the Core build.** A Core build still
  rejecting `version > 2` would treat every entry the full build writes as
  unsupported. Core MUST raise its own guard to `> 3` and MUST implement the
  v2(minor 8) → v3 step before it can coexist with this build.

### `async_migrate_entry` ladder (`migration.py:632-871`)

Entry point rejects future schemas first:

- **`migration.py:686-688`** — `if entry.version > 3: return False` (downgrade from
  a future schema is unsupported).

**Version 1 → 2** (`migration.py:690-704`): the 0.1.0 per-device-entry model → the
single-server ("hub") model.
- `migration.py:690-701` — a legacy **device** entry (`CONF_ENTRY_TYPE ==
  ENTRY_TYPE_DEVICE`) processed on its own re-homes its registry objects to the
  parent server entry (`CONF_RECEIVER_ENTRY_ID`), then sets `version=2,
  minor_version=2` and returns `True`. The parent later folds and removes it.
- `migration.py:704` — a **server** entry consolidates all child device entries
  into `entry.data[CONF_DEVICES]` (via `_migrate_receiver_entry`,
  `migration.py:584`), re-homing their registry objects before removal. Then it
  falls through the minor ladder below.

**Minor-version ladder** — gated as a whole on `if entry.version <= 2`
(`migration.py:706`), because each step below tests only `minor_version` and a v3
entry restarts at `minor_version = 1`; an ungated ladder would read that as an
install stuck at v2 minor 1 and walk the v2 steps again. Each step is then guarded
by `if (entry.minor_version or 1) < N`:

- **→ minor 2** (`migration.py:712-724`): seed this entry's
  `entry.data[CONF_USER_MAPPINGS]` from any pre-existing legacy
  `<config>/rtl_433_mappings.yaml` (read once in the executor; never modified or
  deleted). Sets `version=2, minor_version=2`.
- **→ minor 3** (`migration.py:726-731`): disable any already-created per-device
  "Last seen" sensors (unique-id tail `:last_seen`), which now ship
  disabled-by-default. Sets `minor_version=3`.
- **→ minor 4** (`migration.py:733-754`): drop a `CONF_AVAILABILITY_TIMEOUT`
  option still pinned to the legacy global default
  (`LEGACY_DEFAULT_AVAILABILITY_TIMEOUT == 600`) so the new device-class-aware
  defaults apply; a user-set non-default value is preserved. Sets
  `minor_version=4`.
- **→ minor 5** (`migration.py:756-763`): rewrite already-persisted doorbell
  `event_types` from the raw `"0"`/`"1"` strings to the standardized
  `"ring"`/`"secret_knock"` types. Removes no entity; the doorbell unique_id /
  `object_suffix` is unchanged. Sets `minor_version=5`.
- **→ minor 6** (`migration.py:765-773`): re-enable the "Last seen" sensor for
  event-driven devices (which now never expire) — only instances the integration
  disabled at minor 3, not ones the user disabled. Sets `minor_version=6`.
- **→ minor 7** (`migration.py:775-798`): repeat the minor-4 cleanup — strip a
  `CONF_AVAILABILITY_TIMEOUT` still equal to the legacy default (600) that the
  options flow used to re-persist on save. The options flow no longer writes the
  sentinel, so this heal is final. Sets `minor_version=7`.
- **→ minor 8** (`migration.py:801-831`): strip the retired `discovery_enabled`
  key, which gates nothing now that a device is created only once the user adopts
  it. `data` / `options` are rewritten only when the key was actually present.
  Sets `minor_version=8`.

> Note: the ladder is written to be idempotent and order-tolerant — a legacy device
> entry migrated before its parent only re-homes its own registry objects and bumps
> to minor 2; the parent folds and removes it later. Either ordering converges.

> The minor-3 / minor-6 "Last seen" sweeps deliberately address the **three-segment
> v2** unique_id shape (`migration.py:330-335`, `migration.py:350`). They run inside
> the v2 ladder, strictly before the v3 step re-keys those rows, so the rows they
> have to find are still at three segments.

**Version 2 (minor 8) → 3** (`migration.py:834-871`): the entry stops being one
rtl_433 server and becomes a **location** holding one server.

- `_migrate_entry_to_location` (`migration.py:538`) creates the single receiver
  **subentry** that carries the connection target and the server identity, keeping
  the entry's own `entry_id`. Without it a v2 entry cannot be set up at all.
- `_migrate_receiver_control_unique_ids` (`migration.py:437`) re-keys every
  `f"{entry_id}:hub:{object_suffix}"` row onto
  `f"{entry_id}:receiver:{subentry_id}:{object_suffix}"`, in place via
  `async_update_entity`, so `entity_id`, recorder history and statistics survive.
  It runs **first**, which is what leaves the receiver-owned rows at four segments.
  If the entry has adopted a device literally keyed `hub` the sweep declines and
  logs, rather than mangling that device's fields.
- `_migrate_link_field_unique_ids` (`migration.py:491`) then inserts the receiver
  segment into the three **link** fields (`rssi` / `snr` / `last_seen`), turning
  `f"{entry_id}:{device_key}:{suffix}"` into
  `f"{entry_id}:{device_key}:{subentry_id}:{suffix}"`. Segment count alone
  identifies a device field at this point, because the receiver-owned rows already
  carry four segments and a `device_key` can never contain a colon (§6).
- `migration.py:857-866` — one write strips the connection keys and the server's
  `unique_id` from the location's data (a location is a user-named grouping with no
  hardware identity) and sets `version=3, minor_version=1`.
- The conversion is deliberately **non-merging**: two of a user's existing entries
  are never folded into one location, because the integration cannot know which are
  co-located and folding them would force two histories onto one unique_id. Union
  is therefore strictly opt-in and the upgrade itself cannot lose history.
- **Everything else is byte-identical to v2**: every unioned device field's
  unique_id, every device-registry identifier, and the location link. That is the
  whole reason the v2 → v3 upgrade rewrites so little.

- **`migration.py:871`** — `return True`.

---

## 2. Entity `unique_id` formats

All entity `unique_id`s are scoped by the **location** config entry's `entry_id`
(§4), so two locations observing the same model+id never collide. A receiver is
discriminated inside a location by its **subentry id** — never by the entry id,
which several receivers share.

| Entity kind | Format | Source |
|---|---|---|
| Unioned device field (sensor / binary_sensor / event) | `f"{location_entry_id}:{device_key}:{object_suffix}"` | `entity.py:168` (via `field_unique_id`, `entity.py:145`), set at `entity.py:270-272` |
| Per-receiver **link** field (`rssi` / `snr` / `last_seen`) | `f"{location_entry_id}:{device_key}:{receiver_subentry_id}:{object_suffix}"` | `entity.py:167` (same helper), set at `entity.py:270-272` |
| Receiver radio control (number / select / switch) | `f"{location_entry_id}:receiver:{receiver_subentry_id}:{object_suffix}"` | `entity.py:580-582` |
| Receiver diagnostic sensor (noise, stats) | `f"{location_entry_id}:receiver:{receiver_subentry_id}:{suffix}"` | `sensor.py:587` |
| Receiver connectivity binary_sensor | `f"{location_entry_id}:receiver:{receiver_subentry_id}:connectivity"` | `binary_sensor.py:186` |

Component provenance:
- `location_entry_id` == the location config entry's `entry_id` — read off the
  coordinator, never passed in (`entity.py:254`, `self._location_id =
  coordinator.entry.entry_id`).
- `receiver_subentry_id` == the receiver config subentry's `subentry_id`
  (`coordinator/base.py:199`, `__init__.py:248`). The three receiver-owned rows
  above are all built from the single helper
  `const.receiver_identity(entry_id, receiver_id)` (`const.py:130-145`), exposed as
  `coordinator.receiver_identity` (`coordinator/base.py:200`), so the prefix has
  exactly one definition.
- `device_key` — the deterministic per-device identity `<model-token>-<id>[-ch..][-st..]`
  (`const.py:96`, `CONF_DEVICE_KEY`), stored as the key of
  `entry.data[CONF_DEVICES]` on the **location** entry.
- `object_suffix` — the field/control descriptor's stable object suffix
  (`descriptor.object_suffix` for device fields; `setting.object_suffix` for radio
  controls; `desc.suffix` for receiver diagnostics).

**The union rule**: a device field is one entity per mapped field however many of a
location's receivers decode it, so its id carries no receiver segment. The three
**link** fields are the deliberate exception — "how well does *this* receiver hear
that sensor" is a different measurement per receiver — so they gain a fourth
segment and yield one entity per (sensor × receiver), all of them on the merged
device (§3). The receiver segment sits **before** the object suffix so tail-matching
parsers and `device_replace.py`'s `{entry_id}:{device_key}:` prefix swap keep
working. `entity.py:145` (`field_unique_id`) is the single definition of both
shapes: the entity itself and the platform helper's dedup bookkeeping
(`entity.py:878-883`) call it, so the two cannot disagree about whether a field is
one entity or several.

Migration sweeps depend on these tails and MUST stay valid:
- device unique-id shape `{location_entry_id}:{device_key}:{object_suffix}`
  (`migration.py:188-190`, `migration.py:527-534`);
- `:motion` tail — legacy `event.*_motion` cleanup (`migration.py:163-223`);
- `:last_seen` tail — Last-seen enable/disable sweeps (`migration.py:302`,
  `migration.py:350`);
- the retired `:hub:` infix — read one last time by the v3 step
  (`migration.py:476`) and written by nothing.

---

## 3. Device-registry identifier tuples

`DOMAIN == "rtl_433"` (`const.py:18`). A v3 location entry owns a three-level
device tree: the location device, one receiver device per subentry, and the merged
RF devices.

| Device | Identifier tuple | Source |
|---|---|---|
| Location device | `(DOMAIN, entry.entry_id)` | `__init__.py:471-478` — **byte-identical to the v2 hub device identifier**, which is what lets an upgraded entry keep its root device row |
| Receiver device | `(DOMAIN, f"{entry.entry_id}:receiver:{receiver_subentry_id}")` | `__init__.py:273-281`, built by `const.receiver_identity` (`const.py:145`); receiver-attached entities declare `(DOMAIN, coordinator.receiver_identity)` at `entity.py:517` |
| Merged (nested) RF device | `(DOMAIN, f"{entry.entry_id}:{device_key}")` | `entity.py:300` — **byte-identical to v2** |
| Phantom `unknown` (legacy cleanup target only) | `(DOMAIN, f"{entry.entry_id}:{PHANTOM_DEVICE_KEY}")` | `migration.py:157` |

Links between them (`via_device_id`, **not** the `via_device` identifier tuple —
that spelling is deprecated upstream and gone from `DeviceInfo`):

- The **receiver device** hangs off the location device:
  `via_device_id=location_device_id` (`__init__.py:280`), where the location device
  is registered first (`__init__.py:471`) so the link always resolves on the first
  pass.
- The **merged RF device** hangs off the **location** device, never off a receiver:
  `via_device_id=dr.async_get_device_id_by_identifier(hass, (DOMAIN, location_entry_id), config_entry_id=location_entry_id)`
  (`entity.py:312-316`). A merged device may be fed by several receivers, so a
  "via" pointing at one of them would claim the sensor sits behind that server
  alone.

`PHANTOM_DEVICE_KEY == "unknown"` — **defined in `migration.py:79`, not `const.py`**
(intentionally not exported; the v2 and v3 models never create this device, and the
idempotent cleanup at `migration.py:139-160` removes any pre-fix instance). The Core
build only needs this tuple to reproduce the same cleanup; it MUST NOT create a
phantom device.

---

## 4. Critical invariant: two identity levels — location entry, receiver subentry

Revision 1's invariant `hub_entry_id == entry.entry_id` **no longer holds and no
longer has a referent**: there is no hub. It is replaced by the following two-level
invariant, which the Core build MUST reproduce.

1. **The location config entry's `entry_id` is the identity scope.** Every
   `unique_id` in §2 and every device identifier in §3 begins with it. It is what
   keeps two locations that happen to hear the same `model`+`id` from colliding, and
   it is why the merged device identifier and the unioned device-field unique_id are
   byte-identical across the v2 → v3 break — the entry keeps its `entry_id` through
   the conversion (`migration.py:538`).
2. **A receiver is identified by its config subentry's `subentry_id`**, never by
   the entry id. `coordinator.receiver_id = subentry.subentry_id`
   (`coordinator/base.py:199`) and `__init__.py:248` are the only two sources.
   Simply swapping v2's `:hub:` literal for `:receiver:` would have left the
   template entry-scoped, and two receivers in one location would have minted
   identical control unique_ids.
3. **`const.receiver_identity(entry_id, receiver_id)` (`const.py:130-145`) is the
   single definition of the receiver identity root**
   `f"{entry_id}:receiver:{receiver_id}"`. It is simultaneously the receiver
   device's registry identifier and the prefix of every receiver-owned entity's
   `unique_id`; the two cannot drift because there is one builder.
4. **A receiver identity is never re-labelled.** The location entry id leads
   because a receiver only exists inside a location: moving a receiver to another
   location is a *new subentry*, not a rewrite of this string.

---

## 5. Registry ownership: one config entry, one subentry, per device

Since Home Assistant's device-registry storage **v3** (HA 2026.8), a device row
belongs to **exactly one** `config_entry_id` and **at most one**
`config_subentry_id`. Devices can no longer span config entries, and the
multi-entry accessors (`config_entries`, `config_entries_subentries`,
`primary_config_entry`) are deprecated. Grouping is therefore **intra-entry via
subentries**, which is why a location holds its receivers as subentries rather than
as sibling entries.

Ownership assignments — both builds MUST match these exactly:

| Registry object | `config_entry_id` | `config_subentry_id` | Source |
|---|---|---|---|
| Location device | the location entry | **`None`** — it describes the location, which outlives any one receiver | `__init__.py:471-478` |
| Receiver device | the location entry | the receiver's `subentry_id` — which is what makes it disappear with the receiver | `__init__.py:273-281` |
| Merged RF device | the location entry | **`None`** | `entity.py:300`, added with no subentry (`entity.py:981`) |
| Unioned device-field entity | the location entry | **`None`** | `entity.py:744`, `entity.py:981` |
| **Per-receiver link entity** (`rssi` / `snr` / `last_seen`) | the location entry | **`None`** — even though it is receiver-specific | `entity.py:53`, `entity.py:286` |
| Receiver radio control | the location entry | the receiver's `subentry_id` | `entity.py:625` |
| Receiver diagnostic sensor | the location entry | the receiver's `subentry_id` | `sensor.py:645` |
| Receiver connectivity binary_sensor | the location entry | the receiver's `subentry_id` | `binary_sensor.py:215` |

The per-receiver link entities are the subtle row. They are receiver-specific
entities living on a **shared, location-owned** device, so passing their receiver's
`config_subentry_id` at `async_add_entities` would ask Home Assistant to assign one
device to two subentries. Today that **silently moves** the merged device between
subentries and logs a deprecation; **it raises in HA Core 2027.8**
(`homeassistant/helpers/device_registry.py`, `async_get_or_create`:
`breaks_in_ha_version="2027.8.0"`, *"assigns an existing device to a different
config subentry … A device belongs to one subentry"*). The receiver association for
these entities therefore travels in the **`unique_id` and the entity name** only
(`entity.py:286`) — never in a `config_subentry_id`.

Rule for both builds: **an entity passes a `config_subentry_id` only when the device
it attaches to is that subentry's own receiver device.** Everything on a merged or
location device passes none.

---

## 6. Identity grammar and the reserved `receiver` token

Colon-separated identity strings are parsed **positionally, by segment count plus a
literal marker**, so both builds must respect the same grammar:

| Segments | Shape | Meaning |
|---|---|---|
| 1 | `{location_entry_id}` | the location device |
| 2 | `{location_entry_id}:{device_key}` | a merged RF device |
| 3, middle segment == `receiver` | `{location_entry_id}:receiver:{receiver_subentry_id}` | a receiver device |
| 3, otherwise | `{location_entry_id}:{device_key}:{object_suffix}` | a unioned device field |
| 4, second segment == `receiver` | `{location_entry_id}:receiver:{receiver_subentry_id}:{object_suffix}` | a receiver-owned control / diagnostic |
| 4, otherwise | `{location_entry_id}:{device_key}:{receiver_subentry_id}:{object_suffix}` | a per-receiver link entity |

`pyrtl_433.naming.safe_token` — the frozen builder every `device_key` is made of —
maps `:` to `_`, so a `device_key` can never contain a colon and the segment count is
always unambiguous. It does, however, pass the word `receiver` through unchanged.

**`receiver` is therefore a RESERVED token: no `device_key` may equal it.**
`RECEIVER_SEGMENT` / `RESERVED_DEVICE_KEYS` / `is_reserved_device_key`
(`const.py:112-128`) are the single definition, and the two identity parsers call
the helper rather than comparing against the literal:

- `device_trigger.py:207-215` — decodes a device field only from a three-segment id
  whose middle segment is not reserved; four segments and reserved middles yield
  `(None, None)` rather than a bogus `device_key` of `receiver`.
- `__init__.py:720-726` — the orphan-device scan refuses to un-adopt a reserved key
  and recognises the receiver device by the marker.

The same literal is also the receiver subentry's `subentry_type`
(`const.py:119`, `SUBENTRY_TYPE_RECEIVER = RECEIVER_SEGMENT`), deliberately sharing
one spelling because both answer the same question ("is this naming a receiver?").

The retired v2 marker was `hub` (`migration.py:103`). It was never reserved in v2,
which is why the v3 control sweep declines to run on an entry that adopted a device
keyed `hub` (`migration.py:466-474`). Nothing writes `:hub:` any more.

---

## Change control

Any change to a `version`/`minor_version` value or migration step (§1), a
`unique_id` template (§2), a device `identifiers` tuple or its `via_device_id` link
(§3), the two-level identity invariant (§4), the registry-ownership rule (§5), or
the identity grammar and its reserved token (§6) is a **breaking ABI change**. It
requires:
1. a forward-only, non-downgrading migration,
2. the identical change and migration shipped in **both** the HACS build and the
   minimal Core build at the same time, and
3. a new revision of this document, recorded in the revision history above and
   reflected in [`CORE_UPSTREAM.md`](CORE_UPSTREAM.md) before any upstream PR lands
   the superseded shape.

Until then, these surfaces are **FROZEN** at revision 2.
