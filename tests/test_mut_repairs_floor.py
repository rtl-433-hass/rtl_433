"""Mutation-floor tests for custom_components/rtl_433/repairs.py.

These tests are specifically crafted to kill surviving mutmut mutants that
the existing test_diagnostics_repairs.py test suite did not catch. They are
organised by the function / class being targeted and assert precise values
(string keys, defaults, exact error codes) that many mutants alter.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from custom_components.rtl_433 import repairs
from custom_components.rtl_433.const import (
    CONF_HOST,
    CONF_PATH,
    CONF_PORT,
    CONF_RADIO_ID,
    DEFAULT_PATH,
    DEFAULT_PORT,
    DOMAIN,
)
from custom_components.rtl_433.coordinator import CannotConnect, Rtl433Coordinator
from homeassistant.components.repairs import ConfirmRepairFlow
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir

VALIDATE = "custom_components.rtl_433.coordinator.Rtl433Coordinator.validate_connection"


# ---------------------------------------------------------------------------
# _sample_rate_looks_low boundary conditions
# ---------------------------------------------------------------------------


class TestSampleRateLooksLow:
    """Test _sample_rate_looks_low against boundary and hopping conditions.

    Kills survivors:
    - mutmut_2: ``> 1`` changed to ``>= 1`` for hopping check
    - mutmut_20: ``>=`` changed to ``>`` for _HIGH_BAND_MIN_HZ boundary
    """

    def test_single_frequency_list_not_hopping(self):
        """A single-element frequencies list should NOT trigger the hopping guard.

        If the hopping check is ``>= 1`` instead of ``> 1`` a list with exactly
        one frequency would falsely suppress the advisory. The correct boundary
        is > 1, so one frequency is NOT treated as hopping.
        """
        looks_low = repairs._sample_rate_looks_low
        # Single frequency in the list: should still be flagged if high-band + low rate.
        assert looks_low(
            {
                "center_frequency": 915_000_000,
                "samp_rate": 250_000,
                "frequencies": [915_000_000],
            }
        )

    def test_two_frequencies_is_hopping(self):
        """Two frequencies means hopping -> not flagged."""
        looks_low = repairs._sample_rate_looks_low
        assert not looks_low(
            {
                "center_frequency": 915_000_000,
                "samp_rate": 250_000,
                "frequencies": [433_920_000, 915_000_000],
            }
        )

    def test_exact_800mhz_boundary_is_flagged(self):
        """Exactly 800 MHz at 250 kHz must be flagged (>= 800 MHz, not > 800 MHz).

        Kills mutmut_20 which changes >= to >.
        """
        looks_low = repairs._sample_rate_looks_low
        assert looks_low({"center_frequency": 800_000_000, "samp_rate": 250_000})

    def test_just_below_800mhz_is_not_flagged(self):
        """Just below 800 MHz should not be flagged."""
        looks_low = repairs._sample_rate_looks_low
        assert not looks_low({"center_frequency": 799_999_999, "samp_rate": 250_000})

    def test_exact_250khz_sample_rate_boundary_is_flagged(self):
        """Exactly 250 kHz sample rate at high band should be flagged (<= 250k)."""
        looks_low = repairs._sample_rate_looks_low
        assert looks_low({"center_frequency": 915_000_000, "samp_rate": 250_000})

    def test_just_above_250khz_is_not_flagged(self):
        """251 kHz sample rate at high band should not be flagged."""
        looks_low = repairs._sample_rate_looks_low
        assert not looks_low({"center_frequency": 915_000_000, "samp_rate": 250_001})

    def test_empty_frequencies_list_is_not_hopping(self):
        """Empty frequencies list should not trigger hopping guard."""
        looks_low = repairs._sample_rate_looks_low
        assert looks_low(
            {
                "center_frequency": 915_000_000,
                "samp_rate": 250_000,
                "frequencies": [],
            }
        )

    def test_bool_rate_is_rejected(self):
        """Bool rate should be rejected (isinstance bool check)."""
        looks_low = repairs._sample_rate_looks_low
        assert not looks_low({"center_frequency": 915_000_000, "samp_rate": True})

    def test_bool_freq_is_rejected(self):
        """Bool freq should be rejected."""
        looks_low = repairs._sample_rate_looks_low
        assert not looks_low({"center_frequency": True, "samp_rate": 250_000})


# ---------------------------------------------------------------------------
# HubRadioReplaceRepairFlow.async_step_confirm — form schema defaults
# ---------------------------------------------------------------------------


class TestHubRadioReplaceFlowFormDefaults:
    """Test that the form schema carries correct defaults from entry data.

    The initial form (user_input=None) must echo back the entry's current
    host, port, path, and unique_id so the user sees what's already configured.
    Many mutants change ``default=data.get(...)`` to ``default=None``,
    ``default=None``, or strip the default entirely. Testing that the form
    result has the correct description_placeholders (title from entry) is also
    useful for key-name mutations.
    """

    async def test_init_form_step_id_is_confirm(
        self, hass: HomeAssistant, hub_entry_builder
    ):
        """The init step returns a form with step_id='confirm'."""
        entry = hub_entry_builder()
        entry.add_to_hass(hass)
        flow = repairs.HubRadioReplaceRepairFlow(entry)
        flow.hass = hass

        result = await flow.async_step_init()
        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "confirm"

    async def test_form_description_placeholders_has_title_key(
        self, hass: HomeAssistant, hub_entry_builder
    ):
        """The initial form must include a 'title' placeholder (not 'TITLE' or 'XXtitleXX').

        Kills mutants 130/131 that rename the key.
        """
        entry = hub_entry_builder(host="rtl433.local")
        entry.add_to_hass(hass)
        flow = repairs.HubRadioReplaceRepairFlow(entry)
        flow.hass = hass

        result = await flow.async_step_confirm(None)
        assert result["type"] == FlowResultType.FORM
        placeholders = result["description_placeholders"]
        assert "title" in placeholders
        assert "TITLE" not in placeholders
        assert "XXtitleXX" not in placeholders
        assert placeholders["title"] == entry.title

    async def test_form_has_data_schema(self, hass: HomeAssistant, hub_entry_builder):
        """The initial form must include a data_schema (not None).

        Kills mutants 123/126 that set data_schema=None or drop it.
        """
        entry = hub_entry_builder()
        entry.add_to_hass(hass)
        flow = repairs.HubRadioReplaceRepairFlow(entry)
        flow.hass = hass

        result = await flow.async_step_confirm(None)
        assert result["type"] == FlowResultType.FORM
        assert result.get("data_schema") is not None


# ---------------------------------------------------------------------------
# HubRadioReplaceRepairFlow.async_step_confirm — CannotConnect error path
# ---------------------------------------------------------------------------


class TestHubRadioReplaceFlowCannotConnect:
    """Assert exact details of the cannot-connect re-show.

    Kills mutants 61/65 (data_schema=None/omitted), 63/67 (description_placeholders),
    74/75 (key renamed in description_placeholders).
    """

    async def test_cannot_connect_reshows_form_with_schema(
        self, hass: HomeAssistant, hub_entry_builder
    ):
        """On CannotConnect the re-shown form must have a data_schema, not None.

        Kills mutants 61 and 65.
        """
        entry = hub_entry_builder()
        entry.add_to_hass(hass)
        flow = repairs.HubRadioReplaceRepairFlow(entry)
        flow.hass = hass

        user_input = {
            CONF_RADIO_ID: "",
            CONF_HOST: "bad.host",
            CONF_PORT: 8433,
            CONF_PATH: "/ws",
            repairs.CONF_SECURE: False,
        }
        with patch(VALIDATE, AsyncMock(side_effect=CannotConnect("nope"))):
            result = await flow.async_step_confirm(user_input)

        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "confirm"
        assert result.get("data_schema") is not None

    async def test_cannot_connect_error_key_and_value(
        self, hass: HomeAssistant, hub_entry_builder
    ):
        """The cannot-connect form error must use key 'base' / value 'cannot_connect'.

        This is tested in the existing suite, but we replicate it here to
        make sure any mutation to those strings is caught redundantly.
        """
        entry = hub_entry_builder()
        entry.add_to_hass(hass)
        flow = repairs.HubRadioReplaceRepairFlow(entry)
        flow.hass = hass

        user_input = {
            CONF_RADIO_ID: "",
            CONF_HOST: "bad.host",
            CONF_PORT: 8433,
            CONF_PATH: "/ws",
            repairs.CONF_SECURE: False,
        }
        with patch(VALIDATE, AsyncMock(side_effect=CannotConnect("nope"))):
            result = await flow.async_step_confirm(user_input)

        errors = result["errors"]
        assert "base" in errors
        assert errors["base"] == "cannot_connect"

    async def test_cannot_connect_description_placeholders_has_title(
        self, hass: HomeAssistant, hub_entry_builder
    ):
        """The cannot-connect re-show must carry 'title' in description_placeholders.

        Kills mutants 63, 67, 74, 75.
        """
        entry = hub_entry_builder(host="rtl433.local")
        entry.add_to_hass(hass)
        flow = repairs.HubRadioReplaceRepairFlow(entry)
        flow.hass = hass

        user_input = {
            CONF_RADIO_ID: "",
            CONF_HOST: "bad.host",
            CONF_PORT: 8433,
            CONF_PATH: "/ws",
            repairs.CONF_SECURE: False,
        }
        with patch(VALIDATE, AsyncMock(side_effect=CannotConnect("nope"))):
            result = await flow.async_step_confirm(user_input)

        placeholders = result["description_placeholders"]
        assert placeholders is not None
        assert "title" in placeholders
        assert "XXtitleXX" not in placeholders
        assert "TITLE" not in placeholders
        assert placeholders["title"] == entry.title


# ---------------------------------------------------------------------------
# HubRadioReplaceRepairFlow — validate_connection call arguments
# ---------------------------------------------------------------------------


class TestHubRadioReplaceFlowValidateArgs:
    """Assert validate_connection is called with the exact user-supplied values.

    Many mutants replace host/port/path/secure/hass arguments with None.
    We verify this by checking validate_connection is called with our specific
    input values (not None, not different values).
    """

    async def test_validate_called_with_correct_hass(
        self, hass: HomeAssistant, hub_entry_builder
    ):
        """validate_connection must receive self.hass as first argument.

        Kills mutmut_50 (None) and mutmut_55 (hass dropped).
        """
        entry = hub_entry_builder()
        entry.add_to_hass(hass)
        flow = repairs.HubRadioReplaceRepairFlow(entry)
        flow.hass = hass

        user_input = {
            CONF_RADIO_ID: "",
            CONF_HOST: "rtl433-new.local",
            CONF_PORT: 8433,
            CONF_PATH: "/ws",
            repairs.CONF_SECURE: False,
        }
        validate_calls = []

        async def _capture_validate(hass_arg, host, port, path, *, secure):
            validate_calls.append(
                {
                    "hass": hass_arg,
                    "host": host,
                    "port": port,
                    "path": path,
                    "secure": secure,
                }
            )

        with (
            patch(VALIDATE, side_effect=_capture_validate),
            patch(
                "custom_components.rtl_433.async_setup_entry",
                AsyncMock(return_value=True),
            ),
        ):
            await flow.async_step_confirm(user_input)

        assert len(validate_calls) == 1
        call = validate_calls[0]
        assert call["hass"] is hass  # Not None, not something else
        assert call["host"] == "rtl433-new.local"
        assert call["port"] == 8433
        assert call["path"] == "/ws"
        assert call["secure"] is False

    async def test_validate_called_with_correct_host(
        self, hass: HomeAssistant, hub_entry_builder
    ):
        """validate_connection host arg must not be None or swapped.

        Kills mutmut_51 (host=None).
        """
        entry = hub_entry_builder()
        entry.add_to_hass(hass)
        flow = repairs.HubRadioReplaceRepairFlow(entry)
        flow.hass = hass

        user_input = {
            CONF_RADIO_ID: "",
            CONF_HOST: "myspecialhost.local",
            CONF_PORT: 8433,
            CONF_PATH: "/ws",
            repairs.CONF_SECURE: False,
        }
        validate_calls = []

        async def _capture(hass_arg, host, port, path, *, secure):
            validate_calls.append({"host": host})

        with (
            patch(VALIDATE, side_effect=_capture),
            patch(
                "custom_components.rtl_433.async_setup_entry",
                AsyncMock(return_value=True),
            ),
        ):
            await flow.async_step_confirm(user_input)

        assert validate_calls[0]["host"] == "myspecialhost.local"

    async def test_validate_called_with_correct_port(
        self, hass: HomeAssistant, hub_entry_builder
    ):
        """validate_connection port arg must match user_input.

        Kills mutmut_52 (port=None).
        """
        entry = hub_entry_builder()
        entry.add_to_hass(hass)
        flow = repairs.HubRadioReplaceRepairFlow(entry)
        flow.hass = hass

        user_input = {
            CONF_RADIO_ID: "",
            CONF_HOST: "rtl433.local",
            CONF_PORT: 9999,
            CONF_PATH: "/ws",
            repairs.CONF_SECURE: False,
        }
        validate_calls = []

        async def _capture(hass_arg, host, port, path, *, secure):
            validate_calls.append({"port": port})

        with (
            patch(VALIDATE, side_effect=_capture),
            patch(
                "custom_components.rtl_433.async_setup_entry",
                AsyncMock(return_value=True),
            ),
        ):
            await flow.async_step_confirm(user_input)

        assert validate_calls[0]["port"] == 9999

    async def test_validate_called_with_correct_path(
        self, hass: HomeAssistant, hub_entry_builder
    ):
        """validate_connection path arg must match user_input.

        Kills mutmut_53 (path=None).
        """
        entry = hub_entry_builder()
        entry.add_to_hass(hass)
        flow = repairs.HubRadioReplaceRepairFlow(entry)
        flow.hass = hass

        user_input = {
            CONF_RADIO_ID: "",
            CONF_HOST: "rtl433.local",
            CONF_PORT: 8433,
            CONF_PATH: "/mypath",
            repairs.CONF_SECURE: False,
        }
        validate_calls = []

        async def _capture(hass_arg, host, port, path, *, secure):
            validate_calls.append({"path": path})

        with (
            patch(VALIDATE, side_effect=_capture),
            patch(
                "custom_components.rtl_433.async_setup_entry",
                AsyncMock(return_value=True),
            ),
        ):
            await flow.async_step_confirm(user_input)

        assert validate_calls[0]["path"] == "/mypath"

    async def test_validate_called_with_correct_secure(
        self, hass: HomeAssistant, hub_entry_builder
    ):
        """validate_connection secure kwarg must match user_input.

        Kills mutmut_54 (secure=None) and mutmut_59 (secure omitted).
        """
        entry = hub_entry_builder()
        entry.add_to_hass(hass)
        flow = repairs.HubRadioReplaceRepairFlow(entry)
        flow.hass = hass

        user_input = {
            CONF_RADIO_ID: "",
            CONF_HOST: "rtl433.local",
            CONF_PORT: 8433,
            CONF_PATH: "/ws",
            repairs.CONF_SECURE: True,
        }
        validate_calls = []

        async def _capture(hass_arg, host, port, path, *, secure):
            validate_calls.append({"secure": secure})

        with (
            patch(VALIDATE, side_effect=_capture),
            patch(
                "custom_components.rtl_433.async_setup_entry",
                AsyncMock(return_value=True),
            ),
        ):
            await flow.async_step_confirm(user_input)

        assert validate_calls[0]["secure"] is True


# ---------------------------------------------------------------------------
# HubRadioReplaceRepairFlow — new_uid computation
# ---------------------------------------------------------------------------


class TestHubRadioReplaceFlowNewUid:
    """Test the new_uid fallback logic.

    Kills survivors:
    - mutmut_80: ``or ""`` -> ``or "XXXX"`` for the radio_id empty string fallback
    - mutmut_81: ``or ""`` -> ``and ""`` for entry.unique_id fallback
    - mutmut_82: ``or ""`` -> ``or "XXXX"`` for entry.unique_id fallback
    """

    async def test_empty_radio_id_uses_existing_unique_id(
        self, hass: HomeAssistant, hub_entry_builder
    ):
        """An empty CONF_RADIO_ID input must fall back to the current unique_id.

        Kills mutmut_81 and mutmut_82.
        """
        entry = hub_entry_builder()
        entry.add_to_hass(hass)
        hass.config_entries.async_update_entry(entry, unique_id="radio-existing")

        flow = repairs.HubRadioReplaceRepairFlow(entry)
        flow.hass = hass

        user_input = {
            CONF_RADIO_ID: "",  # empty -> should fall through to entry.unique_id
            CONF_HOST: "rtl433-new.local",
            CONF_PORT: 8433,
            CONF_PATH: "/ws",
            repairs.CONF_SECURE: False,
        }
        rebind_calls = []

        async def _capture_rebind(hass_arg, entry_arg, new_uid, data, *, title):
            rebind_calls.append({"new_uid": new_uid})
            return "ok"

        with (
            patch(VALIDATE, AsyncMock(return_value=True)),
            patch(
                "custom_components.rtl_433.repairs.async_rebind_hub",
                side_effect=_capture_rebind,
            ),
            patch(
                "custom_components.rtl_433.async_setup_entry",
                AsyncMock(return_value=True),
            ),
        ):
            _result = await flow.async_step_confirm(user_input)

        assert rebind_calls[0]["new_uid"] == "radio-existing"

    async def test_whitespace_only_radio_id_uses_existing_unique_id(
        self, hass: HomeAssistant, hub_entry_builder
    ):
        """Whitespace-only CONF_RADIO_ID must also fall back to current unique_id.

        Kills mutmut_80 which changes the empty-string fallback for the strip() result.
        """
        entry = hub_entry_builder()
        entry.add_to_hass(hass)
        hass.config_entries.async_update_entry(entry, unique_id="radio-kept")

        flow = repairs.HubRadioReplaceRepairFlow(entry)
        flow.hass = hass

        user_input = {
            CONF_RADIO_ID: "   ",  # whitespace only -> strip -> empty -> fallback
            CONF_HOST: "rtl433-new.local",
            CONF_PORT: 8433,
            CONF_PATH: "/ws",
            repairs.CONF_SECURE: False,
        }
        rebind_calls = []

        async def _capture_rebind(hass_arg, entry_arg, new_uid, data, *, title):
            rebind_calls.append({"new_uid": new_uid})
            return "ok"

        with (
            patch(VALIDATE, AsyncMock(return_value=True)),
            patch(
                "custom_components.rtl_433.repairs.async_rebind_hub",
                side_effect=_capture_rebind,
            ),
            patch(
                "custom_components.rtl_433.async_setup_entry",
                AsyncMock(return_value=True),
            ),
        ):
            await flow.async_step_confirm(user_input)

        assert rebind_calls[0]["new_uid"] == "radio-kept"

    async def test_provided_radio_id_takes_precedence(
        self, hass: HomeAssistant, hub_entry_builder
    ):
        """A provided radio id should be used as-is (stripped)."""
        entry = hub_entry_builder()
        entry.add_to_hass(hass)
        hass.config_entries.async_update_entry(entry, unique_id="radio-old")

        flow = repairs.HubRadioReplaceRepairFlow(entry)
        flow.hass = hass

        user_input = {
            CONF_RADIO_ID: "  radio-new  ",
            CONF_HOST: "rtl433-new.local",
            CONF_PORT: 8433,
            CONF_PATH: "/ws",
            repairs.CONF_SECURE: False,
        }
        rebind_calls = []

        async def _capture_rebind(hass_arg, entry_arg, new_uid, data, *, title):
            rebind_calls.append({"new_uid": new_uid})
            return "ok"

        with (
            patch(VALIDATE, AsyncMock(return_value=True)),
            patch(
                "custom_components.rtl_433.repairs.async_rebind_hub",
                side_effect=_capture_rebind,
            ),
            patch(
                "custom_components.rtl_433.async_setup_entry",
                AsyncMock(return_value=True),
            ),
        ):
            await flow.async_step_confirm(user_input)

        assert rebind_calls[0]["new_uid"] == "radio-new"


# ---------------------------------------------------------------------------
# HubRadioReplaceRepairFlow — rebind title format
# ---------------------------------------------------------------------------


class TestHubRadioReplaceFlowRebindTitle:
    """Assert async_rebind_hub is called with the correct title string.

    Kills mutmut_88 (title=None) and mutmut_93 (title omitted).
    """

    async def test_rebind_title_is_rtl433_with_host(
        self, hass: HomeAssistant, hub_entry_builder
    ):
        """async_rebind_hub title must be 'rtl_433 (<host>)' not None/omitted."""
        entry = hub_entry_builder()
        entry.add_to_hass(hass)
        flow = repairs.HubRadioReplaceRepairFlow(entry)
        flow.hass = hass

        user_input = {
            CONF_RADIO_ID: "",
            CONF_HOST: "specifiedhost.local",
            CONF_PORT: 8433,
            CONF_PATH: "/ws",
            repairs.CONF_SECURE: False,
        }
        rebind_calls = []

        async def _capture_rebind(hass_arg, entry_arg, new_uid, data, *, title):
            rebind_calls.append({"title": title})
            return "ok"

        with (
            patch(VALIDATE, AsyncMock(return_value=True)),
            patch(
                "custom_components.rtl_433.repairs.async_rebind_hub",
                side_effect=_capture_rebind,
            ),
            patch(
                "custom_components.rtl_433.async_setup_entry",
                AsyncMock(return_value=True),
            ),
        ):
            await flow.async_step_confirm(user_input)

        assert len(rebind_calls) == 1
        assert rebind_calls[0]["title"] == "rtl_433 (specifiedhost.local)"


# ---------------------------------------------------------------------------
# HubRadioReplaceRepairFlow — already_configured error path
# ---------------------------------------------------------------------------


class TestHubRadioReplaceFlowAlreadyConfigured:
    """Assert the already-configured error path details.

    Kills survivors 95/96 (status string wrong), 97 (step_id=None),
    98/102 (data_schema=None/omitted), 99/103 (errors=None/omitted),
    100/104 (description_placeholders=None/omitted), 101 (step_id omitted),
    105/106 (step_id mangled), 107/108 (errors base key mangled),
    109/110 (errors value mangled), 111/112 (description_placeholders key mangled).
    """

    async def test_already_configured_reshows_form_with_id_in_use_error(
        self, hass: HomeAssistant, hub_entry_builder
    ):
        """When rebind returns 'already_configured' the form re-shows with id_in_use error.

        Kills mutants 95/96 that corrupt the 'already_configured' status string check.
        """
        entry = hub_entry_builder()
        entry.add_to_hass(hass)
        flow = repairs.HubRadioReplaceRepairFlow(entry)
        flow.hass = hass

        user_input = {
            CONF_RADIO_ID: "duplicate-radio",
            CONF_HOST: "rtl433.local",
            CONF_PORT: 8433,
            CONF_PATH: "/ws",
            repairs.CONF_SECURE: False,
        }

        with (
            patch(VALIDATE, AsyncMock(return_value=True)),
            patch(
                "custom_components.rtl_433.repairs.async_rebind_hub",
                AsyncMock(return_value="already_configured"),
            ),
        ):
            result = await flow.async_step_confirm(user_input)

        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "confirm"

    async def test_already_configured_error_dict_exact_keys_and_values(
        self, hass: HomeAssistant, hub_entry_builder
    ):
        """The errors dict must have key 'base' with value 'id_in_use'.

        Kills mutants 99, 107, 108, 109, 110.
        """
        entry = hub_entry_builder()
        entry.add_to_hass(hass)
        flow = repairs.HubRadioReplaceRepairFlow(entry)
        flow.hass = hass

        user_input = {
            CONF_RADIO_ID: "duplicate-radio",
            CONF_HOST: "rtl433.local",
            CONF_PORT: 8433,
            CONF_PATH: "/ws",
            repairs.CONF_SECURE: False,
        }

        with (
            patch(VALIDATE, AsyncMock(return_value=True)),
            patch(
                "custom_components.rtl_433.repairs.async_rebind_hub",
                AsyncMock(return_value="already_configured"),
            ),
        ):
            result = await flow.async_step_confirm(user_input)

        errors = result["errors"]
        assert errors is not None
        assert "base" in errors
        assert "XXbaseXX" not in errors
        assert "BASE" not in errors
        assert errors["base"] == "id_in_use"
        assert errors["base"] != "XXid_in_useXX"
        assert errors["base"] != "ID_IN_USE"

    async def test_already_configured_description_placeholders_title_key(
        self, hass: HomeAssistant, hub_entry_builder
    ):
        """description_placeholders must contain 'title', not 'XXtitleXX' or 'TITLE'.

        Kills mutants 100, 104, 111, 112.
        """
        entry = hub_entry_builder(host="rtl433.local")
        entry.add_to_hass(hass)
        flow = repairs.HubRadioReplaceRepairFlow(entry)
        flow.hass = hass

        user_input = {
            CONF_RADIO_ID: "duplicate-radio",
            CONF_HOST: "rtl433.local",
            CONF_PORT: 8433,
            CONF_PATH: "/ws",
            repairs.CONF_SECURE: False,
        }

        with (
            patch(VALIDATE, AsyncMock(return_value=True)),
            patch(
                "custom_components.rtl_433.repairs.async_rebind_hub",
                AsyncMock(return_value="already_configured"),
            ),
        ):
            result = await flow.async_step_confirm(user_input)

        placeholders = result["description_placeholders"]
        assert placeholders is not None
        assert "title" in placeholders
        assert "XXtitleXX" not in placeholders
        assert "TITLE" not in placeholders
        assert placeholders["title"] == entry.title

    async def test_already_configured_has_data_schema(
        self, hass: HomeAssistant, hub_entry_builder
    ):
        """The already_configured re-show must include a data_schema (not None/omitted).

        Kills mutants 98 and 102.
        """
        entry = hub_entry_builder()
        entry.add_to_hass(hass)
        flow = repairs.HubRadioReplaceRepairFlow(entry)
        flow.hass = hass

        user_input = {
            CONF_RADIO_ID: "duplicate-radio",
            CONF_HOST: "rtl433.local",
            CONF_PORT: 8433,
            CONF_PATH: "/ws",
            repairs.CONF_SECURE: False,
        }

        with (
            patch(VALIDATE, AsyncMock(return_value=True)),
            patch(
                "custom_components.rtl_433.repairs.async_rebind_hub",
                AsyncMock(return_value="already_configured"),
            ),
        ):
            result = await flow.async_step_confirm(user_input)

        assert result.get("data_schema") is not None


# ---------------------------------------------------------------------------
# HubRadioReplaceRepairFlow — success path CREATE_ENTRY
# ---------------------------------------------------------------------------


class TestHubRadioReplaceFlowSuccess:
    """Assert that the successful rebind path creates an entry with exact values.

    Kills survivors 117 (title=None), 118 (data=None), 119 (title omitted),
    121 (title='XXXX').
    """

    async def test_success_creates_entry_with_empty_title_and_empty_data(
        self, hass: HomeAssistant, hub_entry_builder
    ):
        """Successful confirm must produce CREATE_ENTRY with title='' and data={}.

        Kills mutmut_117, 118, 119, 121.
        """
        entry = hub_entry_builder()
        entry.add_to_hass(hass)
        flow = repairs.HubRadioReplaceRepairFlow(entry)
        flow.hass = hass

        user_input = {
            CONF_RADIO_ID: "radio-new",
            CONF_HOST: "rtl433-new.local",
            CONF_PORT: 8433,
            CONF_PATH: "/ws",
            repairs.CONF_SECURE: False,
        }

        with (
            patch(VALIDATE, AsyncMock(return_value=True)),
            patch(
                "custom_components.rtl_433.repairs.async_rebind_hub",
                AsyncMock(return_value="ok"),
            ),
            patch(
                "custom_components.rtl_433.async_setup_entry",
                AsyncMock(return_value=True),
            ),
        ):
            result = await flow.async_step_confirm(user_input)

        assert result["type"] == FlowResultType.CREATE_ENTRY
        assert result["title"] == ""
        assert result["data"] == {}


# ---------------------------------------------------------------------------
# SampleRateRepairFlow — apply / ignore step CREATE_ENTRY paths
# ---------------------------------------------------------------------------


class TestSampleRateApplyFlowSuccess:
    """Test precise details of the SampleRateRepairFlow CREATE_ENTRY results.

    Kills survivors that set title=None, data=None, omit title, or set title='XXXX'
    on the ``apply`` and ``ignore`` steps.
    """

    async def test_apply_creates_entry_with_empty_title(
        self, hass: HomeAssistant, hub_entry_builder
    ):
        """The apply step must produce title='' (not None or 'XXXX')."""
        entry = hub_entry_builder()
        entry.add_to_hass(hass)
        coordinator = Rtl433Coordinator(hass, entry, host="rtl433.local")
        hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

        flow = repairs.SampleRateRepairFlow(entry)
        flow.hass = hass

        result = await flow.async_step_apply()
        assert result["type"] == FlowResultType.CREATE_ENTRY
        assert result["title"] == ""

    async def test_apply_creates_entry_with_empty_data(
        self, hass: HomeAssistant, hub_entry_builder
    ):
        """The apply step must produce data={} (not None)."""
        entry = hub_entry_builder()
        entry.add_to_hass(hass)
        coordinator = Rtl433Coordinator(hass, entry, host="rtl433.local")
        hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

        flow = repairs.SampleRateRepairFlow(entry)
        flow.hass = hass

        result = await flow.async_step_apply()
        assert result["type"] == FlowResultType.CREATE_ENTRY
        assert result["data"] == {}

    async def test_ignore_creates_entry_with_empty_title_and_data(
        self, hass: HomeAssistant, hub_entry_builder
    ):
        """The ignore step must produce title='' and data={} on the flow result.

        (This is the flow result, distinct from the entry.data dismissal flag.)
        """
        entry = hub_entry_builder()
        entry.add_to_hass(hass)

        flow = repairs.SampleRateRepairFlow(entry)
        flow.hass = hass

        result = await flow.async_step_ignore()
        assert result["type"] == FlowResultType.CREATE_ENTRY
        assert result["title"] == ""
        assert result["data"] == {}


class TestSampleRateApplyFlowForm:
    """Test the SampleRateRepairFlow initial menu details.

    The init step is a menu offering the ``apply`` and ``ignore`` options, with
    the title/suggested placeholders carried for the description.
    """

    async def test_init_is_menu_with_apply_and_ignore(
        self, hass: HomeAssistant, hub_entry_builder
    ):
        """The initial step must be a menu listing exactly apply then ignore."""
        entry = hub_entry_builder()
        entry.add_to_hass(hass)
        flow = repairs.SampleRateRepairFlow(entry)
        flow.hass = hass

        result = await flow.async_step_init()
        assert result["type"] == FlowResultType.MENU
        assert result["step_id"] == "init"
        assert result["menu_options"] == ["apply", "ignore"]

    async def test_menu_description_placeholders_title_key(
        self, hass: HomeAssistant, hub_entry_builder
    ):
        """description_placeholders must use key 'title' (not 'XXtitleXX' or 'TITLE')."""
        entry = hub_entry_builder(host="rtl433.local")
        entry.add_to_hass(hass)
        flow = repairs.SampleRateRepairFlow(entry)
        flow.hass = hass

        result = await flow.async_step_init()
        placeholders = result["description_placeholders"]
        assert placeholders is not None
        assert "title" in placeholders
        assert "XXtitleXX" not in placeholders
        assert "TITLE" not in placeholders
        assert placeholders["title"] == entry.title

    async def test_menu_description_placeholders_suggested_value(
        self, hass: HomeAssistant, hub_entry_builder
    ):
        """description_placeholders 'suggested' must be '1024000'."""
        entry = hub_entry_builder()
        entry.add_to_hass(hass)
        flow = repairs.SampleRateRepairFlow(entry)
        flow.hass = hass

        result = await flow.async_step_init()
        placeholders = result["description_placeholders"]
        assert placeholders["suggested"] == "1024000"


# ---------------------------------------------------------------------------
# async_create_fix_flow — entry passed to flow constructors
# ---------------------------------------------------------------------------


class TestAsyncCreateFixFlow:
    """Test that async_create_fix_flow passes the correct entry to flow objects.

    Kills survivors:
    - mutmut_8: HubRadioReplaceRepairFlow(None) instead of HubRadioReplaceRepairFlow(entry)
    - mutmut_16: SampleRateRepairFlow(None) instead of SampleRateRepairFlow(entry)
    """

    async def test_unreachable_flow_has_correct_entry(
        self, hass: HomeAssistant, hub_entry_builder
    ):
        """HubRadioReplaceRepairFlow must hold a reference to the real entry, not None.

        Kills mutmut_8.
        """
        entry = hub_entry_builder()
        entry.add_to_hass(hass)

        flow = await repairs.async_create_fix_flow(
            hass, repairs._unreachable_issue_id(entry), None
        )
        assert isinstance(flow, repairs.HubRadioReplaceRepairFlow)
        assert flow._entry is entry

    async def test_sample_rate_flow_has_correct_entry(
        self, hass: HomeAssistant, hub_entry_builder
    ):
        """SampleRateRepairFlow must hold a reference to the real entry, not None.

        Kills mutmut_16.
        """
        entry = hub_entry_builder()
        entry.add_to_hass(hass)

        flow = await repairs.async_create_fix_flow(
            hass, repairs._sample_rate_issue_id(entry), None
        )
        assert isinstance(flow, repairs.SampleRateRepairFlow)
        assert flow._entry is entry

    async def test_unknown_issue_id_returns_confirm_flow(self, hass: HomeAssistant):
        """An unknown issue_id must still return a ConfirmRepairFlow."""
        flow = await repairs.async_create_fix_flow(hass, "completely_unknown", None)
        assert isinstance(flow, ConfirmRepairFlow)

    async def test_unreachable_prefix_without_valid_entry_returns_confirm_flow(
        self, hass: HomeAssistant
    ):
        """If the entry_id portion resolves to no entry, fall through to ConfirmRepairFlow."""
        issue_id = f"{repairs.ISSUE_UNREACHABLE}_no_such_entry"
        flow = await repairs.async_create_fix_flow(hass, issue_id, None)
        assert isinstance(flow, ConfirmRepairFlow)

    async def test_sample_rate_prefix_without_valid_entry_returns_confirm_flow(
        self, hass: HomeAssistant
    ):
        """If the entry_id portion resolves to no entry, fall through to ConfirmRepairFlow."""
        issue_id = f"{repairs.ISSUE_SAMPLE_RATE_LOW}_no_such_entry"
        flow = await repairs.async_create_fix_flow(hass, issue_id, None)
        assert isinstance(flow, ConfirmRepairFlow)


# ---------------------------------------------------------------------------
# async_raise_sample_rate_low — exact translation_placeholder keys
# ---------------------------------------------------------------------------


class TestAsyncRaiseSampleRateLow:
    """Test that the sample-rate advisory carries exactly the right placeholder keys."""

    def test_raise_uses_title_key(self, hass: HomeAssistant, hub_entry_builder):
        """Issue placeholders must include 'title' key (not renamed)."""
        entry = hub_entry_builder(host="rtl433.local")
        entry.add_to_hass(hass)

        repairs.async_raise_sample_rate_low(
            hass, entry, {"center_frequency": 915_000_000, "samp_rate": 250_000}
        )
        issue_reg = ir.async_get(hass)
        issue_id = repairs._sample_rate_issue_id(entry)
        issue = issue_reg.async_get_issue(DOMAIN, issue_id)

        assert issue is not None
        placeholders = issue.translation_placeholders
        assert "title" in placeholders
        assert "frequency" in placeholders
        assert "sample_rate" in placeholders
        assert "suggested" in placeholders

    def test_raise_frequency_formatted_as_mhz(
        self, hass: HomeAssistant, hub_entry_builder
    ):
        """Frequency placeholder must be formatted in MHz (e.g., '915' not '915000000')."""
        entry = hub_entry_builder()
        entry.add_to_hass(hass)

        repairs.async_raise_sample_rate_low(
            hass, entry, {"center_frequency": 915_000_000, "samp_rate": 250_000}
        )
        issue_reg = ir.async_get(hass)
        issue = issue_reg.async_get_issue(DOMAIN, repairs._sample_rate_issue_id(entry))
        assert issue.translation_placeholders["frequency"] == "915"

    def test_raise_suggested_is_1024000(self, hass: HomeAssistant, hub_entry_builder):
        """Suggested placeholder must be '1024000'."""
        entry = hub_entry_builder()
        entry.add_to_hass(hass)

        repairs.async_raise_sample_rate_low(
            hass, entry, {"center_frequency": 915_000_000, "samp_rate": 250_000}
        )
        issue_reg = ir.async_get(hass)
        issue = issue_reg.async_get_issue(DOMAIN, repairs._sample_rate_issue_id(entry))
        assert issue.translation_placeholders["suggested"] == "1024000"

    def test_raise_sample_rate_string(self, hass: HomeAssistant, hub_entry_builder):
        """sample_rate placeholder must be the integer string of the current rate."""
        entry = hub_entry_builder()
        entry.add_to_hass(hass)

        repairs.async_raise_sample_rate_low(
            hass, entry, {"center_frequency": 915_000_000, "samp_rate": 250_000}
        )
        issue_reg = ir.async_get(hass)
        issue = issue_reg.async_get_issue(DOMAIN, repairs._sample_rate_issue_id(entry))
        assert issue.translation_placeholders["sample_rate"] == "250000"


# ---------------------------------------------------------------------------
# async_raise_hub_unreachable — exact issue fields
# ---------------------------------------------------------------------------


class TestAsyncRaiseHubUnreachable:
    """Test that the unreachable issue carries exactly the right fields."""

    def test_unreachable_issue_is_fixable(self, hass: HomeAssistant, hub_entry_builder):
        """The unreachable issue must be fixable."""
        entry = hub_entry_builder()
        entry.add_to_hass(hass)

        repairs.async_raise_hub_unreachable(hass, entry)
        issue_reg = ir.async_get(hass)
        issue = issue_reg.async_get_issue(DOMAIN, repairs._unreachable_issue_id(entry))

        assert issue is not None
        assert issue.is_fixable is True

    def test_unreachable_issue_severity_is_error(
        self, hass: HomeAssistant, hub_entry_builder
    ):
        """The unreachable issue must have ERROR severity."""
        entry = hub_entry_builder()
        entry.add_to_hass(hass)

        repairs.async_raise_hub_unreachable(hass, entry)
        issue_reg = ir.async_get(hass)
        issue = issue_reg.async_get_issue(DOMAIN, repairs._unreachable_issue_id(entry))

        assert issue.severity is ir.IssueSeverity.ERROR

    def test_unreachable_issue_translation_key(
        self, hass: HomeAssistant, hub_entry_builder
    ):
        """The unreachable issue translation_key must be ISSUE_UNREACHABLE."""
        entry = hub_entry_builder()
        entry.add_to_hass(hass)

        repairs.async_raise_hub_unreachable(hass, entry)
        issue_reg = ir.async_get(hass)
        issue = issue_reg.async_get_issue(DOMAIN, repairs._unreachable_issue_id(entry))

        assert issue.translation_key == repairs.ISSUE_UNREACHABLE

    def test_unreachable_issue_title_placeholder(
        self, hass: HomeAssistant, hub_entry_builder
    ):
        """The unreachable issue must carry 'title' in translation_placeholders."""
        entry = hub_entry_builder(host="rtl433.local")
        entry.add_to_hass(hass)

        repairs.async_raise_hub_unreachable(hass, entry)
        issue_reg = ir.async_get(hass)
        issue = issue_reg.async_get_issue(DOMAIN, repairs._unreachable_issue_id(entry))

        assert "title" in issue.translation_placeholders
        assert issue.translation_placeholders["title"] == entry.title


# ---------------------------------------------------------------------------
# async_track_sample_rate — initial evaluation with flagged meta
# ---------------------------------------------------------------------------


class TestAsyncTrackSampleRateInitialEval:
    """Test that async_track_sample_rate evaluates immediately on wire-up.

    If the coordinator.meta is already in the flagged state when the tracker
    is wired, the issue should be raised immediately (without waiting for a
    signal_hub_update). This tests the ``_evaluate()`` call at the end of
    async_track_sample_rate.
    """

    async def test_issue_raised_immediately_when_already_flagged(
        self, hass: HomeAssistant, hub_entry_builder
    ):
        """With flagged meta already set, issue is raised before any signal fires."""

        entry = hub_entry_builder()
        entry.add_to_hass(hass)
        coordinator = Rtl433Coordinator(hass, entry, host="rtl433.local")
        coordinator.meta = {"center_frequency": 915_000_000, "samp_rate": 250_000}

        issue_reg = ir.async_get(hass)
        issue_id = repairs._sample_rate_issue_id(entry)

        unsub = repairs.async_track_sample_rate(hass, entry, coordinator)
        # No signal sent yet — issue should already be raised.
        assert issue_reg.async_get_issue(DOMAIN, issue_id) is not None
        unsub()

    async def test_no_issue_when_initially_not_flagged(
        self, hass: HomeAssistant, hub_entry_builder
    ):
        """With good meta on wire-up, no issue is raised initially."""
        entry = hub_entry_builder()
        entry.add_to_hass(hass)
        coordinator = Rtl433Coordinator(hass, entry, host="rtl433.local")
        coordinator.meta = {"center_frequency": 433_920_000, "samp_rate": 250_000}

        issue_reg = ir.async_get(hass)
        issue_id = repairs._sample_rate_issue_id(entry)

        unsub = repairs.async_track_sample_rate(hass, entry, coordinator)
        assert issue_reg.async_get_issue(DOMAIN, issue_id) is None
        unsub()


# ---------------------------------------------------------------------------
# HubRadioReplaceRepairFlow — form schema contents (defaults from entry data)
# ---------------------------------------------------------------------------


class TestHubRadioReplaceFlowSchemaDefaults:
    """Test that form schema defaults correctly use entry data values.

    These tests ensure that schema field defaults (for host, port, path) are
    drawn from entry.data (not None, not a sentinel XXXX), which kills many
    of the default= mutation variants. We achieve this by instantiating the
    flow with an entry that has specific non-default values, then checking
    the form's data_schema.

    Kills mutants related to default=None, default=data.get(CONF_*, None), etc.
    in the schema definition lines.
    """

    async def test_schema_host_default_matches_entry_host(
        self, hass: HomeAssistant, hub_entry_builder
    ):
        """The schema's CONF_HOST default must match entry.data host.

        Kills mutmut_15 which changes data.get(CONF_HOST, '') to data.get(None, '').
        When CONF_HOST is in data, CONF_HOST key lookup returns the real host,
        but None key lookup returns the fallback '' - so the default differs.
        """
        entry = hub_entry_builder(host="special-host.local")
        entry.add_to_hass(hass)
        flow = repairs.HubRadioReplaceRepairFlow(entry)
        flow.hass = hass

        result = await flow.async_step_confirm(None)
        assert result["type"] == FlowResultType.FORM
        schema = result["data_schema"]
        assert schema is not None

        import voluptuous as vol

        # Omit CONF_HOST from input — the default from the schema should be used.
        # Original: data.get(CONF_HOST, "") -> "special-host.local"
        # Mutant 15: data.get(None, "") -> "" (None key not in data)
        try:
            validated = schema(
                {
                    CONF_PORT: DEFAULT_PORT,
                    CONF_PATH: DEFAULT_PATH,
                    repairs.CONF_SECURE: False,
                }
            )
            # The default must be "special-host.local" (from data), not ""
            assert validated[CONF_HOST] == "special-host.local"
        except vol.Invalid:
            pytest.fail("Schema rejected input with CONF_HOST defaulting")

    async def test_schema_port_default_matches_entry_port(
        self, hass: HomeAssistant, hub_entry_builder
    ):
        """The schema's CONF_PORT default must match entry.data port.

        Kills mutmut_24 which changes data.get(CONF_PORT, DEFAULT_PORT) to
        data.get(None, DEFAULT_PORT). When CONF_PORT is in data, lookup by
        CONF_PORT returns the real port, but None key returns DEFAULT_PORT fallback.
        """
        entry = hub_entry_builder(port=9876)
        entry.add_to_hass(hass)
        flow = repairs.HubRadioReplaceRepairFlow(entry)
        flow.hass = hass

        result = await flow.async_step_confirm(None)
        schema = result["data_schema"]
        assert schema is not None

        import voluptuous as vol

        # Omit CONF_PORT from input — the default from the schema should be used.
        # Original: data.get(CONF_PORT, DEFAULT_PORT) -> 9876
        # Mutant 24: data.get(None, DEFAULT_PORT) -> DEFAULT_PORT (8433) since None not in data
        try:
            validated = schema(
                {
                    CONF_HOST: "rtl433.local",
                    CONF_PATH: DEFAULT_PATH,
                    repairs.CONF_SECURE: False,
                }
            )
            # The default must be 9876 (from data), not DEFAULT_PORT (8433)
            assert validated[CONF_PORT] == 9876
        except vol.Invalid:
            pytest.fail("Schema rejected input with CONF_PORT defaulting")

    async def test_schema_path_default_matches_entry_path(
        self, hass: HomeAssistant, hub_entry_builder
    ):
        """The schema's CONF_PATH default must match entry.data path.

        Kills mutmut_32 which changes data.get(CONF_PATH, DEFAULT_PATH) to
        data.get(None, DEFAULT_PATH). When CONF_PATH is in data, lookup by
        CONF_PATH returns the real path, but None key returns DEFAULT_PATH fallback.
        """
        entry = hub_entry_builder(path="/specialpath")
        entry.add_to_hass(hass)
        flow = repairs.HubRadioReplaceRepairFlow(entry)
        flow.hass = hass

        result = await flow.async_step_confirm(None)
        schema = result["data_schema"]
        assert schema is not None

        import voluptuous as vol

        # Omit CONF_PATH from input — the default from the schema should be used.
        # Original: data.get(CONF_PATH, DEFAULT_PATH) -> "/specialpath"
        # Mutant 32: data.get(None, DEFAULT_PATH) -> DEFAULT_PATH ("/ws")
        try:
            validated = schema(
                {
                    CONF_HOST: "rtl433.local",
                    CONF_PORT: DEFAULT_PORT,
                    repairs.CONF_SECURE: False,
                }
            )
            # The default must be "/specialpath" (from data), not DEFAULT_PATH
            assert validated[CONF_PATH] == "/specialpath"
        except vol.Invalid:
            pytest.fail("Schema rejected input with CONF_PATH defaulting")

    async def test_schema_secure_default_matches_entry_secure(
        self, hass: HomeAssistant, hub_entry_builder
    ):
        """The schema's CONF_SECURE default must match entry.data secure.

        Kills mutmut_40 which changes data.get(CONF_SECURE, False) to
        data.get(None, False). When CONF_SECURE=True is in data, lookup by
        CONF_SECURE returns True, but None key returns False fallback.
        """
        entry = hub_entry_builder(secure=True)
        entry.add_to_hass(hass)
        flow = repairs.HubRadioReplaceRepairFlow(entry)
        flow.hass = hass

        result = await flow.async_step_confirm(None)
        schema = result["data_schema"]
        assert schema is not None

        import voluptuous as vol

        # Omit CONF_SECURE from input — the default from the schema should be used.
        # Original: data.get(CONF_SECURE, False) -> True (secure=True in data)
        # Mutant 40: data.get(None, False) -> False (None key not in data)
        try:
            validated = schema(
                {
                    CONF_HOST: "rtl433.local",
                    CONF_PORT: DEFAULT_PORT,
                    CONF_PATH: DEFAULT_PATH,
                }
            )
            # The default must be True (from data), not False (mutant fallback)
            assert validated[repairs.CONF_SECURE] is True
        except vol.Invalid:
            pytest.fail("Schema rejected input with CONF_SECURE defaulting")

    async def test_schema_radio_id_default_matches_entry_unique_id(
        self, hass: HomeAssistant, hub_entry_builder
    ):
        """The schema's CONF_RADIO_ID default must match entry.unique_id.

        Kills mutmut_6 (default=None), mutmut_8 (no default), mutmut_9
        (or -> and), mutmut_10 (or 'XXXX').
        """
        entry = hub_entry_builder()
        entry.add_to_hass(hass)
        hass.config_entries.async_update_entry(entry, unique_id="my-radio-id")

        flow = repairs.HubRadioReplaceRepairFlow(entry)
        flow.hass = hass

        result = await flow.async_step_confirm(None)
        schema = result["data_schema"]
        assert schema is not None

        import voluptuous as vol

        # Provide no radio_id: the Optional field should default to the unique_id.
        try:
            validated = schema(
                {
                    CONF_HOST: "rtl433.local",
                    CONF_PORT: DEFAULT_PORT,
                    CONF_PATH: DEFAULT_PATH,
                    repairs.CONF_SECURE: False,
                }
            )
            # The CONF_RADIO_ID key should be present with the default from unique_id.
            assert validated.get(CONF_RADIO_ID) == "my-radio-id"
        except vol.Invalid:
            pytest.fail("Schema rejected input missing optional radio_id")

    async def test_schema_radio_id_default_when_unique_id_is_none(
        self, hass: HomeAssistant
    ):
        """When entry.unique_id is None, radio_id schema default must be '' not 'XXXX'.

        Kills mutmut_10 (or 'XXXX' fallback) and mutmut_82 (same in new_uid path).
        """
        from pytest_homeassistant_custom_component.common import MockConfigEntry

        entry = MockConfigEntry(
            domain="rtl_433",
            title="rtl_433 (rtl433.local)",
            data={
                CONF_HOST: "rtl433.local",
                CONF_PORT: DEFAULT_PORT,
                CONF_PATH: DEFAULT_PATH,
            },
            unique_id=None,
        )
        entry.add_to_hass(hass)

        flow = repairs.HubRadioReplaceRepairFlow(entry)
        flow.hass = hass

        result = await flow.async_step_confirm(None)
        schema = result["data_schema"]
        assert schema is not None

        import voluptuous as vol

        # With no unique_id, the fallback should be "" not "XXXX"
        try:
            validated = schema(
                {
                    CONF_HOST: "rtl433.local",
                    CONF_PORT: DEFAULT_PORT,
                    CONF_PATH: DEFAULT_PATH,
                    repairs.CONF_SECURE: False,
                }
            )
            # Default must be "" (empty string fallback), not "XXXX"
            assert validated.get(CONF_RADIO_ID) == ""
        except vol.Invalid:
            pytest.fail("Schema rejected input missing optional radio_id")


# ---------------------------------------------------------------------------
# HubRadioReplaceRepairFlow — schema defaults when entry data keys missing
# ---------------------------------------------------------------------------


class TestHubRadioReplaceFlowSchemaFallbacks:
    """Test schema fallback defaults when entry.data is missing a key.

    These tests create a minimal entry without CONF_HOST/CONF_PORT/CONF_PATH/CONF_SECURE
    and verify the schema defaults match what the code specifies (e.g., "" for host,
    DEFAULT_PORT for port, DEFAULT_PATH for path, False for secure).

    These kill the large cluster of mutants that change the fallback argument in
    data.get(key, fallback).
    """

    async def test_host_schema_default_when_not_in_data(self, hass: HomeAssistant):
        """Schema host default must be '' when CONF_HOST is absent from data.

        Kills mutmut_12 (None), 14 (no default), 15 (data.get(None)),
        16 (None fallback), 17 (data.get('')), 18 (no second arg), 19 ('XXXX').
        """
        from pytest_homeassistant_custom_component.common import MockConfigEntry
        import voluptuous as vol

        # Entry with no host in data - the fallback must be ""
        entry = MockConfigEntry(
            domain="rtl_433",
            title="rtl_433 (unknown)",
            data={CONF_PORT: DEFAULT_PORT, CONF_PATH: DEFAULT_PATH},
            unique_id=None,
        )
        entry.add_to_hass(hass)

        flow = repairs.HubRadioReplaceRepairFlow(entry)
        flow.hass = hass

        result = await flow.async_step_confirm(None)
        schema = result["data_schema"]
        assert schema is not None

        # When CONF_HOST is omitted from input, the default kicks in.
        # vol.Required with a default fills in the default when the key is missing.
        try:
            validated = schema(
                {
                    CONF_PORT: DEFAULT_PORT,
                    CONF_PATH: DEFAULT_PATH,
                    repairs.CONF_SECURE: False,
                }
            )
            # The default for CONF_HOST should be "" (not None, not "XXXX")
            assert validated[CONF_HOST] == ""
        except vol.Invalid:
            # If Required has no default, it raises - that would be a bug too
            pytest.fail("CONF_HOST has no default for required field")

    async def test_port_schema_default_when_not_in_data(self, hass: HomeAssistant):
        """Schema port default must be DEFAULT_PORT when CONF_PORT absent.

        Kills mutmut_21 (None), 23 (no default), 24 (data.get(None)),
        25 (None fallback), 26 (data.get(DEFAULT_PORT) wrong args),
        27 (no second arg).
        """
        from pytest_homeassistant_custom_component.common import MockConfigEntry
        import voluptuous as vol

        entry = MockConfigEntry(
            domain="rtl_433",
            title="rtl_433 (unknown)",
            data={CONF_HOST: "rtl433.local", CONF_PATH: DEFAULT_PATH},
            unique_id=None,
        )
        entry.add_to_hass(hass)

        flow = repairs.HubRadioReplaceRepairFlow(entry)
        flow.hass = hass

        result = await flow.async_step_confirm(None)
        schema = result["data_schema"]
        assert schema is not None

        try:
            validated = schema(
                {
                    CONF_HOST: "rtl433.local",
                    CONF_PATH: DEFAULT_PATH,
                    repairs.CONF_SECURE: False,
                }
            )
            # Default port must be DEFAULT_PORT (8433), not None
            assert validated[CONF_PORT] == DEFAULT_PORT
            assert validated[CONF_PORT] is not None
        except vol.Invalid:
            pytest.fail("CONF_PORT has no default")

    async def test_path_schema_default_when_not_in_data(self, hass: HomeAssistant):
        """Schema path default must be DEFAULT_PATH when CONF_PATH absent.

        Kills mutmut_29 (None), 31 (no default), 32 (data.get(None)),
        33 (None fallback), 34 (data.get(DEFAULT_PATH) wrong args), 35 (no second arg).
        """
        from pytest_homeassistant_custom_component.common import MockConfigEntry
        import voluptuous as vol

        entry = MockConfigEntry(
            domain="rtl_433",
            title="rtl_433 (unknown)",
            data={CONF_HOST: "rtl433.local", CONF_PORT: DEFAULT_PORT},
            unique_id=None,
        )
        entry.add_to_hass(hass)

        flow = repairs.HubRadioReplaceRepairFlow(entry)
        flow.hass = hass

        result = await flow.async_step_confirm(None)
        schema = result["data_schema"]
        assert schema is not None

        try:
            validated = schema(
                {
                    CONF_HOST: "rtl433.local",
                    CONF_PORT: DEFAULT_PORT,
                    repairs.CONF_SECURE: False,
                }
            )
            # Default path must be DEFAULT_PATH (/ws), not None
            assert validated[CONF_PATH] == DEFAULT_PATH
            assert validated[CONF_PATH] is not None
        except vol.Invalid:
            pytest.fail("CONF_PATH has no default")

    async def test_secure_schema_default_when_not_in_data(self, hass: HomeAssistant):
        """Schema secure default must be False when CONF_SECURE absent.

        Kills mutmut_37 (None), 39 (no default), 40 (data.get(None)),
        41 (None fallback), 42 (data.get(False) wrong args), 43 (no second arg),
        44 (True fallback).
        """
        from pytest_homeassistant_custom_component.common import MockConfigEntry
        import voluptuous as vol

        entry = MockConfigEntry(
            domain="rtl_433",
            title="rtl_433 (unknown)",
            data={
                CONF_HOST: "rtl433.local",
                CONF_PORT: DEFAULT_PORT,
                CONF_PATH: DEFAULT_PATH,
            },
            unique_id=None,
        )
        entry.add_to_hass(hass)

        flow = repairs.HubRadioReplaceRepairFlow(entry)
        flow.hass = hass

        result = await flow.async_step_confirm(None)
        schema = result["data_schema"]
        assert schema is not None

        # CONF_SECURE is Optional, so omitting it from input should give the default
        try:
            validated = schema(
                {
                    CONF_HOST: "rtl433.local",
                    CONF_PORT: DEFAULT_PORT,
                    CONF_PATH: DEFAULT_PATH,
                }
            )
            # Default secure must be False (not None, not True)
            assert validated[repairs.CONF_SECURE] is False
        except vol.Invalid:
            pytest.fail("CONF_SECURE has no default or defaults to wrong value")


# ---------------------------------------------------------------------------
# HubRadioReplaceRepairFlow — new_uid fallback when unique_id is None
# ---------------------------------------------------------------------------


class TestHubRadioReplaceFlowNewUidNoneUniqueId:
    """Test new_uid fallback when entry.unique_id is None.

    Kills mutmut_82: ``entry.unique_id or ""`` -> ``entry.unique_id or "XXXX"``
    """

    async def test_none_unique_id_falls_back_to_empty_string(self, hass: HomeAssistant):
        """When both radio_id input and unique_id are empty/None, new_uid must be ''.

        Kills mutmut_82 which changes the fallback to 'XXXX'.
        """
        from pytest_homeassistant_custom_component.common import MockConfigEntry

        entry = MockConfigEntry(
            domain="rtl_433",
            title="rtl_433 (rtl433.local)",
            data={
                CONF_HOST: "rtl433.local",
                CONF_PORT: DEFAULT_PORT,
                CONF_PATH: DEFAULT_PATH,
            },
            unique_id=None,
        )
        entry.add_to_hass(hass)

        flow = repairs.HubRadioReplaceRepairFlow(entry)
        flow.hass = hass

        user_input = {
            CONF_RADIO_ID: "",  # empty user input
            CONF_HOST: "rtl433.local",
            CONF_PORT: DEFAULT_PORT,
            CONF_PATH: DEFAULT_PATH,
            repairs.CONF_SECURE: False,
        }
        rebind_calls = []

        async def _capture_rebind(hass_arg, entry_arg, new_uid, data, *, title):
            rebind_calls.append({"new_uid": new_uid})
            return "ok"

        with (
            patch(VALIDATE, AsyncMock(return_value=True)),
            patch(
                "custom_components.rtl_433.repairs.async_rebind_hub",
                side_effect=_capture_rebind,
            ),
            patch(
                "custom_components.rtl_433.async_setup_entry",
                AsyncMock(return_value=True),
            ),
        ):
            await flow.async_step_confirm(user_input)

        # Both radio_id and unique_id are empty -> new_uid must be "" not "XXXX"
        assert len(rebind_calls) == 1
        assert rebind_calls[0]["new_uid"] == ""


# ---------------------------------------------------------------------------
# SampleRateRepairFlow — ignore step persists the dismissal flag
# ---------------------------------------------------------------------------


class TestSampleRateIgnoreFlow:
    """Test that the ignore step persists the per-hub dismissal flag.

    The flag must land in ``entry.data`` (so it survives reloads) and the ignore
    path must not touch the sample rate.
    """

    async def test_ignore_sets_dismissal_flag_in_entry_data(
        self, hass: HomeAssistant, hub_entry_builder
    ):
        """async_step_ignore must persist CONF_SAMPLE_RATE_DISMISSED=True."""
        from custom_components.rtl_433.const import CONF_SAMPLE_RATE_DISMISSED

        entry = hub_entry_builder()
        entry.add_to_hass(hass)

        flow = repairs.SampleRateRepairFlow(entry)
        flow.hass = hass

        assert not entry.data.get(CONF_SAMPLE_RATE_DISMISSED)
        await flow.async_step_ignore()
        assert entry.data.get(CONF_SAMPLE_RATE_DISMISSED) is True

    async def test_ignore_does_not_apply_sample_rate(
        self, hass: HomeAssistant, hub_entry_builder
    ):
        """The ignore path must leave the coordinator's desired rate untouched."""
        from custom_components.rtl_433.sdr_settings import KEY_SAMPLE_RATE

        entry = hub_entry_builder()
        entry.add_to_hass(hass)
        coordinator = Rtl433Coordinator(hass, entry, host="rtl433.local")
        hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

        flow = repairs.SampleRateRepairFlow(entry)
        flow.hass = hass

        await flow.async_step_ignore()
        assert coordinator.get_desired(KEY_SAMPLE_RATE) is None


# ---------------------------------------------------------------------------
# SampleRateRepairFlow — no coordinator branch (apply)
# ---------------------------------------------------------------------------


class TestSampleRateApplyFlowNoCoordinator:
    """Test the no-coordinator apply path still clears the issue without crashing.

    Adds assertions about CREATE_ENTRY exact values to kill the title/data
    mutation survivors.
    """

    async def test_no_coordinator_still_creates_entry_empty(
        self, hass: HomeAssistant, hub_entry_builder
    ):
        """Without coordinator, apply must still be CREATE_ENTRY with title='' data={}."""
        entry = hub_entry_builder()
        entry.add_to_hass(hass)
        # Ensure no coordinator is registered
        hass.data.setdefault(DOMAIN, {})
        hass.data[DOMAIN].pop(entry.entry_id, None)

        flow = repairs.SampleRateRepairFlow(entry)
        flow.hass = hass

        result = await flow.async_step_apply()
        assert result["type"] == FlowResultType.CREATE_ENTRY
        assert result["title"] == ""
        assert result["data"] == {}
