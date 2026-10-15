"""Notice when an added device comes back under a new transmitter id.

Many cheap 433 MHz sensors pick a fresh random id whenever their batteries are
pulled. rtl_433 keys a device on that id, so after a battery swap the sensor
turns up as a brand-new *pending* candidate while the device the user added goes
quiet for good. :mod:`.device_replace` already re-keys a device onto a new id
without losing its history; this module decides **when to offer that**, and --
for devices the user has opted in -- does it without asking.

rtl_433 cannot tell a replacement from a new device, so nothing here is a proof.
What makes a battery swap recognisable is that every clue lines up at once, and
:func:`find_id_changes` requires all of them:

- **Same model, same channel and subtype.** Only the ``id`` part of the identity
  changes on a battery swap. A device whose key has no ``id`` (channel-only
  devices) cannot change it this way and is never considered.
- **The old device has gone quiet,** for at least :data:`SILENCE`, and has not
  been heard since the candidate first appeared. A neighbour's sensor of the same
  model shows up *while* yours is still transmitting, which rules it out.
- **The candidate is real:** heard at least :data:`MIN_SIGHTINGS` times, so a
  single bad decode (or the id a sensor showed for a moment while its batteries
  were being seated) never qualifies.
- **The readings carry on.** Where both report a temperature, the candidate's
  is within :data:`TEMPERATURE_TOLERANCE_C` of the old device's last one; where
  both report humidity, within :data:`HUMIDITY_TOLERANCE`. Only slow-moving
  quantities are compared, with absolute tolerances: a percentage breaks down
  near zero and changes with the unit, counters (rain, consumption) often reset
  when the batteries come out, and wind or light move too fast to tell two
  sensors apart. A device reporting none of these is matched on the other clues.
- **Unambiguous in both directions.** Exactly one candidate fits the old device,
  and that candidate fits no other quiet device of the same kind. Two identical
  sensors changing batteries at once is precisely when guessing would be wrong,
  so that case is left alone.

What happens to a match depends on the device's :data:`.const.DEVICE_AUTO_REPLACE`
setting. Off (the default): a fixable repair issue names the device, its old id
and the new one, and confirming it runs the replace. On: the replace runs
straight away -- but only when the candidate first appeared within
:data:`FOLLOW_WINDOW` of the old device's last frame, as it does when batteries
are changed. Silence has no upper bound, so without that limit a sensor that died
months ago would be paired with whichever identical sensor turned up next, a
neighbour's for instance. A longer (or, after a restart, unknown) gap still
raises the repair, so a device re-batteried long after it died is offered, not
taken. Either way, a successful replace fires :data:`EVENT_DEVICE_ID_CHANGED` so
an automation can tell the user.

Evaluation is cheap and purely in memory, so it runs whenever the pending list
changes (a new candidate) and on a short interval (a device's silence crossing
the threshold is not an event of its own).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

from pyrtl_433.naming import safe_token
import voluptuous as vol

from homeassistant.components.repairs import RepairsFlow
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from .const import (
    CONF_DEVICES,
    CONF_MODEL,
    DEVICE_AUTO_REPLACE,
    DOMAIN,
    LOGGER,
    signal_pending_update,
)
from .device_replace import DeviceReplaceError, async_replace_device

if TYPE_CHECKING:
    from .coordinator import Rtl433Coordinator

# How long an added device must have been silent before a candidate may stand in
# for it. Long enough that a sensor between two transmissions (or a receiver
# busy on another band) is not mistaken for a battery swap; short enough that the
# user who has just changed the batteries sees the result while still looking.
SILENCE: Final = timedelta(minutes=10)

# Sightings a candidate needs before it is considered. One is a bad decode as
# often as it is a device; the ids a sensor shows while its batteries are being
# seated are heard once each and never again.
MIN_SIGHTINGS: Final = 2

# How far the candidate's temperature may be from the old device's last reading.
# Generous on purpose: a sensor held in a warm hand for a battery change comes
# back several degrees off, while a neighbour's outdoor sensor is usually much
# further from a fridge probe than this.
TEMPERATURE_TOLERANCE_C: Final = 10.0

# How far the candidate's relative humidity may be from the old device's last
# reading, in percentage points. A hand and a room's air move it quickly, so this
# is loose too; it still separates an indoor sensor from an outdoor one on most
# days.
HUMIDITY_TOLERANCE: Final = 20.0

# Automatic follows only: the longest gap between the old device's last frame and
# the candidate's first. A battery swap takes minutes; an hour covers a swap
# interrupted by a trip to the shops, and anything longer is asked about instead.
FOLLOW_WINDOW: Final = timedelta(hours=1)

# How often the silence side is re-evaluated.
EVALUATE_INTERVAL: Final = timedelta(minutes=1)

# translation_key / issue_id prefix of the "looks like it changed its id" issue.
ISSUE_ID_CHANGED: Final = "device_id_changed"

# Fired on the bus after every successful follow, automatic or confirmed.
EVENT_DEVICE_ID_CHANGED: Final = f"{DOMAIN}_device_id_changed"


@dataclass(frozen=True, slots=True)
class IdChange:
    """One added device and the pending candidate that looks like its successor."""

    old_key: str
    new_key: str
    old_temperature: float | None = None
    new_temperature: float | None = None
    signal: float | None = None
    # Old device's last frame -> candidate's first; ``None`` when the old device
    # has not been heard since the hub connected (a restart), so it is unknown.
    gap: timedelta | None = None

    @property
    def may_follow_automatically(self) -> bool:
        """Whether the gap is short enough to follow without asking."""
        return self.gap is not None and self.gap <= FOLLOW_WINDOW


def _kind(model: str, key: str) -> tuple[str, ...] | None:
    """Return the key's identity with the ``id`` part removed, or ``None``.

    ``device_key`` is ``<model>[-<id>][-ch<channel>][-st<subtype>]``. The first
    token after the model is the id unless it is a channel or subtype token, in
    which case the key has no id -- and a device without an id cannot re-roll one,
    so it is not a candidate for any of this.
    """
    prefix = f"{safe_token(model)}-"
    if not model or not key.startswith(prefix):
        return None
    parts = key[len(prefix) :].split("-")
    if not parts or not parts[0] or parts[0].startswith(("ch", "st")):
        return None
    return (safe_token(model), *parts[1:])


def _number(fields: dict[str, Any] | None, key: str) -> float | None:
    """``fields[key]`` as a float when it is a real number, else ``None``."""
    value = (fields or {}).get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _temperature(fields: dict[str, Any] | None) -> float | None:
    """The device's temperature in degrees C when it reports one, else ``None``."""
    celsius = _number(fields, "temperature_C")
    if celsius is not None:
        return celsius
    fahrenheit = _number(fields, "temperature_F")
    return None if fahrenheit is None else (fahrenheit - 32.0) * 5.0 / 9.0


def _humidity(fields: dict[str, Any] | None) -> float | None:
    """The device's relative humidity in percent when it reports one."""
    return _number(fields, "humidity")


# The slow-moving readings compared between the old device and a candidate, each
# with its absolute tolerance. A pair is rejected when any reading both sides
# report differs by more than its tolerance.
_READINGS: Final = (
    (_temperature, TEMPERATURE_TOLERANCE_C),
    (_humidity, HUMIDITY_TOLERANCE),
)


def _readings_agree(
    old_fields: dict[str, Any] | None, new_fields: dict[str, Any] | None
) -> bool:
    """Whether every reading both devices report is within its tolerance."""
    for read, tolerance in _READINGS:
        old, new = read(old_fields), read(new_fields)
        if old is not None and new is not None and abs(new - old) > tolerance:
            return False
    return True


def find_id_changes(
    entry: ConfigEntry, coordinator: Rtl433Coordinator, now: datetime
) -> list[IdChange]:
    """Return every added device that has an unambiguous successor right now.

    See the module docstring for the rules; each one is a filter below, and a
    pair survives only if it passes all of them.
    """
    records: dict[str, dict[str, Any]] = entry.data.get(CONF_DEVICES, {})
    # When the device has not been heard this session, its silence is counted
    # from the moment the hub connected: nothing older is known.
    connected_since: datetime | None = getattr(coordinator, "_connection_time", None)

    candidates: dict[tuple[str, ...], list[Any]] = {}
    for key, pending in coordinator.pending.items():
        if pending.count < MIN_SIGHTINGS or key in records:
            continue
        kind = _kind(pending.model, key)
        if kind is not None:
            candidates.setdefault(kind, []).append(pending)

    pairs: list[IdChange] = []
    for old_key, record in records.items():
        if old_key not in coordinator.adopted:
            continue
        last_event = coordinator.devices.get(old_key)
        model = record.get(CONF_MODEL) or (last_event.model if last_event else "")
        kind = _kind(model, old_key)
        if kind is None or kind not in candidates:
            continue
        heard = coordinator.last_seen.get(old_key)
        quiet_since = heard or connected_since
        if quiet_since is None or now - quiet_since < SILENCE:
            continue
        old_fields = last_event.fields if last_event else None
        for pending in candidates[kind]:
            if heard is not None and heard >= pending.first_seen:
                # Still transmitting after the candidate appeared: two devices.
                continue
            if not _readings_agree(old_fields, pending.fields):
                continue
            pairs.append(
                IdChange(
                    old_key=old_key,
                    new_key=pending.key,
                    old_temperature=_temperature(old_fields),
                    new_temperature=_temperature(pending.fields),
                    signal=pending.signal,
                    gap=None if heard is None else pending.first_seen - heard,
                )
            )

    # Keep a pair only when neither side has a second option.
    per_old: dict[str, int] = {}
    per_new: dict[str, int] = {}
    for pair in pairs:
        per_old[pair.old_key] = per_old.get(pair.old_key, 0) + 1
        per_new[pair.new_key] = per_new.get(pair.new_key, 0) + 1
    return [
        pair
        for pair in pairs
        if per_old[pair.old_key] == 1 and per_new[pair.new_key] == 1
    ]


def _issue_id(entry: ConfigEntry, old_key: str) -> str:
    """The per-hub, per-device issue id (the device key is HA-safe already)."""
    return f"{ISSUE_ID_CHANGED}_{entry.entry_id}_{old_key}"


def _device_name(hass: HomeAssistant, entry: ConfigEntry, key: str) -> str:
    """The name the user knows the device by, falling back to its key."""
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, f"{entry.entry_id}:{key}"), entry.entry_id
    )
    if device is None:
        return key
    return device.name_by_user or device.name or key


def _duration(gap: timedelta | None) -> str:
    """A gap as the repair card shows it: minutes, then hours, then days."""
    if gap is None:
        return "an unknown time"
    minutes = max(0, round(gap.total_seconds() / 60))
    if minutes < 120:
        return f"{minutes} min"
    hours = round(minutes / 60)
    return f"{hours} h" if hours < 48 else f"{round(hours / 24)} days"


def _placeholders(
    hass: HomeAssistant, entry: ConfigEntry, change: IdChange
) -> dict[str, str]:
    """What the repair card and its confirm step show."""

    def _temp(value: float | None) -> str:
        return "unknown" if value is None else f"{value:.1f} °C"

    return {
        "device": _device_name(hass, entry, change.old_key),
        "old_key": change.old_key,
        "new_key": change.new_key,
        "old_temperature": _temp(change.old_temperature),
        "new_temperature": _temp(change.new_temperature),
        "signal": "unknown" if change.signal is None else f"{change.signal:.1f} dB",
        "gap": _duration(change.gap),
    }


async def async_follow_id_change(
    hass: HomeAssistant,
    entry: ConfigEntry,
    old_key: str,
    new_key: str,
    *,
    automatic: bool,
) -> None:
    """Re-key ``old_key`` onto ``new_key`` and announce it on the bus.

    The device id is read before the replace (the replace keeps the same device
    row, so it is also the id afterwards). :func:`async_replace_device` reloads
    the entry, which tears this module's tracker down and builds a new one.
    """
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, f"{entry.entry_id}:{old_key}"), entry.entry_id
    )
    name = _device_name(hass, entry, old_key)
    await async_replace_device(hass, entry, old_key, new_key)
    LOGGER.info(
        "rtl_433 %s changed its transmitter id (%s -> %s)%s",
        name,
        old_key,
        new_key,
        "; followed automatically" if automatic else "",
    )
    hass.bus.async_fire(
        EVENT_DEVICE_ID_CHANGED,
        {
            "entry_id": entry.entry_id,
            "device_id": device.id if device is not None else None,
            "name": name,
            "old_key": old_key,
            "new_key": new_key,
            "automatic": automatic,
        },
    )


@callback
def async_track_id_changes(
    hass: HomeAssistant, entry: ConfigEntry, coordinator: Rtl433Coordinator
) -> Callable[[], None]:
    """Watch one hub for devices that came back under a new id.

    Returns the unsubscribe, which also withdraws every issue this tracker raised:
    after a reload the pending list is rebuilt from live traffic, so a still-valid
    card is simply raised again once the candidate has been re-heard.
    """
    raised: set[str] = set()
    following: set[str] = set()

    @callback
    def _evaluate(*_: Any) -> None:
        if following:
            return  # a replace (and the reload behind it) is already on its way
        live: set[str] = set()
        for change in find_id_changes(entry, coordinator, dt_util.utcnow()):
            record = entry.data.get(CONF_DEVICES, {}).get(change.old_key, {})
            if record.get(DEVICE_AUTO_REPLACE) and change.may_follow_automatically:
                following.add(change.old_key)
                hass.async_create_task(
                    _async_follow_automatically(change),
                    f"rtl_433 follow id change {change.old_key}",
                )
                return
            issue_id = _issue_id(entry, change.old_key)
            live.add(issue_id)
            ir.async_create_issue(
                hass,
                DOMAIN,
                issue_id,
                is_fixable=True,
                is_persistent=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_ID_CHANGED,
                translation_placeholders=_placeholders(hass, entry, change),
                data={
                    "entry_id": entry.entry_id,
                    "old_key": change.old_key,
                    "new_key": change.new_key,
                },
            )
        for issue_id in raised - live:
            ir.async_delete_issue(hass, DOMAIN, issue_id)
        raised.clear()
        raised.update(live)

    async def _async_follow_automatically(change: IdChange) -> None:
        try:
            await async_follow_id_change(
                hass, entry, change.old_key, change.new_key, automatic=True
            )
        except DeviceReplaceError as err:
            following.discard(change.old_key)
            LOGGER.warning(
                "rtl_433 could not follow %s to %s: %s",
                change.old_key,
                change.new_key,
                err,
            )

    unsubs = [
        async_dispatcher_connect(
            hass, signal_pending_update(entry.entry_id), _evaluate
        ),
        async_track_time_interval(hass, _evaluate, EVALUATE_INTERVAL),
    ]

    @callback
    def _unsubscribe() -> None:
        for unsub in unsubs:
            unsub()
        for issue_id in raised:
            ir.async_delete_issue(hass, DOMAIN, issue_id)
        raised.clear()

    return _unsubscribe


class DeviceIdChangedRepairFlow(RepairsFlow):
    """Confirm that a device came back under a new id, then re-key it."""

    def __init__(self, entry: ConfigEntry, old_key: str, new_key: str) -> None:
        """Remember which hub and which pair the card is about."""
        self._entry = entry
        self._old_key = old_key
        self._new_key = new_key

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> Any:
        """Go straight to the confirmation."""
        return await self.async_step_confirm()

    async def async_step_confirm(self, user_input: dict[str, Any] | None = None) -> Any:
        """Show the pair; on submit, run the replace."""
        if user_input is not None:
            if self._old_key not in self._entry.data.get(CONF_DEVICES, {}):
                return self.async_abort(reason="device_id_change_stale")
            try:
                await async_follow_id_change(
                    self.hass,
                    self._entry,
                    self._old_key,
                    self._new_key,
                    automatic=False,
                )
            except DeviceReplaceError:
                return self.async_abort(reason="device_id_change_failed")
            return self.async_create_entry(data={})

        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({}),
            description_placeholders={
                "device": _device_name(self.hass, self._entry, self._old_key),
                "old_key": self._old_key,
                "new_key": self._new_key,
            },
        )
