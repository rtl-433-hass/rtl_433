"""WebSocket API for the rtl_433 discovery panel.

Admin-gated commands that expose the pending-device list, the three things a
user can do with it, and the hub's settings. They are the panel's data source, but they are not
*only* that: the same commands are the scriptable, UI-free way to see what a
receiver is hearing and to approve it, and they are testable without loading any
JavaScript at all.

- ``rtl_433/hubs`` — the configured hubs, so a caller can address one.
- ``rtl_433/devices/pending`` — one hub's candidates, plus its ignore list.
- ``rtl_433/devices/add`` / ``.../ignore`` / ``.../unignore`` — the three
  actions, each delegating to :mod:`.adoption` so they do exactly what the
  options flow does.
- ``rtl_433/devices/subscribe`` — the same payload as ``.../pending``, pushed
  when it changes.
- ``rtl_433/settings/get`` and ``.../hub`` / ``.../device`` / ``.../mappings`` —
  the hub's own settings, one device's settings, and the device-library
  overrides. These are the panel's half of the same forms the options flow
  renders; both sides build their dicts with :mod:`.settings`, so what a form
  stores does not depend on which form was used.

Every command names a hub by ``entry_id`` and resolves it through
:func:`_async_get_coordinator`, which answers a bad id with a WebSocket error
rather than an exception. A panel left open across a hub reload will send
commands for an entry that is momentarily not loaded, and that is a normal
condition to report, not a crash to log.

**The subscription must not become a firehose.** A receiver in a dense
neighbourhood decodes constantly, and the naive wiring -- push the list on every
frame -- would send a full payload down every open socket so one row's count
could tick up by one. So the pushes come from two places with two different
urgencies. A *membership* change (:data:`~.const.SIGNAL_PENDING_UPDATE`: a
candidate appeared, or one was adopted, ignored, or un-ignored) is the answer to
"is there something new for me?" and pushes immediately. A *repeat sighting*
only ages the count and last-seen columns of a row already on screen, so it is
picked up by a slow :data:`_REFRESH_INTERVAL` timer that re-renders the payload
and sends it only when it actually differs from the last one sent. N frames for a
known candidate therefore cost at most one message per interval, and zero when
nothing on screen would change.

That coalescing lives here rather than in the coordinator on purpose: the
coordinator stays a pure state holder that does not know a panel exists, and the
policy about how often a UI may be told is owned by the layer that talks to the
UI.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
import logging
from typing import Any, Final

from pyrtl_433.library import (
    FieldDescriptor,
    Registry,
    apply_transform,
    lookup,
    validate_user_mappings,
)
import voluptuous as vol
import yaml

from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.icon import async_get_icons
from homeassistant.helpers.translation import async_get_translations

from .adoption import (
    AdoptionResult,
    async_adopt_devices,
    async_ignore_devices,
    async_unignore_devices,
)
from .calibration import COMMODITY_UNITS, normalize_calibration
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
    DATA_ENTITY_META,
    DATA_ENTRY_LIBRARY,
    DEFAULT_AVAILABILITY_TIMEOUT,
    DEFAULT_MOTION_CLEAR_DELAY,
    DEVICE_MOTION_CLEAR_DELAY,
    DEVICE_TIMEOUT_OVERRIDE,
    DOMAIN,
    signal_pending_update,
)
from .coordinator import Rtl433Coordinator
from .device_replace import DeviceReplaceError, async_replace_device
from .hub_settings import _hub_ignored_devices
from .settings import (
    MAPPINGS_DOCS_URL,
    build_device_data,
    build_device_options,
    build_hub_options,
    build_mappings_data,
    device_defaults,
    hub_defaults,
)

_LOGGER = logging.getLogger(__name__)

# How often an open subscription re-renders its payload to catch changes that
# fire no signal: the sighting count and last-seen of a candidate already on the
# list. Long enough that a busy receiver cannot turn it into a stream, short
# enough that a user watching a device transmit sees the count move. Membership
# changes do not wait for this -- they push at once.
_REFRESH_INTERVAL: Final = timedelta(seconds=5)

# Error code for a hub whose entry exists but is not set up. Distinct from
# ``ERR_NOT_FOUND`` (no such entry) because the two need different answers: a
# not-loaded hub is a hub to retry against or repair, not a typo.
ERR_NOT_LOADED: Final = "not_loaded"
# A replace the user asked for that the helper refused: an unknown survivor, or
# the same key on both sides. Its own code rather than ``not_loaded`` so a script
# can tell "your hub is mid-reload, retry" from "that request cannot work".
ERR_REPLACE_FAILED: Final = "replace_failed"
# Mapping overrides the user submitted that this hub will not store: unparseable
# YAML, or a document that parses but breaks the override schema. Its own code so
# the caller can render the problems in the editor rather than as a generic
# failure, and the message carries them joined for a client that cannot.
ERR_INVALID_MAPPINGS: Final = "invalid_mappings"


@callback
def async_register_commands(hass: HomeAssistant) -> None:
    """Register the discovery commands.

    Called from every hub's ``async_setup_entry`` because this integration is
    entry-only. Command names are global and registration is really per Home
    Assistant *run*, but it needs no guard of its own:
    ``async_register_command`` is a dict assignment keyed by command name, so a
    second hub -- or the same hub reloading -- rewrites the same entries with the
    same handlers. Registering is idempotent, so the simplest thing that works is
    to just register.
    """
    websocket_api.async_register_command(hass, ws_hubs)
    websocket_api.async_register_command(hass, ws_pending_devices)
    websocket_api.async_register_command(hass, ws_add_devices)
    websocket_api.async_register_command(hass, ws_ignore_devices)
    websocket_api.async_register_command(hass, ws_unignore_devices)
    websocket_api.async_register_command(hass, ws_replace_device)
    websocket_api.async_register_command(hass, ws_clear_devices)
    websocket_api.async_register_command(hass, ws_subscribe_devices)
    websocket_api.async_register_command(hass, ws_get_settings)
    websocket_api.async_register_command(hass, ws_set_hub_settings)
    websocket_api.async_register_command(hass, ws_set_device_settings)
    websocket_api.async_register_command(hass, ws_set_mappings)


@callback
def _async_get_coordinator(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> tuple[ConfigEntry, Rtl433Coordinator] | None:
    """Resolve ``msg["entry_id"]`` to a hub, or answer with an error.

    Returns ``None`` after sending the error, so every handler's first line can
    be a bail-out and a bad ``entry_id`` never escapes as an exception. Two
    distinct failures are worth distinguishing to the caller: an id that names no
    entry of this integration at all (a stale panel bookmark, or a typo in a
    script) and one whose entry exists but is not set up (a hub mid-reload, or one
    whose server is unreachable). The pending list lives only in the
    coordinator's memory, so the second case has nothing to answer with either --
    but it is a wait, not a mistake.
    """
    entry_id: str = msg["entry_id"]
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None or entry.domain != DOMAIN:
        connection.send_error(
            msg["id"],
            websocket_api.const.ERR_NOT_FOUND,
            f"Unknown rtl_433 hub {entry_id}",
        )
        return None

    coordinator: Rtl433Coordinator | None = hass.data.get(DOMAIN, {}).get(entry_id)
    if coordinator is None or entry.state is not ConfigEntryState.LOADED:
        connection.send_error(
            msg["id"],
            ERR_NOT_LOADED,
            f"rtl_433 hub {entry.title} is not loaded",
        )
        return None

    return entry, coordinator


async def async_preload_entity_metadata(hass: HomeAssistant) -> None:
    """Warm the tables Home Assistant describes its own entities with.

    Two of them, both keyed by device class and both shipped by the platform
    integrations themselves: ``icons.json`` for the icon, and the
    ``entity_component`` strings for the name and for a binary entity's on/off
    words. They are what the frontend renders a device page from, so reading
    them is what lets this payload promise "these are the entities you are
    about to get" rather than an approximation of them.

    Loaded once during hub setup because both accessors do file I/O on first
    use, and cached so ``_pending_payload`` -- a sync ``@callback`` -- can
    resolve without awaiting. Failure is not fatal: the preview degrades to
    un-iconed, un-translated rows rather than failing a hub's setup.

    The strings are fetched for the language configured *now*. A hub reload
    picks up a language change; nothing else does.
    """
    domain_data = hass.data.setdefault(DOMAIN, {})
    if DATA_ENTITY_META in domain_data:
        return

    platforms = ["sensor", "binary_sensor", "event"]
    try:
        icons = await async_get_icons(hass, "entity_component", platforms)
    except Exception:  # noqa: BLE001 - decoration must never fail a setup
        _LOGGER.debug("Could not load entity icons; the panel will show none")
        icons = {}
    try:
        strings = await async_get_translations(
            hass, hass.config.language, "entity_component", platforms
        )
    except Exception:  # noqa: BLE001 - as above
        _LOGGER.debug("Could not load entity strings; the panel will derive names")
        strings = {}

    domain_data[DATA_ENTITY_META] = _EntityMeta(
        icons=_with_sorted_ranges(icons), strings=strings
    )


def _with_sorted_ranges(icons: dict[str, Any]) -> dict[str, Any]:
    """Pre-sort every device class's icon bands, once, at load.

    Core keys a ``range`` by its lower bound as a *string* (``battery`` has
    eleven of them). Sorting and float-parsing that on every reading of every
    candidate of every push is pure recomputation of a constant, so it is done
    here instead and stored as ``[(bound, icon), ...]`` ascending.
    """
    converted: dict[str, Any] = {}
    for platform, classes in icons.items():
        converted[platform] = {}
        for device_class, entry in classes.items():
            if ranges := entry.get("range"):
                entry = {
                    **entry,
                    "range": sorted(
                        ((float(bound), icon) for bound, icon in ranges.items()),
                        key=lambda pair: pair[0],
                    ),
                }
            converted[platform][device_class] = entry
    return converted


@dataclass(frozen=True, slots=True)
class _EntityMeta:
    """Core's icon and string tables, as the preview reads them."""

    icons: dict[str, Any]
    strings: dict[str, str]

    def string(
        self, platform: str, device_class: str | None, suffix: str
    ) -> str | None:
        """One ``entity_component`` string, or ``None`` when core has none."""
        if not device_class:
            return None
        return self.strings.get(
            f"component.{platform}.entity_component.{device_class}.{suffix}"
        )


_EMPTY_META: Final = _EntityMeta(icons={}, strings={})


def _reading_icon(
    descriptor: FieldDescriptor, value: Any, meta: _EntityMeta
) -> str | None:
    """The icon Home Assistant would give this field's entity, or ``None``.

    Resolved the way core resolves it, in core's own order of precedence:

    * an ``icon`` on the library descriptor wins outright, because that is this
      repository deliberately overriding the device class;
    * a binary field takes the device class's ``state`` icon for ``on`` when it
      is true, which is what makes an open door and a closed one look different;
    * a numeric field whose device class declares ``range`` takes the band its
      value falls in -- this is why a battery at 1% shows an empty battery and
      one at 90% shows a full one;
    * otherwise the device class's ``default``.

    A field with no device class, or one core does not describe, gets ``None``
    and the panel leaves the space empty rather than inventing a glyph.
    """
    if descriptor.icon:
        return descriptor.icon
    if not descriptor.device_class:
        return None
    entry = meta.icons.get(descriptor.platform, {}).get(descriptor.device_class)
    if not entry:
        return None

    if isinstance(value, bool):
        if value:
            return entry.get("state", {}).get("on") or entry.get("default")
        return entry.get("default")

    if (ranges := entry.get("range")) and isinstance(value, (int, float)):
        chosen = None
        for bound, icon in ranges:
            if value >= bound:
                chosen = icon
        if chosen:
            return chosen
    return entry.get("default")


def _reading_name(descriptor: FieldDescriptor, meta: _EntityMeta) -> str:
    """The entity name Home Assistant would show for this field.

    Mirrors what :class:`~.entity.Rtl433Entity` does with the same descriptor:
    an explicit library ``name`` wins, and a descriptor that deliberately
    leaves it unset is one whose name core derives from the device class.

    That derivation is a *lookup*, not a spelling rule -- core's table says
    ``pm25`` is "PM2.5" and ``aqi`` is "Air quality index", which no amount of
    underscore-replacing produces -- and it is translated, so a German hub
    previews the same word its device page will show.

    A field with neither a name nor a device class core knows is titled after
    its field key. That is this module's choice rather than core's (core would
    fall back to the device's own name), because a row labelled with the key
    the radio sent is more use than a row labelled with the device.
    """
    if descriptor.name is not None:
        return descriptor.name
    if translated := meta.string(descriptor.platform, descriptor.device_class, "name"):
        return translated
    spaced = descriptor.field_key.replace("_", " ").strip()
    return spaced[:1].upper() + spaced[1:]


def _reading_state(
    descriptor: FieldDescriptor, raw: Any, meta: _EntityMeta
) -> tuple[Any, str | None]:
    """The value the entity will hold, and the string its state will read as.

    One function for both because the two are the same question asked twice,
    and splitting them is what let the panel invent its own vocabulary:

    * a **binary** field's state is a *word*, and which word depends on the
      device class -- core calls a ``safety`` sensor Safe/Unsafe and a
      ``moisture`` one Dry/Wet, not On/Off. Rendering "On" for a tamper
      contact previewed something the device page would never say.
    * an **event** field's state is its mapped event type, so ``secret_knock``
      reads "ring" rather than the ``1`` on the wire. The raw value is mapped
      here exactly as :class:`~.event.Rtl433Event` maps it.
    * a **sensor**'s state is ``str()`` of its value, which is why a humidity
      of ``float(99)`` reads "99.0", and the unit is joined to a percentage
      and spaced from everything else -- core's own rule.

    Returning the finished string means the panel prints what it is given and
    holds no formatting rules of its own. Known gap: core converts a handful of
    device classes to the configured unit system (wind speed, rainfall and
    pressure, for this library). That is not applied here, because the
    precision core would then display depends on entity-registry options this
    integration does not set, and a converted preview could disagree about the
    digits where today it disagrees about the unit.
    """
    if descriptor.platform == "event":
        event_map = descriptor.event_map
        event_type = event_map.get(str(raw), str(raw)) if event_map else str(raw)
        return event_type, event_type

    value = apply_transform(descriptor, raw)

    if descriptor.platform == "binary_sensor":
        if not isinstance(value, bool):
            return value, None
        word = meta.string(
            "binary_sensor",
            descriptor.device_class,
            f"state.{'on' if value else 'off'}",
        )
        return value, word or ("On" if value else "Off")

    if value is None:
        return None, None
    shown = str(value)
    unit = descriptor.unit_of_measurement
    if not unit:
        return value, shown
    return value, f"{shown}{unit}" if unit == "%" else f"{shown} {unit}"


@callback
def _entry_registry(hass: HomeAssistant, entry: ConfigEntry) -> Registry | None:
    """This hub's merged library (shipped + its own overrides), or ``None``.

    The same cache the entity platforms read, so a reading previewed here is
    resolved through exactly the descriptor that would create the entity. Absent
    only if the library failed to load, in which case the preview degrades to
    "no readings" rather than to a guess.
    """
    return (
        hass.data.get(DOMAIN, {})
        .get(DATA_ENTRY_LIBRARY, {})
        .get(entry.entry_id, (None, None))[0]
    )


@callback
def _readings(
    fields: dict[str, Any],
    model: str,
    registry: Registry | None,
    meta: _EntityMeta,
) -> list[dict[str, Any]]:
    """Preview a frame as the entities adopting it would create.

    Two filters, both the library's own statements rather than a blocklist this
    module maintains. A field that resolves to no descriptor creates no entity,
    so showing it would promise something adoption will not deliver. A
    descriptor marked ``enabled_by_default: false`` creates an entity that
    arrives disabled, so it is not a reading the user would see either -- which
    is what drops ``time``, ``freq``, ``rssi``, ``snr`` and ``noise`` without
    naming any of them here. The signal figures the card already shows in their
    own right fall out of that second rule for free.

    Ordered the way Home Assistant orders a device page: the readings proper
    first, then the diagnostics, each group alphabetical. Sorting here rather
    than in the panel keeps "what the device page will look like" a property of
    the one module that resolves descriptors. It is also stable -- following the
    frame's own field order would reshuffle a card whenever a device dropped an
    optional field from one transmission.
    """
    if registry is None:
        return []
    readings: list[dict[str, Any]] = []
    for field_key, raw in fields.items():
        descriptor = lookup(field_key, model or None, registry)
        if descriptor is None or not descriptor.enabled_by_default:
            continue
        value, display = _reading_state(descriptor, raw, meta)
        readings.append(
            {
                "key": field_key,
                "name": _reading_name(descriptor, meta),
                "value": value,
                "display": display,
                "unit": descriptor.unit_of_measurement,
                "platform": descriptor.platform,
                "entity_category": descriptor.entity_category,
                "icon": _reading_icon(descriptor, value, meta),
            }
        )
    diagnostic = EntityCategory.DIAGNOSTIC.value
    readings.sort(key=lambda r: (r["entity_category"] == diagnostic, r["name"]))
    return readings


@callback
def _pending_payload(
    hass: HomeAssistant, entry: ConfigEntry, coordinator: Rtl433Coordinator
) -> dict[str, Any]:
    """Render one hub's approval state: the candidates and the ignore list.

    One payload rather than two commands so the panel has a single renderer and a
    single source of truth for a screen that shows both -- and so an un-ignore,
    which changes only the second half, still reaches an open panel through the
    same push.

    Candidates are ordered by
    :meth:`~.coordinator.Rtl433Coordinator.pending_candidates`, which is also
    what the options form renders, so the two surfaces cannot put a different
    device at the top of the same list. Timestamps go out as ISO
    strings because JSON has no datetime and the panel wants to format them in the
    viewer's locale anyway. The ignore list carries a model only when one is known
    -- a device is usually ignored while pending, long before it has a stored
    record -- so the panel falls back to the key.
    """
    stored: dict[str, Any] = entry.data.get(CONF_DEVICES, {})
    registry = _entry_registry(hass, entry)
    meta: _EntityMeta = hass.data.get(DOMAIN, {}).get(DATA_ENTITY_META, _EMPTY_META)
    return {
        "pending": [
            {
                "key": record.key,
                "model": record.model,
                "count": record.count,
                "signal": record.signal,
                "first_seen": record.first_seen.isoformat(),
                "last_seen": record.last_seen.isoformat(),
                "readings": _readings(record.fields, record.model, registry, meta),
            }
            for record in coordinator.pending_candidates()
        ],
        "ignored": [
            {
                "key": device_key,
                "model": stored.get(device_key, {}).get(CONF_MODEL, ""),
            }
            for device_key in sorted(_hub_ignored_devices(entry))
        ],
        # The devices this hub already has, offered as the thing a candidate can
        # replace. Sent with the candidates rather than fetched when the dialog
        # opens so the list cannot be stale against the card beside it: both
        # halves of "which of these is the same hardware?" come from one payload.
        "devices": [
            {"key": device_key, "model": record.get(CONF_MODEL, "")}
            for device_key, record in sorted(stored.items())
        ],
    }


# The parameters every action command takes. Spread into each command's schema
# rather than repeated, so ``entry_id`` and ``device_keys`` cannot come to mean
# something subtly different on one of the three. The ``type`` is added per
# command because ``websocket_command`` reads the command name out of it.
_ACTION_PARAMS: Final[dict[Any, Any]] = {
    vol.Required("entry_id"): str,
    vol.Required("device_keys"): [str],
}


async def _async_run_action(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
    action: Callable[
        [HomeAssistant, ConfigEntry, Rtl433Coordinator, list[str]],
        Awaitable[AdoptionResult],
    ],
) -> None:
    """Resolve the hub, run one :mod:`.adoption` verb, and reply with the result.

    The three action commands differ only in which verb they call: each resolves
    the same ``entry_id``, hands the same ``device_keys`` to its function, and
    replies with the same two lists. Keeping that body in one place is what makes
    "the reply shape is identical across the three" a fact rather than a promise
    -- there is one place to change when it moves.

    Both halves of the reply are always present, even when empty, so a caller
    never has to guard a missing key to find out that everything it asked for
    went through.
    """
    resolved = _async_get_coordinator(hass, connection, msg)
    if resolved is None:
        return
    entry, coordinator = resolved
    result = await action(hass, entry, coordinator, msg["device_keys"])
    connection.send_result(
        msg["id"], {"applied": result.applied, "skipped": result.skipped}
    )


@websocket_api.websocket_command({vol.Required("type"): "rtl_433/hubs"})
@websocket_api.require_admin
@callback
def ws_hubs(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """List this integration's hubs so a caller can pick one to address.

    Every other command needs an ``entry_id``, and a panel opened from the
    sidebar has no way to know one. Hubs that are not loaded are listed too,
    flagged rather than hidden: a user with an unreachable receiver should see it
    named and explained, not silently absent while they wonder where it went.
    """
    connection.send_result(
        msg["id"],
        {
            "hubs": [
                {
                    "entry_id": entry.entry_id,
                    "title": entry.title,
                    "loaded": entry.state is ConfigEntryState.LOADED,
                }
                for entry in hass.config_entries.async_entries(DOMAIN)
            ]
        },
    )


@websocket_api.websocket_command(
    {
        vol.Required("type"): "rtl_433/devices/pending",
        vol.Required("entry_id"): str,
    }
)
@websocket_api.require_admin
@callback
def ws_pending_devices(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Return one hub's pending candidates and its ignore list.

    The one-shot form of what ``rtl_433/devices/subscribe`` pushes, for a caller
    that wants an answer rather than a stream -- a script, a diagnostic, or a
    panel confirming what it just did.
    """
    resolved = _async_get_coordinator(hass, connection, msg)
    if resolved is None:
        return
    entry, coordinator = resolved
    connection.send_result(msg["id"], _pending_payload(hass, entry, coordinator))


@websocket_api.websocket_command(
    {vol.Required("type"): "rtl_433/devices/add", **_ACTION_PARAMS}
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_add_devices(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Adopt the named candidates into Home Assistant.

    Delegates to :func:`.adoption.async_adopt_devices`, so this produces exactly
    the device the options flow produces. The reply distinguishes the keys that
    were adopted from the ones that were no longer pending by the time the click
    arrived, which is the difference the panel has to be able to explain to the
    person who clicked.
    """
    await _async_run_action(hass, connection, msg, async_adopt_devices)


@websocket_api.websocket_command(
    {vol.Required("type"): "rtl_433/devices/ignore", **_ACTION_PARAMS}
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_ignore_devices(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Stop offering the named devices as candidates, for good.

    ``skipped`` here means "already ignored" rather than "could not", so a
    double-click on a row is reported honestly without being an error.
    """
    await _async_run_action(hass, connection, msg, async_ignore_devices)


@websocket_api.websocket_command(
    {
        vol.Required("type"): "rtl_433/devices/clear",
        vol.Required("entry_id"): str,
    }
)
@websocket_api.require_admin
@callback
def ws_clear_devices(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Forget every candidate heard so far, so the list can refill from scratch.

    A receiver left running in a dense neighbourhood accumulates hundreds of
    candidates, and the one the user came to add is somewhere in them. Clearing
    turns the list back into a live question -- trigger the doorbell, and it is
    the only thing on the screen.

    Nothing is persisted and nothing is ignored: the pending list has always
    been memory-only and rebuilt from live traffic, so this discards a working
    set rather than making a decision. Every device cleared comes back on its
    next transmission, which is precisely what makes it safe to offer as a
    one-click button with no confirmation to read.

    A device the user *has* ignored stays ignored -- that list is persisted and
    is a decision, and this is not the control for undoing it.
    """
    resolved = _async_get_coordinator(hass, connection, msg)
    if resolved is None:
        return
    entry, coordinator = resolved
    cleared = len(coordinator.pending)
    coordinator.pending.clear()
    # A membership change, so every open panel is told at once -- including the
    # other admin's, who is looking at a list that just emptied.
    coordinator.emit_pending_update()
    connection.send_result(msg["id"], {"cleared": cleared})


@websocket_api.websocket_command(
    {
        vol.Required("type"): "rtl_433/devices/replace",
        vol.Required("entry_id"): str,
        vol.Required("device_key"): str,
        vol.Required("replaces"): str,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_replace_device(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Re-point an existing device onto a candidate's identity.

    The battery-swap recovery, addressed from the direction the user meets it:
    ``device_key`` is the candidate they are looking at -- the new transmitter id
    the hardware started using -- and ``replaces`` names the device they already
    have, whose history, settings and entity ids should carry across to it. That
    is the inverse of the argument order in
    :func:`~.device_replace.async_replace_device`, which thinks in terms of the
    survivor first, so the mapping is made explicit at the call rather than by
    naming the parameters here to match.

    Not routed through :func:`_async_run_action`: the three list-taking verbs are
    identical to each other and this one is not one of them. It takes two single
    keys, it cannot be batched, and it either happens or fails as a whole.

    A :class:`~.device_replace.DeviceReplaceError` is a user-facing outcome -- a
    panel held open across a reload can easily ask to replace a device that is no
    longer there -- so it comes back as a WebSocket error the panel renders in
    its banner, not as a traceback in the log.
    """
    resolved = _async_get_coordinator(hass, connection, msg)
    if resolved is None:
        return
    entry, _coordinator = resolved
    try:
        await async_replace_device(
            hass, entry, old_key=msg["replaces"], new_key=msg["device_key"]
        )
    except DeviceReplaceError as err:
        connection.send_error(msg["id"], ERR_REPLACE_FAILED, str(err))
        return
    connection.send_result(msg["id"], {"replaced": msg["replaces"]})


@websocket_api.websocket_command(
    {vol.Required("type"): "rtl_433/devices/unignore", **_ACTION_PARAMS}
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_unignore_devices(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Un-ignore the named devices so they are offered again.

    The device does not come back in the same breath: un-ignoring is not
    retroactive and the candidate reappears on its next transmission (see
    :func:`.adoption.async_unignore_devices`). ``applied`` therefore means "taken
    off the ignore list", not "back on the pending list", and a panel should say
    so rather than waiting for a row that is not coming yet.
    """
    await _async_run_action(hass, connection, msg, async_unignore_devices)


@websocket_api.websocket_command(
    {
        vol.Required("type"): "rtl_433/devices/subscribe",
        vol.Required("entry_id"): str,
    }
)
@websocket_api.require_admin
@callback
def ws_subscribe_devices(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Push one hub's approval state whenever it changes.

    The pending list changes continuously by design -- that is what makes a
    config-flow form the wrong shape for it -- so the panel subscribes rather
    than polling, and a device heard while the page is open appears without a
    reload.

    Two triggers, deliberately unequal (see the module docstring). The dispatcher
    signal fires only on a *membership* change and pushes at once. The
    :data:`_REFRESH_INTERVAL` timer covers what fires no signal -- a repeat
    sighting ageing a row's count and last-seen -- and sends only when the
    rendered payload differs from the last one sent, so an idle hub costs nothing
    and a busy one costs at most one message per interval.

    The comparison against ``last_sent`` guards the immediate path too. It is
    cheap, and it means a membership change that happens to leave the rendered
    view identical (ignoring a key that was never a candidate, say) does not
    repaint the panel for no reason.

    Both unsubscribes are stored under ``connection.subscriptions[msg["id"]]`` as
    one callable, so closing the panel -- or the socket dropping -- takes the
    timer down with the listener and never leaves an orphaned interval firing
    against a dead connection.

    The **coordinator is re-resolved on every render**, never captured. A hub
    reload replaces the object in ``hass.data`` while the subscription lives on
    (the config entry, and so the dispatcher signal, survive the reload), so a
    captured coordinator would be a stopped one whose pending map never changes
    again -- the panel would sit on a frozen list for the rest of the session
    without ever reporting an error. Re-reading it means the first render after
    the new coordinator lands shows what the running hub is actually hearing.
    While the entry is mid-reload there is briefly no coordinator at all; that is
    a gap of milliseconds with nothing truthful to say, so the render is skipped
    and the last payload stands until the hub is back.
    """
    resolved = _async_get_coordinator(hass, connection, msg)
    if resolved is None:
        return
    entry, coordinator = resolved

    last_sent: dict[str, Any] = _pending_payload(hass, entry, coordinator)

    @callback
    def _push_if_changed(_now: Any = None) -> None:
        """Re-render and send, unless nothing a subscriber can see has changed.

        Serves both triggers directly. They differ in *when* they fire, not in
        what they do, so the timer's unused tick argument is defaulted away
        rather than absorbed by a second wrapper per trigger.
        """
        nonlocal last_sent
        live: Rtl433Coordinator | None = hass.data.get(DOMAIN, {}).get(entry.entry_id)
        if live is None:
            return
        payload = _pending_payload(hass, entry, live)
        if payload == last_sent:
            return
        last_sent = payload
        connection.send_message(websocket_api.event_message(msg["id"], payload))

    remove_signal = async_dispatcher_connect(
        hass, signal_pending_update(entry.entry_id), _push_if_changed
    )
    remove_timer = async_track_time_interval(
        hass,
        _push_if_changed,
        _REFRESH_INTERVAL,
        name=f"rtl_433 discovery refresh {entry.entry_id}",
    )

    @callback
    def _unsubscribe() -> None:
        """Tear both triggers down together when the subscription ends."""
        remove_signal()
        remove_timer()

    connection.subscriptions[msg["id"]] = _unsubscribe

    # Acknowledge before pushing: the client must see the subscription succeed
    # before the first event arrives under the same id, or it has an event for a
    # subscription it does not yet believe in.
    connection.send_result(msg["id"])
    connection.send_message(websocket_api.event_message(msg["id"], last_sent))


# --------------------------------------------------------------------------- #
# Settings.                                                                    #
#                                                                              #
# The hub's own options, one device's overrides, and the device-library         #
# mapping overrides -- the three forms that used to be reachable only through   #
# the options flow, and are now the panel's dialogs as well. Every rule about   #
# what a submitted value *means* lives in ``settings.py``; these commands are   #
# transport, and deliberately thin enough that reading them tells you nothing   #
# the options flow does not also do.                                           #
# --------------------------------------------------------------------------- #


def _mappings_yaml(entry: ConfigEntry) -> str:
    """Render this hub's mapping overrides as the YAML a user would type.

    The overrides are stored as a plain nested mapping, and YAML is how the
    documentation writes them and how the options flow's editor showed them --
    so the round trip is text out, text in, rather than a JSON object the user
    would have to translate in their head.

    An empty override set renders as the empty string rather than ``{}``, so a
    hub that has never overridden anything opens an empty editor instead of one
    holding a token the user has to delete before typing.
    """
    overrides = entry.data.get(CONF_USER_MAPPINGS) or {}
    if not overrides:
        return ""
    return yaml.safe_dump(overrides, default_flow_style=False, sort_keys=True)


@websocket_api.websocket_command(
    {
        vol.Required("type"): "rtl_433/settings/get",
        vol.Required("entry_id"): str,
    }
)
@websocket_api.require_admin
@callback
def ws_get_settings(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Answer with everything the three settings forms need to render.

    One command rather than three because they are one screenful: the panel
    opens all three dialogs from the same page and would otherwise make three
    round trips to fill controls the user may never look at. The payload is
    small -- a couple of scalars, one row per adopted device, and the override
    document.

    The commodity tables travel with it rather than being hard-coded in the
    panel. Which units Home Assistant will accept for a gas meter is not a fact
    about this panel, it is a fact about :mod:`.calibration`, and a panel that
    carried its own copy would offer a unit the entity build then rejects.
    """
    resolved = _async_get_coordinator(hass, connection, msg)
    if resolved is None:
        return
    entry, _coordinator = resolved

    hub = hub_defaults(entry)
    devices = [
        device_defaults(hass, entry, device_key)
        for device_key in sorted(entry.data.get(CONF_DEVICES, {}))
    ]
    connection.send_result(
        msg["id"],
        {
            "hub": {
                CONF_AVAILABILITY_TIMEOUT: hub[CONF_AVAILABILITY_TIMEOUT],
                CONF_MANAGE_SETTINGS: hub[CONF_MANAGE_SETTINGS],
            },
            "defaults": {
                CONF_AVAILABILITY_TIMEOUT: DEFAULT_AVAILABILITY_TIMEOUT,
                DEVICE_MOTION_CLEAR_DELAY: DEFAULT_MOTION_CLEAR_DELAY,
            },
            "devices": devices,
            "commodities": list(CALIBRATION_COMMODITIES),
            "commodity_units": {
                commodity: list(units) for commodity, units in COMMODITY_UNITS.items()
            },
            "mappings": _mappings_yaml(entry),
            "mappings_docs_url": MAPPINGS_DOCS_URL,
        },
    )


@websocket_api.websocket_command(
    {
        vol.Required("type"): "rtl_433/settings/hub",
        vol.Required("entry_id"): str,
        vol.Required(CONF_AVAILABILITY_TIMEOUT): vol.All(int, vol.Range(min=0)),
        vol.Required(CONF_MANAGE_SETTINGS): bool,
    }
)
@websocket_api.require_admin
@callback
def ws_set_hub_settings(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Persist the hub-level options.

    ``async_update_entry`` is the whole write: it fires the update listener,
    which pushes a changed timeout into the running coordinator live and reloads
    the hub only if the manage-settings toggle moved. Nothing is reloaded here
    for the same reason the config flow does not -- one writer, one listener, one
    reload.
    """
    resolved = _async_get_coordinator(hass, connection, msg)
    if resolved is None:
        return
    entry, _coordinator = resolved
    hass.config_entries.async_update_entry(
        entry,
        options=build_hub_options(
            entry, msg[CONF_AVAILABILITY_TIMEOUT], msg[CONF_MANAGE_SETTINGS]
        ),
    )
    connection.send_result(msg["id"], hub_defaults(entry))


@websocket_api.websocket_command(
    {
        vol.Required("type"): "rtl_433/settings/device",
        vol.Required("entry_id"): str,
        vol.Required("device_key"): str,
        # Each of the three overrides is optional *and* nullable, and the two
        # mean the same thing: clear it. A form that has just emptied a field
        # sends null rather than omitting the key, which is the shape a JSON
        # client naturally produces from an empty input.
        vol.Optional(DEVICE_TIMEOUT_OVERRIDE): vol.Any(
            None, vol.All(int, vol.Range(min=0))
        ),
        vol.Optional(DEVICE_MOTION_CLEAR_DELAY): vol.Any(
            None, vol.All(int, vol.Range(min=1))
        ),
        vol.Optional(CALIBRATION_COMMODITY, default=COMMODITY_NONE): vol.In(
            CALIBRATION_COMMODITIES
        ),
        vol.Optional(CALIBRATION_UNIT): vol.Any(None, str),
        vol.Optional(CALIBRATION_SCALE): vol.Any(None, vol.Coerce(float)),
    }
)
@websocket_api.require_admin
@callback
def ws_set_device_settings(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Persist one device's timeout override, calibration and clear-delay.

    The calibration arrives as its three parts and is put through
    :func:`~.calibration.normalize_calibration`, which is the only thing that
    decides whether they add up to a calibration at all: a commodity of ``none``,
    an unknown one, or a unit that is not convertible for the commodity all come
    back as ``None`` and clear it. The panel therefore cannot store a calibration
    the entity build would refuse to honour, however its controls are wired.

    Data and options are written in a single ``async_update_entry`` -- unlike the
    options flow, which finishes with ``async_create_entry`` for the options half
    -- so one save is one update-listener firing and at most one reload.

    The normalized calibration comes back in the result so the caller can
    re-render from what was actually stored rather than from what it sent; the
    two differ precisely when the user built one that does not add up.
    """
    resolved = _async_get_coordinator(hass, connection, msg)
    if resolved is None:
        return
    entry, _coordinator = resolved

    device_key: str = msg["device_key"]
    if device_key not in entry.data.get(CONF_DEVICES, {}):
        connection.send_error(
            msg["id"],
            websocket_api.const.ERR_NOT_FOUND,
            f"Unknown device {device_key}",
        )
        return

    calibration = normalize_calibration(
        {
            CALIBRATION_COMMODITY: msg.get(CALIBRATION_COMMODITY),
            CALIBRATION_UNIT: msg.get(CALIBRATION_UNIT),
            CALIBRATION_SCALE: msg.get(CALIBRATION_SCALE, 1.0),
        }
    )
    hass.config_entries.async_update_entry(
        entry,
        data=build_device_data(
            entry,
            device_key,
            override=msg.get(DEVICE_TIMEOUT_OVERRIDE),
            calibration=calibration,
        ),
        options=build_device_options(
            entry,
            device_key,
            motion_clear_delay=msg.get(DEVICE_MOTION_CLEAR_DELAY),
        ),
    )
    connection.send_result(msg["id"], device_defaults(hass, entry, device_key))


@websocket_api.websocket_command(
    {
        vol.Required("type"): "rtl_433/settings/mappings",
        vol.Required("entry_id"): str,
        vol.Required("yaml"): str,
    }
)
@websocket_api.require_admin
@callback
def ws_set_mappings(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Validate and store this hub's device-library mapping overrides.

    The document arrives as text and is parsed here rather than in the panel,
    which has no YAML parser and should not grow one. ``yaml.safe_load`` and not
    Home Assistant's loader: this is a string a client submitted, and the safe
    loader is the one with no tag that can reach the filesystem.

    Two failures are reported the same way and both leave the stored overrides
    untouched -- text that is not YAML, and YAML that is not a valid override
    document. They arrive as one error carrying every problem found, because a
    user fixing a mapping file wants the whole list, not the first line of it.
    """
    resolved = _async_get_coordinator(hass, connection, msg)
    if resolved is None:
        return
    entry, _coordinator = resolved

    text: str = msg["yaml"]
    try:
        raw = yaml.safe_load(text) if text.strip() else {}
    except yaml.YAMLError as err:
        connection.send_error(msg["id"], ERR_INVALID_MAPPINGS, str(err))
        return
    # An empty document parses to None, which is "no overrides" rather than a
    # malformed one -- clearing the editor is how a user removes them all.
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        connection.send_error(
            msg["id"],
            ERR_INVALID_MAPPINGS,
            "Device mappings must be a mapping of model to field overrides",
        )
        return

    problems = validate_user_mappings(raw)
    if problems:
        connection.send_error(msg["id"], ERR_INVALID_MAPPINGS, "; ".join(problems))
        return

    hass.config_entries.async_update_entry(entry, data=build_mappings_data(entry, raw))
    connection.send_result(msg["id"], {"mappings": _mappings_yaml(entry)})
