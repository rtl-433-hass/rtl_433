"""Mutation-killing tests for custom_components/rtl_433/library.py.

All tests target the two functions in library.py:
  - _async_load_library: caching behaviour, executor delegation
  - _merge_entry_library: happy-path merge + exception-fallback branch

The surviving mutants are all in the exception handler of _merge_entry_library.
Tests here pin every observable aspect of that handler:
  - LOGGER.warning is called with the correct message template, entry_id arg,
    and exc_info=True
  - The returned Registry.flat is a shallow copy of shipped_registry.flat
  - The returned Registry.models is a per-model shallow copy of shipped_registry.models
  - The returned skip_keys is a fresh set equal to shipped_skip_keys
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from custom_components.rtl_433.const import CONF_USER_MAPPINGS, DATA_LIBRARY, DOMAIN
from custom_components.rtl_433.library import _async_load_library, _merge_entry_library
from custom_components.rtl_433.mapping import FieldDescriptor, Registry

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_descriptor(field_key: str = "temperature_C") -> FieldDescriptor:
    """Build a minimal FieldDescriptor for testing."""
    return FieldDescriptor(
        field_key=field_key,
        platform="sensor",
        name="Temperature",
        object_suffix="Temperature",
    )


def _make_registry(
    flat_keys: list[str] | None = None,
    model_keys: dict[str, list[str]] | None = None,
) -> Registry:
    """Build a Registry with the given flat keys and optional model overrides."""
    flat: dict[str, FieldDescriptor] = {}
    for key in flat_keys or ["temperature_C", "humidity"]:
        flat[key] = _make_descriptor(key)

    models: dict[str, dict[str, FieldDescriptor]] = {}
    for model, keys in (model_keys or {}).items():
        models[model] = {k: _make_descriptor(k) for k in keys}

    return Registry(flat=flat, models=models)


def _make_entry(
    entry_id: str = "test-hub-entry-id", user_mappings=None, title: str = "Test hub"
):
    """Build a minimal mock ConfigEntry."""
    entry = MagicMock()
    entry.entry_id = entry_id
    entry.title = title
    data = {}
    if user_mappings is not None:
        data[CONF_USER_MAPPINGS] = user_mappings
    entry.data = data
    return entry


def _make_bad_overrides():
    """Return an overrides dict that will cause merge_overrides to raise."""
    # patch merge_overrides to raise instead of using a real bad override
    return {"some_field": "will be ignored because merge_overrides is patched"}


# ---------------------------------------------------------------------------
# _async_load_library — caching
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_load_library_caches_result(hass):
    """Second call to _async_load_library returns the cached tuple (same object)."""
    shipped_registry = _make_registry()
    shipped_skip_keys = {"time", "model"}

    # Patch load_library to return a known result
    with patch(
        "custom_components.rtl_433.library.load_library",
        return_value=(shipped_registry, shipped_skip_keys),
    ) as mock_load:
        result1 = await _async_load_library(hass)
        result2 = await _async_load_library(hass)

    # load_library (synchronous) must be called exactly once even for two awaits
    mock_load.assert_called_once()
    # Both calls return equal results
    assert result1 == result2
    # The second call returns the cached value (same object as what was stored)
    # result2 is the cached tuple from hass.data
    reg1, skip1 = result1
    reg2, skip2 = result2
    assert reg1 == reg2
    assert skip1 == skip2


@pytest.mark.asyncio
async def test_async_load_library_stores_in_hass_data(hass):
    """The result is stored under hass.data[DOMAIN][DATA_LIBRARY]."""
    shipped_registry = _make_registry()
    shipped_skip_keys = {"time"}

    with patch(
        "custom_components.rtl_433.library.load_library",
        return_value=(shipped_registry, shipped_skip_keys),
    ):
        await _async_load_library(hass)

    assert DOMAIN in hass.data
    cached = hass.data[DOMAIN][DATA_LIBRARY]
    assert cached == (shipped_registry, shipped_skip_keys)


@pytest.mark.asyncio
async def test_async_load_library_reuses_existing_cache(hass):
    """If hass.data already has DATA_LIBRARY, load_library is not called."""
    registry = _make_registry()
    skip = {"id"}
    hass.data.setdefault(DOMAIN, {})[DATA_LIBRARY] = (registry, skip)

    with patch("custom_components.rtl_433.library.load_library") as mock_load:
        result = await _async_load_library(hass)

    mock_load.assert_not_called()
    assert result == (registry, skip)


@pytest.mark.asyncio
async def test_async_load_library_returns_correct_types(hass):
    """_async_load_library returns (Registry, set[str])."""
    shipped_registry = _make_registry()
    shipped_skip_keys = {"time", "model"}

    with patch(
        "custom_components.rtl_433.library.load_library",
        return_value=(shipped_registry, shipped_skip_keys),
    ):
        result = await _async_load_library(hass)

    reg, skip = result
    assert isinstance(reg, Registry)
    assert isinstance(skip, set)


# ---------------------------------------------------------------------------
# _merge_entry_library — happy path
# ---------------------------------------------------------------------------


def test_merge_entry_library_happy_path_calls_merge_overrides():
    """On success, merge_overrides is called with the correct arguments."""
    hass = MagicMock()
    shipped_registry = _make_registry()
    shipped_skip_keys = {"time"}
    user_mappings = {
        "humidity": {"platform": "sensor", "name": "H", "object_suffix": "H"}
    }
    entry = _make_entry(user_mappings=user_mappings)

    merged_registry = _make_registry(["humidity"])
    merged_skip = {"time", "extra"}

    with patch(
        "custom_components.rtl_433.library.merge_overrides",
        return_value=(merged_registry, merged_skip),
    ) as mock_merge:
        result = _merge_entry_library(hass, entry, shipped_registry, shipped_skip_keys)

    mock_merge.assert_called_once_with(
        shipped_registry, shipped_skip_keys, user_mappings
    )
    assert result == (merged_registry, merged_skip)


def test_merge_entry_library_no_user_mappings_uses_empty_dict():
    """When CONF_USER_MAPPINGS is absent, merge_overrides receives {} as overrides."""
    hass = MagicMock()
    shipped_registry = _make_registry()
    shipped_skip_keys = set()
    entry = _make_entry()  # no user_mappings

    merged_registry = _make_registry()

    with patch(
        "custom_components.rtl_433.library.merge_overrides",
        return_value=(merged_registry, set()),
    ) as mock_merge:
        _merge_entry_library(hass, entry, shipped_registry, shipped_skip_keys)

    mock_merge.assert_called_once_with(shipped_registry, shipped_skip_keys, {})


def test_merge_entry_library_none_user_mappings_uses_empty_dict():
    """When CONF_USER_MAPPINGS is explicitly None, overrides defaults to {}."""
    hass = MagicMock()
    shipped_registry = _make_registry()
    shipped_skip_keys = set()
    entry = _make_entry(user_mappings=None)

    merged_registry = _make_registry()

    with patch(
        "custom_components.rtl_433.library.merge_overrides",
        return_value=(merged_registry, set()),
    ) as mock_merge:
        _merge_entry_library(hass, entry, shipped_registry, shipped_skip_keys)

    # Should use {} not None
    _args, _kwargs = mock_merge.call_args
    assert _args[2] == {}


# ---------------------------------------------------------------------------
# _merge_entry_library — exception-fallback branch (mutant killers)
# ---------------------------------------------------------------------------


def _call_merge_fallback(
    entry_id: str = "hub-fallback-id",
    flat_keys: list[str] | None = None,
    model_map: dict[str, list[str]] | None = None,
    skip_keys: set[str] | None = None,
):
    """
    Call _merge_entry_library in a way that forces the except branch.

    Patches merge_overrides to raise RuntimeError so the fallback path runs,
    then returns (hass, entry, shipped_registry, shipped_skip_keys, result).
    """
    hass = MagicMock()
    shipped_registry = _make_registry(
        flat_keys=flat_keys or ["temperature_C", "humidity"],
        model_keys=model_map or {},
    )
    shipped_skip_keys = skip_keys if skip_keys is not None else {"time", "model"}
    entry = _make_entry(entry_id=entry_id)

    with patch(
        "custom_components.rtl_433.library.merge_overrides",
        side_effect=RuntimeError("boom"),
    ):
        result = _merge_entry_library(hass, entry, shipped_registry, shipped_skip_keys)

    return hass, entry, shipped_registry, shipped_skip_keys, result


# --- Warning message and arguments (kills mutants 10-19) ---


def test_fallback_logs_warning_called():
    """LOGGER.warning is called when merge_overrides raises."""
    with (
        patch("custom_components.rtl_433.library.LOGGER") as mock_logger,
        patch(
            "custom_components.rtl_433.library.merge_overrides",
            side_effect=RuntimeError("boom"),
        ),
    ):
        hass = MagicMock()
        shipped_registry = _make_registry()
        shipped_skip_keys = {"time"}
        entry = _make_entry(entry_id="entry-abc")
        _merge_entry_library(hass, entry, shipped_registry, shipped_skip_keys)

    mock_logger.warning.assert_called_once()


def test_fallback_warning_message_contains_hub_phrasing():
    """The warning message template starts with 'Failed to merge user mappings for hub'."""
    with (
        patch("custom_components.rtl_433.library.LOGGER") as mock_logger,
        patch(
            "custom_components.rtl_433.library.merge_overrides",
            side_effect=RuntimeError("boom"),
        ),
    ):
        hass = MagicMock()
        shipped_registry = _make_registry()
        entry = _make_entry(entry_id="entry-x")
        _merge_entry_library(hass, entry, shipped_registry, set())

    call_args = mock_logger.warning.call_args
    msg_template = call_args[0][0]
    # The exact template from the source
    assert "Failed to merge user mappings for hub" in msg_template
    assert "using shipped library" in msg_template


def test_fallback_warning_message_exact_start():
    """The warning message template starts exactly with 'Failed' (not 'XX...')."""
    with (
        patch("custom_components.rtl_433.library.LOGGER") as mock_logger,
        patch(
            "custom_components.rtl_433.library.merge_overrides",
            side_effect=RuntimeError("boom"),
        ),
    ):
        hass = MagicMock()
        shipped_registry = _make_registry()
        entry = _make_entry(entry_id="entry-z")
        _merge_entry_library(hass, entry, shipped_registry, set())

    msg_template = mock_logger.warning.call_args[0][0]
    # Must start with 'Failed' — not any prefixed/suffixed version
    assert msg_template.startswith("Failed"), (
        f"Expected message to start with 'Failed', got: {msg_template!r}"
    )


def test_fallback_warning_message_template_not_none():
    """The first positional arg to LOGGER.warning must not be None."""
    with (
        patch("custom_components.rtl_433.library.LOGGER") as mock_logger,
        patch(
            "custom_components.rtl_433.library.merge_overrides",
            side_effect=RuntimeError("boom"),
        ),
    ):
        hass = MagicMock()
        shipped_registry = _make_registry()
        entry = _make_entry(entry_id="entry-y")
        _merge_entry_library(hass, entry, shipped_registry, set())

    msg_template = mock_logger.warning.call_args[0][0]
    assert msg_template is not None
    assert isinstance(msg_template, str)


def test_fallback_warning_passes_hub_title_as_second_arg():
    """The hub title is passed as the second positional arg to LOGGER.warning."""
    title = "My unique hub"
    with (
        patch("custom_components.rtl_433.library.LOGGER") as mock_logger,
        patch(
            "custom_components.rtl_433.library.merge_overrides",
            side_effect=RuntimeError("boom"),
        ),
    ):
        hass = MagicMock()
        shipped_registry = _make_registry()
        entry = _make_entry(title=title)
        _merge_entry_library(hass, entry, shipped_registry, set())

    positional_args = mock_logger.warning.call_args[0]
    # positional_args[0] = message template, [1] = hub title
    assert positional_args[1] == title


def test_fallback_warning_hub_title_not_none():
    """The hub-title arg passed to LOGGER.warning is not None."""
    with (
        patch("custom_components.rtl_433.library.LOGGER") as mock_logger,
        patch(
            "custom_components.rtl_433.library.merge_overrides",
            side_effect=RuntimeError("boom"),
        ),
    ):
        hass = MagicMock()
        shipped_registry = _make_registry()
        entry = _make_entry(title="Real hub")
        _merge_entry_library(hass, entry, shipped_registry, set())

    positional_args = mock_logger.warning.call_args[0]
    assert positional_args[1] is not None


def test_fallback_warning_exc_info_is_true():
    """LOGGER.warning is called with exc_info=True (not None, not False)."""
    with (
        patch("custom_components.rtl_433.library.LOGGER") as mock_logger,
        patch(
            "custom_components.rtl_433.library.merge_overrides",
            side_effect=RuntimeError("boom"),
        ),
    ):
        hass = MagicMock()
        shipped_registry = _make_registry()
        entry = _make_entry(entry_id="e1")
        _merge_entry_library(hass, entry, shipped_registry, set())

    kwargs = mock_logger.warning.call_args[1]
    assert "exc_info" in kwargs
    assert kwargs["exc_info"] is True


def test_fallback_warning_exc_info_not_false():
    """Explicitly verifies exc_info is not False (mutant_19 kills this)."""
    with (
        patch("custom_components.rtl_433.library.LOGGER") as mock_logger,
        patch(
            "custom_components.rtl_433.library.merge_overrides",
            side_effect=RuntimeError("boom"),
        ),
    ):
        hass = MagicMock()
        shipped_registry = _make_registry()
        entry = _make_entry(entry_id="e2")
        _merge_entry_library(hass, entry, shipped_registry, set())

    kwargs = mock_logger.warning.call_args[1]
    assert kwargs.get("exc_info") != False  # noqa: E712
    assert kwargs.get("exc_info") != None  # noqa: E711


def test_fallback_warning_exc_info_not_none():
    """Explicitly verifies exc_info is not None (mutant_12 kills this)."""
    with (
        patch("custom_components.rtl_433.library.LOGGER") as mock_logger,
        patch(
            "custom_components.rtl_433.library.merge_overrides",
            side_effect=RuntimeError("boom"),
        ),
    ):
        hass = MagicMock()
        shipped_registry = _make_registry()
        entry = _make_entry(entry_id="e3")
        _merge_entry_library(hass, entry, shipped_registry, set())

    kwargs = mock_logger.warning.call_args[1]
    assert kwargs.get("exc_info") is True


# --- Fallback return value: Registry.flat (kills mutants 20, 22, 24) ---


def test_fallback_returns_registry_with_correct_flat_keys():
    """The fallback Registry.flat has the same keys as shipped_registry.flat."""
    _, _, shipped_registry, _, (reg, _) = _call_merge_fallback(
        flat_keys=["temperature_C", "humidity", "wind_avg_km_h"]
    )
    assert set(reg.flat.keys()) == set(shipped_registry.flat.keys())


def test_fallback_returns_registry_flat_not_none():
    """Registry.flat in the fallback return is not None."""
    _, _, _, _, (reg, _) = _call_merge_fallback()
    assert reg.flat is not None


def test_fallback_returns_registry_flat_same_values():
    """The fallback Registry.flat values match shipped_registry.flat values."""
    _, _, shipped_registry, _, (reg, _) = _call_merge_fallback(
        flat_keys=["temperature_C", "humidity"]
    )
    assert reg.flat["temperature_C"] == shipped_registry.flat["temperature_C"]
    assert reg.flat["humidity"] == shipped_registry.flat["humidity"]


def test_fallback_registry_flat_is_a_copy_not_same_object():
    """The fallback Registry.flat is a new dict (not the same object)."""
    _, _, shipped_registry, _, (reg, _) = _call_merge_fallback()
    # It must be a copy — mutations to one don't affect the other
    assert reg.flat is not shipped_registry.flat


def test_fallback_returns_registry_flat_as_dict():
    """Registry.flat in the fallback return is a dict."""
    _, _, _, _, (reg, _) = _call_merge_fallback()
    assert isinstance(reg.flat, dict)


# --- Fallback return value: Registry.models (kills mutants 21, 23, 25) ---


def test_fallback_returns_registry_with_correct_model_keys():
    """The fallback Registry.models has the same model names as shipped."""
    _, _, shipped_registry, _, (reg, _) = _call_merge_fallback(
        model_map={"Acurite-606TX": ["temperature_C", "humidity"]}
    )
    assert set(reg.models.keys()) == set(shipped_registry.models.keys())


def test_fallback_returns_registry_models_not_none():
    """Registry.models in the fallback return is not None."""
    _, _, _, _, (reg, _) = _call_merge_fallback()
    assert reg.models is not None


def test_fallback_registry_models_is_dict():
    """Registry.models in the fallback return is a dict."""
    _, _, _, _, (reg, _) = _call_merge_fallback()
    assert isinstance(reg.models, dict)


def test_fallback_returns_registry_models_same_field_values():
    """The fallback Registry.models entries match shipped values."""
    model = "Acurite-606TX"
    _, _, shipped_registry, _, (reg, _) = _call_merge_fallback(
        model_map={model: ["temperature_C"]}
    )
    assert model in reg.models
    assert (
        reg.models[model]["temperature_C"]
        == shipped_registry.models[model]["temperature_C"]
    )


def test_fallback_registry_models_are_per_model_copies():
    """Each per-model dict in the fallback Registry.models is a new dict."""
    model = "Acurite-606TX"
    _, _, shipped_registry, _, (reg, _) = _call_merge_fallback(
        model_map={model: ["temperature_C"]}
    )
    # The per-model sub-dict should be a copy, not the same object
    assert reg.models[model] is not shipped_registry.models[model]


def test_fallback_registry_models_entries_are_dicts():
    """Each model entry in the fallback Registry.models is a dict (not None)."""
    model = "Bresser-3CH"
    _, _, _, _, (reg, _) = _call_merge_fallback(
        model_map={model: ["temperature_C", "humidity"]}
    )
    assert isinstance(reg.models[model], dict)
    # Each value is not None
    for v in reg.models[model].values():
        assert v is not None


# --- Fallback return value: skip_keys (kills mutant 26) ---


def test_fallback_returns_correct_skip_keys():
    """The fallback skip_keys equals the shipped_skip_keys."""
    shipped_skip = {"time", "model", "id"}
    _, _, _, _, (_, skip) = _call_merge_fallback(skip_keys=shipped_skip)
    assert skip == shipped_skip


def test_fallback_skip_keys_not_none():
    """The fallback skip_keys is not None."""
    _, _, _, _, (_, skip) = _call_merge_fallback(skip_keys={"time"})
    assert skip is not None


def test_fallback_skip_keys_is_set():
    """The fallback skip_keys is a set."""
    _, _, _, _, (_, skip) = _call_merge_fallback()
    assert isinstance(skip, set)


def test_fallback_skip_keys_is_a_copy():
    """The fallback skip_keys is a new set, not the same object as shipped."""
    shipped_skip = {"time", "model"}
    _, _, _, shipped_skip_keys, (_, skip) = _call_merge_fallback(skip_keys=shipped_skip)
    assert skip is not shipped_skip_keys


def test_fallback_skip_keys_contains_all_shipped_keys():
    """Every key in shipped_skip_keys appears in the fallback skip_keys."""
    shipped = {"time", "model", "id", "channel"}
    _, _, _, _, (_, skip) = _call_merge_fallback(skip_keys=shipped)
    for key in shipped:
        assert key in skip


def test_fallback_skip_keys_empty_when_shipped_empty():
    """When shipped_skip_keys is empty, the fallback returns an empty set."""
    _, _, _, _, (_, skip) = _call_merge_fallback(skip_keys=set())
    assert skip == set()


# --- Fallback return structure: overall tuple ---


def test_fallback_returns_tuple_of_registry_and_set():
    """_merge_entry_library fallback returns (Registry, set)."""
    _, _, _, _, result = _call_merge_fallback()
    assert isinstance(result, tuple)
    assert len(result) == 2
    reg, skip = result
    assert isinstance(reg, Registry)
    assert isinstance(skip, set)


# --- Verify the happy path vs fallback distinction ---


def test_fallback_is_not_same_as_shipped_object():
    """The fallback Registry is a new object (deep-copy, not the shipped one)."""
    _, _, shipped_registry, _, (reg, _) = _call_merge_fallback()
    assert reg is not shipped_registry


def test_fallback_triggered_on_any_exception():
    """The fallback path runs for any exception type, not just specific ones."""
    for exc_type in [RuntimeError, ValueError, KeyError, TypeError, Exception]:
        hass = MagicMock()
        shipped_registry = _make_registry()
        entry = _make_entry()
        with patch(
            "custom_components.rtl_433.library.merge_overrides",
            side_effect=exc_type("test error"),
        ):
            result = _merge_entry_library(hass, entry, shipped_registry, {"time"})
        reg, skip = result
        assert isinstance(reg, Registry)
        assert skip == {"time"}
