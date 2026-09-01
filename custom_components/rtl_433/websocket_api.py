"""WebSocket API for the rtl_433 discovery panel.

Six admin-gated commands that expose the pending-device list and the three
things a user can do with it. They are the panel's data source, but they are not
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
from datetime import timedelta
from typing import Any, Final

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.event import async_track_time_interval

from .adoption import (
    AdoptionResult,
    async_adopt_devices,
    async_ignore_devices,
    async_unignore_devices,
)
from .const import CONF_DEVICES, CONF_MODEL, DOMAIN, signal_pending_update
from .coordinator import Rtl433Coordinator
from .hub_settings import _hub_ignored_devices

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


@callback
def async_register_commands(hass: HomeAssistant) -> None:
    """Register the discovery commands.

    Called from every hub's ``async_setup_entry`` because this integration is
    entry-only. Command names are global and registration is really per Home
    Assistant *run*, but it needs no guard of its own:
    ``async_register_command`` is a dict assignment keyed by command name, so a
    second hub -- or the same hub reloading -- rewrites the same six entries with
    the same handlers. Registering is idempotent, so the simplest thing that
    works is to just register.
    """
    websocket_api.async_register_command(hass, ws_hubs)
    websocket_api.async_register_command(hass, ws_pending_devices)
    websocket_api.async_register_command(hass, ws_add_devices)
    websocket_api.async_register_command(hass, ws_ignore_devices)
    websocket_api.async_register_command(hass, ws_unignore_devices)
    websocket_api.async_register_command(hass, ws_subscribe_devices)


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


@callback
def _pending_payload(
    entry: ConfigEntry, coordinator: Rtl433Coordinator
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
    return {
        "pending": [
            {
                "key": record.key,
                "model": record.model,
                "count": record.count,
                "signal": record.signal,
                "first_seen": record.first_seen.isoformat(),
                "last_seen": record.last_seen.isoformat(),
                "fields": dict(record.event.fields),
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
    connection.send_result(msg["id"], _pending_payload(entry, coordinator))


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

    last_sent: dict[str, Any] = _pending_payload(entry, coordinator)

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
        payload = _pending_payload(entry, live)
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
