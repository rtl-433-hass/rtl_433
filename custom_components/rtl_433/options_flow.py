"""Options flow for the rtl_433 integration's hub config entry.

A small menu offering a *hub* step, a *device* step, a *mappings* step, and a
*replace* step:

- **hub** persists the per-hub discovery toggle, the default availability
  timeout, and the manage-settings toggle to ``entry.options``.
- **device** picks a known device from the hub's ``entry.data["devices"]`` map,
  then **device_settings** sets/clears that device's availability-timeout
  override, an optional utility-meter calibration (advancing to the *calibration*
  step for a real commodity), and a per-device motion clear-delay. The picker is
  its own step so every default on the settings form can be derived from the
  selected device.
- **mappings** edits this hub's device-library overrides as YAML.
- **replace** picks a device to keep, then **replace_target** picks the newly
  seen device that is really the same hardware, and re-keys the survivor onto it
  via :func:`~.device_replace.async_replace_device` (the battery-swap recovery).
  It is last on the menu: the rarest action, and the most consequential.

Split out of ``config_flow.py`` (which keeps the hub add/reconfigure/discovery
flow); ``Rtl433ConfigFlow.async_get_options_flow`` returns this class.
"""

from __future__ import annotations

from typing import Any

from pyrtl_433.library import (
    Registry,
    lookup,
    normalize_overrides,
    validate_user_mappings,
)
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
from .device_replace import DeviceReplaceError, async_replace_device

# Selector key for the device picker on the options device step.
CONF_DEVICE = "device"

# Documentation link for the Device-mappings step. Passed as a description
# placeholder (hassfest forbids literal URLs in translation strings).
MAPPINGS_DOCS_URL = (
    "https://github.com/rtl-433-hass/rtl_433#device-library-and-user-overrides"
)


class Rtl433OptionsFlow(OptionsFlow):
    """Hub options: a menu with a hub-settings step and a device-settings pair.

    The hub step persists the discovery toggle and the default availability
    timeout to ``entry.options``. The device picker chooses one device and the
    device-settings step writes that device's availability-timeout override and
    an optional utility-meter calibration into ``entry.data["devices"]``.
    """

    # The device chosen on the device step, carried into the device-settings step
    # (whose defaults are all derived from it) and on into the calibration step.
    _device_key: str = ""
    # State carried from the device-settings step into the calibration step.
    _calibration_override: int | None = None
    _calibration_commodity: str = COMMODITY_NONE
    # Per-device motion clear-delay override submitted on the settings step, carried
    # through the (optional) calibration step into the finish path. ``None`` means
    # "no value submitted" -> clear any prior override.
    _motion_clear_delay: int | None = None
    # The device to keep, chosen on the replace step and carried into
    # replace_target (whose candidate list and ordering are both derived from it).
    _replace_old_key: str = ""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the options menu."""
        return self.async_show_menu(
            step_id="init",
            menu_options=["hub", "device", "mappings", "replace"],
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

    def _device_label(self, device_key: str, record: dict[str, Any]) -> str:
        """Human label for the device picker, annotated with a detected commodity.

        Surfaces the ``MeterType`` / ``ert_type`` hint *in the picker itself* so a
        user with several meters can see the integration already recognizes one as
        gas/water before choosing it -- the per-device calibration is otherwise
        easy to miss.
        """
        label = f"{record.get(CONF_MODEL, device_key)} ({device_key})"
        commodity = self._device_commodity_default(device_key)
        if commodity != COMMODITY_NONE:
            label = f"{label} — {commodity} detected"
        return label

    async def async_step_device(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick the device whose settings to edit.

        Deliberately a picker-only step: every knob on the following
        :meth:`async_step_device_settings` form is pre-filled *from the chosen
        device*, which is impossible while the picker shares a form with them.
        """
        devices: dict[str, Any] = dict(self.config_entry.data.get(CONF_DEVICES, {}))
        if not devices:
            return self.async_abort(reason="no_devices")

        if user_input is not None:
            self._device_key = user_input[CONF_DEVICE]
            return await self.async_step_device_settings()

        options = [
            SelectOptionDict(
                value=device_key, label=self._device_label(device_key, record)
            )
            for device_key, record in sorted(devices.items())
        ]
        return self.async_show_form(
            step_id="device",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_DEVICE): SelectSelector(
                        SelectSelectorConfig(
                            options=options,
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            ),
        )

    async def async_step_device_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Set the chosen device's timeout override, commodity and clear-delay.

        Every default here reflects **the device selected on the previous step**:
        the timeout override and motion clear-delay are pre-filled from that
        device's persisted record, and the commodity from its existing calibration
        or, failing that, its decoded ``MeterType`` / ``ert_type`` hint.

        Choosing commodity ``none`` writes the record (clearing any calibration)
        and finishes; choosing a real commodity advances to
        :meth:`async_step_calibration` to pick a commodity-constrained base unit
        + scale.
        """
        device_key = self._device_key
        record: dict[str, Any] = self.config_entry.data.get(CONF_DEVICES, {}).get(
            device_key, {}
        )

        if user_input is not None:
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

            # Carry the timeout + commodity into the calibration step.
            self._calibration_override = override
            self._calibration_commodity = commodity
            self._motion_clear_delay = clear_delay
            return await self.async_step_calibration()

        commodity_options = [
            SelectOptionDict(value=value, label=value)
            for value in CALIBRATION_COMMODITIES
        ]
        # Pre-fill the commodity from this device's existing calibration when it
        # has one, else from its decoded MeterType / ert_type hint.
        existing = normalize_calibration(record.get(DEVICE_CALIBRATION))
        commodity_default = (
            existing[CALIBRATION_COMMODITY]
            if existing is not None
            else self._device_commodity_default(device_key)
        )
        # ``suggested_value`` (not ``default``) so an emptied field still submits
        # as absent and clears the persisted override.
        schema_dict: dict[Any, Any] = {
            vol.Optional(
                DEVICE_TIMEOUT_OVERRIDE,
                description={"suggested_value": record.get(DEVICE_TIMEOUT_OVERRIDE)},
            ): vol.All(int, vol.Range(min=0)),
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
        # (those with a field whose descriptor carries a ``clear_delay``), so it
        # appears iff *this* device is one, pre-filled from its persisted override.
        if self._is_motion_bearing(device_key):
            schema_dict[
                vol.Optional(
                    DEVICE_MOTION_CLEAR_DELAY,
                    default=record.get(
                        DEVICE_MOTION_CLEAR_DELAY, DEFAULT_MOTION_CLEAR_DELAY
                    ),
                )
            ] = vol.All(int, vol.Range(min=1))
        return self.async_show_form(
            step_id="device_settings",
            data_schema=vol.Schema(schema_dict),
            description_placeholders={"device": self._device_label(device_key, record)},
        )

    async def async_step_calibration(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick the commodity-constrained base unit + scale for a consumption meter.

        Reached from :meth:`async_step_device_settings` only when a real commodity was
        chosen. The unit selector is constrained to the units Home Assistant
        recognizes as convertible for the commodity's device_class, so the
        resulting consumption sensor is Energy-dashboard-eligible. The
        ``{commodity, unit, scale}`` triple is written into the device record and
        applies to the device's known consumption field(s) only.
        """
        device_key = self._device_key
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

    async def async_step_replace(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick the device to keep -- the one whose history should survive.

        Picker-only for the same reason :meth:`async_step_device` is: the next
        form is derived entirely from this choice. The candidate list on
        :meth:`async_step_replace_target` excludes this device and sorts
        same-model candidates first, neither of which is possible while both
        picks share one form. Only devices with a stored record are offered here
        -- the survivor must have settings and a device row to carry across -- so
        an empty map aborts with ``no_devices``, matching the device step.
        """
        devices: dict[str, Any] = dict(self.config_entry.data.get(CONF_DEVICES, {}))
        if not devices:
            return self.async_abort(reason="no_devices")

        if user_input is not None:
            self._replace_old_key = user_input[CONF_DEVICE]
            return await self.async_step_replace_target()

        options = [
            SelectOptionDict(
                value=device_key, label=self._device_label(device_key, record)
            )
            for device_key, record in sorted(devices.items())
        ]
        return self.async_show_form(
            step_id="replace",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_DEVICE): SelectSelector(
                        SelectSelectorConfig(
                            options=options,
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            ),
        )

    def _replacement_model(
        self, device_key: str, devices: dict[str, Any], seen: dict[str, Any]
    ) -> str:
        """Model for a replacement candidate: stored record, else last event.

        A candidate can legitimately have no record in ``entry.data["devices"]``
        -- with discovery off the coordinator hears a device it never registers,
        and that is exactly the device a replace has to offer -- so the model
        falls back to the coordinator's last :class:`NormalizedEvent` for the key
        and then to ``""``. Never raises: a missing record, a missing event and a
        blank model all degrade to the bare key in the picker.
        """
        record: dict[str, Any] = devices.get(device_key, {})
        return record.get(CONF_MODEL) or getattr(seen.get(device_key), "model", "")

    async def async_step_replace_target(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick the new identity to adopt onto the device kept on the previous step.

        Candidates are the **union** of the stored devices map and the
        coordinator's seen-device keys, because the docs recommend turning
        discovery off in urban areas: with discovery off the replacement never
        gets a registered row, but the coordinator has still heard it and
        :func:`async_replace_device` accepts an unregistered ``new_key``. The
        coordinator is reached the way :meth:`_device_commodity_default` reaches
        it and is guarded the same way -- the options flow can be opened while the
        entry is not loaded, and then the devices map alone is the candidate set.
        Same-model candidates sort first, since a battery swap keeps the model,
        and an empty candidate set (a single-device hub) aborts rather than
        showing a dead-end dropdown.

        The finish path deliberately hands back ``entry.options`` unchanged: the
        helper has already written ``entry.data`` and reloaded the entry, so (as
        in :meth:`async_step_mappings`) this flow only has to close without
        clobbering options. A :class:`DeviceReplaceError` is a user-facing
        outcome -- a picker gone stale against a reloaded entry, say -- so it
        re-shows this form as an error instead of escaping as a traceback.
        """
        old_key = self._replace_old_key
        devices: dict[str, Any] = dict(self.config_entry.data.get(CONF_DEVICES, {}))
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                await async_replace_device(
                    self.hass, self.config_entry, old_key, user_input[CONF_DEVICE]
                )
            except DeviceReplaceError:
                errors["base"] = "replace_failed"
            else:
                return self.async_create_entry(
                    title="", data=dict(self.config_entry.options)
                )

        coordinator = self.hass.data.get(DOMAIN, {}).get(self.config_entry.entry_id)
        seen: dict[str, Any] = getattr(coordinator, "devices", None) or {}

        old_model = self._replacement_model(old_key, devices, seen)
        candidates = sorted(
            (set(devices) | set(seen)) - {old_key},
            key=lambda key: (
                0
                if old_model
                and self._replacement_model(key, devices, seen) == old_model
                else 1,
                key,
            ),
        )
        if not candidates:
            return self.async_abort(reason="no_replacement_candidates")

        options = [
            SelectOptionDict(
                value=device_key,
                label=(
                    f"{model} ({device_key})"
                    if (model := self._replacement_model(device_key, devices, seen))
                    else device_key
                ),
            )
            for device_key in candidates
        ]
        return self.async_show_form(
            step_id="replace_target",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_DEVICE): SelectSelector(
                        SelectSelectorConfig(
                            options=options,
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            ),
            errors=errors,
            description_placeholders={
                "device": self._device_label(old_key, devices.get(old_key, {}))
            },
        )
