"""YAML device-library loader for the mapping package.

Parses the themed ``device_library/*.yaml`` files into a :class:`Registry`
(flat table + model-scoped overlay), loads ``_skip_keys.yaml``, and merges a
per-installation override mapping on top. Every entry point is defensive: a
malformed file, model, or entry is logged and skipped rather than aborting the
whole load.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from ..const import LOGGER
from ._model import (
    _DESCRIPTOR_ATTRS,
    MODELS_FIELD,
    SKIP_KEYS_FIELD,
    FieldDescriptor,
    Registry,
    _normalize_payload,
)

# Directory holding the shipped library, resolved relative to the package so the
# loader works regardless of the installed location (``.parent`` is the package
# dir, ``.parent.parent`` the integration root that holds ``device_library/``).
_LIBRARY_DIR = Path(__file__).parent.parent / "device_library"


def _coerce_event_map(known: dict[str, Any], field_key: str) -> None:
    """Coerce a valid ``event_map`` to a ``str``->``str`` map, or drop it.

    rtl_433 emits raw values as strings/numbers, so both keys and values are
    coerced to ``str`` to keep the entity lookup value-stable (mirroring the
    ``payload`` string normalization). A non-mapping ``event_map`` is logged and
    dropped.
    """
    if "event_map" not in known:
        return
    raw = known["event_map"]
    if not isinstance(raw, dict):
        LOGGER.debug("Ignoring invalid 'event_map' %r on field %r", raw, field_key)
        known.pop("event_map")
    else:
        known["event_map"] = {str(k): str(v) for k, v in raw.items()}


def _drop_invalid_clear_delay(known: dict[str, Any], field_key: str) -> None:
    """Drop a ``clear_delay`` that is not a positive (non-bool) integer."""
    if "clear_delay" not in known:
        return
    raw = known["clear_delay"]
    if not isinstance(raw, int) or isinstance(raw, bool) or raw <= 0:
        LOGGER.debug("Ignoring invalid 'clear_delay' %r on field %r", raw, field_key)
        known.pop("clear_delay")


def _drop_invalid_event_driven(known: dict[str, Any], field_key: str) -> None:
    """Drop an ``event_driven`` value that is not a ``bool``."""
    if "event_driven" in known and not isinstance(known["event_driven"], bool):
        LOGGER.debug(
            "Ignoring invalid 'event_driven' %r on field %r",
            known["event_driven"],
            field_key,
        )
        known.pop("event_driven")


def _sanitize_entry_attrs(known: dict[str, Any], field_key: str) -> None:
    """Coerce or drop the descriptor attributes that need validation, in place.

    Mutates ``known`` (the recognised subset of a YAML entry): canonicalizes the
    ``payload`` keys, coerces ``event_map`` to a ``str``->``str`` map, and drops
    ``clear_delay`` / ``event_driven`` values that fail their type checks. Each
    attribute's rule lives in its own helper; everything left untouched passes
    straight to :class:`FieldDescriptor`.
    """
    if "payload" in known:
        known["payload"] = _normalize_payload(known["payload"])
    _coerce_event_map(known, field_key)
    _drop_invalid_clear_delay(known, field_key)
    _drop_invalid_event_driven(known, field_key)


def _descriptor_from_entry(field_key: str, entry: dict[str, Any]) -> FieldDescriptor:
    """Build a :class:`FieldDescriptor` from one YAML mapping entry.

    Unknown attributes are ignored (with a debug log) so a newer library file
    that adds keys does not break an older loader. ``platform`` and
    ``object_suffix`` are required; ``name`` is optional (a missing or null
    ``name`` becomes ``None``, so HA derives the entity name from
    ``device_class``). A ``KeyError``/``TypeError`` here is caught by the
    per-file handler in :func:`_load_descriptor_file`.
    """
    if not isinstance(entry, dict):
        raise TypeError(
            f"entry for {field_key!r} is not a mapping: {type(entry).__name__}"
        )

    known = {k: v for k, v in entry.items() if k in _DESCRIPTOR_ATTRS}
    # ``name`` has no dataclass default, so always supply it; absent or null
    # both mean "let HA auto-name from device_class".
    known.setdefault("name", None)
    unknown = set(entry) - _DESCRIPTOR_ATTRS
    if unknown:
        LOGGER.debug(
            "Ignoring unknown attribute(s) %s on field %r", sorted(unknown), field_key
        )

    _sanitize_entry_attrs(known, field_key)
    return FieldDescriptor(field_key=field_key, **known)


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


def _extract_skip_keys(data: Any) -> set[str]:
    """Pull the ``skip_keys`` list out of parsed YAML data, defensively."""
    if not isinstance(data, dict):
        return set()
    raw = data.get(SKIP_KEYS_FIELD)
    if not isinstance(raw, list):
        return set()
    return {str(key) for key in raw}


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


def load_library(
    library_dir: Path | None = None,
) -> tuple[Registry, set[str]]:
    """Load the shipped device library and skip-key list.

    Globs ``<library_dir>/*.yaml`` (default: the packaged ``device_library``
    directory). Files whose name starts with ``_`` are not parsed as mapping
    tables; ``_skip_keys.yaml`` is loaded separately into the skip-key set.

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
