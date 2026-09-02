"""The one implementation of adopting, ignoring, and un-ignoring a device.

Two surfaces put the same three questions to the user — the hub's options flow
(``options_flow.py``, universally available) and the discovery panel's WebSocket
API (``websocket_api.py``, admin-only and dependent on a JS module loading). Two
surfaces must not mean two implementations: adopting from the panel has to
produce byte-for-byte the device that adopting from the options form produces,
or a user who moves between them gets two different integrations. So the *doing*
lives here and both surfaces are thin presentation over it.

Every function takes ``(hass, entry, coordinator, device_keys)`` and returns an
:class:`AdoptionResult` naming what actually happened. The options flow could
afford to drop a key that was no longer actionable — the form closes and the
list re-renders from live state next time. The WebSocket caller cannot: a person
is watching a row they just clicked, and "nothing happened" has to be
distinguishable from "done". Reporting skips rather than silently discarding
them is what lets the panel say which of the two it was.

The split of responsibility these functions preserve is the one the coordinator
already draws: the coordinator's in-memory sets (``adopted`` / ``ignored`` /
``pending``) are what makes a change take effect on a device's *very next
transmission*, and ``entry.data`` is what makes it survive a restart. Both are
written here, in that order, for exactly that reason.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant

from .const import CONF_IGNORED_DEVICES
from .entity import async_upsert_device
from .hub_settings import _hub_ignored_devices

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

    from .coordinator import Rtl433Coordinator


@dataclass(frozen=True, slots=True)
class AdoptionResult:
    """What one adopt / ignore / un-ignore call actually changed.

    ``applied`` holds the keys the call really acted on and ``skipped`` the ones
    it could not, in the order they were requested. The two together always
    account for every key handed in, so a caller can report on a request without
    re-deriving state that may have moved underneath it — the pending list is
    live, and a device can stop being a candidate between a panel render and the
    click on its row.
    """

    applied: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


async def async_adopt_devices(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator: Rtl433Coordinator,
    device_keys: Iterable[str],
) -> AdoptionResult:
    """Adopt each pending device and persist it, reporting what was applied.

    Adoption goes through the coordinator so the device is built by the same
    ``new_device_callback`` seam a live first sighting used -- one registration
    path, not two -- and is then written into ``entry.data[CONF_DEVICES]`` with
    :func:`~.entity.async_upsert_device`, the same idempotent union-write the
    entity platforms use, so an adopted device's record has exactly the shape
    every other write path produces and the device is rebuilt after a restart.

    A key that is no longer pending yields ``None`` from
    :meth:`~.coordinator.Rtl433Coordinator.adopt_device` -- it stopped being a
    candidate between the render and the call, or a second caller already took it
    -- and is reported as skipped rather than storing a record for a device the
    coordinator knows nothing about.

    The one announcement is made here, after the loop, rather than by
    ``adopt_device`` per key: a batch is one user action, and until each device's
    record is written it is only half adopted. Announcing per key would push a
    full list down every open socket once per device -- forty pushes to adopt
    forty candidates, thirty-nine of them immediately superseded.
    """
    result = AdoptionResult()

    for device_key in device_keys:
        record = coordinator.adopt_device(device_key)
        if record is None:
            result.skipped.append(device_key)
            continue
        await async_upsert_device(
            hass,
            entry,
            device_key,
            model=record.model,
            fields=set(record.fields),
        )
        result.applied.append(device_key)

    if result.applied:
        coordinator.emit_pending_update()
    return result


async def async_ignore_devices(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator: Rtl433Coordinator,
    device_keys: Iterable[str],
) -> AdoptionResult:
    """Stop offering each device as a candidate, for good.

    The coordinator's ``ignored`` set is updated key by key first, which is what
    makes the very next transmission drop; ``entry.data[CONF_IGNORED_DEVICES]``
    is what makes that survive a restart. The persisted list is written in a
    single call because it is a single value -- one entry update for a whole
    batch, not one per device.

    A key already on the stored list is reported as skipped: the coordinator is
    still told about it (harmless, and it repairs a mirror that has drifted from
    ``entry.data``), but nothing is persisted a second time and the caller can
    tell the user the device was already ignored. An empty request short-circuits
    before touching the entry at all, so "ignore nothing" never writes.

    An *adopted* key is skipped without being touched at all. Ignoring only ever
    means "stop offering this as a candidate", and an adopted device is not a
    candidate -- the event path checks ``adopted`` first, so adding the key to
    ``ignored`` would change nothing about the device while leaving it listed as
    ignored in a panel that also shows it working, and un-ignoring it would then
    appear to do nothing. Both surfaces only offer Ignore on a pending row, so
    this is reached by a script or by losing the race to another admin's Add;
    either way the honest answer is "skipped", not a persisted contradiction.
    Removing an adopted device is a separate action, from its device page.

    The single announcement comes after the write, for the reason
    :meth:`~.coordinator.Rtl433Coordinator.ignore_device` stays silent: dispatched
    from the loop it would fire once per device, and each of those would render a
    subscriber a half-applied view -- the device gone from the pending list and
    not yet on the ignored one. One dispatch, once both halves are true.
    """
    result = AdoptionResult()
    keys = list(device_keys)
    if not keys:
        return result

    ignored = _hub_ignored_devices(entry)
    for device_key in keys:
        if device_key in coordinator.adopted:
            result.skipped.append(device_key)
            continue
        coordinator.ignore_device(device_key)
        if device_key in ignored:
            result.skipped.append(device_key)
            continue
        ignored.append(device_key)
        result.applied.append(device_key)

    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_IGNORED_DEVICES: ignored}
    )
    coordinator.emit_pending_update()
    return result


async def async_unignore_devices(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator: Rtl433Coordinator,
    device_keys: Iterable[str],
) -> AdoptionResult:
    """Un-ignore devices so they are offered for adding again.

    Un-ignoring is not retroactive: the pending list is in-memory and an ignored
    device was never recorded while it was ignored, so it reappears as a
    candidate on its *next transmission* rather than immediately. For a sensor
    that reports every few minutes that is a short wait; for a door sensor it
    takes a door.

    ``entry.data`` is the source of truth here (the coordinator's ``ignored`` set
    mirrors it), but the coordinator's copy is still discarded from directly:
    that is what un-ignores the device on its next transmission instead of only
    after a reload. A key that is not on the stored list is reported as skipped.

    The pending map's membership does not change here -- but every subscriber's
    view of the *ignored* list just did, and that list is part of what the
    discovery panel renders, so the one announcement is still made. Announcing
    from the service rather than from one surface means an un-ignore from the
    options flow updates an open panel too, which is the whole point of both
    surfaces sharing this module. An empty request short-circuits before touching
    the entry, as in :func:`async_ignore_devices`: the options form's picker
    defaults to selecting nothing, so submitting it unchanged must not write.
    """
    result = AdoptionResult()
    keys = list(device_keys)
    if not keys:
        return result

    ignored = _hub_ignored_devices(entry)
    selected = set(keys)
    for device_key in keys:
        coordinator.ignored.discard(device_key)
        if device_key in ignored:
            result.applied.append(device_key)
        else:
            result.skipped.append(device_key)

    hass.config_entries.async_update_entry(
        entry,
        data={
            **entry.data,
            CONF_IGNORED_DEVICES: [key for key in ignored if key not in selected],
        },
    )
    coordinator.emit_pending_update()
    return result
