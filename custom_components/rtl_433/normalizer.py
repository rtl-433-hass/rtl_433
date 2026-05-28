"""Event normalization for the rtl_433 integration.

An rtl_433 WebSocket event is a flat JSON object: a ``model`` string plus some
combination of identity keys (``id`` / ``channel`` / ``subtype``) and a variable
set of measurement fields. Normalization derives a *deterministic, stable*
device key from the identity keys and separates measurement fields from
identity/skip fields.

This module is deliberately decoupled from the mapping library and the
config flow: it imports nothing from the rest of the integration except
the constants it shares, and ``normalize`` takes the ``skip_keys`` set as a
parameter so the caller (the coordinator, wired in ``__init__.py``) injects the loaded
skip-keys rather than this module importing the loader.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final

# Identity keys, in the order they contribute to the device key. ``model`` is
# always the prefix when present; ``id`` / ``channel`` / ``subtype`` are appended
# only when present, so the same physical device always yields the same key.
IDENTITY_KEYS: Final[tuple[str, ...]] = ("model", "id", "channel", "subtype")

# Minimal default skip-set used when the caller injects nothing. The real
# skip-keys come from the mapping library at runtime; this fallback
# keeps the normalizer usable standalone (e.g. in tests) without that loader.
DEFAULT_SKIP_KEYS: Final[frozenset[str]] = frozenset(
    {"model", "id", "channel", "subtype", "time"}
)


@dataclass(frozen=True, slots=True)
class NormalizedEvent:
    """A single rtl_433 event split into identity metadata and measurements.

    Attributes:
        device_key: Deterministic, HA-safe key identifying the physical device.
        model: The rtl_433 ``model`` string (empty if the event lacks one).
        identity: The present identity keys and their raw values.
        fields: Measurement field name -> value, with identity and skip-keys
            removed. This is what the entity platforms map to readings.
        is_replay: Whether the coordinator classified this frame as a replayed or
            stale gap event (vs a live transmission). Carried on the event object
            so the dispatch needs no extra signature: replays seed sensor values
            but ``Rtl433Event`` must not fire / persist for them. The coordinator
            stamps this (via :func:`dataclasses.replace`) after ``normalize`` runs;
            a frame produced by ``normalize`` alone is live (the default).
        event_time: The parsed event timestamp (UTC) when one was usable, else
            ``None``. Carried alongside ``is_replay`` so the event platform can log
            the suppressed transmission's time/age at INFO; ``None`` when the raw
            ``time`` was missing/blank/unparseable (such a frame is treated live).
    """

    device_key: str
    model: str
    identity: dict[str, Any] = field(default_factory=dict)
    fields: dict[str, Any] = field(default_factory=dict)
    is_replay: bool = False
    event_time: datetime | None = None


def _safe_token(value: Any) -> str:
    """Return an HA-safe token for an identity value.

    Keeps the token deterministic and human-readable: only characters that are
    unsafe in unique_ids / dispatcher signals (whitespace and ``/``) are
    collapsed to underscores. The same input always produces the same token.
    """
    text = str(value).strip()
    out: list[str] = []
    for ch in text:
        if ch.isalnum() or ch in ("-", "_", "."):
            out.append(ch)
        else:
            out.append("_")
    token = "".join(out).strip("_")
    return token or "unknown"


def device_key(event: dict[str, Any]) -> str:
    """Derive a deterministic, stable device key from an event's identity keys.

    The key is built from the present subset of ``IDENTITY_KEYS``:

    - ``model``     -> the model token (the prefix when present)
    - ``id``        -> ``-<id>``
    - ``channel``   -> ``-ch<channel>``
    - ``subtype``   -> ``-st<subtype>``

    Examples:
        ``{"model": "Acurite-606TX", "id": 42}``        -> ``Acurite-606TX-42``
        ``{"model": "Foo", "channel": 3}``              -> ``Foo-ch3``
        ``{"model": "Foo", "id": 5, "subtype": 2}``     -> ``Foo-5-st2``
        ``{"channel": 1}`` (no model)                   -> ``unknown-ch1``
        ``{}``                                          -> ``unknown``

    Missing ``id`` (channel-only / model-only devices) is handled without
    crashing, and the same identity-field combination always yields the same
    key, so the key is safe to use in unique_ids and dispatcher signals.
    """
    parts: list[str] = []

    model = event.get("model")
    parts.append(_safe_token(model) if model is not None else "unknown")

    if (raw_id := event.get("id")) is not None:
        parts.append(_safe_token(raw_id))
    if (channel := event.get("channel")) is not None:
        parts.append(f"ch{_safe_token(channel)}")
    if (subtype := event.get("subtype")) is not None:
        parts.append(f"st{_safe_token(subtype)}")

    return "-".join(parts)


def normalize(
    event: dict[str, Any], skip_keys: set[str] | frozenset[str] | None = None
) -> NormalizedEvent:
    """Split an rtl_433 event into a ``NormalizedEvent``.

    Args:
        event: The raw decoded JSON event (a flat dict).
        skip_keys: Keys to exclude from ``fields`` in addition to the identity
            keys. When ``None``, ``DEFAULT_SKIP_KEYS`` is used. The coordinator
            injects the mapping library's skip-keys here (in ``__init__.py``).

    Returns:
        A ``NormalizedEvent`` whose ``fields`` contains only measurement data
        (identity keys and skip-keys removed).
    """
    if skip_keys is None:
        skip_keys = DEFAULT_SKIP_KEYS

    key = device_key(event)
    model = event.get("model")

    identity: dict[str, Any] = {
        ik: event[ik] for ik in IDENTITY_KEYS if event.get(ik) is not None
    }

    excluded = set(IDENTITY_KEYS) | set(skip_keys)
    fields = {k: v for k, v in event.items() if k not in excluded}

    return NormalizedEvent(
        device_key=key,
        model=str(model) if model is not None else "",
        identity=identity,
        fields=fields,
    )
