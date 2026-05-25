"""Config, options, and discovery flows for the rtl_433 integration.

This module implements three flows backed by one config-flow domain:

- **Hub user flow** (``async_step_user``): collects a single rtl_433 HTTP
  server's WebSocket connection parameters (host/port/path, optional ``wss://``
  via a ``secure`` toggle), validates reachability with the coordinator's
  ``validate_connection`` helper, and creates a *hub* config entry. The hub's
  unique_id is derived from host/port so the same server cannot be added twice.

- **Per-device discovery flow** (``async_step_integration_discovery`` +
  ``async_step_confirm``): the coordinator's ``new_device_callback`` (wired in a
  later task) calls ``discovery_flow.async_create_flow`` with the parent hub
  entry id, the normalized device key, and the model. The flow sets an
  instance-scoped unique_id (``{hub_entry_id}:{device_key}``) so two hubs that
  observe the same model+id never collide, and presents a confirm card. Accepting
  creates a *device* config entry; dismissing the discovered card makes Home
  Assistant record a ``SOURCE_IGNORE`` entry under the same unique_id, so the
  device is not re-surfaced.

- **Options flow** (``async_get_options_flow``): branches on the entry type. A
  hub exposes the per-hub discovery toggle and the default availability timeout;
  a device exposes an optional per-device availability-timeout override. Both
  persist to ``entry.options``.

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

from .const import (
    CONF_AVAILABILITY_TIMEOUT,
    CONF_DEVICE_KEY,
    CONF_DISCOVERY_ENABLED,
    CONF_ENTRY_TYPE,
    CONF_HOST,
    CONF_HUB_ENTRY_ID,
    CONF_MODEL,
    CONF_PATH,
    CONF_PORT,
    DEFAULT_AVAILABILITY_TIMEOUT,
    DEFAULT_PATH,
    DEFAULT_PORT,
    DOMAIN,
    ENTRY_TYPE_DEVICE,
    ENTRY_TYPE_HUB,
)
from .coordinator import CannotConnect, Rtl433Coordinator

# Discovery-info keys carried by ``async_step_integration_discovery``. These are
# the contract between the coordinator's new-device callback (wired later) and
# this flow; keep them stable.
CONF_SECURE = "secure"


def is_hub_entry(entry: ConfigEntry) -> bool:
    """Return ``True`` if ``entry`` is the per-instance hub config entry."""
    return entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_HUB


def is_device_entry(entry: ConfigEntry) -> bool:
    """Return ``True`` if ``entry`` is a per-device config entry."""
    return entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_DEVICE


def _hub_unique_id(host: str, port: int) -> str:
    """Return the unique_id for a hub entry (one per host:port)."""
    return f"hub:{host}:{port}"


def _device_unique_id(hub_entry_id: str, device_key: str) -> str:
    """Return the instance-scoped unique_id for a per-device entry.

    Scoping the device id by the parent hub entry id means two hubs observing
    the same ``model``+``id`` produce distinct unique_ids and never collide.
    """
    return f"{hub_entry_id}:{device_key}"


STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
        vol.Required(CONF_PATH, default=DEFAULT_PATH): str,
        vol.Optional(CONF_SECURE, default=False): bool,
    }
)


class Rtl433ConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle hub setup and per-device discovery for rtl_433."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize transient discovery state."""
        self._discovery_hub_entry_id: str | None = None
        self._discovery_device_key: str | None = None
        self._discovery_model: str | None = None

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
                        CONF_ENTRY_TYPE: ENTRY_TYPE_HUB,
                        CONF_HOST: host,
                        CONF_PORT: port,
                        CONF_PATH: path,
                        CONF_SECURE: secure,
                    },
                )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_SCHEMA,
            errors=errors,
        )

    # ------------------------------------------------------------------ #
    # Per-device discovery flow                                          #
    # ------------------------------------------------------------------ #
    async def async_step_integration_discovery(
        self, discovery_info: dict[str, Any]
    ) -> ConfigFlowResult:
        """Receive a newly observed device from the coordinator's callback.

        Sets an instance-scoped unique_id and aborts if the device is already
        configured or has been ignored (HA's ``_abort_if_unique_id_configured``
        covers ``SOURCE_IGNORE`` entries), so a dismissed device never
        re-prompts. Otherwise presents the confirm card.
        """
        hub_entry_id: str = discovery_info[CONF_HUB_ENTRY_ID]
        device_key: str = discovery_info[CONF_DEVICE_KEY]
        model: str = discovery_info.get(CONF_MODEL, "")

        await self.async_set_unique_id(_device_unique_id(hub_entry_id, device_key))
        self._abort_if_unique_id_configured()

        self._discovery_hub_entry_id = hub_entry_id
        self._discovery_device_key = device_key
        self._discovery_model = model

        # Friendly title for the discovered card in the Integrations list.
        self.context["title_placeholders"] = {
            "model": model or device_key,
            "device_key": device_key,
        }

        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm a discovered device and create its per-device entry."""
        hub_entry_id = self._discovery_hub_entry_id
        device_key = self._discovery_device_key
        model = self._discovery_model or ""

        if user_input is not None:
            return self.async_create_entry(
                title=f"{model} ({device_key})" if model else str(device_key),
                data={
                    CONF_ENTRY_TYPE: ENTRY_TYPE_DEVICE,
                    CONF_HUB_ENTRY_ID: hub_entry_id,
                    CONF_DEVICE_KEY: device_key,
                    CONF_MODEL: model,
                },
            )

        return self.async_show_form(
            step_id="confirm",
            description_placeholders={
                "model": model or str(device_key),
                "device_key": str(device_key),
            },
        )

    # ------------------------------------------------------------------ #
    # Options flow                                                       #
    # ------------------------------------------------------------------ #
    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow appropriate to the entry type."""
        if is_device_entry(config_entry):
            return Rtl433DeviceOptionsFlow()
        return Rtl433HubOptionsFlow()


class Rtl433HubOptionsFlow(OptionsFlow):
    """Options for a hub entry: discovery toggle and default availability."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and persist the hub-level options."""
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

        schema = vol.Schema(
            {
                vol.Required(CONF_DISCOVERY_ENABLED, default=discovery_default): bool,
                vol.Required(
                    CONF_AVAILABILITY_TIMEOUT, default=timeout_default
                ): vol.All(int, vol.Range(min=1)),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)


class Rtl433DeviceOptionsFlow(OptionsFlow):
    """Options for a device entry: optional availability-timeout override."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and persist the per-device availability-timeout override."""
        if user_input is not None:
            # An empty submission clears the override (falls back to the hub).
            data = {k: v for k, v in user_input.items() if v is not None}
            return self.async_create_entry(title="", data=data)

        entry = self.config_entry
        current = entry.options.get(CONF_AVAILABILITY_TIMEOUT)
        timeout_field = vol.Optional(CONF_AVAILABILITY_TIMEOUT)
        if current is not None:
            timeout_field = vol.Optional(CONF_AVAILABILITY_TIMEOUT, default=current)

        schema = vol.Schema(
            {
                timeout_field: vol.All(int, vol.Range(min=1)),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
