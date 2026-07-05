"""Tests for the integration's local entity-slug helper ``_safe_token``.

Event normalization itself (``normalize`` / ``device_key`` / ``NormalizedEvent`` /
``DEFAULT_SKIP_KEYS``) now lives in :mod:`pyrtl_433.normalizer` and is tested
upstream in the library; this module only retains :func:`_safe_token`, the
private slug helper the library does not export. The integration relies on it to
keep unique_ids and dispatcher signals byte-identical, so the tests here lock its
slug-stability contract (the adaptation this repo owns) rather than re-testing the
library's normalization.
"""

from __future__ import annotations

from custom_components.rtl_433.normalizer import _safe_token


def test_safe_token_is_deterministic_and_preserves_safe_characters():
    """Alphanumerics plus ``- _ .`` pass through unchanged and stably."""
    assert _safe_token("Acurite-606TX") == "Acurite-606TX"
    assert _safe_token("Acurite-606TX") == _safe_token("Acurite-606TX")
    assert _safe_token("v1.2_beta") == "v1.2_beta"
    # Integer / non-string identity values are stringified first.
    assert _safe_token(42) == "42"


def test_safe_token_collapses_unsafe_characters_to_underscore():
    """Whitespace and ``/`` (unsafe in unique_ids/signals) collapse to ``_``."""
    assert _safe_token("Brand X/Model 1") == "Brand_X_Model_1"
    # Leading/trailing unsafe runs are stripped, interior runs collapse.
    assert _safe_token("  spaced  ") == "spaced"
    assert _safe_token("a//b") == "a__b"


def test_safe_token_empty_or_all_unsafe_falls_back_to_unknown():
    """An empty or entirely-unsafe token yields the stable ``unknown`` sentinel."""
    assert _safe_token("") == "unknown"
    assert _safe_token("   ") == "unknown"
    assert _safe_token("/ /") == "unknown"
