"""The rtl_433 integration.

This module wires the integration's config-entry lifecycle. There is one kind of
config entry: a **hub** entry that owns one rtl_433 server's WebSocket
connection. Setting one up loads the shipped mapping library (cached once on
``hass.data[DOMAIN][DATA_LIBRARY]``), merges this hub's stored
``entry.data[CONF_USER_MAPPINGS]`` over it and caches the per-entry merged
``(registry, skip_keys)`` on ``hass.data[DOMAIN][DATA_ENTRY_LIBRARY][entry_id]``
so the entity platforms reuse it, instantiates the push
:class:`~custom_components.rtl_433.coordinator.Rtl433Coordinator`, injects the
skip-keys, the effective-timeout resolver, and the new-device callback, registers
the hub device, starts the coordinator, registers an options-update listener so a
changed availability timeout takes effect live, and forwards the
``sensor`` / ``binary_sensor`` platforms once on the hub entry.

RF devices are represented as **device-registry devices nested under the hub
entry** (rfxtrx-style), not as their own config entries. They are recreated on
startup from ``entry.data[CONF_DEVICES]`` — the restart-safe record of the
devices the user has adopted — and added at runtime via the new-device
dispatcher signal when the user approves one from the coordinator's pending
list. A device the user has not adopted is only ever *heard*; it never reaches
the device registry. A single nested device can be removed from its device page
via :func:`async_remove_config_entry_device`, which returns it to the pending
list; deleting the hub entry removes all nested devices and entities
automatically.

Setting up the first hub also registers the discovery WebSocket commands
(:mod:`.websocket_api`), which back the approval panel and are equally usable
from a script, and the **discovery panel** itself — the shipped
``frontend/rtl_433-panel.js`` served as a static path and registered with
``panel_custom``. Both are per Home Assistant *run*, not per entry, so both are
guarded to happen once rather than once per hub.

The library loading lives in :mod:`.library`, the hub-setting resolvers in
:mod:`.hub_settings`, the shared adopt / ignore / un-ignore service in
:mod:`.adoption`, and the config-entry migration / one-time legacy cleanups in
:mod:`.migration`; this module keeps only the steady-state lifecycle.
"""

from __future__ import annotations

from pathlib import Path

from homeassistant.components import panel_custom
from homeassistant.components.frontend import async_panel_exists
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntry
from homeassistant.helpers.dispatcher import async_dispatcher_send

from . import repairs
from .const import (
    CONF_DEVICES,
    CONF_HOST,
    CONF_INITIAL_FREQUENCY,
    CONF_PATH,
    CONF_PORT,
    CONF_USER_MAPPINGS,
    DATA_ENTRY_LIBRARY,
    DEFAULT_MOTION_CLEAR_DELAY,
    DEVICE_MOTION_CLEAR_DELAY,
    DEVICE_TIMEOUT_OVERRIDE,
    DOMAIN,
    LOGGER,
    MANUFACTURER,
    PLATFORMS,
    signal_new_device,
)
from .coordinator import Rtl433Coordinator
from .hub_settings import (
    _calibration_map,
    _explicit_hub_timeout,
    _hub_availability_timeout,
    _hub_connection,
    _hub_ignored_devices,
    _hub_manage_settings,
    _hub_secure,
)
from .library import _async_load_library, _merge_entry_library
from .mapping import event_driven_field_keys
from .migration import (
    _cleanup_phantom_unknown_device,
    _migrate_motion_event_to_binary_sensor,
    async_migrate_entry,
)
from .websocket_api import async_register_commands

# Where the shipped ``frontend/`` directory is served from. Its own path rather
# than something under ``/api`` because it is a plain static directory, and a
# distinct one rather than the domain so the served assets can never be confused
# with the panel's own front-end route (``/rtl_433``, from ``frontend_url_path``
# below).
PANEL_URL_BASE = "/rtl_433_panel"

# The custom element the module defines, and the module that defines it. Both
# have to agree with ``frontend/rtl_433-panel.js`` exactly: Home Assistant loads
# the module and then instantiates this tag name, so a mismatch is a blank page
# with nothing in the log.
PANEL_ELEMENT_NAME = "rtl-433-panel"
PANEL_MODULE_NAME = "rtl_433-panel.js"

# Key under ``hass.data[DOMAIN]`` claimed synchronously by whichever hub setup
# gets to the panel registration first. ``async_panel_exists`` alone is not
# enough: the registration awaits (the static-path helper hops to the executor),
# and Home Assistant sets a domain's config entries up with ``asyncio.gather``,
# so two receivers both pass an existence check that is only true *after* the
# await. This flag is set before the first await, which is what makes the guard
# hold across it.
DATA_PANEL_CLAIMED = "_panel_claimed"


async def _async_register_panel(hass: HomeAssistant) -> None:
    """Serve and register the discovery panel, once per Home Assistant run.

    Guarded on the panel already existing because registration is per-run while
    this is called from per-entry setup: a second receiver must not try to
    register a second panel, and ``async_register_built_in_panel`` raises rather
    than tolerating the duplicate.

    The guard is two-part on purpose. ``async_panel_exists`` covers the sequential
    case (a hub added later, or an entry reloading), but it cannot cover the
    startup case on its own: Home Assistant sets a domain's entries up
    concurrently (``asyncio.gather`` in ``setup.py``) and the registration below
    awaits before the panel exists, so both receivers would sail past an
    existence check and the second would die on ``Overwriting panel``. The
    :data:`DATA_PANEL_CLAIMED` flag is claimed synchronously — before any await —
    so exactly one caller ever reaches the registration.

    ``config_panel_domain`` is deliberately **not** passed, though it would put
    the panel behind this integration's entry in Settings → Devices & services.
    It does not add a route, it *replaces* one: the entry's Configure control
    becomes a link to the panel, and the options flow behind it -- receiver
    settings, device settings, device mappings, calibration, replace-device --
    loses its only entry point. ``knx`` is the one core integration that ships
    both a panel and an options flow, and it omits the argument for the same
    reason; ``dynalite`` and ``insteon`` pass it and have no options flow to
    lose. The panel is reached from the sidebar instead.

    Two arguments are the whole reason this works the way it does:

    - ``embed_iframe=False`` is what gets ``hass`` handed to the element as a
      property. An iframe would isolate the panel from the frontend's connection
      *and* its theme, and then the panel would need its own authentication and
      its own colours. Unlike ``knx`` and ``dynalite``, which embed large
      pre-built SPAs with their own routing, this is one screen and belongs in
      the page.
    - ``cache_headers=False`` because the file ships *inside* the integration
      and changes on upgrade. There is no content hash in its URL to bust a
      cache with, and a browser serving yesterday's panel against today's
      WebSocket API is a miserable bug to be handed.
    """
    domain_data = hass.data.setdefault(DOMAIN, {})
    if async_panel_exists(hass, DOMAIN) or domain_data.get(DATA_PANEL_CLAIMED):
        return
    domain_data[DATA_PANEL_CLAIMED] = True

    # The claim is released again if registration fails, so Home Assistant's
    # retry of a hub that went ``ConfigEntryNotReady`` here (or the user's next
    # receiver) tries once more rather than quietly setting up a hub whose panel
    # nobody can reach.
    try:
        await hass.http.async_register_static_paths(
            [
                StaticPathConfig(
                    PANEL_URL_BASE,
                    str(Path(__file__).parent / "frontend"),
                    cache_headers=False,
                )
            ]
        )
        await panel_custom.async_register_panel(
            hass=hass,
            frontend_url_path=DOMAIN,
            webcomponent_name=PANEL_ELEMENT_NAME,
            module_url=f"{PANEL_URL_BASE}/{PANEL_MODULE_NAME}",
            embed_iframe=False,
            require_admin=True,
            sidebar_title="rtl_433",
            sidebar_icon="mdi:radio-tower",
        )
    except Exception:
        domain_data.pop(DATA_PANEL_CLAIMED, None)
        raise


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up an rtl_433 hub config entry.

    Loads the library, registers the hub device, builds and starts the
    coordinator, wires the reachability watcher and options-update listener, and
    forwards the entity platforms once on the hub entry.
    """
    hass.data.setdefault(DOMAIN, {})
    # Command names are global and registration is per Home Assistant run, not
    # per entry -- but this integration is entry-only (no ``async_setup``), so the
    # call is made from every hub's setup and made idempotent inside. A user with
    # two receivers must not lose the second entry to a duplicate registration.
    async_register_commands(hass)
    # Same story for the panel: per-run, called from per-entry setup, idempotent
    # inside. It is awaited before anything else because a failure here is a
    # failure to set the hub up at all, and that should be loud rather than a
    # working hub with a panel nobody can reach.
    await _async_register_panel(hass)

    shipped_registry, shipped_skip_keys = await _async_load_library(hass)
    entry_registry, entry_skip_keys = _merge_entry_library(
        hass, entry, shipped_registry, shipped_skip_keys
    )
    hass.data[DOMAIN].setdefault(DATA_ENTRY_LIBRARY, {})[entry.entry_id] = (
        entry_registry,
        entry_skip_keys,
    )
    # Field keys whose presence marks a device as event-driven (never-expire
    # availability). Derived from this entry's merged library so the
    # classification follows the shipped library plus any user mappings; a reload
    # after an options/user-mapping change re-runs setup and refreshes the set.
    entry_event_driven_keys = event_driven_field_keys(entry_registry)

    def effective_timeout_resolver(device_key: str) -> int | None:
        """Resolve a device's *explicit* effective timeout, or ``None``.

        Resolution order for the two explicit tiers handled here:
        per-device ``timeout_override`` (``entry.data[CONF_DEVICES][device_key]``)
        → explicit hub default (only when ``CONF_AVAILABILITY_TIMEOUT`` is actually
        present in the entry's options/data). Returns ``None`` when neither is set,
        signalling the coordinator to apply the device-class default from the
        device's latest payload. An explicit ``0`` at either tier means
        never-expire and is returned as ``0`` (never falls through).
        """
        override = (
            entry.data.get(CONF_DEVICES, {})
            .get(device_key, {})
            .get(DEVICE_TIMEOUT_OVERRIDE)
        )
        if override is not None:
            return int(override)
        return _explicit_hub_timeout(entry)

    def effective_clear_delay_resolver(device_key: str) -> int:
        """Resolve a device's effective motion clear-delay (override > default).

        Reads the per-device ``motion_clear_delay`` from the hub's devices map
        (``entry.data[CONF_DEVICES][device_key]``); falls back to
        ``DEFAULT_MOTION_CLEAR_DELAY`` when none is set.
        """
        override = (
            entry.data.get(CONF_DEVICES, {})
            .get(device_key, {})
            .get(DEVICE_MOTION_CLEAR_DELAY)
        )
        if override is not None:
            return int(override)
        return DEFAULT_MOTION_CLEAR_DELAY

    def new_device_callback(device_key: str, model: str, is_replay: bool) -> None:
        """Dispatch the hub-level new-device signal for an adopted device.

        The coordinator invokes this only for devices the user has adopted —
        either on an adopted device's first frame this process, or from
        ``adopt_device`` the moment the user approves a pending one — so the
        platform listeners can add the nested device + its entities directly.
        There is no notification: a device now exists in Home Assistant only
        because the user asked for it, so there is nothing to alert them to.

        ``is_replay`` flags that the frame the entities are seeding from is a
        reconnect re-broadcast rather than a live transmission; it is passed
        through to the platform listeners unchanged.
        """
        async_dispatcher_send(
            hass, signal_new_device(entry.entry_id), device_key, model
        )

    # Register the hub device so nested devices can link to it via ``via_device``.
    # The manufacturer/model start generic and are refined to the real SDR's
    # vendor/product/serial once the coordinator connects (``hub_info_callback``).
    device_registry = dr.async_get(hass)
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        manufacturer=MANUFACTURER,
        name=entry.title,
        model="rtl_433 server",
    )
    _cleanup_phantom_unknown_device(hass, entry, device_registry)
    _migrate_motion_event_to_binary_sensor(hass, entry, er.async_get(hass))

    coordinator = Rtl433Coordinator(
        hass,
        entry,
        host=entry.data[CONF_HOST],
        port=entry.data[CONF_PORT],
        path=entry.data[CONF_PATH],
        secure=_hub_secure(entry),
        manage_settings=_hub_manage_settings(entry),
        availability_timeout=_hub_availability_timeout(entry),
        initial_center_frequency=entry.data.get(CONF_INITIAL_FREQUENCY),
        skip_keys=entry_skip_keys,
        event_driven_keys=entry_event_driven_keys,
        # The persisted devices map is the restart-safe record of what the user
        # has approved, so it is what tells the coordinator which frames may
        # reach Home Assistant; everything else is heard into the pending list.
        adopted_keys=set(entry.data.get(CONF_DEVICES, {})),
        ignored_keys=set(_hub_ignored_devices(entry)),
    )

    @callback
    def hub_info_callback() -> None:
        """Refresh the hub device's identity from the SDR's ``dev_info``.

        ``coordinator.dev_info`` is the librtlsdr USB label
        (``{"vendor", "product", "serial"}``); map it onto the hub device so the
        device page shows which physical dongle this hub is, instead of the
        generic ``rtl_433`` / ``rtl_433 server`` placeholders. Absent fields (e.g.
        ``-D manual`` with no SDR open) leave the existing values untouched.
        """
        info = coordinator.dev_info
        updates: dict[str, str] = {}
        if info.get("vendor"):
            updates["manufacturer"] = info["vendor"]
        if info.get("product"):
            updates["model"] = info["product"]
        if info.get("serial"):
            updates["serial_number"] = info["serial"]
        if not updates:
            return
        device = device_registry.async_get_device(
            identifiers={(DOMAIN, entry.entry_id)}
        )
        if device is not None:
            device_registry.async_update_device(device.id, **updates)

    coordinator.new_device_callback = new_device_callback
    coordinator.hub_info_callback = hub_info_callback
    coordinator.effective_timeout_resolver = effective_timeout_resolver
    coordinator.effective_clear_delay_resolver = effective_clear_delay_resolver
    # Global descriptor keys from the merged library, so the coordinator can flag
    # observed fields with no mapping at DEBUG (matches the diagnostics
    # ``unmatched_field_keys`` semantics, which resolve against the flat table).
    coordinator.known_field_keys = frozenset(entry_registry.flat)
    # Snapshot the per-device calibration so the update listener can detect a
    # real calibration change (and reload) while ignoring routine devices-map
    # upserts — the same change-vs-snapshot pattern as ``manage_settings``.
    coordinator.calibration_snapshot = _calibration_map(entry)
    # Snapshot the stored user mappings so the update listener can detect a real
    # mappings change (and reload to rebuild the merged library + entities) while
    # ignoring routine devices-map upserts.
    coordinator.user_mappings_snapshot = entry.data.get(CONF_USER_MAPPINGS) or {}
    # Snapshot the connection target + stable identity so the update listener can
    # reload the hub when a reconfigure / discovery / rebind re-points the entry:
    # those flows write the new target into entry.data and leave the reload to
    # this listener (see ``_async_update_listener``).
    coordinator.connection_snapshot = _hub_connection(entry)

    hass.data[DOMAIN][entry.entry_id] = coordinator
    await coordinator.async_start()

    # Watch reachability and surface / clear a repair issue accordingly.
    entry.async_on_unload(
        repairs.async_track_hub_reachability(hass, entry, coordinator)
    )
    # Advise when a single high-band frequency is left at the default sample rate.
    entry.async_on_unload(repairs.async_track_sample_rate(hass, entry, coordinator))

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Push changed hub options into the running coordinator.

    The manage-settings toggle changes the entity set (the SDR control entities
    appear / disappear) and the coordinator's adoption/enforcement behaviour, so
    a change there requires a full reload to rebuild everything. The running
    coordinator holds the *previous* effective value as
    ``coordinator.manage_settings``; comparing it against the new effective value
    detects the change without persisting extra bookkeeping.

    A per-device calibration change is detected the same way: the options device
    step writes the calibration into ``entry.data[CONF_DEVICES]`` (firing this
    listener), and a consumption sensor's ``device_class`` / unit / ``state_class``
    are construction-time, so the affected entity must be rebuilt by reloading the
    hub. The new calibration map is compared against ``coordinator.calibration_
    snapshot`` (captured at setup) so the *frequent* idempotent devices-map upserts
    (``async_upsert_device`` / ``async_upsert_event_types``), which leave the
    calibration sub-record untouched, never trigger a reload.

    A changed connection target (host / port / path / secure) or stable radio
    unique_id is detected the same way, against ``coordinator.connection_
    snapshot``. Home Assistant forbids an integration from combining a
    config-entry update listener with the reloading config-flow helpers
    (``async_update_reload_and_abort`` / ``_abort_if_unique_id_configured(
    reload_on_update=True)``) — that pair double-reloads and races — so the
    reconfigure, Supervisor-discovery and rebind paths only *write* the new
    target and this listener is the single place that reloads the hub.

    An availability-timeout change is applied live instead (the coordinator
    reads ``availability_timeout`` on every watchdog tick), so no reload is
    required for it and we avoid the disruption of tearing the socket down.

    The hub's ignore list is applied live too, and *first*: ignoring or
    un-ignoring a device changes nothing about the devices and entities that
    exist, so tearing the WebSocket down for it would be gratuitous. Pushing it
    before the reload comparisons also means the options flow's ignore-only write
    can never fall through to one of them, and a change that does reload simply
    re-seeds the set from ``entry.data`` at setup.
    """
    coordinator: Rtl433Coordinator | None = hass.data.get(DOMAIN, {}).get(
        entry.entry_id
    )
    if coordinator is None:
        return

    # Applied first, and unconditionally: ignoring a device must take effect on
    # its very next transmission, and it is the one change here that never needs
    # a reload, so it must not sit behind an early return below.
    coordinator.ignored = set(_hub_ignored_devices(entry))

    if _hub_connection(entry) != coordinator.connection_snapshot:
        # A reconfigure / re-advertised discovery / rebind re-pointed the hub at a
        # new server (or a new stable radio id); the socket is built at setup, so
        # reload to reconnect against the new target.
        await hass.config_entries.async_reload(entry.entry_id)
        return

    new_manage = _hub_manage_settings(entry)
    if new_manage != coordinator.manage_settings:
        # The entity set changes (SDR controls appear / disappear) and the
        # coordinator's adoption/enforcement flips, so reload to rebuild.
        await hass.config_entries.async_reload(entry.entry_id)
        return

    if _calibration_map(entry) != coordinator.calibration_snapshot:
        # A consumption sensor's device_class / unit / state_class are
        # construction-time, so rebuild the affected entity by reloading the hub.
        await hass.config_entries.async_reload(entry.entry_id)
        return

    if (entry.data.get(CONF_USER_MAPPINGS) or {}) != coordinator.user_mappings_snapshot:
        # The user mappings drive the merged library (descriptors + skip_keys),
        # which is consumed at construction time, so reload to rebuild the merged
        # library and the affected entities.
        await hass.config_entries.async_reload(entry.entry_id)
        return

    coordinator.availability_timeout = _hub_availability_timeout(entry)
    LOGGER.debug(
        "rtl_433 hub %s options updated (timeout=%ss)",
        entry.title,
        coordinator.availability_timeout,
    )


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload the hub config entry.

    Stops the coordinator, drops its runtime state, clears any reachability
    repair issue, and unloads the forwarded entity platforms.
    """
    coordinator: Rtl433Coordinator | None = hass.data.get(DOMAIN, {}).get(
        entry.entry_id
    )
    if coordinator is not None:
        await coordinator.async_stop()
    hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    hass.data.get(DOMAIN, {}).get(DATA_ENTRY_LIBRARY, {}).pop(entry.entry_id, None)
    repairs.async_clear_hub_unreachable(hass, entry)
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_config_entry_device(
    hass: HomeAssistant, config_entry: ConfigEntry, device_entry: DeviceEntry
) -> bool:
    """Allow removing a single nested RF device from its device page.

    Refuses to remove the hub device itself (identifier
    ``(DOMAIN, entry.entry_id)``) so the hub cannot be deleted out from under its
    config entry. For a nested device, drops it from the hub's devices map and
    un-adopts its ``device_key`` in the coordinator, so the device's next
    transmission makes it a pending candidate the user can choose to add again
    rather than silently re-creating the device they just deleted.
    """
    if (DOMAIN, config_entry.entry_id) in device_entry.identifiers:
        return False

    # Find this device's device_key from its identifier
    # ``(DOMAIN, f"{entry_id}:{device_key}")``.
    device_key: str | None = None
    for domain, ident in device_entry.identifiers:
        if domain == DOMAIN and ident.startswith(f"{config_entry.entry_id}:"):
            device_key = ident.split(":", 1)[1]
            break

    if device_key is not None:
        devices = {
            k: v
            for k, v in config_entry.data.get(CONF_DEVICES, {}).items()
            if k != device_key
        }
        hass.config_entries.async_update_entry(
            config_entry, data={**config_entry.data, CONF_DEVICES: devices}
        )
        coordinator: Rtl433Coordinator | None = hass.data.get(DOMAIN, {}).get(
            config_entry.entry_id
        )
        if coordinator is not None:
            coordinator.forget_device(device_key)
            # Drop the entity platforms' per-device dedup cache and field
            # listeners so the device re-appears cleanly if the user later adds
            # it back from the pending list.
            for remover in list(coordinator.device_removers):
                remover(device_key)

    return True


__all__: list[str] = [
    "async_migrate_entry",
    "async_remove_config_entry_device",
    "async_setup_entry",
    "async_unload_entry",
]
