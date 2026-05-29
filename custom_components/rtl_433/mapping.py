"""Device-library loader for the rtl_433 integration.

This is a thin, dependency-light loader that parses the themed YAML files under
``device_library/`` into an in-memory registry of :class:`FieldDescriptor`
objects keyed by rtl_433 field name, loads the ``_skip_keys.yaml`` exclusion
list, and (optionally) layers a per-installation user-override file on top.

The public surface consumed by later tasks is:

* :func:`load_library` -> ``(registry, skip_keys)`` -- parse the shipped library.
* :func:`lookup` -- resolve one field name (and ``model``) to a descriptor (or
  ``None``).
* :func:`should_skip` -- test a field against the skip-key set.
* :func:`apply_transform` -- turn a raw rtl_433 value into the HA-facing state.
* :func:`validate_user_mappings` -- pure validator returning per-entry problems.
* :func:`normalize_overrides` -- pure, JSON-serialisable, payload-canonical copy.
* :func:`merge_overrides` -- pure merge helper (used by the loader + tests).

The ``registry`` returned by :func:`load_library` is a :class:`Registry`: a flat
``{field_key: FieldDescriptor}`` table (the global default) plus an optional
model-scoped ``{model: {field_key: FieldDescriptor}}`` table populated from each
file's reserved top-level ``models:`` block. :func:`lookup` resolves the
model-scoped entry for ``(model, field_key)`` first, then the global flat entry.

File I/O is synchronous. Async callers (the integration setup) must
run :func:`load_library` off the event loop via ``hass.async_add_executor_job``
because it touches the filesystem.

The YAML schema is documented in ``docs/device-library.md``; this module
conforms to the attribute names used by the ``device_library/`` YAML files.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

import yaml

from .const import LOGGER

# Name of the optional per-installation override file, dropped into the Home
# Assistant configuration directory.
USER_OVERRIDE_FILENAME = "rtl_433_mappings.yaml"

# Top-level key inside ``_skip_keys.yaml`` (and optionally an override file)
# holding the flat list of fields that must never produce an entity.
SKIP_KEYS_FIELD = "skip_keys"

# Reserved top-level key holding the model-scoped descriptor table
# (``model -> {field_key -> descriptor}``). Intercepted by the loader / merge so
# it is never mis-parsed as a field named ``models``.
MODELS_FIELD = "models"

# Directory holding the shipped library, resolved relative to this module so the
# loader works regardless of the installed location.
_LIBRARY_DIR = Path(__file__).parent / "device_library"


@dataclass(frozen=True)
class FieldDescriptor:
    """Immutable description of how one rtl_433 field maps to an HA entity.

    Field names mirror the YAML attributes documented in
    ``docs/device-library.md``. ``field_key`` is the rtl_433 JSON key (the
    top-level YAML key); the remaining attributes are the entity descriptor.
    """

    field_key: str
    platform: str
    name: str
    object_suffix: str
    device_class: str | None = None
    unit_of_measurement: str | None = None
    state_class: str | None = None
    value_transform: dict[str, Any] | None = None
    payload: dict[str, Any] | None = None
    force_update: bool = False
    entity_category: str | None = None
    enabled_by_default: bool = True
    icon: str | None = None
    clear_delay: int | None = None


# Attribute names the descriptor accepts from a YAML entry (everything except
# ``field_key``, which is supplied positionally from the top-level key).
_DESCRIPTOR_ATTRS = frozenset(
    f.name for f in fields(FieldDescriptor) if f.name != "field_key"
)

# Entity platforms the shipped library uses; the UI validator rejects any
# ``platform`` value outside this set. Confirmed by the ``platform:`` values in
# ``device_library/*.yaml``.
_SUPPORTED_PLATFORMS = frozenset({"sensor", "binary_sensor", "event"})

# Entry attributes a user mapping must supply for a valid descriptor.
_REQUIRED_ENTRY_ATTRS = ("platform", "name", "object_suffix")


@dataclass(frozen=True)
class Registry:
    """The loaded device library: a flat table plus a model-scoped table.

    ``flat`` is the global ``{field_key: FieldDescriptor}`` mapping (the
    historical registry shape, the global default for every device). ``models``
    is the optional model-scoped overlay ``{model: {field_key: FieldDescriptor}}``
    populated from each file's reserved top-level ``models:`` block.

    :func:`lookup` resolves the model-scoped entry for ``(model, field_key)``
    first, falling back to the global ``flat`` entry.
    """

    flat: dict[str, FieldDescriptor]
    models: dict[str, dict[str, FieldDescriptor]]


def _descriptor_from_entry(field_key: str, entry: dict[str, Any]) -> FieldDescriptor:
    """Build a :class:`FieldDescriptor` from one YAML mapping entry.

    Unknown attributes are ignored (with a debug log) so a newer library file
    that adds keys does not break an older loader. ``platform``, ``name`` and
    ``object_suffix`` are required; a ``KeyError``/``TypeError`` here is caught
    by the per-file handler in :func:`_load_descriptor_file`.
    """
    if not isinstance(entry, dict):
        raise TypeError(
            f"entry for {field_key!r} is not a mapping: {type(entry).__name__}"
        )

    known = {k: v for k, v in entry.items() if k in _DESCRIPTOR_ATTRS}
    unknown = set(entry) - _DESCRIPTOR_ATTRS
    if unknown:
        LOGGER.debug(
            "Ignoring unknown attribute(s) %s on field %r", sorted(unknown), field_key
        )

    if "payload" in known:
        known["payload"] = _normalize_payload(known["payload"])

    if "clear_delay" in known:
        raw = known["clear_delay"]
        if not isinstance(raw, int) or isinstance(raw, bool) or raw <= 0:
            LOGGER.debug(
                "Ignoring invalid 'clear_delay' %r on field %r", raw, field_key
            )
            known.pop("clear_delay")

    return FieldDescriptor(field_key=field_key, **known)


def _normalize_payload(payload: Any) -> dict[str, Any] | None:
    """Normalize a binary ``payload`` mapping to string ``on``/``off`` keys.

    PyYAML (YAML 1.1) parses the *unquoted* bare keys ``on`` / ``off`` as the
    booleans ``True`` / ``False``, so ``{ on: "1", off: "0" }`` in the library
    files arrives as ``{True: "1", False: "0"}``. This rewrites those (and any
    already-string variants) back to canonical ``{"on": ..., "off": ...}`` so
    downstream consumers and :func:`apply_transform` see a stable shape.
    """
    if not isinstance(payload, dict):
        return payload

    normalized: dict[str, Any] = {}
    for key, value in payload.items():
        if key is True or key == "on":
            normalized["on"] = value
        elif key is False or key == "off":
            normalized["off"] = value
        else:
            normalized[str(key)] = value
    return normalized


def _parse_models_block(raw: Any, source: str) -> dict[str, dict[str, FieldDescriptor]]:
    """Parse a reserved ``models:`` block into ``{model: {field_key: descriptor}}``.

    ``raw`` is the value of the top-level ``models:`` key. A non-mapping block,
    a non-mapping per-model entry, or an individual malformed descriptor is
    logged and skipped (matching the per-entry defensiveness elsewhere) rather
    than aborting the whole load. ``source`` names the originating file/override
    for log context.
    """
    models: dict[str, dict[str, FieldDescriptor]] = {}
    if not isinstance(raw, dict):
        LOGGER.warning(
            "Ignoring %s 'models:' block: not a mapping (%s)",
            source,
            type(raw).__name__,
        )
        return models

    for model, entries in raw.items():
        if not isinstance(entries, dict):
            LOGGER.warning(
                "Ignoring %s model %r: not a mapping (%s)",
                source,
                model,
                type(entries).__name__,
            )
            continue
        descriptors: dict[str, FieldDescriptor] = {}
        for field_key, entry in entries.items():
            try:
                descriptors[field_key] = _descriptor_from_entry(field_key, entry)
            except TypeError, ValueError:
                LOGGER.exception(
                    "Ignoring malformed %s model %r field %r", source, model, field_key
                )
        if descriptors:
            models[str(model)] = descriptors
    return models


def _load_descriptor_file(
    path: Path,
) -> tuple[dict[str, FieldDescriptor], dict[str, dict[str, FieldDescriptor]]]:
    """Parse one themed library file into ``(flat, models)`` descriptor tables.

    Raises on a malformed file; the caller decides whether that is fatal. Each
    top-level key is an rtl_433 field name mapped to its entry, except the
    reserved ``models:`` key, which is intercepted and parsed into the
    model-scoped ``{model: {field_key: descriptor}}`` table instead of being
    treated as a field named ``models``.
    """
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)

    if data is None:
        return {}, {}
    if not isinstance(data, dict):
        raise TypeError(f"{path.name}: top-level YAML is not a mapping")

    descriptors: dict[str, FieldDescriptor] = {}
    models: dict[str, dict[str, FieldDescriptor]] = {}
    for field_key, entry in data.items():
        if field_key == MODELS_FIELD:
            models = _parse_models_block(entry, path.name)
            continue
        descriptors[field_key] = _descriptor_from_entry(field_key, entry)
    return descriptors, models


def _load_skip_keys(path: Path) -> set[str]:
    """Parse ``_skip_keys.yaml`` into a set of field names.

    Returns an empty set (and logs) if the file is missing or malformed so a
    bad skip file never blocks startup.
    """
    if not path.is_file():
        LOGGER.warning(
            "Skip-keys file not found at %s; no fields will be skipped", path
        )
        return set()

    try:
        with path.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
    except OSError, yaml.YAMLError:
        LOGGER.exception("Failed to parse skip-keys file %s; ignoring it", path)
        return set()

    return _extract_skip_keys(data)


def _extract_skip_keys(data: Any) -> set[str]:
    """Pull the ``skip_keys`` list out of parsed YAML data, defensively."""
    if not isinstance(data, dict):
        return set()
    raw = data.get(SKIP_KEYS_FIELD)
    if not isinstance(raw, list):
        return set()
    return {str(key) for key in raw}


def load_library(
    library_dir: Path | None = None,
) -> tuple[Registry, set[str]]:
    """Load the shipped device library and skip-key list.

    Globs ``<library_dir>/*.yaml`` (default: the packaged ``device_library``
    directory next to this module). Files whose name starts with ``_`` are not
    parsed as mapping tables; ``_skip_keys.yaml`` is loaded separately into the
    skip-key set.

    A malformed individual file is logged and skipped rather than aborting the
    whole load, so one bad file never prevents the rest of the library from
    working.

    Returns ``(registry, skip_keys)`` where ``registry`` is a :class:`Registry`
    (flat ``{field_key: FieldDescriptor}`` table plus the model-scoped overlay)
    and ``skip_keys`` is a ``set[str]``.

    This performs blocking file I/O; async callers must invoke it via
    ``hass.async_add_executor_job``.
    """
    base = library_dir if library_dir is not None else _LIBRARY_DIR

    flat: dict[str, FieldDescriptor] = {}
    models: dict[str, dict[str, FieldDescriptor]] = {}
    if not base.is_dir():
        LOGGER.error("Device-library directory not found: %s", base)
        return Registry(flat=flat, models=models), set()

    for path in sorted(base.glob("*.yaml")):
        if path.name.startswith("_"):
            # Underscore-prefixed files are not mapping tables.
            continue
        try:
            descriptors, file_models = _load_descriptor_file(path)
        except OSError, yaml.YAMLError, TypeError, ValueError:
            LOGGER.exception("Skipping malformed device-library file %s", path)
            continue
        # Later files override earlier ones on key collision; warn so it is
        # visible during development.
        for key in descriptors.keys() & flat.keys():
            LOGGER.warning(
                "Field %r in %s overrides an earlier definition", key, path.name
            )
        flat.update(descriptors)
        for model, entries in file_models.items():
            models.setdefault(model, {}).update(entries)

    skip_keys = _load_skip_keys(base / "_skip_keys.yaml")
    LOGGER.debug(
        "Loaded %d descriptor(s), %d model(s) and %d skip key(s)",
        len(flat),
        len(models),
        len(skip_keys),
    )
    return Registry(flat=flat, models=models), skip_keys


# Lazily-populated default library, used by :func:`lookup` / :func:`should_skip`
# when no explicit registry is passed. Callers that need overrides applied
# should pass their own merged registry/skip set instead.
_DEFAULT_REGISTRY: Registry | None = None
_DEFAULT_SKIP_KEYS: set[str] | None = None


def _default_library() -> tuple[Registry, set[str]]:
    """Return the cached shipped library, loading it on first use.

    Blocking on first call; async callers should instead call
    :func:`load_library` via ``hass.async_add_executor_job`` and pass the result
    explicitly so the event loop is never blocked.
    """
    global _DEFAULT_REGISTRY, _DEFAULT_SKIP_KEYS
    if _DEFAULT_REGISTRY is None or _DEFAULT_SKIP_KEYS is None:
        _DEFAULT_REGISTRY, _DEFAULT_SKIP_KEYS = load_library()
    return _DEFAULT_REGISTRY, _DEFAULT_SKIP_KEYS


def merge_overrides(
    registry: Registry,
    skip_keys: set[str],
    override_data: dict[str, Any],
) -> tuple[Registry, set[str]]:
    """Layer a parsed override mapping on top of a base registry/skip set.

    Pure (no I/O): returns new ``(registry, skip_keys)`` objects, leaving the
    inputs untouched. Override flat entries fully replace existing descriptors
    (no deep merge); a reserved ``models:`` block is merged so an override
    model-scoped entry replaces the shipped one for the same ``(model,
    field_key)`` (other shipped model fields are preserved); a ``skip_keys`` list
    in the override is unioned with the base. A malformed individual override
    entry is logged and skipped.
    """
    merged_flat = dict(registry.flat)
    merged_models = {model: dict(entries) for model, entries in registry.models.items()}
    merged_skips = set(skip_keys)

    if not isinstance(override_data, dict):
        LOGGER.warning(
            "Override data is not a mapping (%s); ignoring",
            type(override_data).__name__,
        )
        return Registry(flat=merged_flat, models=merged_models), merged_skips

    for field_key, entry in override_data.items():
        if field_key == SKIP_KEYS_FIELD:
            merged_skips |= _extract_skip_keys({SKIP_KEYS_FIELD: entry})
            continue
        if field_key == MODELS_FIELD:
            for model, entries in _parse_models_block(entry, "override").items():
                merged_models.setdefault(model, {}).update(entries)
            continue
        try:
            merged_flat[field_key] = _descriptor_from_entry(field_key, entry)
        except TypeError, ValueError:
            LOGGER.exception(
                "Ignoring malformed override entry for field %r", field_key
            )

    return Registry(flat=merged_flat, models=merged_models), merged_skips


def _validate_entry(field_key: str, entry: Any) -> list[str]:
    """Validate one field entry, returning self-contained problem strings.

    Mirrors what :func:`_descriptor_from_entry` will accept so the
    "validator accepts => merge keeps it" invariant holds: the entry must be a
    mapping, must supply the required ``platform``/``name``/``object_suffix``
    attributes, and any ``platform`` it does supply must be supported. Unknown
    extra attributes are tolerated (the descriptor builder ignores them). Each
    returned problem is prefixed with ``<field_key>: `` so the caller can present
    it without further context.
    """
    if not isinstance(entry, dict):
        return [f"{field_key}: must be a mapping"]

    problems: list[str] = []
    for attr in _REQUIRED_ENTRY_ATTRS:
        value = entry.get(attr)
        if value is None or (isinstance(value, str) and not value.strip()):
            problems.append(f"{field_key}: missing required {attr!r}")

    platform = entry.get("platform")
    if platform is not None and platform != "" and platform not in _SUPPORTED_PLATFORMS:
        problems.append(
            f"{field_key}: unknown platform {platform!r} "
            "(expected one of sensor, binary_sensor, event)"
        )

    return problems


def validate_user_mappings(data: Any) -> list[str]:
    """Validate a user-supplied mapping object, returning per-entry problems.

    Pure: never raises and never mutates ``data``. An empty list means the object
    is valid. ``None`` (an empty mapping) is valid. A non-mapping top level yields
    a single problem. The reserved ``skip_keys`` key must be a list; the reserved
    ``models`` key must be a mapping of ``model -> {field_key -> entry}``; every
    other key is a flat field entry. Each problem string is self-contained (it
    names the offending field/path) so the options step can join them for a form
    error. The rules deliberately match :func:`_descriptor_from_entry`'s
    tolerance so anything the validator accepts survives :func:`merge_overrides`.
    """
    if data is None:
        return []
    if not isinstance(data, dict):
        return ["top-level mapping must be a YAML object"]

    problems: list[str] = []
    for key, value in data.items():
        if key == SKIP_KEYS_FIELD:
            if not isinstance(value, list):
                problems.append("skip_keys: must be a list")
            continue
        if key == MODELS_FIELD:
            if not isinstance(value, dict):
                problems.append("models: must be a mapping")
                continue
            for model, entries in value.items():
                if not isinstance(entries, dict):
                    problems.append(f"models.{model}: must be a mapping")
                    continue
                for field_key, entry in entries.items():
                    problems.extend(
                        _validate_entry(f"models.{model}.{field_key}", entry)
                    )
            continue
        problems.extend(_validate_entry(str(key), value))

    return problems


def normalize_overrides(data: Any) -> dict[str, Any]:
    """Return a deep-copied, JSON-serialisable, payload-canonical override dict.

    Pure: never mutates ``data``. Returns ``{}`` for any non-mapping input. Every
    flat entry and every ``models.<model>.<field_key>`` entry that carries a
    ``payload`` has it rewritten via :func:`_normalize_payload`, so boolean keys
    parsed from YAML (``True``/``False``) and bare ``on``/``off`` keys become the
    canonical string keys ``"on"``/``"off"``. This guarantees the result has no
    non-string dict keys and is safe to JSON-serialise for storage. The
    ``skip_keys`` list and the ``models`` block structure are preserved.
    """
    if not isinstance(data, dict):
        return {}

    result: dict[str, Any] = copy.deepcopy(data)

    for key, value in result.items():
        if key == SKIP_KEYS_FIELD:
            continue
        if key == MODELS_FIELD:
            if not isinstance(value, dict):
                continue
            for entries in value.values():
                if not isinstance(entries, dict):
                    continue
                for entry in entries.values():
                    if isinstance(entry, dict) and "payload" in entry:
                        entry["payload"] = _normalize_payload(entry["payload"])
            continue
        if isinstance(value, dict) and "payload" in value:
            value["payload"] = _normalize_payload(value["payload"])

    return result


def should_skip(field_key: str, skip_keys: set[str] | None = None) -> bool:
    """Return ``True`` if ``field_key`` is in the skip-key set.

    Checked by callers *before* attempting a :func:`lookup` so identity /
    transport keys never produce entities. ``skip_keys`` defaults to the cached
    shipped library (without user overrides); callers that have applied
    overrides should pass their merged set explicitly.
    """
    if skip_keys is None:
        _, skip_keys = _default_library()
    return field_key in skip_keys


def lookup(
    field_key: str,
    model: str | None = None,
    registry: Registry | None = None,
) -> FieldDescriptor | None:
    """Return the descriptor for ``field_key`` on ``model``, or ``None``.

    Resolution is specificity-first: the model-scoped entry for
    ``(model, field_key)`` wins if present, else the global flat entry for
    ``field_key``, else ``None``. ``model`` may be ``None`` (or unknown) to
    resolve only the global flat entry.

    ``registry`` defaults to the cached shipped library (without user
    overrides); callers that have applied overrides should pass their merged
    registry explicitly.
    """
    if registry is None:
        registry, _ = _default_library()
    if model is not None:
        scoped = registry.models.get(model)
        if scoped is not None and field_key in scoped:
            return scoped[field_key]
    return registry.flat.get(field_key)


# ---------------------------------------------------------------------------
# Value transformation
# ---------------------------------------------------------------------------

# Truthy raw strings used by binary payloads / coercion fallbacks.
_TRUE_TOKENS = frozenset({"1", "true", "on", "yes"})


def _coerce_number(raw_value: Any, *, as_int: bool) -> float | int | None:
    """Coerce ``raw_value`` to ``int``/``float``; return ``None`` on failure."""
    try:
        number = float(raw_value)
    except TypeError, ValueError:
        return None
    return int(number) if as_int else number


def _apply_sensor_transform(transform: dict[str, Any], raw_value: Any) -> Any:
    """Apply a ``value_transform`` mapping to a sensor's raw value.

    Application order matches ``docs/device-library.md``: coerce (``float`` /
    ``int``, with ``round`` / ``scale`` / ``offset`` implying float) -> ``scale``
    -> ``offset`` -> ``round``. Returns ``raw_value`` unchanged if it is not
    numeric so non-numeric/None readings pass through untouched.
    """
    has_int = bool(transform.get("int"))
    has_round = "round" in transform
    # ``round``/``scale``/``offset`` all imply float arithmetic; ``int`` forces
    # integer coercion only when no float-implying key overrides it.
    needs_float = (
        "scale" in transform
        or "offset" in transform
        or has_round
        or bool(transform.get("float"))
    )
    as_int = has_int and not needs_float

    number = _coerce_number(raw_value, as_int=as_int)
    if number is None:
        # Non-numeric value: nothing to transform, hand it back as-is.
        return raw_value

    if "scale" in transform:
        try:
            number = number * float(transform["scale"])
        except TypeError, ValueError:
            LOGGER.debug("Invalid 'scale' %r; skipping", transform.get("scale"))

    if "offset" in transform:
        try:
            number = number + float(transform["offset"])
        except TypeError, ValueError:
            LOGGER.debug("Invalid 'offset' %r; skipping", transform.get("offset"))

    if has_round:
        try:
            digits = int(transform["round"])
        except TypeError, ValueError:
            LOGGER.debug("Invalid 'round' %r; skipping", transform.get("round"))
        else:
            number = round(float(number), digits)
            # ``round(x, 0)`` yields a float; normalize a whole number to int so
            # e.g. battery_ok percentage reads as 100 rather than 100.0.
            if digits <= 0 and float(number).is_integer():
                number = int(number)

    return number


def _apply_binary_payload(payload: dict[str, Any], raw_value: Any) -> bool | None:
    """Map a raw value to ``True``/``False`` using a binary ``payload`` mapping.

    Matching is string-based (rtl_433 emits values as strings/numbers and the
    payload tokens are quoted strings). Returns ``None`` when the value matches
    neither token so the caller can decide how to handle the unknown state.
    """
    raw_str = str(raw_value).strip()
    on_token = payload.get("on")
    off_token = payload.get("off")

    if on_token is not None and raw_str == str(on_token):
        return True
    if off_token is not None and raw_str == str(off_token):
        return False

    # Numeric near-equality fallback (e.g. raw 1.0 vs token "1").
    number = _coerce_number(raw_value, as_int=False)
    if number is not None:
        if on_token is not None and _coerce_number(on_token, as_int=False) == number:
            return True
        if off_token is not None and _coerce_number(off_token, as_int=False) == number:
            return False

    LOGGER.debug(
        "Binary value %r matched neither on/off payload %r", raw_value, payload
    )
    return None


def apply_transform(descriptor: FieldDescriptor, raw_value: Any) -> Any:
    """Convert a raw rtl_433 value into the Home-Assistant-facing state.

    * ``binary_sensor`` descriptors: apply the ``payload`` mapping and return a
      ``bool`` (or ``None`` if the value matches neither token, or no payload is
      defined and the value cannot be interpreted as a truthy token).
    * ``sensor`` descriptors: apply ``value_transform`` (coerce -> scale ->
      offset -> round) and return the number; non-numeric values pass through
      unchanged.

    Defensive throughout: invalid transform parameters are logged and skipped
    rather than raising.
    """
    if raw_value is None:
        return None

    if descriptor.platform == "binary_sensor":
        if descriptor.payload:
            return _apply_binary_payload(descriptor.payload, raw_value)
        # No payload defined: best-effort truthy interpretation.
        return str(raw_value).strip().lower() in _TRUE_TOKENS

    transform = descriptor.value_transform
    if not transform:
        return raw_value
    return _apply_sensor_transform(transform, raw_value)
