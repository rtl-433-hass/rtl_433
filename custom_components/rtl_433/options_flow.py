"""Options flow for the rtl_433 integration's hub config entry.

A small menu offering an *add devices* step, an *ignored devices* step, a *hub*
step, a *device* step, a *mappings* step, and a *replace* step:

- **add_devices** renders the coordinator's in-memory pending list -- every
  device heard since the last restart that the user has neither added nor
  ignored -- and on submit adopts the selected keys and ignores the ones the user
  never wants offered again. It leads the menu because it is the route by which
  an RF device reaches the Home Assistant device registry.
- **ignored_devices** un-ignores keys from ``entry.data["ignored_devices"]`` so
  the devices are offered again on their next transmission.

Neither approval step implements adopting or ignoring itself: both call
:mod:`.adoption`, the service they share with the discovery panel's WebSocket
API, so a device added from a form and one added from the panel are the same
device. What lives here is the *presentation* -- what the picker labels look
like, when a step aborts, and how the dialog closes.
- **hub** persists the default availability timeout and the manage-settings
  toggle to ``entry.options``.
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
  Its candidates include *pending* devices, because the replacement for a
  battery-swapped sensor is by definition one the user has not added yet.
  It is last on the menu: the rarest action, and the most consequential.

Split out of ``config_flow.py`` (which keeps the hub add/reconfigure/discovery
flow); ``Rtl433ConfigFlow.async_get_options_flow`` returns this class.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

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
from homeassistant.util import dt as dt_util

from .adoption import async_adopt_devices, async_ignore_devices, async_unignore_devices
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
from .hub_settings import _hub_ignored_devices
from .mapping import Registry, lookup, normalize_overrides, validate_user_mappings

if TYPE_CHECKING:
    from datetime import datetime

    from .coordinator import PendingDevice, Rtl433Coordinator

# Selector key for the device picker on the options device step.
CONF_DEVICE = "device"
# Selector keys for the two independent multi-selects on the add-devices step and
# for the un-ignore picker on the ignored-devices step. They name form fields
# only: nothing is persisted under them, the steps translate a selection into
# ``entry.data[CONF_DEVICES]`` / ``entry.data[CONF_IGNORED_DEVICES]``.
CONF_ADD_DEVICES = "add"
CONF_IGNORE_DEVICES = "ignore"
CONF_UNIGNORE_DEVICES = "unignore"

# Documentation link for the Device-mappings step. Passed as a description
# placeholder (hassfest forbids literal URLs in translation strings).
MAPPINGS_DOCS_URL = (
    "https://github.com/rtl-433-hass/rtl_433#device-library-and-user-overrides"
)


def _model_label(model: str, device_key: str) -> str:
    """Name one device for a picker: its model and key, or the key alone.

    Every picker in this flow names a device the same way, and they have to: the
    add-devices step, the ignored-devices step, the device picker and both
    replace steps can all show the same hardware in one session, and a user who
    sees it written three ways has to work out that they are the same device. A
    model is not always known -- an ignored device usually has no stored record
    and a bad decode may carry no model at all -- and then the key stands alone
    rather than being prefixed with a blank.
    """
    return f"{model} ({device_key})" if model else device_key


def _pending_label(record: PendingDevice, now: datetime) -> str:
    """Describe one pending device densely enough to judge it from the picker.

    A neighbour's sensor, a one-off bad decode and the device the user is
    actually waiting for are indistinguishable by name, so the label leads with
    the model and device key and then carries the three signals that do
    discriminate: how often the device has been heard (a bad decode is typically
    heard once, a real sensor keeps checking in), how strong its most recent
    frame was, and how long ago that was. The signal reading comes from
    :attr:`~.coordinator.PendingDevice.signal` — the same property the WebSocket
    payload reports, so the form and the discovery panel cannot disagree about a
    device — and one reporting no level at all (the server was started without
    ``-M level``) simply omits the segment rather than showing a placeholder.

    ``now`` is passed in rather than read here so every row of one render shares
    a single clock, and the age is clamped to it because
    :func:`~homeassistant.util.dt.get_age` raises on a future timestamp and a
    form render must never raise.
    """
    parts = [
        _model_label(record.model or "unknown", record.key),
        f"seen {record.count}x",
    ]
    if (level := record.signal) is not None:
        parts.append(f"{level:.1f} dB")
    parts.append(f"last seen {dt_util.get_age(min(record.last_seen, now))} ago")
    return " — ".join(parts)


class Rtl433OptionsFlow(OptionsFlow):
    """Hub options: the approval steps, a hub-settings step and a device pair.

    The add-devices step is where a heard device becomes a Home Assistant device
    (or is ignored for good), and the ignored-devices step reverses the latter.
    The hub step persists the default availability timeout and the
    manage-settings toggle to ``entry.options``. The device picker chooses one device and the
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
        """Show the options menu.

        The two approval steps lead, as a pair: adding a heard device is the only
        way one reaches Home Assistant at all, and "ignored devices" is where a
        user goes looking for a device that has stopped being offered. The
        settings steps follow in their established order, with *replace* still
        last as the rarest and most consequential action.
        """
        return self.async_show_menu(
            step_id="init",
            menu_options=[
                "add_devices",
                "ignored_devices",
                "hub",
                "device",
                "mappings",
                "replace",
            ],
        )

    def _coordinator(self) -> Rtl433Coordinator | None:
        """Return this hub's running coordinator, or ``None`` when unloaded.

        The options flow can be opened while the entry is not loaded (a hub whose
        server is unreachable, or one the user disabled), and the pending list
        lives only in the coordinator's memory -- so the steps that need it have
        to be able to say so rather than raise.
        """
        return self.hass.data.get(DOMAIN, {}).get(self.config_entry.entry_id)

    async def async_step_add_devices(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """List the devices heard but not added, and add or ignore them.

        This is the only route by which an RF device reaches the Home Assistant
        device registry: the coordinator records everything it hears into an
        in-memory pending list, and nothing leaves that list without an explicit
        choice here. The list is rebuilt from live traffic after every restart, so
        an empty one shortly after a reload is normal -- it means nothing has
        transmitted yet, not that anything is broken -- and the step aborts saying
        so rather than rendering a form with nothing to choose from.

        The two multi-selects are deliberately independent so one submit can add
        some devices and ignore others: that is how a long list is actually worked
        through (the reporter in issue #128 heard 77 devices in a day). Selecting
        the same device in both is a contradiction the flow refuses to resolve on
        the user's behalf -- it re-shows the form with an error and writes
        nothing, rather than silently picking one of the two meanings.
        """
        coordinator = self._coordinator()
        if coordinator is None:
            return self.async_abort(reason="hub_not_loaded")
        if not coordinator.pending:
            return self.async_abort(reason="no_pending_devices")

        errors: dict[str, str] = {}
        if user_input is not None:
            add = list(user_input.get(CONF_ADD_DEVICES, []))
            ignore = list(user_input.get(CONF_IGNORE_DEVICES, []))
            if set(add) & set(ignore):
                errors["base"] = "add_and_ignore_conflict"
            else:
                return await self._apply_add_and_ignore(coordinator, add, ignore)

        now = dt_util.utcnow()
        # Ordered by the coordinator, which is also what the discovery panel
        # renders, so a long list is worked from the top in the same order on
        # both surfaces.
        options = [
            SelectOptionDict(value=record.key, label=_pending_label(record, now))
            for record in coordinator.pending_candidates()
        ]
        # One selector serves both fields: same candidates, same rendering; only
        # the meaning of the selection differs.
        selector = SelectSelector(
            SelectSelectorConfig(
                options=options,
                multiple=True,
                mode=SelectSelectorMode.LIST,
            )
        )
        return self.async_show_form(
            step_id="add_devices",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_ADD_DEVICES, default=[]): selector,
                    vol.Optional(CONF_IGNORE_DEVICES, default=[]): selector,
                }
            ),
            errors=errors,
        )

    async def _apply_add_and_ignore(
        self, coordinator: Rtl433Coordinator, add: list[str], ignore: list[str]
    ) -> ConfigFlowResult:
        """Adopt the selected devices, ignore the selected devices, and finish.

        Both halves are delegated to :mod:`.adoption`, the single implementation
        this step shares with the discovery panel's WebSocket API, so adopting
        from a form and adopting from the panel cannot drift apart. What stays
        here is the flow mechanics: the order (adopt first, then ignore, matching
        the form's own reading order) and how the dialog closes.

        A key that is no longer pending -- it stopped being a candidate between
        the render and the submit, or a second submit repeated the selection --
        comes back as ``skipped``. The form ignores that: it has already closed by
        the time anyone could be told, and the list it re-renders next time is
        rebuilt from live state anyway. The WebSocket caller, with a user watching
        the row they clicked, is the one that needs the answer.

        ``entry.options`` is handed back unchanged: as in the mappings step, this
        step writes ``entry.data`` and only needs the dialog to close without
        clobbering options.
        """
        entry = self.config_entry

        await async_adopt_devices(self.hass, entry, coordinator, add)
        await async_ignore_devices(self.hass, entry, coordinator, ignore)

        return self.async_create_entry(title="", data=dict(entry.options))

    async def async_step_ignored_devices(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Un-ignore devices so they are offered for adding again.

        Un-ignoring is not retroactive -- the reappearance waits for the device's
        next transmission, for the reasons :func:`.adoption.async_unignore_devices`
        explains -- so the step's own job is only to render the stored list and
        hand the selection to that service.

        ``entry.data`` is the source of truth for what is ignored, but the step
        still requires a running coordinator: discarding the key from the
        coordinator's mirrored set is what un-ignores the device on its next
        transmission instead of only after a reload, and the service needs the
        coordinator to do it.
        """
        coordinator = self._coordinator()
        if coordinator is None:
            return self.async_abort(reason="hub_not_loaded")

        entry = self.config_entry
        ignored = _hub_ignored_devices(entry)
        if not ignored:
            return self.async_abort(reason="no_ignored_devices")

        if user_input is not None:
            await async_unignore_devices(
                self.hass,
                entry,
                coordinator,
                set(user_input.get(CONF_UNIGNORE_DEVICES, [])),
            )
            return self.async_create_entry(title="", data=dict(entry.options))

        # An ignored device usually has no stored record -- it is ignored while
        # pending, long before it would have one -- so the key is the label, and a
        # model appears only for a device that was adopted, deleted, and then
        # ignored on its return to the pending list.
        devices: dict[str, Any] = entry.data.get(CONF_DEVICES, {})
        options = [
            SelectOptionDict(
                value=device_key,
                label=_model_label(
                    devices.get(device_key, {}).get(CONF_MODEL, ""), device_key
                ),
            )
            for device_key in sorted(ignored)
        ]
        return self.async_show_form(
            step_id="ignored_devices",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_UNIGNORE_DEVICES, default=[]): SelectSelector(
                        SelectSelectorConfig(
                            options=options,
                            multiple=True,
                            mode=SelectSelectorMode.LIST,
                        )
                    )
                }
            ),
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
        coordinator = self._coordinator()
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
        self, device_key: str, devices: dict[str, Any], heard: dict[str, Any]
    ) -> str:
        """Model for a replacement candidate: stored record, else what was heard.

        A candidate can legitimately have no record in ``entry.data["devices"]``:
        a *pending* device has none by definition (nothing is stored until the
        user adds it), and a device adopted this session is in the coordinator's
        runtime state before its devices-map upsert lands. Both are exactly the
        devices a replace has to offer, so the model falls back to ``heard`` --
        the coordinator's adopted events merged with its pending records, whose
        entries both expose ``.model`` -- and then to ``""``. Never raises: a
        missing record, a missing entry and a blank model all degrade to the bare
        key in the picker.
        """
        record: dict[str, Any] = devices.get(device_key, {})
        return record.get(CONF_MODEL) or getattr(heard.get(device_key), "model", "")

    def _replacement_label(
        self,
        device_key: str,
        model: str,
        record: PendingDevice | None,
        now: datetime,
    ) -> str:
        """Label one replacement candidate, marking the ones not yet added.

        A pending candidate is described the way the add-devices step describes
        it -- same sighting count, signal level and age, so the two lists read
        alike -- and then explicitly marked, because picking one has a different
        meaning: the user is adopting a device they have never added onto the
        history of one they have. The sighting count is what identifies the
        replacement here, since a sensor put back into service after a battery
        change starts checking in immediately.

        Takes the already-resolved ``model`` and pending ``record`` rather than
        the three maps to look them up in: the caller resolves each model exactly
        once for the sort it has to do anyway, so re-deriving them per label
        would repeat work the caller has already finished.
        """
        if record is not None:
            return f"{_pending_label(record, now)} — not added yet"
        return _model_label(model, device_key)

    async def async_step_replace_target(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick the new identity to adopt onto the device kept on the previous step.

        Candidates are the **union** of the stored devices map, the coordinator's
        adopted runtime keys, and its **pending** keys. Pending is the important
        one: a sensor that drew a new transmitter id when its batteries were
        changed is heard under that new id and nothing more -- it is never added
        automatically -- so without pending candidates this step could not offer
        the one device it exists to adopt. The adopted runtime keys stay in the
        union because they can still lead the devices map by a beat (a device
        adopted this session is in runtime state before its upsert lands), and
        :func:`async_replace_device` accepts a ``new_key`` with no stored record,
        which is precisely what a pending key is. The coordinator is reached the
        way :meth:`_device_commodity_default` reaches it and is guarded the same
        way -- the options flow can be opened while the entry is not loaded, and
        then the devices map alone is the candidate set. Same-model candidates
        sort first across the whole combined set, since a battery swap keeps the
        model, and an empty candidate set (a single-device hub) aborts rather
        than showing a dead-end dropdown.

        Adopting a pending key needs no eviction here:
        :func:`async_replace_device` reloads the entry, which rebuilds the
        coordinator and with it the in-memory pending list, so the candidate
        cannot linger as a stale "add me" offer next to the device it was just
        merged into. Its next transmission arrives under a key that is now in the
        stored devices map, so it is routed as an adopted device.

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

        coordinator = self._coordinator()
        pending: dict[str, Any] = getattr(coordinator, "pending", None) or {}
        # One mapping for model resolution: a coordinator ``NormalizedEvent`` and
        # a ``PendingDevice`` both carry ``.model``, so the adopted and pending
        # halves of "what the hub has heard" resolve through the same lookup.
        # ``getattr`` with a default keeps a not-yet-loaded hub (and a stub
        # coordinator) from breaking the render.
        heard: dict[str, Any] = {
            **(getattr(coordinator, "devices", None) or {}),
            **pending,
        }

        old_model = self._replacement_model(old_key, devices, heard)
        # Resolve each candidate's model once. The sort comparator would
        # otherwise re-derive it on every comparison and the label pass once
        # more, which is the same lookup done O(n log n) times for a value that
        # cannot change during the render.
        models = {
            key: self._replacement_model(key, devices, heard)
            for key in (set(devices) | set(heard)) - {old_key}
        }
        candidates = sorted(
            models,
            key=lambda key: (0 if old_model and models[key] == old_model else 1, key),
        )
        if not candidates:
            return self.async_abort(reason="no_replacement_candidates")

        now = dt_util.utcnow()
        options = [
            SelectOptionDict(
                value=device_key,
                label=self._replacement_label(
                    device_key, models[device_key], pending.get(device_key), now
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
