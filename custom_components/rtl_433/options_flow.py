"""Options flow for the rtl_433 integration's hub config entry.

A small menu offering a *hub* step, a *device* step, and a *mappings* step:

- **hub** persists the per-hub discovery toggle, the default availability
  timeout, and the manage-settings toggle to ``entry.options``.
- **device** picks a known device from the hub's ``entry.data["devices"]`` map
  and sets/clears that device's availability-timeout override, an optional
  utility-meter calibration (advancing to the *calibration* step for a real
  commodity), and a per-device motion clear-delay.
- **mappings** edits this hub's device-library overrides as YAML.

Split out of ``config_flow.py`` (which keeps the hub add/reconfigure/discovery
flow); ``Rtl433ConfigFlow.async_get_options_flow`` returns this class.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlowResult, OptionsFlow
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
    CONF_MANAGE_SETTINGS,
    CONF_MODEL,
    CONF_USER_MAPPINGS,
    DATA_ENTRY_LIBRARY,
    DEFAULT_AVAILABILITY_TIMEOUT,
    DEFAULT_MANAGE_SETTINGS,
    DEFAULT_MOTION_CLEAR_DELAY,
    DEVICE_CALIBRATION,
    DEVICE_FIELDS,
    DEVICE_MOTION_CLEAR_DELAY,
    DEVICE_TIMEOUT_OVERRIDE,
    DOMAIN,
)
from .mapping import Registry, lookup, normalize_overrides, validate_user_mappings

# Selector key for the device picker on the options device step.
CONF_DEVICE = "device"

# Documentation link for the Device-mappings step. Passed as a description
# placeholder (hassfest forbids literal URLs in translation strings).
MAPPINGS_DOCS_URL = (
    "https://github.com/rtl-433-hass/rtl_433#device-library-and-user-overrides"
)


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
        """Show and persist the hub-level options (writes ``entry.options``).

        The availability-timeout field is ``vol.Required`` and pre-filled with the
        plain :data:`DEFAULT_AVAILABILITY_TIMEOUT`, so the form echoes a value back
        on every save even when the user never touched it. Persisting that default
        as an *explicit* hub timeout would mask the device-class defaults — most
        importantly it would expire event-driven devices (doorbells, motion,
        contacts) that must never go unavailable on silence. So a submitted value
        equal to the plain default is treated as "use the per-device-type defaults"
        and the key is dropped; any deliberately chosen value (including ``0`` =
        never-expire) is persisted unchanged. This mirrors the one-time migration
        that strips the same sentinel from older entries and stops the entry from
        re-acquiring it on every options save.
        """
        if user_input is not None:
            options = dict(user_input)
            if options.get(CONF_AVAILABILITY_TIMEOUT) == DEFAULT_AVAILABILITY_TIMEOUT:
                options.pop(CONF_AVAILABILITY_TIMEOUT, None)
            return self.async_create_entry(title="", data=options)

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
