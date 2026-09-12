"""The rtl_433 integration.

This module wires the integration's config-entry lifecycle. A config entry is a
**location**: a user-named grouping that holds one **receiver config subentry**
per rtl_433 server. Setting one up loads the shipped mapping library (cached once
on ``hass.data[DOMAIN][DATA_LIBRARY]``), merges the location's stored
``entry.data[CONF_USER_MAPPINGS]`` over it and caches the per-entry merged
``(registry, skip_keys)`` on ``hass.data[DOMAIN][DATA_ENTRY_LIBRARY][entry_id]``
so the entity platforms reuse it, registers the **location device** (the root of
the entry's device tree, and what every merged RF device links to with
``via_device_id``), then walks the receiver subentries: each gets a
receiver device, a push
:class:`~custom_components.rtl_433.coordinator.Rtl433Coordinator` (the WebSocket
transport is per endpoint, so one per receiver) with the skip-keys, the
effective-timeout resolver and the new-device callback injected, and its own
reachability watchers. Each coordinator is stored on ``hass.data[DOMAIN]`` under
its **receiver id** (the subentry id). A **location aggregator**
(:mod:`.aggregator`) is then started over those coordinators: it dedupes the
frames several receivers decode from one transmission and re-emits them on a
single location-scoped signal, which is what lets one physical sensor carry one
merged device and one entity per field. Finally an options-update listener is
registered so a changed availability timeout takes effect live, and the entity
platforms are forwarded **once**, on the location entry.

Subentry ownership is deliberate and load-bearing. A device belongs to exactly
one config entry and one subentry, and adding entities from two subentries that
share a device silently moves it today and raises in HA Core 2027.8. So the
**receiver** device -- and everything that hangs off it: the radio controls, the
noise sensors, the connectivity sensor -- is registered under its receiver's
``config_subentry_id``, while every **RF device** and its entities are added with
no ``config_subentry_id`` at all, leaving them owned by the location entry so a
later merge across receivers is legal.

RF devices are represented as **device-registry devices nested under the location
entry** (rfxtrx-style), not as their own config entries. They are recreated on
startup from ``entry.data[CONF_DEVICES]`` — the restart-safe record of the
devices the user has adopted — and added at runtime via the new-device
dispatcher signal when the user approves one from a coordinator's pending
list. A device the user has not adopted is only ever *heard*; it never reaches
the device registry. A single nested device can be removed from its device page
via :func:`async_remove_config_entry_device`, which returns it to the pending
list; deleting the location entry removes all nested devices and entities
automatically, and deleting a receiver subentry removes that receiver's own
device (and, via :func:`_async_purge_removed_receiver_entities`, its
per-receiver signal entities on the merged devices) while every merged device
and its history survive.

Setting up the first location also registers the discovery WebSocket commands
(:mod:`.websocket_api`), which back the approval panel and are equally usable
from a script, and the **discovery panel** itself — the shipped
``frontend/rtl_433-panel.js`` served as a static path and registered with
``panel_custom``. Both are per Home Assistant *run*, not per entry, so both are
guarded to happen once rather than once per location.

The library loading lives in :mod:`.library`, the location/receiver topology
helpers and setting resolvers in
:mod:`.receiver_settings`, the shared adopt / ignore / un-ignore service in
:mod:`.adoption`, and the config-entry migration / one-time legacy cleanups in
:mod:`.migration`; this module keeps only the steady-state lifecycle.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from pyrtl_433.library import Registry, event_driven_field_keys

from homeassistant.components import panel_custom
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntry
from homeassistant.helpers.dispatcher import async_dispatcher_send

from . import repairs
from .aggregator import Rtl433LocationAggregator
from .const import (
    CONF_DEVICES,
    CONF_HOST,
    CONF_INITIAL_FREQUENCY,
    CONF_PATH,
    CONF_PORT,
    CONF_USER_MAPPINGS,
    DATA_AGGREGATOR,
    DATA_ENTRY_LIBRARY,
    DEFAULT_MOTION_CLEAR_DELAY,
    DEVICE_TIMEOUT_OVERRIDE,
    DOMAIN,
    LOGGER,
    MANUFACTURER,
    PLATFORMS,
    is_reserved_device_key,
    receiver_identity,
    signal_new_device,
)
from .coordinator import Rtl433Coordinator
from .library import _async_load_library, _merge_entry_library
from .migration import (
    _cleanup_phantom_unknown_device,
    _migrate_motion_event_to_binary_sensor,
    async_migrate_entry,
)
from .receiver_settings import (
    _calibration_map,
    _explicit_receiver_timeout,
    _receiver_availability_timeout,
    _receiver_connection,
    _receiver_ignored_devices,
    _receiver_manage_settings,
    _receiver_secure,
    receiver_coordinators,
    receiver_subentries,
    running_coordinators,
)
from .settings import device_clear_delay
from .websocket_api import async_preload_entity_metadata, async_register_commands

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

# Key under ``hass.data[DOMAIN]`` claimed synchronously by whichever location setup
# gets to the panel registration first. An "is the panel already there?" check
# is not enough: the registration awaits (the static-path helper hops to the
# executor), and Home Assistant sets a domain's config entries up with
# ``asyncio.gather``, so two receivers both pass a check that only becomes true
# *after* the await. This flag is set before the first await, which is what
# makes the guard hold across it.
DATA_PANEL_CLAIMED = "_panel_claimed"


async def _async_register_panel(hass: HomeAssistant) -> None:
    """Serve and register the discovery panel, once per Home Assistant run.

    Registration is per Home Assistant *run* while this is called from
    per-entry setup, so a second receiver must not try to register a second
    panel: ``async_register_built_in_panel`` raises rather than tolerating the
    duplicate.

    The guard is :data:`DATA_PANEL_CLAIMED`, claimed synchronously — before any
    await — because the obvious guard does not hold. Home Assistant sets a
    domain's entries up concurrently (``asyncio.gather`` in ``setup.py``) and
    the registration below awaits before the panel exists, so an "is it already
    registered?" check would be passed by both receivers and the second would
    die on ``Overwriting panel``. A flag taken before the first await is what
    survives that, and it covers the sequential cases (a receiver added later, an
    entry reloading) as well.

    ``config_panel_domain`` puts the panel behind this integration's entry in
    Settings → Devices & services, and no ``sidebar_title``/``sidebar_icon`` are
    passed, so it takes no top-level sidebar slot. A custom integration cannot
    join the Settings list that Bluetooth, Tags and Z-Wave appear in: those are
    hard-coded routes inside the ``config`` panel, which core's frontend ships
    compiled (``zwave_js`` registers no panel of its own at all). Behind the
    integration's own entry is as close to "in Settings" as a custom panel gets.

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
    if domain_data.get(DATA_PANEL_CLAIMED):
        return
    domain_data[DATA_PANEL_CLAIMED] = True

    # The claim is released again if registration fails, so Home Assistant's
    # retry of a receiver that went ``ConfigEntryNotReady`` here (or the user's next
    # receiver) tries once more rather than quietly setting up a receiver whose panel
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
            config_panel_domain=DOMAIN,
        )
    except Exception:
        domain_data.pop(DATA_PANEL_CLAIMED, None)
        raise


async def _async_setup_receiver(
    hass: HomeAssistant,
    entry: ConfigEntry,
    subentry: ConfigSubentry,
    *,
    device_registry: dr.DeviceRegistry,
    entry_registry: Registry,
    entry_skip_keys: set[str],
    entry_event_driven_keys: frozenset[str],
    effective_timeout_resolver: Callable[[str], int | None],
    effective_clear_delay_resolver: Callable[[str], int],
    location_device_id: str,
) -> Rtl433Coordinator:
    """Register one receiver's device and build + start its coordinator.

    One receiver subentry, one coordinator: the WebSocket transport is per
    endpoint, so a location with two servers dials two sockets. Everything the
    coordinator reads that describes *sensors* (the devices map, the ignore
    list, the merged library, the availability defaults) comes from the location
    entry and is therefore shared; everything that describes *this server* (the
    connection target, the managed-radio toggle, the initial frequency) comes
    from the subentry.

    The receiver device is created **under the subentry**
    (``config_subentry_id``), which is what makes it disappear with the receiver
    when the user deletes one. Its nested RF devices deliberately are not: they
    belong to the location entry alone, because a device may end up shared
    between receivers and Home Assistant gives a device exactly one owning
    subentry -- adding entities from two subentries that share a device silently
    moves it today and raises in HA Core 2027.8.
    """
    receiver_id = subentry.subentry_id
    identity = receiver_identity(entry.entry_id, receiver_id)

    def new_device_callback(device_key: str, model: str, is_replay: bool) -> None:
        """Dispatch the receiver-level new-device signal for an adopted device.

        The coordinator invokes this only for devices the user has adopted --
        either on an adopted device's first frame this process, or from
        ``adopt_device`` the moment the user approves a pending one -- so the
        platform listeners can add the nested device + its entities directly.
        There is no notification: a device now exists in Home Assistant only
        because the user asked for it, so there is nothing to alert them to.

        ``is_replay`` flags that the frame the entities are seeding from is a
        reconnect re-broadcast rather than a live transmission; it is passed
        through to the platform listeners unchanged.
        """
        async_dispatcher_send(hass, signal_new_device(receiver_id), device_key, model)

    # Register the receiver device, hung off the location device. The
    # manufacturer/model start generic and are refined to the real SDR's
    # vendor/product/serial once the coordinator connects
    # (``receiver_info_callback``). Nested RF devices link to the *location*, not
    # here: a merged device may be fed by several receivers, so a link to one of
    # them would claim the sensor sits behind that server alone.
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        config_subentry_id=receiver_id,
        identifiers={(DOMAIN, identity)},
        manufacturer=MANUFACTURER,
        name=subentry.title,
        model="rtl_433 server",
        via_device_id=location_device_id,
    )

    coordinator = Rtl433Coordinator(
        hass,
        entry,
        subentry,
        host=subentry.data[CONF_HOST],
        port=subentry.data[CONF_PORT],
        path=subentry.data[CONF_PATH],
        secure=_receiver_secure(subentry),
        manage_settings=_receiver_manage_settings(entry, subentry),
        availability_timeout=_receiver_availability_timeout(entry),
        initial_center_frequency=subentry.data.get(CONF_INITIAL_FREQUENCY),
        skip_keys=entry_skip_keys,
        event_driven_keys=entry_event_driven_keys,
        # The persisted devices map is the restart-safe record of what the user
        # has approved, so it is what tells the coordinator which frames may
        # reach Home Assistant; everything else is heard into the pending list.
        adopted_keys=set(entry.data.get(CONF_DEVICES, {})),
        ignored_keys=set(_receiver_ignored_devices(entry)),
    )

    @callback
    def receiver_info_callback() -> None:
        """Refresh the receiver device's identity from the SDR's ``dev_info``.

        ``coordinator.dev_info`` is the librtlsdr USB label
        (``{"vendor", "product", "serial"}``); map it onto the receiver device so
        the device page shows which physical dongle this receiver is, instead of
        the generic ``rtl_433`` / ``rtl_433 server`` placeholders. Absent fields
        (e.g. ``-D manual`` with no SDR open) leave the existing values untouched.
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
        device = device_registry.async_get_device_by_identifier(
            (DOMAIN, identity), entry.entry_id
        )
        if device is not None:
            device_registry.async_update_device(device.id, **updates)

    coordinator.new_device_callback = new_device_callback
    coordinator.receiver_info_callback = receiver_info_callback
    coordinator.effective_timeout_resolver = effective_timeout_resolver
    coordinator.effective_clear_delay_resolver = effective_clear_delay_resolver
    # Global descriptor keys from the merged library, so the coordinator can flag
    # observed fields with no mapping at DEBUG (matches the diagnostics
    # ``unmatched_field_keys`` semantics, which resolve against the flat table).
    coordinator.known_field_keys = frozenset(entry_registry.flat)
    # Snapshot the per-device calibration so the update listener can detect a
    # real calibration change (and reload) while ignoring routine devices-map
    # upserts -- the same change-vs-snapshot pattern as ``manage_settings``.
    coordinator.calibration_snapshot = _calibration_map(entry)
    # Snapshot the stored user mappings so the update listener can detect a real
    # mappings change (and reload to rebuild the merged library + entities) while
    # ignoring routine devices-map upserts.
    coordinator.user_mappings_snapshot = entry.data.get(CONF_USER_MAPPINGS) or {}
    # Snapshot the connection target + stable identity so the update listener can
    # reload the location when a reconfigure / discovery / rebind re-points this
    # receiver: those flows write the new target into the subentry and leave the
    # reload to this listener (see ``_async_update_listener``).
    coordinator.connection_snapshot = _receiver_connection(subentry)

    hass.data[DOMAIN][receiver_id] = coordinator
    await coordinator.async_start()

    # Watch reachability and surface / clear a repair issue accordingly.
    entry.async_on_unload(
        repairs.async_track_receiver_reachability(hass, entry, coordinator)
    )
    # Advise when a single high-band frequency is left at the default sample rate.
    entry.async_on_unload(repairs.async_track_sample_rate(hass, entry, coordinator))
    # Advise when the server stamps events in a form that cannot be parsed, which
    # leaves the library's reconnect-replay suppression switched off.
    entry.async_on_unload(
        repairs.async_track_event_time_precision(hass, entry, coordinator)
    )
    return coordinator


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up an rtl_433 **location** config entry.

    Loads the library once for the location, then walks the location's receiver
    subentries: each gets its receiver device, its coordinator and its
    reachability watchers. The entity platforms are forwarded **once**, on the
    location entry, and each platform fans out over the same subentries -- that
    is what lets one device carry entities fed by several receivers.

    A location with no receiver subentry cannot be created by any flow, so one
    here is a config entry written by an older schema. It is refused loudly
    rather than set up as an empty shell.
    """
    hass.data.setdefault(DOMAIN, {})
    # Command names are global and registration is per Home Assistant run, not
    # per entry -- but this integration is entry-only (no ``async_setup``), so the
    # call is made from every location's setup and made idempotent inside. A user
    # with two locations must not lose the second entry to a duplicate registration.
    async_register_commands(hass)
    # Core's own icon and string tables, read once so the discovery payload can
    # preview a field's entity without file I/O on the event loop.
    await async_preload_entity_metadata(hass)
    # Same story for the panel: per-run, called from per-entry setup, idempotent
    # inside. It is awaited before anything else because a failure here is a
    # failure to set the location up at all, and that should be loud rather than a
    # working location with a panel nobody can reach.
    await _async_register_panel(hass)

    subentries = receiver_subentries(entry)
    if not subentries:
        raise ConfigEntryError(
            f"{entry.title} has no receiver: it predates the location/receiver "
            "model and has not been migrated"
        )

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
        -> explicit location default (only when ``CONF_AVAILABILITY_TIMEOUT`` is
        actually present in the entry's options/data). Returns ``None`` when neither
        is set, signalling the coordinator to apply the device-class default from
        the device's latest payload. An explicit ``0`` at either tier means
        never-expire and is returned as ``0`` (never falls through).
        """
        override = (
            entry.data.get(CONF_DEVICES, {})
            .get(device_key, {})
            .get(DEVICE_TIMEOUT_OVERRIDE)
        )
        if override is not None:
            return int(override)
        return _explicit_receiver_timeout(entry)

    def effective_clear_delay_resolver(device_key: str) -> int:
        """Resolve a device's effective motion clear-delay (override > default).

        Reads the per-device ``motion_clear_delay`` through
        :func:`.settings.device_clear_delay`, which looks in ``entry.options``
        before ``entry.data``; falls back to ``DEFAULT_MOTION_CLEAR_DELAY`` when
        neither holds one.

        Both locations have to be consulted because the two are written by
        different eras of this integration. The migration from per-device child
        entries lands the value in ``data``, which is the only place this
        resolver used to look -- while every edit made through the settings UI
        writes it to ``options``, where nothing read it. A clear-delay set by
        hand therefore did nothing at all; consulting options first is what
        makes that knob take effect, including for anyone who set one long ago
        and assumed it had.
        """
        override = device_clear_delay(entry, device_key)
        if override is not None:
            return override
        return DEFAULT_MOTION_CLEAR_DELAY

    device_registry = dr.async_get(hass)
    _cleanup_phantom_unknown_device(hass, entry, device_registry)
    _migrate_motion_event_to_binary_sensor(hass, entry, er.async_get(hass))

    # The location device: the root of this entry's device tree and the target
    # every merged RF device links to with ``via_device_id``. Registered before
    # the receivers so both that link and the receivers' own resolve on the first
    # pass. It is owned by the entry with **no** ``config_subentry_id`` -- it
    # describes the location, which outlives any one receiver in it.
    location_device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        manufacturer=MANUFACTURER,
        name=entry.title,
        model="rtl_433 location",
        entry_type=dr.DeviceEntryType.SERVICE,
    )

    for subentry in subentries:
        await _async_setup_receiver(
            hass,
            entry,
            subentry,
            device_registry=device_registry,
            entry_registry=entry_registry,
            entry_skip_keys=entry_skip_keys,
            entry_event_driven_keys=entry_event_driven_keys,
            effective_timeout_resolver=effective_timeout_resolver,
            effective_clear_delay_resolver=effective_clear_delay_resolver,
            location_device_id=location_device.id,
        )

    # The union's fan-in. Started once every receiver's coordinator exists (it
    # subscribes to each of them) and before the platforms are forwarded, so the
    # merged devices' entities find a live location-scoped stream the moment they
    # subscribe to it.
    aggregator = Rtl433LocationAggregator(hass, entry)
    aggregator.async_start()
    hass.data[DOMAIN].setdefault(DATA_AGGREGATOR, {})[entry.entry_id] = aggregator

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Push changed location / receiver options into the running coordinators.

    The receiver set itself is checked first: adding, removing or re-pointing a
    receiver subentry all change what ``_receiver_connection`` reports, and every
    one of them needs the location reloaded -- a receiver that has just been
    added has no coordinator and no entities until setup runs again, and a
    re-pointed one is still dialling the old endpoint (the socket is built at
    setup). The reconfigure / Supervisor-discovery / rebind flows deliberately
    only *write* the new target, because Home Assistant forbids an integration
    from combining a config-entry update listener with the reloading config-flow
    helpers -- that pair double-reloads and races -- so this listener is the
    single place that reloads.

    The manage-settings toggle changes the entity set (the radio control entities
    appear / disappear) and the coordinator's adoption/enforcement behaviour, so
    a change there requires a full reload to rebuild everything. Each running
    coordinator holds the *previous* effective value as
    ``coordinator.manage_settings``; comparing it against the new effective value
    detects the change without persisting extra bookkeeping.

    A per-device calibration change is detected the same way: the options device
    step writes the calibration into ``entry.data[CONF_DEVICES]`` (firing this
    listener), and a consumption sensor's ``device_class`` / unit / ``state_class``
    are construction-time, so the affected entity must be rebuilt by reloading the
    location. The new calibration map is compared against ``coordinator.calibration_
    snapshot`` (captured at setup) so the *frequent* idempotent devices-map upserts
    (``async_upsert_device`` / ``async_upsert_event_types``), which leave the
    calibration sub-record untouched, never trigger a reload.

    An availability-timeout change is applied live instead (each coordinator
    reads ``availability_timeout`` on every watchdog tick), so no reload is
    required for it and we avoid the disruption of tearing the sockets down.

    The location's ignore list is applied live too, and *first*: ignoring or
    un-ignoring a device changes nothing about the devices and entities that
    exist, so tearing the WebSockets down for it would be gratuitous. Pushing it
    before the reload comparisons also means the options flow's ignore-only write
    can never fall through to one of them, and a change that does reload simply
    re-seeds the set from ``entry.data`` at setup.
    """
    coordinators = running_coordinators(hass, entry)
    if not coordinators:
        return

    # Applied first, and unconditionally: ignoring a device must take effect on
    # its very next transmission, and it is the one change here that never needs
    # a reload, so it must not sit behind an early return below.
    ignored = set(_receiver_ignored_devices(entry))
    for coordinator in coordinators.values():
        coordinator.ignored = set(ignored)

    stored = {subentry.subentry_id for subentry in receiver_subentries(entry)}
    if set(coordinators) != stored or any(
        _receiver_connection(coordinator.subentry) != coordinator.connection_snapshot
        for coordinator in coordinators.values()
    ):
        # A receiver was added or removed, or a reconfigure / re-advertised
        # discovery / rebind re-pointed one at a new server (or a new stable
        # radio id). Either way the running set no longer matches the stored one.
        _async_purge_removed_receiver_entities(hass, entry, set(coordinators) - stored)
        await hass.config_entries.async_reload(entry.entry_id)
        return

    if any(
        _receiver_manage_settings(entry, coordinator.subentry)
        != coordinator.manage_settings
        for coordinator in coordinators.values()
    ):
        # The entity set changes (radio controls appear / disappear) and the
        # coordinator's adoption/enforcement flips, so reload to rebuild.
        await hass.config_entries.async_reload(entry.entry_id)
        return

    calibration = _calibration_map(entry)
    user_mappings = entry.data.get(CONF_USER_MAPPINGS) or {}
    for coordinator in coordinators.values():
        if calibration != coordinator.calibration_snapshot:
            # A consumption sensor's device_class / unit / state_class are
            # construction-time, so rebuild the affected entity by reloading.
            await hass.config_entries.async_reload(entry.entry_id)
            return
        if user_mappings != coordinator.user_mappings_snapshot:
            # The user mappings drive the merged library (descriptors +
            # skip_keys), which is consumed at construction time, so reload to
            # rebuild the merged library and the affected entities.
            await hass.config_entries.async_reload(entry.entry_id)
            return

    timeout = _receiver_availability_timeout(entry)
    for coordinator in coordinators.values():
        coordinator.availability_timeout = timeout
    LOGGER.debug(
        "rtl_433 location %s options updated (timeout=%ss, receivers=%s)",
        entry.title,
        timeout,
        len(coordinators),
    )


@callback
def _async_purge_removed_receiver_entities(
    hass: HomeAssistant, entry: ConfigEntry, removed: set[str]
) -> None:
    """Remove a deleted receiver's per-receiver signal entities.

    Deleting a receiver subentry is mostly Home Assistant's job: it clears the
    subentry off the registries, which takes the **receiver** device with
    everything on it — the radio controls, the noise sensors, the connectivity
    sensor — because all of those are owned by that subentry.

    It cannot take the one set of entities that describes the receiver but does
    **not** live on its device: the per-receiver ``rssi`` / ``snr`` / ``last_seen``
    entities on every merged device. Those are added with no
    ``config_subentry_id`` on purpose (a merged device fed by two receivers may
    not hold entities from two subentries), so the subentry sweep does not see
    them and they would survive as permanently-unavailable orphans of a server
    that is gone. They are found here by their identity instead: the four-segment
    ``{location_entry_id}:{device_key}:{receiver_subentry_id}:{object_suffix}``,
    matched on the removed receiver's id in the third segment.

    What deliberately does **not** happen: the merged devices themselves and
    their unioned entities are left completely alone, history included. A sensor
    only the removed receiver ever heard keeps its device and its recorded
    history and simply goes unavailable once no remaining receiver vouches for it
    — deleting it stays an explicit user action, the same as any other RF device.

    Runs before the reload so the rebuild does not re-add an entity for a
    receiver that no longer exists; a no-op when nothing was removed (the same
    branch also fires for a receiver *added* or re-pointed).
    """
    if not removed:
        return
    entity_registry = er.async_get(hass)
    for ent in list(er.async_entries_for_config_entry(entity_registry, entry.entry_id)):
        parts = ent.unique_id.split(":")
        if len(parts) == 4 and parts[0] == entry.entry_id and parts[2] in removed:
            entity_registry.async_remove(ent.entity_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a location config entry and every receiver in it.

    Stops each receiver's coordinator, drops its runtime state, clears its
    reachability repair issue, tears down the location aggregator, and unloads
    the entity platforms that were forwarded once on the location.
    """
    aggregator = (
        hass.data.get(DOMAIN, {}).get(DATA_AGGREGATOR, {}).pop(entry.entry_id, None)
    )
    if aggregator is not None:
        aggregator.async_stop()
    # Driven from what is *running*, not from what is stored, so a receiver whose
    # subentry was deleted still has its socket closed and its card cleared.
    for receiver_id, coordinator in running_coordinators(hass, entry).items():
        await coordinator.async_stop()
        hass.data.get(DOMAIN, {}).pop(receiver_id, None)
        repairs.async_clear_receiver_unreachable(hass, entry, receiver_id)
    hass.data.get(DOMAIN, {}).get(DATA_ENTRY_LIBRARY, {}).pop(entry.entry_id, None)
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_config_entry_device(
    hass: HomeAssistant, config_entry: ConfigEntry, device_entry: DeviceEntry
) -> bool:
    """Allow removing a single nested RF device from its device page.

    Refuses to remove the **location** device (identifier ``(DOMAIN, entry_id)``,
    one segment) and a **receiver** device, so neither can be deleted out from
    under the entry it structures: removing a receiver is subentry surgery
    (delete the receiver) and removing the location is deleting the entry,
    not the "forget this RF device" this handler implements. A receiver's
    identifier is the three-segment
    ``f"{location_entry_id}:receiver:{receiver_subentry_id}"``, recognised by the
    reserved ``receiver`` marker rather than by guessing at which ids are entry
    ids, so it is never decoded into a bogus ``device_key``.

    For a merged RF device -- identifier
    ``(DOMAIN, f"{location_entry_id}:{device_key}")``, where ``device_key`` never
    contains a colon (``pyrtl_433.naming.safe_token``) -- the key is dropped from
    the location's devices map and un-adopted in every receiver's coordinator, so
    the device's next transmission from *any* receiver makes it a pending
    candidate the user can choose to add again rather than silently re-creating
    the device they just deleted.
    """
    coordinators = receiver_coordinators(hass, config_entry)

    device_key: str | None = None
    for domain, ident in device_entry.identifiers:
        if domain != DOMAIN:
            continue
        if ident == config_entry.entry_id:
            # The location device itself.
            return False
        parts = ident.split(":")
        if len(parts) == 3 and is_reserved_device_key(parts[1]):
            # A receiver device.
            return False
        if len(parts) != 2 or parts[0] != config_entry.entry_id:
            continue
        if is_reserved_device_key(parts[1]):
            return False
        device_key = parts[1]
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
        for coordinator in coordinators.values():
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
