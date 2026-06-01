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
    ObjectSelector,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)
from homeassistant.helpers.service_info.hassio import HassioServiceInfo

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
    CONF_INITIAL_FREQUENCY,
    CONF_MANAGE_SETTINGS,
    CONF_MODEL,
    CONF_PATH,
    CONF_PORT,
    CONF_USER_MAPPINGS,
    DATA_ENTRY_LIBRARY,
    DEFAULT_AVAILABILITY_TIMEOUT,
    DEFAULT_MANAGE_SETTINGS,
    DEFAULT_MOTION_CLEAR_DELAY,
    DEFAULT_PATH,
    DEFAULT_PORT,
    DEVICE_CALIBRATION,
    DEVICE_FIELDS,
    DEVICE_MOTION_CLEAR_DELAY,
    DEVICE_TIMEOUT_OVERRIDE,
    DOMAIN,
)
from .coordinator import CannotConnect, Rtl433Coordinator
from .mapping import Registry, lookup, normalize_overrides, validate_user_mappings

# Whether to dial the server over ``wss://`` instead of ``ws://``.
CONF_SECURE = "secure"

# Selector key for the device picker on the options device step.
CONF_DEVICE = "device"

# Documentation link for the Device-mappings step. Passed as a description
# placeholder (hassfest forbids literal URLs in translation strings).
MAPPINGS_DOCS_URL = (
    "https://github.com/rtl-433-hass/rtl_433#device-library-and-user-overrides"
)


def _hub_unique_id(host: str, port: int) -> str:
    """Return the unique_id for a hub entry (one per host:port)."""
    return f"hub:{host}:{port}"


# Optional initial center-frequency field shared by both add flows. Presented in
# MHz (the unit the Center-frequency control uses); blank means "adopt the
# server's current frequency". Only honored when ``manage_settings`` is on.
_FREQUENCY_SELECTOR = NumberSelector(
    NumberSelectorConfig(
        min=0,
        step="any",
        mode=NumberSelectorMode.BOX,
        unit_of_measurement="MHz",
    )
)


STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
        vol.Required(CONF_PATH, default=DEFAULT_PATH): str,
        vol.Optional(CONF_SECURE, default=False): bool,
        vol.Optional(CONF_MANAGE_SETTINGS, default=DEFAULT_MANAGE_SETTINGS): bool,
        vol.Optional(CONF_DISCOVERY_ENABLED, default=True): bool,
        vol.Optional(CONF_INITIAL_FREQUENCY): _FREQUENCY_SELECTOR,
    }
)


class Rtl433ConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle setup of an rtl_433 hub (one config entry per server)."""

    VERSION = 2
    MINOR_VERSION = 4

    # Connection params carried from ``async_step_hassio`` into the confirm step.
    _discovery: dict[str, Any] | None = None

    def _find_entry_by_host_port(self, host: str, port: int) -> ConfigEntry | None:
        """Return an existing entry targeting this host:port, if any."""
        for entry in self._async_current_entries():
            if entry.data.get(CONF_HOST) == host and entry.data.get(CONF_PORT) == port:
                return entry
        return None

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
                # Guard against duplicating a radio already added by discovery
                # (which keys entries by a stable radio id, not host:port).
                if self._find_entry_by_host_port(host, port) is not None:
                    return self.async_abort(reason="already_configured")
                await self.async_set_unique_id(_hub_unique_id(host, port))
                self._abort_if_unique_id_configured()
                data: dict[str, Any] = {
                    CONF_HOST: host,
                    CONF_PORT: port,
                    CONF_PATH: path,
                    CONF_SECURE: secure,
                    CONF_MANAGE_SETTINGS: manage_settings,
                    CONF_DISCOVERY_ENABLED: user_input[CONF_DISCOVERY_ENABLED],
                }
                # The initial frequency rides the managed desired-state path, so
                # it is only meaningful (and only persisted) when managing settings.
                freq = user_input.get(CONF_INITIAL_FREQUENCY)
                if manage_settings and freq is not None:
                    data[CONF_INITIAL_FREQUENCY] = float(freq)
                return self.async_create_entry(
                    title=f"rtl_433 ({host})",
                    data=data,
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
                current_uid = entry.unique_id or ""
                if current_uid.startswith("hub:") or not current_uid:
                    # Legacy manual entry: keep the host:port identity scheme.
                    new_unique_id = _hub_unique_id(host, port)
                    await self.async_set_unique_id(new_unique_id)
                    # Abort only if a *different* entry already owns this
                    # unique_id; the entry being reconfigured must not abort
                    # against itself.
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
                # Discovered/adopted entry: preserve its stable radio unique_id.
                return self.async_update_reload_and_abort(
                    entry,
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
    # Supervisor (hassio) discovery flow                                 #
    # ------------------------------------------------------------------ #
    async def async_step_hassio(
        self, discovery_info: HassioServiceInfo
    ) -> ConfigFlowResult:
        """Handle a Supervisor add-on discovery message for one radio.

        Reads the advertised connection target and the add-on's stable per-radio
        ``unique_id``. A pre-existing entry on the same ``host:port`` is adopted
        onto that stable id (migration; aborts ``already_configured``). Otherwise
        the stable id is set so a re-advertisement updates the stored connection
        in place, and a genuinely new radio is routed to the confirm step.
        """
        config = discovery_info.config
        radio_uid = config.get("unique_id")
        if not radio_uid:
            return self.async_abort(reason="invalid_discovery_info")

        host: str = config[CONF_HOST]
        port: int = config[CONF_PORT]
        path: str = config.get(CONF_PATH, DEFAULT_PATH)
        secure: bool = config.get(CONF_SECURE, False)
        addon: str = config.get("addon", "rtl_433")

        # Adopt/migrate a pre-existing entry on the same server onto the stable id.
        existing = self._find_entry_by_host_port(host, port)
        if existing is not None and existing.unique_id != radio_uid:
            self.hass.config_entries.async_update_entry(
                existing,
                unique_id=radio_uid,
                data={
                    **existing.data,
                    CONF_HOST: host,
                    CONF_PORT: port,
                    CONF_PATH: path,
                    CONF_SECURE: secure,
                },
            )
            return self.async_abort(reason="already_configured")

        # Same radio (matched by stable id) — update connection target in place.
        await self.async_set_unique_id(radio_uid)
        self._abort_if_unique_id_configured(
            updates={
                CONF_HOST: host,
                CONF_PORT: port,
                CONF_PATH: path,
                CONF_SECURE: secure,
            }
        )

        self._discovery = {
            CONF_HOST: host,
            CONF_PORT: port,
            CONF_PATH: path,
            CONF_SECURE: secure,
            "addon": addon,
        }
        self.context["title_placeholders"] = {"name": f"{addon} ({host}:{port})"}
        return await self.async_step_hassio_confirm()

    async def async_step_hassio_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm adoption of a discovered radio, then create the entry.

        Shows a confirmation form (``addon``/``host``/``port`` placeholders) that
        offers the same setup choices as the manual flow: the manage-settings and
        discover-new-devices toggles and an optional initial frequency. On submit,
        validates connectivity and creates the hub entry; a failed validation
        re-shows the form with ``cannot_connect``.
        """
        assert self._discovery is not None
        disc = self._discovery
        placeholders = {
            "addon": disc["addon"],
            "host": disc[CONF_HOST],
            "port": str(disc[CONF_PORT]),
        }
        confirm_schema = vol.Schema(
            {
                vol.Optional(
                    CONF_MANAGE_SETTINGS, default=DEFAULT_MANAGE_SETTINGS
                ): bool,
                vol.Optional(CONF_DISCOVERY_ENABLED, default=True): bool,
                vol.Optional(CONF_INITIAL_FREQUENCY): _FREQUENCY_SELECTOR,
            }
        )

        if user_input is not None:
            try:
                await Rtl433Coordinator.validate_connection(
                    self.hass,
                    disc[CONF_HOST],
                    disc[CONF_PORT],
                    disc[CONF_PATH],
                    secure=disc[CONF_SECURE],
                )
            except CannotConnect:
                return self.async_show_form(
                    step_id="hassio_confirm",
                    data_schema=confirm_schema,
                    errors={"base": "cannot_connect"},
                    description_placeholders=placeholders,
                )
            manage_settings: bool = user_input[CONF_MANAGE_SETTINGS]
            data: dict[str, Any] = {
                CONF_HOST: disc[CONF_HOST],
                CONF_PORT: disc[CONF_PORT],
                CONF_PATH: disc[CONF_PATH],
                CONF_SECURE: disc[CONF_SECURE],
                CONF_MANAGE_SETTINGS: manage_settings,
                CONF_DISCOVERY_ENABLED: user_input[CONF_DISCOVERY_ENABLED],
            }
            # As in the manual flow, the initial frequency only applies (and is
            # only persisted) when settings are managed.
            freq = user_input.get(CONF_INITIAL_FREQUENCY)
            if manage_settings and freq is not None:
                data[CONF_INITIAL_FREQUENCY] = float(freq)
            return self.async_create_entry(
                title=f"rtl_433 ({disc[CONF_HOST]}:{disc[CONF_PORT]})",
                data=data,
            )

        return self.async_show_form(
            step_id="hassio_confirm",
            data_schema=confirm_schema,
            description_placeholders=placeholders,
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
    # Per-device motion clear-delay override submitted on the device step, carried
    # through the (optional) calibration step into the finish path. ``None`` means
    # "no value submitted" -> clear any prior override.
    _motion_clear_delay: int | None = None

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the options menu."""
        return self.async_show_menu(
            step_id="init",
            menu_options=["hub", "device", "mappings"],
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
                ): vol.All(int, vol.Range(min=0)),
                vol.Required(CONF_MANAGE_SETTINGS, default=manage_default): bool,
            }
        )
        return self.async_show_form(step_id="hub", data_schema=schema)

    async def async_step_mappings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit this hub's device-library mapping overrides as YAML.

        Renders Home Assistant's native YAML editor (:class:`ObjectSelector`)
        pre-filled with the hub's current ``entry.data[CONF_USER_MAPPINGS]``. On
        submit the parsed object is validated by :func:`validate_user_mappings`;
        any problems re-show the form (storing nothing) with the offending fields
        surfaced. A valid object is normalized and written into ``entry.data``
        (which fires the update listener and reloads the hub); ``entry.options``
        is passed back unchanged so the dialog closes without clobbering options.
        """
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {"problems": "", "docs_url": MAPPINGS_DOCS_URL}

        if user_input is not None:
            raw = user_input.get(CONF_USER_MAPPINGS) or {}
            problems = validate_user_mappings(raw)
            if problems:
                errors["base"] = "invalid_mappings"
                placeholders["problems"] = "; ".join(problems)
            else:
                self.hass.config_entries.async_update_entry(
                    self.config_entry,
                    data={
                        **self.config_entry.data,
                        CONF_USER_MAPPINGS: normalize_overrides(raw),
                    },
                )
                return self.async_create_entry(
                    title="", data=dict(self.config_entry.options)
                )

        current = self.config_entry.data.get(CONF_USER_MAPPINGS) or {}
        schema = vol.Schema(
            {
                vol.Optional(CONF_USER_MAPPINGS, default=current): ObjectSelector(),
            }
        )
        return self.async_show_form(
            step_id="mappings",
            data_schema=schema,
            errors=errors,
            description_placeholders=placeholders,
        )

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

    def _registry(self) -> Registry | None:
        """Return this hub's merged device-library registry cached at setup.

        The hub builds the shipped library + this hub's user overrides at setup
        and caches ``(registry, skip_keys)`` per entry under
        ``hass.data[DOMAIN][DATA_ENTRY_LIBRARY][entry_id]``; reuse it so descriptor
        lookups never re-read the YAML on the event loop. Returns ``None`` if the
        hub has not finished loading (the conditional clear-delay field then
        simply does not appear).
        """
        return (
            self.hass.data.get(DOMAIN, {})
            .get(DATA_ENTRY_LIBRARY, {})
            .get(self.config_entry.entry_id, (None, None))[0]
        )

    def _is_motion_bearing(self, device_key: str) -> bool:
        """Return ``True`` if the device has a field carrying a ``clear_delay``.

        A device is "motion-bearing" iff any of its observed fields resolves
        (model-scoped) to a descriptor with a truthy ``clear_delay`` -- i.e. a
        motion/event binary_sensor that auto-clears. Only such devices expose the
        per-device clear-delay knob.
        """
        record = self.config_entry.data.get(CONF_DEVICES, {}).get(device_key, {})
        model = record.get(CONF_MODEL)
        registry = self._registry()
        return any(
            (descriptor := lookup(field_key, model, registry)) is not None
            and descriptor.clear_delay
            for field_key in record.get(DEVICE_FIELDS, [])
        )

    def _write_device_record(
        self,
        device_key: str,
        *,
        override: int | None,
        calibration: dict[str, Any] | None,
        motion_clear_delay: int | None,
    ) -> ConfigFlowResult:
        """Persist a device's timeout override + calibration; finish the flow.

        Writes the timeout override + calibration into the hub's
        ``entry.data["devices"]`` map (the single source of truth read by the
        coordinator and the entity build). ``calibration is None`` clears any
        prior calibration. The resulting ``async_update_entry`` fires
        ``_async_update_listener``, which reloads the hub iff the calibration map
        actually changed.

        The per-device motion clear-delay is persisted into ``entry.options``
        instead (keyed by ``DEVICE_MOTION_CLEAR_DELAY``); setup copies that into
        the device record. ``motion_clear_delay is None`` clears any prior
        override (the field falls back to the descriptor default).
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

        # The motion clear-delay lives in entry.options (setup copies it into the
        # device record). Merge it into the per-device options sub-map; a blank
        # submission clears the override.
        options = dict(self.config_entry.options)
        opt_devices = dict(options.get(CONF_DEVICES, {}))
        opt_record = dict(opt_devices.get(device_key, {}))
        if motion_clear_delay is None:
            opt_record.pop(DEVICE_MOTION_CLEAR_DELAY, None)
        else:
            opt_record[DEVICE_MOTION_CLEAR_DELAY] = motion_clear_delay
        if opt_record:
            opt_devices[device_key] = opt_record
        else:
            opt_devices.pop(device_key, None)
        options[CONF_DEVICES] = opt_devices

        return self.async_create_entry(title="", data=options)

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
            # Optional + no key in the schema for non-motion devices -> ``None``.
            clear_delay = user_input.get(DEVICE_MOTION_CLEAR_DELAY)

            if commodity == COMMODITY_NONE:
                return self._write_device_record(
                    device_key,
                    override=override,
                    calibration=None,
                    motion_clear_delay=clear_delay,
                )

            # Carry the device + timeout + commodity into the calibration step.
            self._calibration_device = device_key
            self._calibration_override = override
            self._calibration_commodity = commodity
            self._motion_clear_delay = clear_delay
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
        schema_dict: dict[Any, Any] = {
            vol.Required(CONF_DEVICE): SelectSelector(
                SelectSelectorConfig(
                    options=options,
                    mode=SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Optional(DEVICE_TIMEOUT_OVERRIDE): vol.All(int, vol.Range(min=0)),
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
        # The clear-delay knob is only meaningful for motion-bearing devices
        # (those with a field whose descriptor carries a ``clear_delay``). The
        # device picker and this field share one form, so -- like the commodity
        # pre-fill -- the field is conditionally included iff any device on the
        # hub is motion-bearing, and pre-filled from the persisted override only
        # when there is exactly one device (the focused-config case).
        if any(self._is_motion_bearing(key) for key in devices):
            clear_default = DEFAULT_MOTION_CLEAR_DELAY
            if len(devices) == 1:
                clear_default = (
                    self.config_entry.data.get(CONF_DEVICES, {})
                    .get(next(iter(devices)), {})
                    .get(DEVICE_MOTION_CLEAR_DELAY, DEFAULT_MOTION_CLEAR_DELAY)
                )
            schema_dict[
                vol.Optional(DEVICE_MOTION_CLEAR_DELAY, default=clear_default)
            ] = vol.All(int, vol.Range(min=1))
        return self.async_show_form(
            step_id="device", data_schema=vol.Schema(schema_dict)
        )

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
                motion_clear_delay=self._motion_clear_delay,
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
