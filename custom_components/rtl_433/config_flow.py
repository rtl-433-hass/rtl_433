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
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .const import (
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
    availability-timeout override into the hub's ``entry.data["devices"]`` map.
    """

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

    async def async_step_device(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick a known device and set/clear its availability-timeout override.

        The override is written into the hub's ``entry.data["devices"]`` map (the
        single source of truth read by the coordinator), not into
        ``entry.options``.
        """
        devices: dict[str, Any] = dict(self.config_entry.data.get(CONF_DEVICES, {}))
        if not devices:
            return self.async_abort(reason="no_devices")

        if user_input is not None:
            device_key: str = user_input[CONF_DEVICE]
            override = user_input.get(DEVICE_TIMEOUT_OVERRIDE)

            data = dict(self.config_entry.data)
            new_devices = dict(data.get(CONF_DEVICES, {}))
            record = dict(new_devices.get(device_key, {}))
            if override is None:
                # Blank submission clears the override (fall back to hub default).
                record.pop(DEVICE_TIMEOUT_OVERRIDE, None)
            else:
                record[DEVICE_TIMEOUT_OVERRIDE] = override
            new_devices[device_key] = record
            data[CONF_DEVICES] = new_devices

            self.hass.config_entries.async_update_entry(self.config_entry, data=data)
            # Finish the flow without altering entry.options.
            return self.async_create_entry(title="", data=self.config_entry.options)

        options = [
            SelectOptionDict(
                value=device_key,
                label=f"{record.get(CONF_MODEL, device_key)} ({device_key})",
            )
            for device_key, record in sorted(devices.items())
        ]
        schema = vol.Schema(
            {
                vol.Required(CONF_DEVICE): SelectSelector(
                    SelectSelectorConfig(
                        options=options,
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Optional(DEVICE_TIMEOUT_OVERRIDE): vol.All(int, vol.Range(min=1)),
            }
        )
        return self.async_show_form(step_id="device", data_schema=schema)
