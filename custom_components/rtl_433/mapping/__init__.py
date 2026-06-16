"""Device-library mapping package for the rtl_433 integration.

A thin, dependency-light loader + query layer that parses the themed YAML files
under ``device_library/`` into an in-memory registry of :class:`FieldDescriptor`
objects keyed by rtl_433 field name, loads the ``_skip_keys.yaml`` exclusion
list, optionally layers a per-installation user-override file on top, and
resolves / transforms field values for the entity platforms.

The implementation is split by concern; this package module is the public facade
that re-exports the surface and owns the cached default-library accessor and the
query helpers:

* ``_model`` -- :class:`FieldDescriptor` / :class:`Registry`, reserved key names,
  and payload canonicalization (the leaf every other module depends on).
* ``_loader`` -- YAML parsing into a registry, skip-key loading, and override
  merging (:func:`load_library`, :func:`merge_overrides`).
* ``_overrides`` -- pure override validation / normalization
  (:func:`validate_user_mappings`, :func:`normalize_overrides`).
* ``_transform`` -- raw-value to HA-state conversion (:func:`apply_transform`).

The ``registry`` returned by :func:`load_library` is a :class:`Registry`: a flat
``{field_key: FieldDescriptor}`` table (the global default) plus an optional
model-scoped ``{model: {field_key: FieldDescriptor}}`` table. :func:`lookup`
resolves the model-scoped entry for ``(model, field_key)`` first, then the global
flat entry.

File I/O is synchronous. Async callers (the integration setup) must run
:func:`load_library` off the event loop via ``hass.async_add_executor_job``
because it touches the filesystem.

The YAML schema is documented in ``docs/device-library.md``.
"""

from __future__ import annotations

from ._loader import (
    _descriptor_from_entry as _descriptor_from_entry,  # re-export for tests
    _extract_skip_keys as _extract_skip_keys,  # re-export for tests
    _load_descriptor_file as _load_descriptor_file,  # re-export for tests
    _parse_models_block as _parse_models_block,  # re-export for tests
    load_library,
    merge_overrides as merge_overrides,  # re-export
)
from ._model import (
    USER_OVERRIDE_FILENAME as USER_OVERRIDE_FILENAME,  # re-export
    FieldDescriptor,
    Registry,
    _normalize_payload as _normalize_payload,  # re-export for tests
)
from ._overrides import (
    _validate_entry as _validate_entry,  # re-export for tests
    normalize_overrides as normalize_overrides,  # re-export
    validate_user_mappings as validate_user_mappings,  # re-export
)
from ._transform import (
    _apply_binary_payload as _apply_binary_payload,  # re-export for tests
    _apply_sensor_transform as _apply_sensor_transform,  # re-export for tests
    _coerce_number as _coerce_number,  # re-export for tests
    apply_transform as apply_transform,  # re-export
)

__all__ = [
    "FieldDescriptor",
    "Registry",
    "USER_OVERRIDE_FILENAME",
    "apply_transform",
    "event_driven_field_keys",
    "load_library",
    "lookup",
    "merge_overrides",
    "normalize_overrides",
    "should_skip",
    "validate_user_mappings",
]

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


def event_driven_field_keys(registry: Registry | None = None) -> frozenset[str]:
    """Return the rtl_433 field keys that mark a device as *event-driven*.

    A field is event-driven when its descriptor either uses ``platform: event``
    (momentary transmissions — buttons, doorbells) or sets ``event_driven: true``
    (state fields that transmit only on a change — door/contact/motion). Such
    devices have no periodic check-in, so a silence-based timeout would
    eventually misfire; the coordinator maps this set onto the never-expire
    availability default (see :func:`const.class_default_timeout`).

    The set is derived from the *active* registry — both the global ``flat``
    table and every model-scoped overlay — so it stays in sync with the shipped
    library and any per-hub user mappings. ``registry`` defaults to the cached
    shipped library.
    """
    if registry is None:
        registry, _ = _default_library()

    def _is_event_driven(descriptor: FieldDescriptor) -> bool:
        return descriptor.platform == "event" or descriptor.event_driven

    keys = {key for key, desc in registry.flat.items() if _is_event_driven(desc)}
    for scoped in registry.models.values():
        keys.update(key for key, desc in scoped.items() if _is_event_driven(desc))
    return frozenset(keys)


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
