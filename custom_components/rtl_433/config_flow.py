"""Hub config flow for the rtl_433 integration.

Implements the add/reconfigure/discovery flow for a hub config entry
(:class:`Rtl433ConfigFlow`):

- **Hub user flow** (``async_step_user``): collects a single rtl_433 HTTP
  server's WebSocket connection parameters (host/port/path, optional ``wss://``
  via a ``secure`` toggle), validates reachability with the coordinator's
  ``validate_connection`` helper, and creates the hub config entry. The hub's
  unique_id is derived from host/port so the same server cannot be added twice.
- **Reconfigure** (``async_step_reconfigure``) and **Supervisor discovery**
  (``async_step_hassio`` / ``async_step_hassio_confirm``) edit/adopt a hub in
  place under the dual host:port / stable-radio-id identity scheme.

The **options flow** lives in :mod:`.options_flow` (:class:`Rtl433OptionsFlow`);
``async_get_options_flow`` returns it. This module only validates connectivity;
it never starts the coordinator. The coordinator lifecycle is wired elsewhere.
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
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
)
from homeassistant.helpers.service_info.hassio import HassioServiceInfo

from .const import (
    CONF_DEVICES,
    CONF_DISCOVERY_ENABLED,
    CONF_HOST,
    CONF_INITIAL_FREQUENCY,
    CONF_MANAGE_SETTINGS,
    CONF_PATH,
    CONF_PORT,
    CONF_RADIO_ID,
    DEFAULT_INITIAL_FREQUENCY,
    DEFAULT_MANAGE_SETTINGS,
    DEFAULT_PATH,
    DEFAULT_PORT,
    DOMAIN,
)
from .coordinator import CannotConnect, Rtl433Coordinator
from .options_flow import Rtl433OptionsFlow

# Whether to dial the server over ``wss://`` instead of ``ws://``.
CONF_SECURE = "secure"


def _hub_unique_id(host: str, port: int) -> str:
    """Return the unique_id for a hub entry (one per host:port)."""
    return f"hub:{host}:{port}"


async def async_rebind_hub(
    hass: HomeAssistant,
    entry: ConfigEntry,
    new_unique_id: str,
    conn_updates: dict[str, Any],
    title: str | None = None,
) -> str:
    """Re-point a hub entry at a new stable radio unique_id, in place.

    Preserves entry_id (so all nested devices/entities/history survive). When a
    *different* entry already owns ``new_unique_id``: if that entry has a
    populated devices map it is a real hub -> return ``"already_configured"`` and
    change nothing; if it is an empty orphan (e.g. a duplicate auto-created by
    discovery on a new host:port) it is removed and the rebind proceeds.
    Returns ``"ok"`` on success.
    """
    for other in hass.config_entries.async_entries(DOMAIN):
        if other.entry_id == entry.entry_id:
            continue
        if other.unique_id == new_unique_id:
            if other.data.get(CONF_DEVICES):
                return "already_configured"
            await hass.config_entries.async_remove(other.entry_id)
            break
    updates: dict[str, Any] = {
        "unique_id": new_unique_id,
        "data": {**entry.data, **conn_updates},
    }
    if title is not None:
        updates["title"] = title
    hass.config_entries.async_update_entry(entry, **updates)
    await hass.config_entries.async_reload(entry.entry_id)
    return "ok"


# Optional initial center-frequency field shared by both add flows. Presented in
# MHz (the unit the Center-frequency control uses) and pre-filled with the common
# 433.92 MHz band; clearing it means "adopt the server's current frequency". Only
# honored when ``manage_settings`` is on.
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
        vol.Optional(
            CONF_INITIAL_FREQUENCY, default=DEFAULT_INITIAL_FREQUENCY
        ): _FREQUENCY_SELECTOR,
        vol.Optional(CONF_DISCOVERY_ENABLED, default=True): bool,
    }
)


class Rtl433ConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle setup of an rtl_433 hub (one config entry per server)."""

    VERSION = 2
    MINOR_VERSION = 6

    # Connection params carried from ``async_step_hassio`` into the confirm step.
    _discovery: dict[str, Any] | None = None

    def _find_entry_by_host_port(self, host: str, port: int) -> ConfigEntry | None:
        """Return an existing entry targeting this host:port, if any."""
        for entry in self._async_current_entries():
            if entry.data.get(CONF_HOST) == host and entry.data.get(CONF_PORT) == port:
                return entry
        return None

    @staticmethod
    def _safe_to_adopt(entry: ConfigEntry) -> bool:
        """Whether a host:port-matched entry may be re-keyed onto a stable id.

        Safe only for a placeholder ``hub:host:port`` entry (a manual add awaiting
        its stable radio id) or an empty orphan. An entry already bound to a
        *different* stable radio id and carrying real devices is a separate radio
        that happens to share this host:port -- re-keying it would corrupt that
        radio's identity, so the discovery falls through and treats the
        advertisement as a new radio instead.
        """
        return (entry.unique_id or "").startswith("hub:") or not entry.data.get(
            CONF_DEVICES
        )

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
        fields: dict[Any, Any] = {}
        uid = entry.unique_id or ""
        # Only discovered/adopted entries carry a stable radio id worth rebinding;
        # legacy hub:host:port entries rebind via host:port alone.
        if uid and not uid.startswith("hub:"):
            fields[vol.Optional(CONF_RADIO_ID, default=uid)] = str
        fields.update(
            {
                vol.Required(CONF_HOST, default=data.get(CONF_HOST, "")): str,
                vol.Required(CONF_PORT, default=data.get(CONF_PORT, DEFAULT_PORT)): int,
                vol.Required(CONF_PATH, default=data.get(CONF_PATH, DEFAULT_PATH)): str,
                vol.Optional(CONF_SECURE, default=data.get(CONF_SECURE, False)): bool,
            }
        )
        return vol.Schema(fields)

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
                # Discovered/adopted entry: allow re-pointing at a new stable radio id.
                conn = {
                    CONF_HOST: host,
                    CONF_PORT: port,
                    CONF_PATH: path,
                    CONF_SECURE: secure,
                }
                new_uid = (user_input.get(CONF_RADIO_ID) or "").strip() or (
                    entry.unique_id or ""
                )
                if new_uid and new_uid != entry.unique_id:
                    status = await async_rebind_hub(
                        self.hass, entry, new_uid, conn, title=f"rtl_433 ({host})"
                    )
                    if status == "already_configured":
                        return self.async_abort(reason="already_configured")
                    return self.async_abort(reason="reconfigure_successful")
                return self.async_update_reload_and_abort(
                    entry,
                    title=f"rtl_433 ({host})",
                    data_updates=conn,
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
        if (
            existing is not None
            and existing.unique_id != radio_uid
            and self._safe_to_adopt(existing)
        ):
            # Never create a duplicate unique_id: if another entry already owns
            # this radio id, leave it untouched when it is a real (populated) hub
            # or drop it when it is an empty orphan, before re-keying. Mirrors the
            # collision handling in ``async_rebind_hub``.
            collision = next(
                (
                    other
                    for other in self._async_current_entries()
                    if other.entry_id != existing.entry_id
                    and other.unique_id == radio_uid
                ),
                None,
            )
            if collision is not None:
                if collision.data.get(CONF_DEVICES):
                    return self.async_abort(reason="already_configured")
                await self.hass.config_entries.async_remove(collision.entry_id)
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
            "unique_id": radio_uid,
            CONF_HOST: host,
            CONF_PORT: port,
            CONF_PATH: path,
            CONF_SECURE: secure,
            "addon": addon,
        }
        self.context["title_placeholders"] = {"name": f"{addon} ({host}:{port})"}
        # Offer a guided replace when other hubs already exist; else add as new.
        if self._async_current_entries():
            return await self.async_step_hassio_replace()
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
                vol.Optional(
                    CONF_INITIAL_FREQUENCY, default=DEFAULT_INITIAL_FREQUENCY
                ): _FREQUENCY_SELECTOR,
                vol.Optional(CONF_DISCOVERY_ENABLED, default=True): bool,
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

    async def async_step_hassio_replace(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Offer to rebind an existing hub to a newly discovered radio.

        Shown when discovery sees an unknown radio id while hubs already exist (the
        likely "replacement landed on a new host:port" case). The user explicitly
        chooses to replace a specific hub or to add the radio as new; we never
        auto-rebind silently.
        """
        assert self._discovery is not None
        disc = self._discovery
        entries = self._async_current_entries()
        options = [
            SelectOptionDict(value=e.entry_id, label=e.title or e.entry_id)
            for e in entries
        ]
        options.append(SelectOptionDict(value="__new__", label="It's a new radio"))
        placeholders = {
            "addon": disc["addon"],
            "host": disc[CONF_HOST],
            "port": str(disc[CONF_PORT]),
        }

        if user_input is not None:
            choice = user_input["replaces"]
            if choice == "__new__":
                return await self.async_step_hassio_confirm()
            entry = self.hass.config_entries.async_get_entry(choice)
            if entry is None:
                return await self.async_step_hassio_confirm()
            # The discovered radio id reached this step only because
            # ``_abort_if_unique_id_configured`` did not abort, so no entry owns
            # it — the rebind can never collide here and always succeeds.
            await async_rebind_hub(
                self.hass,
                entry,
                disc["unique_id"],
                {
                    CONF_HOST: disc[CONF_HOST],
                    CONF_PORT: disc[CONF_PORT],
                    CONF_PATH: disc[CONF_PATH],
                    CONF_SECURE: disc[CONF_SECURE],
                },
                title=f"rtl_433 ({disc[CONF_HOST]})",
            )
            return self.async_abort(reason="rebind_successful")

        schema = vol.Schema(
            {
                vol.Required("replaces", default="__new__"): SelectSelector(
                    SelectSelectorConfig(options=options)
                )
            }
        )
        return self.async_show_form(
            step_id="hassio_replace",
            data_schema=schema,
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
