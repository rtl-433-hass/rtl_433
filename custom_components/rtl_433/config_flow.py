"""Config and options flows for the rtl_433 integration.

This module implements two flows backed by one config-flow domain:

- **Hub user flow** (``async_step_user``): collects a single rtl_433 HTTP
  server's WebSocket connection parameters (host/port/path, optional ``wss://``
  via a ``secure`` toggle), validates reachability with the coordinator's
  ``validate_connection`` helper, and creates the hub config entry. The hub's
  unique_id is derived from host/port so the same server cannot be added twice.

- **Options flow** (``async_get_options_flow`` -> :class:`Rtl433OptionsFlow`):
  a small menu offering a *hub* step and a *device* step. The hub step exposes
  the per-hub discovery toggle and the default availability timeout and persists
  them to ``entry.options``. The device step lets the user pick a known device
  from the hub's ``entry.data["devices"]`` map and set or clear that device's
  per-device availability-timeout override, which is written back into the same
  devices map via ``async_update_entry(entry, data=...)`` (so the coordinator's
  ``effective_timeout_resolver`` reads it from exactly one place).

This module only validates connectivity; it never starts the coordinator. The
coordinator lifecycle is wired elsewhere.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .calibration import (
    COMMODITY_UNITS,
    commodity_from_fields,
    default_unit,
    normalize_calibration,
)
from .const import (
    CALIBRATION_COMMODITIES,
    CALIBRATION_COMMODITY,
    CALIBRATION_SCALE,
    CALIBRATION_UNIT,
    COMMODITY_NONE,
    CONF_AVAILABILITY_TIMEOUT,
    CONF_DEVICES,
    CONF_DISCOVERY_ENABLED,
    CONF_HOST,
    CONF_MANAGE_SETTINGS,
    CONF_MODEL,
    CONF_PATH,
    CONF_PORT,
    DEFAULT_AVAILABILITY_TIMEOUT,
    DEFAULT_MANAGE_SETTINGS,
    DEFAULT_PATH,
    DEFAULT_PORT,
    DEVICE_CALIBRATION,
    DEVICE_TIMEOUT_OVERRIDE,
    DOMAIN,
)
from .coordinator import CannotConnect, Rtl433Coordinator

# Whether to dial the server over ``wss://`` instead of ``ws://``.
CONF_SECURE = "secure"

# Selector key for the device picker on the options device step.
CONF_DEVICE = "device"


def _hub_unique_id(host: str, port: int) -> str:
    """Return the unique_id for a hub entry (one per host:port)."""
    return f"hub:{host}:{port}"


STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
        vol.Required(CONF_PATH, default=DEFAULT_PATH): str,
        vol.Optional(CONF_SECURE, default=False): bool,
        vol.Optional(CONF_MANAGE_SETTINGS, default=DEFAULT_MANAGE_SETTINGS): bool,
    }
)


class Rtl433ConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle setup of an rtl_433 hub (one config entry per server)."""

    VERSION = 2

    # ------------------------------------------------------------------ #
    # Hub user flow                                                      #
    # ------------------------------------------------------------------ #
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect hub connection params, validate, and create a hub entry."""
        errors: dict[str, str] = {}

        if user_input is not None:
            host: str = user_input[CONF_HOST]
            port: int = user_input[CONF_PORT]
            path: str = user_input[CONF_PATH]
            secure: bool = user_input[CONF_SECURE]
            manage_settings: bool = user_input[CONF_MANAGE_SETTINGS]

            try:
                await Rtl433Coordinator.validate_connection(
                    self.hass, host, port, path, secure=secure
                )
            except CannotConnect:
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(_hub_unique_id(host, port))
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"rtl_433 ({host})",
                    data={
                        CONF_HOST: host,
                        CONF_PORT: port,
                        CONF_PATH: path,
                        CONF_SECURE: secure,
                        CONF_MANAGE_SETTINGS: manage_settings,
                    },
                )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_SCHEMA,
            errors=errors,
        )

    # ------------------------------------------------------------------ #
    # Hub reconfigure flow                                               #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _reconfigure_schema(entry: ConfigEntry) -> vol.Schema:
        """Build the reconfigure form schema pre-filled from ``entry.data``.

        Mirrors the connection subset of :data:`STEP_USER_SCHEMA` but omits
        ``manage_settings`` (that toggle is owned by the options flow).
        """
        data = entry.data
        return vol.Schema(
            {
                vol.Required(CONF_HOST, default=data.get(CONF_HOST, "")): str,
                vol.Required(CONF_PORT, default=data.get(CONF_PORT, DEFAULT_PORT)): int,
                vol.Required(CONF_PATH, default=data.get(CONF_PATH, DEFAULT_PATH)): str,
                vol.Optional(CONF_SECURE, default=data.get(CONF_SECURE, False)): bool,
            }
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit an existing hub's connection target in place.

        Validates the new host/port/path/secure, recomputes the host:port
        unique_id (guarding against collision with a *different* configured
        hub), then merges the new connection params into the entry via
        ``data_updates=`` and reloads it in place — preserving the entry_id,
        ``entry.data["devices"]``, and ``manage_settings``.
        """
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            host: str = user_input[CONF_HOST]
            port: int = user_input[CONF_PORT]
            path: str = user_input[CONF_PATH]
            secure: bool = user_input[CONF_SECURE]

            try:
                await Rtl433Coordinator.validate_connection(
                    self.hass, host, port, path, secure=secure
                )
            except CannotConnect:
                errors["base"] = "cannot_connect"
            else:
                new_unique_id = _hub_unique_id(host, port)
                await self.async_set_unique_id(new_unique_id)
                # Abort only if a *different* entry already owns this unique_id;
                # the entry being reconfigured must not abort against itself.
                for other in self._async_current_entries():
                    if (
                        other.unique_id == new_unique_id
                        and other.entry_id != entry.entry_id
                    ):
                        return self.async_abort(reason="already_configured")
                return self.async_update_reload_and_abort(
                    entry,
                    unique_id=new_unique_id,
                    title=f"rtl_433 ({host})",
                    data_updates={
                        CONF_HOST: host,
                        CONF_PORT: port,
                        CONF_PATH: path,
                        CONF_SECURE: secure,
                    },
                )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self._reconfigure_schema(entry),
            errors=errors,
        )

    # ------------------------------------------------------------------ #
    # Options flow                                                       #
    # ------------------------------------------------------------------ #
    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the hub options flow (one entry == one hub)."""
        return Rtl433OptionsFlow()


class Rtl433OptionsFlow(OptionsFlow):
    """Hub options: a menu with a hub-settings step and a device step.

    The hub step persists the discovery toggle and the default availability
    timeout to ``entry.options``. The device step writes a per-device
    availability-timeout override and an optional utility-meter calibration
    into the hub's ``entry.data["devices"]`` map.
    """

    # State carried from the device step into the (optional) calibration step.
    _calibration_device: str = ""
    _calibration_override: int | None = None
    _calibration_commodity: str = COMMODITY_NONE

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the options menu."""
        return self.async_show_menu(
            step_id="init",
            menu_options=["hub", "device"],
        )

    async def async_step_hub(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and persist the hub-level options (writes ``entry.options``)."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        entry = self.config_entry
        discovery_default = entry.options.get(
            CONF_DISCOVERY_ENABLED,
            entry.data.get(CONF_DISCOVERY_ENABLED, True),
        )
        timeout_default = entry.options.get(
            CONF_AVAILABILITY_TIMEOUT,
            entry.data.get(CONF_AVAILABILITY_TIMEOUT, DEFAULT_AVAILABILITY_TIMEOUT),
        )
        manage_default = entry.options.get(
            CONF_MANAGE_SETTINGS,
            entry.data.get(CONF_MANAGE_SETTINGS, DEFAULT_MANAGE_SETTINGS),
        )

        schema = vol.Schema(
            {
                vol.Required(CONF_DISCOVERY_ENABLED, default=discovery_default): bool,
                vol.Required(
                    CONF_AVAILABILITY_TIMEOUT, default=timeout_default
                ): vol.All(int, vol.Range(min=1)),
                vol.Required(CONF_MANAGE_SETTINGS, default=manage_default): bool,
            }
        )
        return self.async_show_form(step_id="hub", data_schema=schema)

    def _device_commodity_default(self, device_key: str) -> str:
        """Best-effort commodity pre-fill from the device's last decoded event.

        Reads the running coordinator's most recent ``NormalizedEvent`` for the
        device and derives a commodity hint from its ``MeterType`` / ``ert_type``
        fields. Everything is guarded: a missing coordinator/event/field falls
        back to ``none`` and never raises into the form render.
        """
        coordinator = self.hass.data.get(DOMAIN, {}).get(self.config_entry.entry_id)
        event = getattr(coordinator, "devices", {}).get(device_key)
        fields = getattr(event, "fields", None)
        return commodity_from_fields(fields)

    def _write_device_record(
        self,
        device_key: str,
        *,
        override: int | None,
        calibration: dict[str, Any] | None,
    ) -> ConfigFlowResult:
        """Persist a device's timeout override + calibration; finish the flow.

        Writes into the hub's ``entry.data["devices"]`` map (the single source of
        truth read by the coordinator and the entity build), not into
        ``entry.options``. ``calibration is None`` clears any prior calibration.
        The resulting ``async_update_entry`` fires ``_async_update_listener``,
        which reloads the hub iff the calibration map actually changed.
        """
        data = dict(self.config_entry.data)
        new_devices = dict(data.get(CONF_DEVICES, {}))
        record = dict(new_devices.get(device_key, {}))
        if override is None:
            # Blank submission clears the override (fall back to hub default).
            record.pop(DEVICE_TIMEOUT_OVERRIDE, None)
        else:
            record[DEVICE_TIMEOUT_OVERRIDE] = override
        if calibration is None:
            record.pop(DEVICE_CALIBRATION, None)
        else:
            record[DEVICE_CALIBRATION] = calibration
        new_devices[device_key] = record
        data[CONF_DEVICES] = new_devices

        self.hass.config_entries.async_update_entry(self.config_entry, data=data)
        # Finish the flow without altering entry.options.
        return self.async_create_entry(title="", data=self.config_entry.options)

    async def async_step_device(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick a known device; set its timeout override and meter commodity.

        Collects the per-device availability-timeout override and the consumption
        commodity (none / energy / gas / water). Choosing ``none`` writes the
        record (clearing any calibration) and finishes; choosing a real commodity
        advances to :meth:`async_step_calibration` to pick a commodity-constrained
        base unit + scale. The commodity default is pre-filled from the device's
        decoded ``MeterType`` / ``ert_type`` when present.
        """
        devices: dict[str, Any] = dict(self.config_entry.data.get(CONF_DEVICES, {}))
        if not devices:
            return self.async_abort(reason="no_devices")

        if user_input is not None:
            device_key: str = user_input[CONF_DEVICE]
            override = user_input.get(DEVICE_TIMEOUT_OVERRIDE)
            commodity = user_input.get(CALIBRATION_COMMODITY, COMMODITY_NONE)

            if commodity == COMMODITY_NONE:
                return self._write_device_record(
                    device_key, override=override, calibration=None
                )

            # Carry the device + timeout + commodity into the calibration step.
            self._calibration_device = device_key
            self._calibration_override = override
            self._calibration_commodity = commodity
            return await self.async_step_calibration()

        options = [
            SelectOptionDict(
                value=device_key,
                label=f"{record.get(CONF_MODEL, device_key)} ({device_key})",
            )
            for device_key, record in sorted(devices.items())
        ]
        commodity_options = [
            SelectOptionDict(value=value, label=value)
            for value in CALIBRATION_COMMODITIES
        ]
        # Best-effort commodity pre-fill: the device picker and commodity share
        # one form, so the default can only reflect a specific device when there
        # is exactly one (the focused-calibration case). With several devices the
        # default stays ``none`` and the user picks the commodity explicitly.
        commodity_default = COMMODITY_NONE
        if len(devices) == 1:
            commodity_default = self._device_commodity_default(next(iter(devices)))
        schema = vol.Schema(
            {
                vol.Required(CONF_DEVICE): SelectSelector(
                    SelectSelectorConfig(
                        options=options,
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Optional(DEVICE_TIMEOUT_OVERRIDE): vol.All(int, vol.Range(min=1)),
                vol.Optional(
                    CALIBRATION_COMMODITY, default=commodity_default
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=commodity_options,
                        mode=SelectSelectorMode.DROPDOWN,
                        translation_key="commodity",
                    )
                ),
            }
        )
        return self.async_show_form(step_id="device", data_schema=schema)

    async def async_step_calibration(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick the commodity-constrained base unit + scale for a consumption meter.

        Reached from :meth:`async_step_device` only when a real commodity was
        chosen. The unit selector is constrained to the units Home Assistant
        recognizes as convertible for the commodity's device_class, so the
        resulting consumption sensor is Energy-dashboard-eligible. The
        ``{commodity, unit, scale}`` triple is written into the device record and
        applies to the device's known consumption field(s) only.
        """
        device_key = self._calibration_device
        commodity = self._calibration_commodity

        if user_input is not None:
            calibration = normalize_calibration(
                {
                    CALIBRATION_COMMODITY: commodity,
                    CALIBRATION_UNIT: user_input[CALIBRATION_UNIT],
                    CALIBRATION_SCALE: user_input[CALIBRATION_SCALE],
                }
            )
            return self._write_device_record(
                device_key,
                override=self._calibration_override,
                calibration=calibration,
            )

        # Pre-fill from an existing calibration when re-editing the same device.
        existing = normalize_calibration(
            self.config_entry.data.get(CONF_DEVICES, {})
            .get(device_key, {})
            .get(DEVICE_CALIBRATION)
        )
        unit_default = (
            existing[CALIBRATION_UNIT]
            if existing is not None and existing[CALIBRATION_COMMODITY] == commodity
            else default_unit(commodity)
        )
        scale_default = existing[CALIBRATION_SCALE] if existing is not None else 1.0

        unit_options = [
            SelectOptionDict(value=unit, label=unit)
            for unit in COMMODITY_UNITS[commodity]
        ]
        schema = vol.Schema(
            {
                vol.Required(CALIBRATION_UNIT, default=unit_default): SelectSelector(
                    SelectSelectorConfig(
                        options=unit_options,
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Required(CALIBRATION_SCALE, default=scale_default): NumberSelector(
                    NumberSelectorConfig(
                        min=0,
                        step="any",
                        mode=NumberSelectorMode.BOX,
                    )
                ),
            }
        )
        return self.async_show_form(
            step_id="calibration",
            data_schema=schema,
            description_placeholders={"commodity": commodity},
        )
