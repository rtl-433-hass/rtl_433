"""Location config flow and receiver config-subentry flow for the rtl_433 integration.

A config entry is a **location**: a user-named grouping that owns the adopted
devices and holds one **receiver config subentry** per rtl_433 server. The
identities live where the thing they name lives — a receiver's ``host:port`` (or
its stable radio id) is a property of that *server*, so it is the **subentry's**
``unique_id``; the location entry's own ``unique_id`` is left unset, because a
location is a name the user chose and has no intrinsic hardware identity.

- **Location user flow** (:meth:`Rtl433ConfigFlow.async_step_user`): collects a
  single rtl_433 HTTP server's WebSocket connection parameters (host/port/path,
  optional ``wss://`` via a ``secure`` toggle), validates reachability with the
  coordinator's ``validate_connection`` helper, and creates the location entry
  **and its first receiver subentry together**. The common single-receiver
  install therefore gains no extra step, and a receiver-less location — which
  could not be set up — is unreachable by construction.
- **Receiver subentry flow** (:class:`ReceiverSubentryFlowHandler`): the same
  endpoint form, used to add a second receiver to an existing location
  (``async_step_user``) or to re-point one in place (``async_step_reconfigure``)
  under the dual host:port / stable-radio-id identity scheme.
- **Supervisor discovery** (``async_step_hassio`` / ``async_step_hassio_confirm``
  / ``async_step_hassio_replace``) adopts a discovered server. It defaults to a
  **new location** and offers attaching the receiver to an existing one instead.

The **options flow** lives in :mod:`.options_flow` (:class:`Rtl433OptionsFlow`);
``async_get_options_flow`` returns it. This module only validates connectivity;
it never starts a coordinator. The coordinator lifecycle is wired elsewhere.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentry,
    ConfigSubentryData,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
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
    SUBENTRY_TYPE_RECEIVER,
)
from .coordinator import CannotConnect, Rtl433Coordinator
from .options_flow import Rtl433OptionsFlow
from .receiver_settings import receiver_subentries

# Whether to dial the server over ``wss://`` instead of ``ws://``.
CONF_SECURE = "secure"

# Sentinel option value for "put this receiver in a brand-new location" on the
# Supervisor-discovery confirm step, and for "this is not a replacement" on the
# replace step. Not a valid entry/subentry id (both are ULIDs), so it can never
# collide with a real choice.
NEW_LOCATION = "__new__"


def _receiver_unique_id(host: str, port: int) -> str:
    """Return the unique_id for a receiver subentry added by host:port.

    The ``hub:`` prefix is frozen storage: it is written into subentry unique_ids
    and read back by every "is this a placeholder identity?" test below, so it
    keeps the pre-rename spelling even though the thing it names is now called a
    receiver.
    """
    return f"hub:{host}:{port}"


def _receiver_title(host: str) -> str:
    """Return the display title for a receiver (and for its first location)."""
    return f"rtl_433 ({host})"


def _receiver_data(
    *,
    host: str,
    port: int,
    path: str,
    secure: bool,
    manage_settings: bool,
    initial_frequency: float | None = None,
) -> dict[str, Any]:
    """Build one receiver subentry's stored data.

    Everything here describes *this server*: where to dial it, and whether Home
    Assistant manages its radio. The initial frequency rides the managed
    desired-state path, so it is only meaningful — and only persisted — when
    managing settings.
    """
    data: dict[str, Any] = {
        CONF_HOST: host,
        CONF_PORT: port,
        CONF_PATH: path,
        CONF_SECURE: secure,
        CONF_MANAGE_SETTINGS: manage_settings,
    }
    if manage_settings and initial_frequency is not None:
        data[CONF_INITIAL_FREQUENCY] = float(initial_frequency)
    return data


def _all_receivers(
    hass: HomeAssistant,
) -> list[tuple[ConfigEntry, ConfigSubentry]]:
    """Return every configured receiver, paired with the location that holds it.

    Receiver identities are unique across *all* locations, not just within one:
    Home Assistant only enforces subentry unique_ids per entry, but two locations
    both dialling the same server would run two coordinators against one
    endpoint. Every duplicate guard in this module scans this list.
    """
    return [
        (entry, subentry)
        for entry in hass.config_entries.async_entries(DOMAIN)
        for subentry in receiver_subentries(entry)
    ]


def _receiver_by_unique_id(
    hass: HomeAssistant, unique_id: str, *, skip: str | None = None
) -> tuple[ConfigEntry, ConfigSubentry] | None:
    """Return the receiver owning ``unique_id``, ignoring subentry ``skip``."""
    for entry, subentry in _all_receivers(hass):
        if subentry.subentry_id != skip and subentry.unique_id == unique_id:
            return entry, subentry
    return None


def _receiver_by_host_port(
    hass: HomeAssistant, host: str, port: int, *, skip: str | None = None
) -> tuple[ConfigEntry, ConfigSubentry] | None:
    """Return the receiver dialling ``host:port``, ignoring subentry ``skip``."""
    for entry, subentry in _all_receivers(hass):
        if subentry.subentry_id == skip:
            continue
        if (
            subentry.data.get(CONF_HOST) == host
            and subentry.data.get(CONF_PORT) == port
        ):
            return entry, subentry
    return None


async def _async_drop_receiver(
    hass: HomeAssistant, entry: ConfigEntry, subentry: ConfigSubentry
) -> None:
    """Remove one receiver subentry, and its location if that empties it.

    Removing a subentry clears the devices and entities Home Assistant recorded
    against it — here, the receiver device and its radio controls — and leaves
    the location's RF devices alone, because those are owned by the entry
    directly. A location left with no receiver could never be set up again, so it
    goes with the last one rather than lingering as an unloadable shell.
    """
    hass.config_entries.async_remove_subentry(entry, subentry.subentry_id)
    if not receiver_subentries(entry):
        await hass.config_entries.async_remove(entry.entry_id)


async def async_rebind_receiver(
    hass: HomeAssistant,
    entry: ConfigEntry,
    subentry: ConfigSubentry,
    new_unique_id: str,
    conn_updates: dict[str, Any],
    title: str | None = None,
) -> str:
    """Re-point a receiver at a new stable radio unique_id, in place.

    Preserves the subentry_id (so the receiver's device, its radio controls and
    its desired-state Store all survive) and the location entry_id (so every RF
    device and its history survives). When a *different* receiver already owns
    ``new_unique_id``: if its location has a populated devices map it is a real
    install -> return ``"already_configured"`` and change nothing; if it is an
    empty orphan (e.g. a duplicate auto-created by discovery on a new host:port)
    it is removed and the rebind proceeds. Returns ``"ok"`` on success.

    The write alone re-points the receiver: ``_async_update_listener`` sees the
    changed connection target / unique_id and reloads the location. Reloading
    here as well is what Home Assistant deprecated in 2026.6 (an update listener
    combined with a flow-side reload double-reloads and races), so this function
    never reloads.
    """
    collision = _receiver_by_unique_id(hass, new_unique_id, skip=subentry.subentry_id)
    if collision is not None:
        other_entry, other_subentry = collision
        if other_entry.data.get(CONF_DEVICES):
            return "already_configured"
        await _async_drop_receiver(hass, other_entry, other_subentry)
    hass.config_entries.async_update_subentry(
        entry,
        subentry,
        unique_id=new_unique_id,
        title=subentry.title if title is None else title,
        data={**subentry.data, **conn_updates},
    )
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
    }
)


def _reconfigure_schema(subentry: ConfigSubentry) -> vol.Schema:
    """Build the reconfigure form schema pre-filled from the subentry's data.

    Mirrors the connection subset of :data:`STEP_USER_SCHEMA` plus the
    per-receiver managed-radio toggle; the availability defaults stay on the
    location's options flow, because they describe sensors rather than servers.
    """
    data = subentry.data
    fields: dict[Any, Any] = {}
    uid = subentry.unique_id or ""
    # Only discovered/adopted receivers carry a stable radio id worth rebinding;
    # hub:host:port placeholders rebind via host:port alone.
    if uid and not uid.startswith("hub:"):
        fields[vol.Optional(CONF_RADIO_ID, default=uid)] = str
    fields.update(
        {
            vol.Required(CONF_HOST, default=data.get(CONF_HOST, "")): str,
            vol.Required(CONF_PORT, default=data.get(CONF_PORT, DEFAULT_PORT)): int,
            vol.Required(CONF_PATH, default=data.get(CONF_PATH, DEFAULT_PATH)): str,
            vol.Optional(CONF_SECURE, default=data.get(CONF_SECURE, False)): bool,
            vol.Optional(
                CONF_MANAGE_SETTINGS,
                default=data.get(CONF_MANAGE_SETTINGS, DEFAULT_MANAGE_SETTINGS),
            ): bool,
        }
    )
    return vol.Schema(fields)


class ReceiverSubentryFlowHandler(ConfigSubentryFlow):
    """Add or re-point one receiver inside a location.

    Adding a second receiver to a location is the primary path to the device
    union: both servers then feed the same location entry, which is what lets
    their views of one sensor be merged. Adding a whole new location stays
    available for a genuinely distant site, where merging would be wrong.
    """

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Collect a receiver's connection params and add it to this location."""
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
                unique_id = _receiver_unique_id(host, port)
                # Two guards, because a receiver can be keyed either way: by
                # host:port when added by hand, or by a stable radio id when
                # discovery adopted it. Either match means this server is already
                # configured somewhere.
                if (
                    _receiver_by_host_port(self.hass, host, port) is not None
                    or _receiver_by_unique_id(self.hass, unique_id) is not None
                ):
                    return self.async_abort(reason="already_configured")
                return self.async_create_entry(
                    title=_receiver_title(host),
                    unique_id=unique_id,
                    data=_receiver_data(
                        host=host,
                        port=port,
                        path=path,
                        secure=secure,
                        manage_settings=manage_settings,
                        initial_frequency=user_input.get(CONF_INITIAL_FREQUENCY),
                    ),
                )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_SCHEMA,
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Edit one receiver's connection target in place.

        Validates the new host/port/path/secure, recomputes the host:port
        unique_id (guarding against collision with a *different* configured
        receiver), then merges the new connection params into the subentry —
        preserving the subentry_id, so the receiver's device and radio controls
        survive, and the location entry, so every RF device and its history does.

        The write alone re-points the receiver: ``_async_update_listener`` sees
        the changed connection target and reloads the location, so this step uses
        the *non*-reloading ``async_update_and_abort``. Home Assistant refuses the
        reloading variant outright on an entry that has an update listener,
        because the pair double-reloads and races.
        """
        entry = self._get_entry()
        subentry = self._get_reconfigure_subentry()
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
                conn = {
                    CONF_HOST: host,
                    CONF_PORT: port,
                    CONF_PATH: path,
                    CONF_SECURE: secure,
                    CONF_MANAGE_SETTINGS: manage_settings,
                }
                current_uid = subentry.unique_id or ""
                if current_uid.startswith("hub:") or not current_uid:
                    # Placeholder identity: keep the host:port identity scheme.
                    new_unique_id = _receiver_unique_id(host, port)
                    if (
                        _receiver_by_unique_id(
                            self.hass, new_unique_id, skip=subentry.subentry_id
                        )
                        is not None
                    ):
                        return self.async_abort(reason="already_configured")
                    return self.async_update_and_abort(
                        entry,
                        subentry,
                        unique_id=new_unique_id,
                        title=_receiver_title(host),
                        data_updates=conn,
                    )
                # Discovered/adopted receiver: allow re-pointing at a new stable
                # radio id.
                new_uid = (user_input.get(CONF_RADIO_ID) or "").strip() or current_uid
                if new_uid != current_uid:
                    status = await async_rebind_receiver(
                        self.hass,
                        entry,
                        subentry,
                        new_uid,
                        conn,
                        title=_receiver_title(host),
                    )
                    if status == "already_configured":
                        return self.async_abort(reason="already_configured")
                    return self.async_abort(reason="reconfigure_successful")
                return self.async_update_and_abort(
                    entry,
                    subentry,
                    title=_receiver_title(host),
                    data_updates=conn,
                )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_reconfigure_schema(subentry),
            errors=errors,
        )


class Rtl433ConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle setup of an rtl_433 location (one config entry per location)."""

    VERSION = 2
    MINOR_VERSION = 8

    # Connection params carried from ``async_step_hassio`` into the confirm step.
    _discovery: dict[str, Any] | None = None

    @staticmethod
    def _safe_to_adopt(entry: ConfigEntry, subentry: ConfigSubentry) -> bool:
        """Whether a host:port-matched receiver may be re-keyed onto a stable id.

        Safe only for a placeholder ``hub:host:port`` receiver (a manual add
        awaiting its stable radio id) or one whose location has adopted nothing.
        A receiver already bound to a *different* stable radio id whose location
        carries real devices is a separate radio that happens to share this
        host:port -- re-keying it would corrupt that radio's identity, so the
        discovery falls through and treats the advertisement as a new radio
        instead.
        """
        return (subentry.unique_id or "").startswith("hub:") or not entry.data.get(
            CONF_DEVICES
        )

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Expose the receiver subentry flow, so a location can gain receivers."""
        return {SUBENTRY_TYPE_RECEIVER: ReceiverSubentryFlowHandler}

    # ------------------------------------------------------------------ #
    # Location user flow                                                 #
    # ------------------------------------------------------------------ #
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect one receiver's connection params and create the location.

        The form is exactly the one a single-receiver install has always seen;
        what changes is what it produces — a location entry *and* its first
        receiver subentry, created together, so no receiver-less location is ever
        reachable and the user is never asked to name a grouping they did not ask
        for. The location takes the receiver's title; a second receiver added
        later does not rename it.
        """
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
                unique_id = _receiver_unique_id(host, port)
                # Guard against duplicating a radio already added by discovery
                # (which keys receivers by a stable radio id, not host:port), and
                # against a second manual add of the same endpoint.
                if (
                    _receiver_by_host_port(self.hass, host, port) is not None
                    or _receiver_by_unique_id(self.hass, unique_id) is not None
                ):
                    return self.async_abort(reason="already_configured")
                return self.async_create_entry(
                    title=_receiver_title(host),
                    # The location's own data starts empty: it fills with the
                    # devices the user adopts. The connection lives on the
                    # receiver subentry below.
                    data={},
                    subentries=[
                        ConfigSubentryData(
                            subentry_type=SUBENTRY_TYPE_RECEIVER,
                            title=_receiver_title(host),
                            unique_id=unique_id,
                            data=_receiver_data(
                                host=host,
                                port=port,
                                path=path,
                                secure=secure,
                                manage_settings=manage_settings,
                                initial_frequency=user_input.get(
                                    CONF_INITIAL_FREQUENCY
                                ),
                            ),
                        )
                    ],
                )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_SCHEMA,
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
        ``unique_id``. A pre-existing receiver on the same ``host:port`` is
        adopted onto that stable id (migration; aborts ``already_configured``).
        A receiver already keyed on that stable id has its stored connection
        updated in place. A genuinely new radio is routed to the confirm step,
        which offers a new location by default and attaching to an existing one
        as the alternative.
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
        conn = {
            CONF_HOST: host,
            CONF_PORT: port,
            CONF_PATH: path,
            CONF_SECURE: secure,
        }

        # Adopt/migrate a pre-existing receiver on the same server onto the
        # stable id.
        existing = _receiver_by_host_port(self.hass, host, port)
        if (
            existing is not None
            and existing[1].unique_id != radio_uid
            and self._safe_to_adopt(*existing)
        ):
            existing_entry, existing_subentry = existing
            # Never create a duplicate unique_id: if another receiver already
            # owns this radio id, leave it untouched when its location is real
            # (populated) or drop it when it is an empty orphan, before re-keying.
            # Mirrors the collision handling in ``async_rebind_receiver``.
            collision = _receiver_by_unique_id(
                self.hass, radio_uid, skip=existing_subentry.subentry_id
            )
            if collision is not None:
                if collision[0].data.get(CONF_DEVICES):
                    return self.async_abort(reason="already_configured")
                await _async_drop_receiver(self.hass, *collision)
            self.hass.config_entries.async_update_subentry(
                existing_entry,
                existing_subentry,
                unique_id=radio_uid,
                data={**existing_subentry.data, **conn},
            )
            return self.async_abort(reason="already_configured")

        # Same radio (matched by stable id) — update connection target in place.
        # Done by hand rather than through ``_abort_if_unique_id_configured``,
        # which only knows about *entry* unique_ids, and a receiver's identity now
        # lives on its subentry.
        same_radio = _receiver_by_unique_id(self.hass, radio_uid)
        if same_radio is not None:
            same_entry, same_subentry = same_radio
            # The update listener reloads the location when the target actually
            # changed; the write is all this needs to do.
            self.hass.config_entries.async_update_subentry(
                same_entry, same_subentry, data={**same_subentry.data, **conn}
            )
            return self.async_abort(reason="already_configured")

        self._discovery = {
            "unique_id": radio_uid,
            **conn,
            "addon": addon,
        }
        self.context["title_placeholders"] = {"name": f"{addon} ({host}:{port})"}
        # Offer a guided replace when other receivers already exist; else add as new.
        if _all_receivers(self.hass):
            return await self.async_step_hassio_replace()
        return await self.async_step_hassio_confirm()

    def _location_options(self) -> list[SelectOptionDict]:
        """Return the "which location?" choices, new-location first."""
        options = [
            SelectOptionDict(value=NEW_LOCATION, label="A new location"),
        ]
        options.extend(
            SelectOptionDict(value=entry.entry_id, label=entry.title or entry.entry_id)
            for entry in self.hass.config_entries.async_entries(DOMAIN)
        )
        return options

    async def async_step_hassio_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm adoption of a discovered radio, then create it.

        Shows a confirmation form (``addon``/``host``/``port`` placeholders) that
        offers the same setup choices as the manual flow: the manage-settings
        toggle and an optional initial frequency. When locations already exist it
        also asks which one the receiver belongs to, defaulting to a **new**
        location: a discovered server is usually a second site, and merging two
        sites' devices is the one outcome that cannot be undone by editing a
        form. Choosing an existing location adds the receiver to it as a subentry,
        which is what opts its devices into the union.

        On submit, validates connectivity; a failed validation re-shows the form
        with ``cannot_connect``.
        """
        assert self._discovery is not None
        disc = self._discovery
        placeholders = {
            "addon": disc["addon"],
            "host": disc[CONF_HOST],
            "port": str(disc[CONF_PORT]),
        }
        fields: dict[Any, Any] = {}
        locations = self.hass.config_entries.async_entries(DOMAIN)
        if locations:
            fields[vol.Required("location", default=NEW_LOCATION)] = SelectSelector(
                SelectSelectorConfig(options=self._location_options())
            )
        fields.update(
            {
                vol.Optional(
                    CONF_MANAGE_SETTINGS, default=DEFAULT_MANAGE_SETTINGS
                ): bool,
                vol.Optional(
                    CONF_INITIAL_FREQUENCY, default=DEFAULT_INITIAL_FREQUENCY
                ): _FREQUENCY_SELECTOR,
            }
        )
        confirm_schema = vol.Schema(fields)

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
            title = f"rtl_433 ({disc[CONF_HOST]}:{disc[CONF_PORT]})"
            data = _receiver_data(
                host=disc[CONF_HOST],
                port=disc[CONF_PORT],
                path=disc[CONF_PATH],
                secure=disc[CONF_SECURE],
                manage_settings=manage_settings,
                initial_frequency=user_input.get(CONF_INITIAL_FREQUENCY),
            )
            chosen = user_input.get("location", NEW_LOCATION)
            location = (
                None
                if chosen == NEW_LOCATION
                else self.hass.config_entries.async_get_entry(chosen)
            )
            if location is not None:
                self.hass.config_entries.async_add_subentry(
                    location,
                    ConfigSubentry(
                        data=data,
                        subentry_type=SUBENTRY_TYPE_RECEIVER,
                        title=title,
                        unique_id=disc["unique_id"],
                    ),
                )
                return self.async_abort(reason="receiver_added")
            return self.async_create_entry(
                title=title,
                data={},
                subentries=[
                    ConfigSubentryData(
                        subentry_type=SUBENTRY_TYPE_RECEIVER,
                        title=title,
                        unique_id=disc["unique_id"],
                        data=data,
                    )
                ],
            )

        return self.async_show_form(
            step_id="hassio_confirm",
            data_schema=confirm_schema,
            description_placeholders=placeholders,
        )

    async def async_step_hassio_replace(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Offer to rebind an existing receiver to a newly discovered radio.

        Shown when discovery sees an unknown radio id while receivers already
        exist (the likely "replacement landed on a new host:port" case). The user
        explicitly chooses to replace a specific receiver or to add the radio as
        new; we never auto-rebind silently.
        """
        assert self._discovery is not None
        disc = self._discovery
        receivers = _all_receivers(self.hass)
        options = [
            SelectOptionDict(
                value=subentry.subentry_id,
                label=f"{entry.title} / {subentry.title}",
            )
            for entry, subentry in receivers
        ]
        options.append(SelectOptionDict(value=NEW_LOCATION, label="It's a new radio"))
        placeholders = {
            "addon": disc["addon"],
            "host": disc[CONF_HOST],
            "port": str(disc[CONF_PORT]),
        }

        if user_input is not None:
            choice = user_input["replaces"]
            target = next(
                (pair for pair in receivers if pair[1].subentry_id == choice), None
            )
            if target is None:
                return await self.async_step_hassio_confirm()
            target_entry, target_subentry = target
            # The discovered radio id reached this step only because no receiver
            # owns it, so the rebind can never collide here and always succeeds.
            await async_rebind_receiver(
                self.hass,
                target_entry,
                target_subentry,
                disc["unique_id"],
                {
                    CONF_HOST: disc[CONF_HOST],
                    CONF_PORT: disc[CONF_PORT],
                    CONF_PATH: disc[CONF_PATH],
                    CONF_SECURE: disc[CONF_SECURE],
                },
                title=_receiver_title(disc[CONF_HOST]),
            )
            return self.async_abort(reason="rebind_successful")

        schema = vol.Schema(
            {
                vol.Required("replaces", default=NEW_LOCATION): SelectSelector(
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
        """Return the location options flow (one entry == one location)."""
        return Rtl433OptionsFlow()
