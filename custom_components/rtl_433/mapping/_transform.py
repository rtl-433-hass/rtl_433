"""Value transformation for the mapping package.

Turns a raw rtl_433 value into the Home-Assistant-facing state: binary
``payload`` mapping for ``binary_sensor`` descriptors, and the ``value_transform``
pipeline (coerce -> scale -> offset -> round) for ``sensor`` descriptors.
Defensive throughout: invalid transform parameters are logged and skipped rather
than raising.
"""

from __future__ import annotations

import operator
from typing import Any

from ..const import LOGGER
from ._model import FieldDescriptor

# Truthy raw strings used by binary payloads / coercion fallbacks.
_TRUE_TOKENS = frozenset({"1", "true", "on", "yes"})


def _coerce_number(raw_value: Any, *, as_int: bool) -> float | int | None:
    """Coerce ``raw_value`` to ``int``/``float``; return ``None`` on failure."""
    try:
        number = float(raw_value)
    except TypeError, ValueError:
        return None
    return int(number) if as_int else number


def _apply_factor(
    number: float | int,
    transform: dict[str, Any],
    key: str,
    combine: Any,
) -> float | int:
    """Apply one numeric ``value_transform`` step (``scale``/``offset``).

    ``combine`` is ``operator.mul`` for ``scale`` or ``operator.add`` for
    ``offset``. Returns ``number`` unchanged when the key is absent or its value
    is not a valid float (the bad value is logged and skipped, matching the rest
    of the pipeline's defensiveness).
    """
    if key not in transform:
        return number
    try:
        return combine(number, float(transform[key]))
    except TypeError, ValueError:
        LOGGER.debug("Invalid %r %r; skipping", key, transform.get(key))
        return number


def _apply_round(number: float | int, transform: dict[str, Any]) -> float | int:
    """Apply the ``round`` step, if present.

    ``round(x, 0)`` yields a float; a whole number rounded to <= 0 digits is
    normalized back to ``int`` so e.g. a battery_ok percentage reads as 100
    rather than 100.0. An invalid ``round`` value is logged and skipped.
    """
    if "round" not in transform:
        return number
    try:
        digits = int(transform["round"])
    except TypeError, ValueError:
        LOGGER.debug("Invalid 'round' %r; skipping", transform.get("round"))
        return number
    number = round(float(number), digits)
    if digits <= 0 and float(number).is_integer():
        number = int(number)
    return number


def _apply_sensor_transform(transform: dict[str, Any], raw_value: Any) -> Any:
    """Apply a ``value_transform`` mapping to a sensor's raw value.

    Application order matches ``docs/device-library.md``: coerce (``float`` /
    ``int``, with ``round`` / ``scale`` / ``offset`` implying float) -> ``scale``
    -> ``offset`` -> ``round``. Returns ``raw_value`` unchanged if it is not
    numeric so non-numeric/None readings pass through untouched.
    """
    # ``round``/``scale``/``offset`` all imply float arithmetic; ``int`` forces
    # integer coercion only when no float-implying key overrides it.
    needs_float = (
        "scale" in transform
        or "offset" in transform
        or "round" in transform
        or bool(transform.get("float"))
    )
    as_int = bool(transform.get("int")) and not needs_float

    number = _coerce_number(raw_value, as_int=as_int)
    if number is None:
        # Non-numeric value: nothing to transform, hand it back as-is.
        return raw_value

    number = _apply_factor(number, transform, "scale", operator.mul)
    number = _apply_factor(number, transform, "offset", operator.add)
    return _apply_round(number, transform)


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
