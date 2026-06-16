"""User-override validation and normalization for the mapping package.

Pure helpers (no I/O) used by the options flow and the loader: validate a
user-supplied mapping object into a list of self-contained problem strings, and
produce a deep-copied, JSON-serialisable, payload-canonical copy for storage.
Both deliberately mirror :func:`._loader._descriptor_from_entry`'s tolerance so
the "validator accepts => merge keeps it" invariant holds.
"""

from __future__ import annotations

import copy
from typing import Any

from ._model import (
    _REQUIRED_ENTRY_ATTRS,
    _SUPPORTED_PLATFORMS,
    MODELS_FIELD,
    SKIP_KEYS_FIELD,
    _normalize_payload,
)


def _validate_entry(field_key: str, entry: Any) -> list[str]:
    """Validate one field entry, returning self-contained problem strings.

    Mirrors what :func:`._loader._descriptor_from_entry` will accept so the
    "validator accepts => merge keeps it" invariant holds: the entry must be a
    mapping, must supply the required ``platform``/``object_suffix`` attributes
    (``name`` is optional), and any ``platform`` it does supply must be
    supported. Unknown extra attributes are tolerated (the descriptor builder
    ignores them). Each returned problem is prefixed with ``<field_key>: `` so
    the caller can present it without further context.
    """
    if not isinstance(entry, dict):
        return [f"{field_key}: must be a mapping"]

    problems: list[str] = []
    for attr in _REQUIRED_ENTRY_ATTRS:
        value = entry.get(attr)
        if value is None or (isinstance(value, str) and not value.strip()):
            problems.append(f"{field_key}: missing required {attr!r}")

    platform = entry.get("platform")
    if platform and platform not in _SUPPORTED_PLATFORMS:
        problems.append(
            f"{field_key}: unknown platform {platform!r} "
            "(expected one of sensor, binary_sensor, event)"
        )

    return problems


def _validate_models_block(value: Any) -> list[str]:
    """Validate a reserved ``models:`` block, returning per-entry problems.

    The block must be a mapping of ``model -> {field_key -> entry}``; a
    non-mapping block, or a non-mapping per-model entry, yields one problem and
    skips deeper validation of that part.
    """
    if not isinstance(value, dict):
        return ["models: must be a mapping"]

    problems: list[str] = []
    for model, entries in value.items():
        if not isinstance(entries, dict):
            problems.append(f"models.{model}: must be a mapping")
            continue
        for field_key, entry in entries.items():
            problems.extend(_validate_entry(f"models.{model}.{field_key}", entry))
    return problems


def validate_user_mappings(data: Any) -> list[str]:
    """Validate a user-supplied mapping object, returning per-entry problems.

    Pure: never raises and never mutates ``data``. An empty list means the object
    is valid. ``None`` (an empty mapping) is valid. A non-mapping top level yields
    a single problem. The reserved ``skip_keys`` key must be a list; the reserved
    ``models`` key must be a mapping of ``model -> {field_key -> entry}``; every
    other key is a flat field entry. Each problem string is self-contained (it
    names the offending field/path) so the options step can join them for a form
    error.
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
            problems.extend(_validate_models_block(value))
            continue
        problems.extend(_validate_entry(str(key), value))

    return problems


def _normalize_models_payloads(models_block: Any) -> None:
    """Canonicalize every ``payload`` inside a ``models:`` block, in place.

    Walks ``{model: {field_key: entry}}`` and rewrites each entry's ``payload``
    via :func:`._model._normalize_payload`; non-mapping blocks/entries are left
    untouched.
    """
    if not isinstance(models_block, dict):
        return
    for entries in models_block.values():
        if not isinstance(entries, dict):
            continue
        for entry in entries.values():
            if isinstance(entry, dict) and "payload" in entry:
                entry["payload"] = _normalize_payload(entry["payload"])


def normalize_overrides(data: Any) -> dict[str, Any]:
    """Return a deep-copied, JSON-serialisable, payload-canonical override dict.

    Pure: never mutates ``data``. Returns ``{}`` for any non-mapping input. Every
    flat entry and every ``models.<model>.<field_key>`` entry that carries a
    ``payload`` has it rewritten via :func:`._model._normalize_payload`, so
    boolean keys parsed from YAML (``True``/``False``) and bare ``on``/``off``
    keys become the canonical string keys ``"on"``/``"off"``. This guarantees the
    result has no non-string dict keys and is safe to JSON-serialise for storage.
    The ``skip_keys`` list and the ``models`` block structure are preserved.
    """
    if not isinstance(data, dict):
        return {}

    result: dict[str, Any] = copy.deepcopy(data)

    for key, value in result.items():
        if key == SKIP_KEYS_FIELD:
            continue
        if key == MODELS_FIELD:
            _normalize_models_payloads(value)
            continue
        if isinstance(value, dict) and "payload" in value:
            value["payload"] = _normalize_payload(value["payload"])

    return result
