"""Entity-slug helper for the rtl_433 integration.

The event normalizer itself (``normalize``, ``device_key``, ``NormalizedEvent``,
``DEFAULT_SKIP_KEYS``) now lives in :mod:`pyrtl_433.normalizer`; consumers import
those directly from the library. This module retains only :func:`_safe_token`,
the private entity-slug helper the library does not export, so entity ids and
dispatcher signals remain byte-identical.
"""

from __future__ import annotations

from typing import Any


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
