"""The one implementation of adopting, ignoring, and un-ignoring a device.

Two surfaces put the same three questions to the user — the location's options
flow (``options_flow.py``, universally available) and the discovery panel's
WebSocket API (``websocket_api.py``, admin-only and dependent on a JS module
loading). Both must create exactly the same device: if the panel and the options
form adopted a device differently, someone who used both would end up with an
inconsistent set of devices. So the actual work lives here, and both surfaces
are thin presentation over it.

**Every verb is scoped to the location, not to a receiver.** A user approves a
*sensor*; which of the location's servers happened to decode it is an accident
of radio range, and a second receiver in range must not queue the same sensor a
second time or ask for the same approval again. So each function takes
``(hass, entry, device_keys)`` — the location entry, no coordinator — and fans
the decision out over every receiver in it: the candidate adopted is the
**merged** one (:func:`~.aggregator.merged_candidate`, built from whichever
receiver heard it last), and the key lands in every receiver's ``adopted`` or
``ignored`` set, including the receivers that have never heard the device and so
had no candidate of their own.

Each returns an :class:`AdoptionResult` naming what actually happened. The
options flow could afford to drop a key that was no longer actionable — the form
closes and the list re-renders from live state next time. The WebSocket caller
cannot: a person is watching a row they just clicked, and "nothing happened" has
to be distinguishable from "done". Reporting skips rather than silently
discarding them is what lets the panel say which of the two it was.

These functions keep the same split the coordinators already use. Their
in-memory sets (``adopted`` / ``ignored`` / ``pending``) are what makes a change
take effect on a device's *very next transmission*; ``entry.data`` is what makes
it survive a restart. Both are written here, in that order, for exactly that
reason.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant

from .aggregator import async_emit_pending_update, location_adopted, merged_candidate
from .const import CONF_IGNORED_DEVICES
from .entity import async_upsert_device
from .receiver_settings import _receiver_ignored_devices, receiver_coordinators

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry


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
    device_keys: Iterable[str],
) -> AdoptionResult:
    """Adopt each pending device into the location, reporting what was applied.

    One approval, every receiver. The record adopted is the location's **merged**
    candidate (:func:`~.aggregator.merged_candidate`), so a sensor two servers
    both hear is added once, from the frame that arrived most recently, carrying
    every field *either* of them has seen it report -- a weather station whose
    wind frame only reached the attic and whose rain frame only reached the
    garage still arrives with all of its entities.

    Adoption then goes through each receiver's coordinator so the device is built
    by the same ``new_device_callback`` seam a live first sighting used -- one
    registration path, not two; the entity platforms share their created-id
    bookkeeping across a location's receivers, so the second receiver's pass
    finds every merged entity already built rather than minting a duplicate. A
    receiver that never heard the device has no candidate to promote and is told
    :meth:`~.coordinator.Rtl433Coordinator.mark_adopted` instead, which is what
    keeps it from re-queueing the device as new the first time it does hear it.

    The device is finally written into ``entry.data[CONF_DEVICES]`` with
    :func:`~.entity.async_upsert_device`, the same idempotent union-write the
    entity platforms use, so an adopted device's record has exactly the shape
    every other write path produces and the device is rebuilt after a restart.

    A key no receiver is offering is reported as skipped rather than storing a
    record for a device nothing knows anything about: it stopped being a
    candidate between the render and the call, or a second caller already took
    it.

    The one announcement is made here, after the loop, rather than per key: a
    batch is one user action, and until each device's record is written it is
    only half adopted. Announcing per key would push a full list down every open
    socket once per device -- forty pushes to adopt forty candidates,
    thirty-nine of them immediately superseded.
    """
    result = AdoptionResult()
    coordinators = receiver_coordinators(hass, entry)

    for device_key in device_keys:
        candidate = merged_candidate(hass, entry, device_key)
        if candidate is None:
            result.skipped.append(device_key)
            continue
        for coordinator in coordinators.values():
            if coordinator.adopt_device(device_key) is None:
                coordinator.mark_adopted(device_key)
        await async_upsert_device(
            hass,
            entry,
            device_key,
            model=candidate.record.model,
            fields=set(candidate.record.fields),
        )
        result.applied.append(device_key)

    if result.applied:
        async_emit_pending_update(hass, entry)
    return result


async def async_ignore_devices(
    hass: HomeAssistant,
    entry: ConfigEntry,
    device_keys: Iterable[str],
) -> AdoptionResult:
    """Stop offering each device as a candidate, for good, across the location.

    Every receiver's ``ignored`` set is updated key by key first, which is what
    makes the very next transmission drop -- from *any* of them, which is the
    whole point: "I do not want my neighbour's sensor" is not a statement about
    which of my servers can hear it, and one receiver still offering the row
    would put it straight back on the merged list.
    ``entry.data[CONF_IGNORED_DEVICES]`` is what makes that survive a restart,
    and it lives on the location for the same reason. The persisted list is
    written in a single call because it is a single value -- one entry update for
    a whole batch, not one per device.

    A key already on the stored list is reported as skipped: the coordinator is
    still told about it (harmless, and it repairs a mirror that has drifted from
    ``entry.data``), but nothing is persisted a second time and the caller can
    tell the user the device was already ignored. An empty request short-circuits
    before touching the entry at all, so "ignore nothing" never writes.

    An *adopted* key -- adopted anywhere in the location -- is skipped without
    being touched at all. Ignoring only ever
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

    coordinators = receiver_coordinators(hass, entry)
    adopted = location_adopted(hass, entry)
    ignored = _receiver_ignored_devices(entry)
    for device_key in keys:
        if device_key in adopted:
            result.skipped.append(device_key)
            continue
        for coordinator in coordinators.values():
            coordinator.ignore_device(device_key)
        if device_key in ignored:
            result.skipped.append(device_key)
            continue
        ignored.append(device_key)
        result.applied.append(device_key)

    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_IGNORED_DEVICES: ignored}
    )
    async_emit_pending_update(hass, entry)
    return result


async def async_unignore_devices(
    hass: HomeAssistant,
    entry: ConfigEntry,
    device_keys: Iterable[str],
) -> AdoptionResult:
    """Un-ignore devices so they are offered for adding again.

    Un-ignoring is not retroactive: the pending list is in-memory and an ignored
    device was never recorded while it was ignored, so it reappears as a
    candidate on its *next transmission* rather than immediately. For a sensor
    that reports every few minutes that is a short wait; for a door sensor it
    takes a door.

    ``entry.data`` is the source of truth here (each coordinator's ``ignored``
    set mirrors it), but every receiver's copy is still discarded from directly:
    that is what un-ignores the device on its next transmission instead of only
    after a reload, and missing one would leave the device hidden from whichever
    receiver hears it best. A key that is not on the stored list is reported as
    skipped.

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

    coordinators = receiver_coordinators(hass, entry)
    ignored = _receiver_ignored_devices(entry)
    selected = set(keys)
    for device_key in keys:
        for coordinator in coordinators.values():
            coordinator.ignored.discard(device_key)
            # No longer ignored, so nothing is left to name: dropping the
            # remembered model keeps the map from growing for the life of the
            # process with devices that are back on the pending list.
            coordinator.ignored_models.pop(device_key, None)
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
    async_emit_pending_update(hass, entry)
    return result
