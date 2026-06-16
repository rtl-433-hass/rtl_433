"""Data model for the device-library mapping package.

Defines the immutable descriptor / registry dataclasses, the reserved YAML key
names, the attribute sets the loader and validator key off, and the payload
canonicalization shared by the loader and the override normalizer.

This is the package's leaf module: ``_loader``, ``_overrides``, and
``_transform`` all depend on it and nothing here imports back from them.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any

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


@dataclass(frozen=True)
class FieldDescriptor:
    """Immutable description of how one rtl_433 field maps to an HA entity.

    Field names mirror the YAML attributes documented in
    ``docs/device-library.md``. ``field_key`` is the rtl_433 JSON key (the
    top-level YAML key); the remaining attributes are the entity descriptor.
    """

    field_key: str
    platform: str
    name: str | None
    object_suffix: str
    device_class: str | None = None
    unit_of_measurement: str | None = None
    state_class: str | None = None
    value_transform: dict[str, Any] | None = None
    payload: dict[str, Any] | None = None
    event_map: dict[str, str] | None = None
    force_update: bool = False
    entity_category: str | None = None
    enabled_by_default: bool = True
    icon: str | None = None
    clear_delay: int | None = None
    event_driven: bool = False


# Attribute names the descriptor accepts from a YAML entry (everything except
# ``field_key``, which is supplied positionally from the top-level key).
_DESCRIPTOR_ATTRS = frozenset(
    f.name for f in fields(FieldDescriptor) if f.name != "field_key"
)

# Entity platforms the shipped library uses; the UI validator rejects any
# ``platform`` value outside this set. Confirmed by the ``platform:`` values in
# ``device_library/*.yaml``.
_SUPPORTED_PLATFORMS = frozenset({"sensor", "binary_sensor", "event"})

# Entry attributes a user mapping must supply for a valid descriptor. ``name`` is
# optional: omit it (or set it to null) to let Home Assistant derive the entity
# name from ``device_class``.
_REQUIRED_ENTRY_ATTRS = ("platform", "object_suffix")


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


def _normalize_payload(payload: Any) -> dict[str, Any] | None:
    """Normalize a binary ``payload`` mapping to string ``on``/``off`` keys.

    PyYAML (YAML 1.1) parses the *unquoted* bare keys ``on`` / ``off`` as the
    booleans ``True`` / ``False``, so ``{ on: "1", off: "0" }`` in the library
    files arrives as ``{True: "1", False: "0"}``. This rewrites those (and any
    already-string variants) back to canonical ``{"on": ..., "off": ...}`` so
    downstream consumers and :func:`apply_transform` see a stable shape.

    Shared by the loader (:func:`_descriptor_from_entry`) and the override
    normalizer (:func:`normalize_overrides`).
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
