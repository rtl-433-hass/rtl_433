"""Tests for rtl_433 event normalization.

Covers deterministic/stable device-key derivation, the missing-``id``
(channel-only / model-only) cases, and the measurement-vs-skip field split.
"""

from __future__ import annotations

from custom_components.rtl_433.normalizer import device_key, normalize


def test_device_key_is_deterministic_and_stable():
    """The same identity fields always produce the same key, in id/ch/st order."""
    event = {"model": "Acurite-606TX", "id": 42, "temperature_C": 21.0}
    assert device_key(event) == "Acurite-606TX-42"
    # Stable across calls and insensitive to measurement-field ordering.
    assert device_key(event) == device_key(
        {"temperature_C": 99.0, "id": 42, "model": "Acurite-606TX"}
    )

    assert device_key({"model": "Foo", "channel": 3}) == "Foo-ch3"
    assert device_key({"model": "Foo", "id": 5, "subtype": 2}) == "Foo-5-st2"


def test_device_key_handles_missing_id():
    """Channel-only and model-only / empty events do not crash and stay stable."""
    assert device_key({"channel": 1}) == "unknown-ch1"
    assert device_key({"model": "OnlyModel"}) == "OnlyModel"
    assert device_key({}) == "unknown"


def test_device_key_sanitizes_unsafe_characters():
    """Whitespace and ``/`` collapse to underscores; the result stays HA-safe."""
    key = device_key({"model": "Brand X/Model 1", "id": 7})
    assert key == "Brand_X_Model_1-7"


def test_normalize_separates_measurement_from_skip():
    """Identity and skip keys are removed; only measurements remain in fields."""
    event = {
        "time": "2026-05-25 10:00:00",
        "model": "Acurite-606TX",
        "id": 42,
        "mic": "CRC",
        "temperature_C": 21.4,
        "humidity": 55,
    }
    skip = {"time", "mic", "model", "id", "channel", "subtype"}
    normalized = normalize(event, skip)

    assert normalized.device_key == "Acurite-606TX-42"
    assert normalized.model == "Acurite-606TX"
    assert normalized.identity == {"model": "Acurite-606TX", "id": 42}
    assert normalized.fields == {"temperature_C": 21.4, "humidity": 55}
    # Skip / identity keys never leak into the measurement fields.
    assert "time" not in normalized.fields
    assert "mic" not in normalized.fields
    assert "model" not in normalized.fields


def test_normalize_uses_default_skip_keys_when_none():
    """With no skip set injected, the built-in default identity set applies."""
    normalized = normalize({"model": "Foo", "id": 1, "temperature_C": 10.0})
    assert normalized.fields == {"temperature_C": 10.0}
    assert normalized.model == "Foo"


def test_normalize_model_only_event():
    """A model-only event normalizes with an empty identity-id and a stable key."""
    normalized = normalize({"channel": 3, "temperature_C": 19.0}, {"channel"})
    assert normalized.device_key == "unknown-ch3"
    assert normalized.model == ""
    assert normalized.fields == {"temperature_C": 19.0}
