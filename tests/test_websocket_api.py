"""The discovery WebSocket API, driven the way the panel drives it.

``custom_components/rtl_433/websocket_api.py`` is the only programmatic route to
the approval flow: six admin-gated commands that expose what a receiver has
heard and the three things a user can do about it. The discovery panel is one
caller, a script is another, and neither is exercised by the options-flow tests
in ``tests/test_config_flow.py`` — those drive the *other* surface over the same
shared :mod:`~custom_components.rtl_433.adoption` service.

Everything here runs through a real ``hass_ws_client`` against a real config
entry, because the things worth protecting are integration-shaped:

* the payload contract the panel renders (ordering, ISO timestamps, the SNR /
  RSSI preference, applied-vs-skipped);
* the admin gate and the bad-``entry_id`` answers, one parametrised sweep each
  across the commands rather than a test per command per error;
* the subscription: pushing on a membership change, **not** pushing once per RF
  frame, and going quiet on unsubscribe;
* that adopting over the socket produces the same device as adopting from the
  options form;
* that the panel and its static path survive a second hub entry.

Pending state is always built by feeding real frames through the client's own
normalize + classify seam (``_hear`` below), never by assigning
``coordinator.pending``, so a regression in the routing that fills the list fails
here too instead of being papered over by a hand-built fixture.

Two conventions make the socket assertions deterministic without timeouts:

* :func:`_call` sends a command and returns its reply *plus* every subscription
  event that arrived first — the action commands dispatch their membership
  signal synchronously, before ``send_result``, so the event genuinely precedes
  the reply on the wire and a naive ``receive_json`` would read the wrong one.
* Proving that *nothing* was pushed is done with the same helper against a cheap
  sentinel command (``rtl_433/hubs``): the socket preserves order, so an empty
  event list before the sentinel's reply means nothing was queued behind it.
  That is an exact claim, where waiting on a timeout would only be a guess.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from unittest.mock import patch

from freezegun import freeze_time
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.rtl_433 import (
    PANEL_ELEMENT_NAME,
    PANEL_MODULE_NAME,
    PANEL_URL_BASE,
)
from custom_components.rtl_433.const import (
    CALIBRATION_COMMODITY,
    CALIBRATION_SCALE,
    CALIBRATION_UNIT,
    CONF_AVAILABILITY_TIMEOUT,
    CONF_DEVICES,
    CONF_IGNORED_DEVICES,
    CONF_MANAGE_SETTINGS,
    CONF_MODEL,
    CONF_USER_MAPPINGS,
    DEFAULT_AVAILABILITY_TIMEOUT,
    DEVICE_CALIBRATION,
    DEVICE_FIELDS,
    DEVICE_MOTION_CLEAR_DELAY,
    DEVICE_TIMEOUT_OVERRIDE,
    DOMAIN,
)
from homeassistant.components.frontend import DATA_PANELS
from homeassistant.config_entries import ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util

# The three devices the hub hears in the fixture below, spelled out as the
# normalizer derives them from ``model`` + ``id`` so the assertions read the way
# the panel's table does.
#
# The signal columns are the reason each frame carries what it does: the newest
# device reports *both* ``snr`` and ``rssi`` (so the preference between them is
# observable rather than assumed), the middle one reports only ``rssi`` (the
# fallback), and the oldest reports neither (the ``None`` a receiver started
# without ``-M level`` produces, which must not render as a real 0 dB reading).
_OLD_KEY = "Acurite-606TX-42"
_OLD_FRAME = {"model": "Acurite-606TX", "id": 42, "temperature_C": 21.4, "humidity": 55}

_MID_KEY = "GenericDoor-X1-88"
_MID_FRAME = {
    "model": "GenericDoor-X1",
    "id": 88,
    "closed": 0,
    "battery_ok": 1,
    "rssi": -8.5,
}

_NEW_KEY = "EnergyMeter-2000-1234"
_NEW_FRAME = {
    "model": "EnergyMeter-2000",
    "id": 1234,
    "power_W": 1450.5,
    "snr": 11.5,
    "rssi": -3.0,
}

# A fourth device, heard only by the tests that need a *new* candidate to appear
# while a subscription is open.
_EXTRA_KEY = "Bresser-3CH-7"
_EXTRA_FRAME = {"model": "Bresser-3CH", "id": 7, "temperature_C": 3.5}

_ENTRY_COMMANDS = [
    "rtl_433/devices/pending",
    "rtl_433/devices/add",
    "rtl_433/devices/ignore",
    "rtl_433/devices/unignore",
    "rtl_433/devices/clear",
    "rtl_433/devices/subscribe",
    "rtl_433/settings/get",
    "rtl_433/settings/hub",
    "rtl_433/settings/device",
    "rtl_433/settings/mappings",
]
_ALL_COMMANDS = ["rtl_433/hubs", *_ENTRY_COMMANDS]


def _message(command: str, entry_id: str) -> dict[str, Any]:
    """Build a minimally-valid message for ``command`` addressed at ``entry_id``.

    The parametrised gating and bad-id sweeps need one well-formed message per
    command; building them here keeps those tests about the *answer* rather than
    about each command's schema. ``rtl_433/hubs`` takes no entry at all, and the
    three action commands additionally require ``device_keys``.
    """
    if command == "rtl_433/hubs":
        return {"type": command}
    message: dict[str, Any] = {"type": command, "entry_id": entry_id}
    verb = command.rsplit("/", 1)[-1]
    if verb in ("add", "ignore", "unignore"):
        message["device_keys"] = [_NEW_KEY]
    elif command == "rtl_433/settings/hub":
        message[CONF_AVAILABILITY_TIMEOUT] = 900
        message[CONF_MANAGE_SETTINGS] = True
    elif command == "rtl_433/settings/device":
        message["device_key"] = _NEW_KEY
    elif command == "rtl_433/settings/mappings":
        message["yaml"] = ""
    return message


def _coordinator(hass, entry):
    """Return the running coordinator for a loaded hub entry."""
    return hass.data[DOMAIN][entry.entry_id]


def _hear(coordinator, frame: dict[str, Any]) -> None:
    """Inject one live frame through the client's normalize + classify seam.

    Drives ``_process_event`` -> ``_on_client_event``, the exact path an incoming
    WebSocket frame takes, so these tests exercise the routing code that actually
    builds the pending list. The frames carry no ``time`` on purpose: a frame
    with no usable timestamp classifies as a live transmission, while a
    timestamped frame older than the connect anchor is a reconnect replay and
    deliberately never becomes a candidate.
    """
    coordinator._client._process_event(frame)


def _registry_device(hass, entry, device_key):
    """Return the registry device for a device key, or ``None``."""
    return dr.async_get(hass).async_get_device(
        identifiers={(DOMAIN, f"{entry.entry_id}:{device_key}")}
    )


def _device_entity_unique_ids(hass, entry, device_key) -> set[str]:
    """Return the unique_ids of every registry entity belonging to a device."""
    prefix = f"{entry.entry_id}:{device_key}:"
    return {
        registry_entry.unique_id
        for registry_entry in er.async_get(hass).entities.values()
        if registry_entry.unique_id.startswith(prefix)
    }


async def _call(client, message: dict[str, Any]):
    """Send one command and return ``(reply, events_that_arrived_first)``.

    The action commands dispatch their membership signal *inside* the handler,
    before ``connection.send_result``, so an open subscription's push is already
    on the wire ahead of the reply. Reading with a bare ``receive_json`` would
    therefore hand a caller the event where it expected the result — intermittently,
    and only when a subscription happens to be open. Draining to the first
    non-event message makes that ordering explicit instead of a hazard.

    It doubles as the proof that nothing was pushed: the socket preserves order,
    so an empty event list ahead of a sentinel command's reply means the queue
    behind it was empty — an exact statement where a timeout would be a guess.
    """
    await client.send_json_auto_id(message)
    events: list[dict[str, Any]] = []
    while True:
        received = await client.receive_json()
        if received["type"] == "event":
            events.append(received)
            continue
        return received, events


async def _setup_hub(hass, hub_entry_builder, **kwargs):
    """Add and set up one hub entry, returning it loaded."""
    entry = hub_entry_builder(availability_timeout=600, **kwargs)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


@pytest.fixture
async def hub(hass, hub_entry_builder, no_socket):
    """A loaded hub that has heard three devices at four distinct instants.

    The sightings are frozen a minute apart so "most recently heard first" is a
    real ordering rather than an artefact of insertion order, and the newest
    device is heard twice — a minute apart — so its sighting count is
    distinguishable from the others' and its ``first_seen`` is provably not its
    ``last_seen``.
    """
    entry = await _setup_hub(hass, hub_entry_builder)
    coordinator = _coordinator(hass, entry)
    start = dt_util.utcnow()
    with freeze_time(start):
        _hear(coordinator, _OLD_FRAME)
    with freeze_time(start + timedelta(minutes=1)):
        _hear(coordinator, _MID_FRAME)
    with freeze_time(start + timedelta(minutes=2)):
        _hear(coordinator, _NEW_FRAME)
    with freeze_time(start + timedelta(minutes=3)):
        _hear(coordinator, _NEW_FRAME)
    await hass.async_block_till_done()
    return entry


# --------------------------------------------------------------------------- #
# The payload the panel renders.                                              #
# --------------------------------------------------------------------------- #
async def test_pending_returns_the_columns_the_panel_renders(hass, hub, hass_ws_client):
    """One command, the whole table: order, counts, signal, ISO times, values.

    Every field asserted here is one the panel puts on screen and a user judges a
    device by, so a silent change to any of them is a silently wrong table. The
    ordering is newest-first because a long list is worked from the top and the
    device the user just triggered is the one they came here for. The three
    signal readings cover the whole of ``_signal_level``: SNR wins over an RSSI
    in the same frame (it is the figure that tracks decodability), RSSI is the
    fallback, and a receiver reporting neither yields ``null`` rather than a zero
    the panel would render as a real — and terrible — reading.

    Timestamps go out as ISO strings because JSON has no datetime; parsing them
    back is what proves they are actually parseable rather than a ``repr``.
    """
    client = await hass_ws_client(hass)

    reply, _ = await _call(
        client, {"type": "rtl_433/devices/pending", "entry_id": hub.entry_id}
    )

    assert reply["success"]
    result = reply["result"]
    assert [row["key"] for row in result["pending"]] == [_NEW_KEY, _MID_KEY, _OLD_KEY]
    # Nothing has been ignored yet, and the key is present rather than absent so
    # a caller never has to guard for it.
    assert result["ignored"] == []

    rows = {row["key"]: row for row in result["pending"]}

    newest = rows[_NEW_KEY]
    assert newest["model"] == "EnergyMeter-2000"
    assert newest["count"] == 2
    assert newest["signal"] == 11.5  # snr, not the rssi in the same frame
    # Readings are the frame previewed as the entities adoption would create:
    # named the way Home Assistant will name them rather than the way the radio
    # sent them, and carrying the unit the entity would show.
    readings = {reading["key"]: reading for reading in newest["readings"]}
    assert readings["power_W"]["value"] == 1450.5
    assert readings["power_W"]["name"] == "Power"
    assert readings["power_W"]["unit"] == "W"
    # The frame metadata the card shows in its own right (or not at all) maps to
    # no descriptor, so it never reaches the readings list.
    assert "snr" not in readings
    assert "rssi" not in readings
    first_seen = dt_util.parse_datetime(newest["first_seen"])
    last_seen = dt_util.parse_datetime(newest["last_seen"])
    assert first_seen is not None and last_seen is not None
    assert last_seen - first_seen == timedelta(minutes=1)

    assert rows[_MID_KEY]["signal"] == -8.5  # rssi, the fallback
    assert rows[_OLD_KEY]["signal"] is None  # neither reported: no reading at all
    assert rows[_OLD_KEY]["count"] == 1


async def test_pending_readings_preview_the_entities_adoption_would_create(
    hass, hub, hass_ws_client
):
    """A candidate's readings are named and valued the way its entities will be.

    This is the whole point of previewing a frame rather than dumping it: the
    user is deciding whether the thing on the patio is the thing in the list,
    and "Temperature 21.4 °C" answers that where ``temperature_C: 21.4`` makes
    them translate. So the name comes from the library descriptor exactly as
    :class:`~.entity.Rtl433Entity` takes it -- here via the device-class
    fallback, since none of these fields carries an explicit ``name`` -- and the
    unit comes with it.

    Binary fields stay real booleans over the wire. The panel owns the on/off
    vocabulary; sending it a rendered string would put half the presentation in
    Python and half in JavaScript.

    The two exclusions are the load-bearing part. ``snr`` and ``rssi`` *do* have
    descriptors, so "has a descriptor" alone would show them -- they are dropped
    because the library marks them ``enabled_by_default: false``, which is its
    own statement that adoption creates them disabled and the user will not see
    them. An unmapped field creates no entity at all and is dropped for the
    simpler reason.
    """
    client = await hass_ws_client(hass)

    reply, _ = await _call(
        client, {"type": "rtl_433/devices/pending", "entry_id": hub.entry_id}
    )
    rows = {row["key"]: row for row in reply["result"]["pending"]}

    old = {reading["key"]: reading for reading in rows[_OLD_KEY]["readings"]}
    assert old["temperature_C"]["name"] == "Temperature"
    assert old["temperature_C"]["value"] == 21.4
    assert old["temperature_C"]["unit"] == "°C"
    assert old["humidity"]["name"] == "Humidity"
    assert old["humidity"]["unit"] == "%"

    # ``display`` is the finished state string, unit and all, so the panel
    # prints it rather than re-deriving it: the humidity transform floats an
    # integer, and a device page shows that as "55.0%" -- JavaScript rendering
    # the same value would say "55" and drop the unit's spacing rule.
    assert old["humidity"]["value"] == 55.0
    assert old["humidity"]["display"] == "55.0%"
    # A percentage joins its unit; everything else is spaced from it.
    assert old["temperature_C"]["display"] == "21.4 °C"

    # The icon is Home Assistant's own, resolved from the device class through
    # core's ``icons.json`` rather than from a table maintained here.
    assert old["temperature_C"]["icon"] == "mdi:thermometer"
    assert old["humidity"]["icon"] == "mdi:water-percent"

    mid = {reading["key"]: reading for reading in rows[_MID_KEY]["readings"]}
    # ``closed: 0`` is a binary field: it arrives as a bool, not as the 0 the
    # radio sent nor as a string this module chose to render.
    assert mid["closed"]["platform"] == "binary_sensor"
    assert isinstance(mid["closed"]["value"], bool)
    # A binary state is a *word*, and which word is core's to say: an
    # ``opening`` sensor reads Open/Closed, never On/Off. The library maps this
    # field ``payload: {on: "0"}``, so the ``closed: 0`` in the frame is an open
    # contact and the entity reads "Open" -- exactly the pair of translations a
    # hand-written On/Off could not have produced.
    assert mid["closed"]["display"] == "Open"
    # A binary device class has one icon per state, which is what makes an open
    # door look different from a shut one.
    assert mid["closed"]["icon"] in ("mdi:square", "mdi:square-outline")
    # Disabled by default in the library, so never previewed -- even though both
    # resolve to a descriptor and one of them is in this very frame.
    assert "rssi" not in mid
    assert "snr" not in mid


async def test_binary_readings_use_the_device_class_vocabulary(
    hass, hub, hass_ws_client
):
    """Safety reads Safe/Unsafe and moisture Dry/Wet, not On/Off.

    Home Assistant names a binary entity's states from its device class, and
    this library leans on that: the leak detector is ``moisture`` and the tamper
    contact is ``safety``. A card that renders every binary field as On/Off
    previews a word the device page will never show, which is the one thing the
    preview exists not to do.
    """
    _hear(
        _coordinator(hass, hub),
        {"model": "Wet-1", "id": 9, "detect_wet": 1, "tamper": 0},
    )
    client = await hass_ws_client(hass)

    reply, _ = await _call(
        client, {"type": "rtl_433/devices/pending", "entry_id": hub.entry_id}
    )
    rows = {row["key"]: row for row in reply["result"]["pending"]}
    readings = {r["key"]: r for r in rows["Wet-1-9"]["readings"]}

    assert readings["detect_wet"]["display"] == "Wet"
    assert readings["tamper"]["display"] == "Safe"


async def test_an_event_field_previews_its_mapped_event_type(hass, hub, hass_ws_client):
    """A doorbell previews the event type its entity will fire, not the raw code.

    An ``event`` descriptor does not become a sensor: its entity's state is the
    mapped event type from the library's ``event_map``. Treating it like a
    numeric field showed the digit off the wire -- a value the created entity
    never holds.
    """
    _hear(
        _coordinator(hass, hub),
        {"model": "Honeywell-Doorbell", "id": 77, "secret_knock": 0},
    )
    client = await hass_ws_client(hass)

    reply, _ = await _call(
        client, {"type": "rtl_433/devices/pending", "entry_id": hub.entry_id}
    )
    rows = {row["key"]: row for row in reply["result"]["pending"]}
    readings = {r["key"]: r for r in rows["Honeywell-Doorbell-77"]["readings"]}

    knock = readings["secret_knock"]
    assert knock["platform"] == "event"
    # The library maps this field's ``0`` to a named event type, and that name
    # is the entity's state -- not the digit that arrived on the wire.
    assert knock["display"] == "ring"
    assert knock["value"] == "ring"


async def test_a_device_class_name_comes_from_core_not_from_spelling(
    hass, hub, hass_ws_client
):
    """Names are looked up in core's table, not derived from the class string.

    ``pm25`` is "PM2.5" to Home Assistant; no underscore-replacing rule produces
    that. Pinning one such class keeps the lookup from quietly regressing to a
    spelling heuristic, which would also silently drop translation.
    """
    _hear(
        _coordinator(hass, hub),
        {"model": "Air-1", "id": 4, "pm2_5_ug_m3": 12.0, "temperature_C": 18.0},
    )
    client = await hass_ws_client(hass)

    reply, _ = await _call(
        client, {"type": "rtl_433/devices/pending", "entry_id": hub.entry_id}
    )
    rows = {row["key"]: row for row in reply["result"]["pending"]}
    readings = {r["key"]: r for r in rows["Air-1-4"]["readings"]}

    # Temperature carries no library ``name``, so this is core's table talking.
    assert readings["temperature_C"]["name"] == "Temperature"


async def test_readings_are_ordered_like_a_device_page(hass, hub, hass_ws_client):
    """Readings first, diagnostics last, alphabetical within each group.

    A device page puts the readings a user came for above the diagnostics, and
    the card is meant to be recognisable as that page. The ordering is settled
    in the payload rather than in the panel so the rule lives with the module
    that knows each field's entity category.

    ``battery_ok`` is the case worth pinning: it is a genuine reading a user
    judges a candidate by, but Home Assistant files it under diagnostics, so it
    must sort *after* a humidity that is alphabetically later than it.
    """
    _hear(
        _coordinator(hass, hub),
        {
            "model": "Ordered-1",
            "id": 3,
            "temperature_C": 20.0,
            "battery_ok": 1,
            "humidity": 40,
        },
    )
    client = await hass_ws_client(hass)

    reply, _ = await _call(
        client, {"type": "rtl_433/devices/pending", "entry_id": hub.entry_id}
    )
    rows = {row["key"]: row for row in reply["result"]["pending"]}
    names = [reading["name"] for reading in rows["Ordered-1-3"]["readings"]]

    assert names == ["Humidity", "Temperature", "Battery"]


async def test_a_field_with_no_library_mapping_is_not_previewed(
    hass, hub, hass_ws_client
):
    """An unmapped field creates no entity, so the card must not promise one.

    A bad decode is exactly how an unrecognised field key turns up, and it is
    also one of the things the pending list exists to help a user spot. Showing
    it among the readings would suggest adoption produces an entity for it,
    which it does not.
    """
    _hear(
        _coordinator(hass, hub),
        {"model": "Oddball-1", "id": 5, "temperature_C": 9.0, "not_a_real_field": 7},
    )
    client = await hass_ws_client(hass)

    reply, _ = await _call(
        client, {"type": "rtl_433/devices/pending", "entry_id": hub.entry_id}
    )
    rows = {row["key"]: row for row in reply["result"]["pending"]}
    readings = {reading["key"]: reading for reading in rows["Oddball-1-5"]["readings"]}

    assert "temperature_C" in readings
    assert "not_a_real_field" not in readings


async def test_replace_repoints_an_existing_device_onto_a_candidate(
    hass, hub, hass_ws_client
):
    """The battery-swap recovery, driven from the candidate the user is looking at.

    ``device_key`` is the new transmitter id heard on the air and ``replaces``
    is the device already in Home Assistant whose history should survive, which
    is the inverse of :func:`async_replace_device`'s own argument order -- so
    this pins the mapping, not just the outcome. A silent swap of the two would
    re-key the wrong device and strand exactly the history the feature exists to
    keep.
    """
    client = await hass_ws_client(hass)
    await _call(
        client,
        {
            "type": "rtl_433/devices/add",
            "entry_id": hub.entry_id,
            "device_keys": [_OLD_KEY],
        },
    )
    await hass.async_block_till_done()

    reply, _ = await _call(
        client,
        {
            "type": "rtl_433/devices/replace",
            "entry_id": hub.entry_id,
            "device_key": _NEW_KEY,
            "replaces": _OLD_KEY,
        },
    )

    assert reply["success"]
    assert reply["result"] == {"replaced": _OLD_KEY}
    # The survivor now lives under the candidate's key, and the old one is gone.
    stored = hub.data[CONF_DEVICES]
    assert _NEW_KEY in stored
    assert _OLD_KEY not in stored


async def test_replace_reports_a_refused_request_as_an_error(hass, hub, hass_ws_client):
    """A replace the helper refuses comes back as an error, not a traceback.

    A panel is held open across reloads and adoptions, so asking to replace a
    device that is no longer there is an ordinary stale-UI outcome. It carries
    its own error code so a caller can tell it from "the hub is not loaded",
    which is retryable and this is not.
    """
    client = await hass_ws_client(hass)

    reply, _ = await _call(
        client,
        {
            "type": "rtl_433/devices/replace",
            "entry_id": hub.entry_id,
            "device_key": _NEW_KEY,
            "replaces": "Nothing-Like-This-99",
        },
    )

    assert not reply["success"]
    assert reply["error"]["code"] == "replace_failed"


async def test_the_payload_lists_the_devices_a_candidate_could_replace(
    hass, hub, hass_ws_client
):
    """The hub's own devices ride along with the candidates.

    The replace dialog is built from this list, and it travels in the same
    payload as the cards so the two halves of "which of these is the same
    hardware?" can never disagree about what the hub has.
    """
    client = await hass_ws_client(hass)
    await _call(
        client,
        {
            "type": "rtl_433/devices/add",
            "entry_id": hub.entry_id,
            "device_keys": [_OLD_KEY],
        },
    )
    await hass.async_block_till_done()

    reply, _ = await _call(
        client, {"type": "rtl_433/devices/pending", "entry_id": hub.entry_id}
    )

    devices = {row["key"]: row for row in reply["result"]["devices"]}
    assert _OLD_KEY in devices
    assert devices[_OLD_KEY]["model"] == "Acurite-606TX"
    # An adopted device is no longer a candidate, so it appears in exactly one
    # of the two lists.
    assert _OLD_KEY not in {row["key"] for row in reply["result"]["pending"]}


# --------------------------------------------------------------------------- #
# The three actions.                                                          #
# --------------------------------------------------------------------------- #
async def test_add_creates_only_what_was_asked_for_and_reports_the_rest_skipped(
    hass, hub, hass_ws_client
):
    """Adding over the socket builds the device; a stale key is skipped, not an error.

    The pending list is live, so a panel row can be gone by the time the click on
    it arrives — adopted by another admin, or dropped by a reload. That is a
    normal condition the panel has to explain to the person who clicked, so it
    comes back in ``skipped`` rather than as a WebSocket error that would fail the
    whole batch and lose the keys that were still good.

    The rest asserts the add really went all the way: a persisted record in the
    same shape every other write path produces (so a restart rebuilds it),
    entities in the registry seeded from the frame already heard, and the two
    devices that were not named left strictly alone.
    """
    client = await hass_ws_client(hass)

    reply, _ = await _call(
        client,
        {
            "type": "rtl_433/devices/add",
            "entry_id": hub.entry_id,
            "device_keys": [_NEW_KEY, "Ghost-Device-1"],
        },
    )
    await hass.async_block_till_done()

    assert reply["success"]
    assert reply["result"] == {"applied": [_NEW_KEY], "skipped": ["Ghost-Device-1"]}

    assert set(hub.data[CONF_DEVICES]) == {_NEW_KEY}
    record = hub.data[CONF_DEVICES][_NEW_KEY]
    assert record[CONF_MODEL] == "EnergyMeter-2000"
    assert "power_W" in record[DEVICE_FIELDS]

    assert _registry_device(hass, hub, _NEW_KEY) is not None
    assert _device_entity_unique_ids(hass, hub, _NEW_KEY)

    coordinator = _coordinator(hass, hub)
    assert _NEW_KEY in coordinator.adopted
    assert set(coordinator.pending) == {_MID_KEY, _OLD_KEY}
    for key in (_MID_KEY, _OLD_KEY):
        assert _registry_device(hass, hub, key) is None


async def test_an_ignored_device_is_still_named_by_its_model(hass, hub, hass_ws_client):
    """The ignore list names the device, before and after it transmits again.

    The persisted list is bare keys, and an ignored device has no stored record
    and no entry in the coordinator's ``devices`` map -- its frames are dropped
    before they get there. So without the coordinator remembering what it last
    decoded, the only honest label is "Unknown model" over a key that visibly
    contains the model, which is what the panel showed.

    Both halves are checked because they cover different moments: the model is
    seeded at the instant of ignoring (the user is looking straight at the list),
    and re-seeded by every later transmission (so a neighbour's sensor names
    itself again after a restart, which is the only way a pre-existing ignore
    ever gets a model at all).
    """
    client = await hass_ws_client(hass)
    coordinator = _coordinator(hass, hub)

    reply, _ = await _call(
        client,
        {
            "type": "rtl_433/devices/ignore",
            "entry_id": hub.entry_id,
            "device_keys": [_NEW_KEY],
        },
    )
    assert reply["success"] is True

    reply, _ = await _call(client, _message("rtl_433/devices/pending", hub.entry_id))
    (ignored,) = reply["result"]["ignored"]
    assert ignored["key"] == _NEW_KEY
    assert ignored["model"] == _NEW_FRAME["model"]

    # Now forget it the way a restart would, and let the device transmit again.
    coordinator.ignored_models.clear()
    _hear(coordinator, _NEW_FRAME)
    await hass.async_block_till_done()

    reply, _ = await _call(client, _message("rtl_433/devices/pending", hub.entry_id))
    (ignored,) = reply["result"]["ignored"]
    assert ignored["model"] == _NEW_FRAME["model"]
    # ...and it is still ignored: naming it is not un-ignoring it.
    assert not reply["result"]["pending"] or all(
        row["key"] != _NEW_KEY for row in reply["result"]["pending"]
    )


async def test_ignore_and_unignore_round_trip_through_the_entry_and_coordinator(
    hass, hub, hass_ws_client
):
    """Ignoring and un-ignoring reach both stores, and take effect immediately.

    ``entry.data[CONF_IGNORED_DEVICES]`` is what survives a restart; the
    coordinator's ``ignored`` set is what the device's *very next* frame is routed
    against. A command that wrote only one of them would either forget the choice
    on restart or make the user wait for a reload, so the device's next
    transmission is fed after each step — that is the only thing that
    distinguishes the two.

    Two contract details ride along. Re-ignoring an already-ignored key is
    ``skipped``, not an error: a double-click on a panel row is reported honestly
    rather than failing. And un-ignoring is deliberately **not** retroactive — the
    pending list is in-memory and the device was never recorded while ignored, so
    ``applied`` means "taken off the list", and the candidate only reappears on
    its next transmission.
    """
    client = await hass_ws_client(hass)
    coordinator = _coordinator(hass, hub)

    reply, _ = await _call(
        client,
        {
            "type": "rtl_433/devices/ignore",
            "entry_id": hub.entry_id,
            "device_keys": [_MID_KEY],
        },
    )
    assert reply["result"] == {"applied": [_MID_KEY], "skipped": []}
    assert hub.data[CONF_IGNORED_DEVICES] == [_MID_KEY]
    assert coordinator.ignored == {_MID_KEY}
    assert _MID_KEY not in coordinator.pending

    # The next transmission is dropped rather than re-listing the candidate.
    _hear(coordinator, _MID_FRAME)
    await hass.async_block_till_done()
    assert _MID_KEY not in coordinator.pending

    # Ignoring it again changes nothing and says so.
    reply, _ = await _call(
        client,
        {
            "type": "rtl_433/devices/ignore",
            "entry_id": hub.entry_id,
            "device_keys": [_MID_KEY],
        },
    )
    assert reply["result"] == {"applied": [], "skipped": [_MID_KEY]}
    assert hub.data[CONF_IGNORED_DEVICES] == [_MID_KEY]

    # The pending payload carries the ignore list, which is how the panel renders
    # its second view. No model is stored for a device ignored while pending, so
    # the panel falls back to the key.
    reply, _ = await _call(
        client, {"type": "rtl_433/devices/pending", "entry_id": hub.entry_id}
    )
    # Named, not just keyed: the coordinator remembers what it was told to
    # ignore, so the list can say what the user is looking at.
    assert reply["result"]["ignored"] == [
        {"key": _MID_KEY, "model": _MID_FRAME["model"]}
    ]
    assert _MID_KEY not in {row["key"] for row in reply["result"]["pending"]}

    # Un-ignoring clears both stores; a key that was never ignored is skipped.
    reply, _ = await _call(
        client,
        {
            "type": "rtl_433/devices/unignore",
            "entry_id": hub.entry_id,
            "device_keys": [_MID_KEY, "Never-Ignored-1"],
        },
    )
    assert reply["result"] == {"applied": [_MID_KEY], "skipped": ["Never-Ignored-1"]}
    assert hub.data[CONF_IGNORED_DEVICES] == []
    assert coordinator.ignored == set()
    # Not retroactive: still absent until the device transmits again.
    assert _MID_KEY not in coordinator.pending

    _hear(coordinator, _MID_FRAME)
    await hass.async_block_till_done()
    assert _MID_KEY in coordinator.pending


async def test_clear_empties_the_list_without_undoing_any_decision(
    hass, hub, hass_ws_client
):
    """Clearing forgets candidates, keeps ignores, and refills from live traffic.

    The button exists because a receiver in a dense neighbourhood ends up with
    hundreds of candidates and the one the user came to add is lost among them.
    Clearing turns the list back into a live question, so the three things worth
    pinning down are: the count reported back matches what was on screen, an open
    panel is repainted at once, and nothing the user actually *decided* is undone
    -- an ignored device stays ignored, because that list is persisted and this is
    not the control for changing it.

    The last step is what makes clearing safe to offer with no confirmation: the
    pending list has always been memory-only, so a cleared device comes straight
    back on its next transmission.
    """
    client = await hass_ws_client(hass)
    coordinator = _coordinator(hass, hub)

    # Ignore one candidate first, so the clear has a decision to leave alone.
    reply, _ = await _call(
        client,
        {
            "type": "rtl_433/devices/ignore",
            "entry_id": hub.entry_id,
            "device_keys": [_MID_KEY],
        },
    )
    assert reply["success"]

    await client.send_json_auto_id(
        {"type": "rtl_433/devices/subscribe", "entry_id": hub.entry_id}
    )
    ack = await client.receive_json()
    assert ack["success"]
    snapshot = await client.receive_json()
    listed = [row["key"] for row in snapshot["event"]["pending"]]
    assert listed == [_NEW_KEY, _OLD_KEY]

    reply, events = await _call(
        client, {"type": "rtl_433/devices/clear", "entry_id": hub.entry_id}
    )
    # The count is what the user was looking at, so the panel can say "cleared 2".
    assert reply["result"] == {"cleared": len(listed)}
    assert coordinator.pending == {}
    assert events, "clearing the list must repaint an open panel"
    assert events[-1]["event"]["pending"] == []

    # The ignore list is a persisted decision and is left exactly as it was.
    assert [row["key"] for row in events[-1]["event"]["ignored"]] == [_MID_KEY]
    assert hub.data[CONF_IGNORED_DEVICES] == [_MID_KEY]
    assert coordinator.ignored == {_MID_KEY}

    # Nothing was persisted, so the next transmission re-lists the candidate --
    # while the ignored one stays dropped.
    _hear(coordinator, _NEW_FRAME)
    _hear(coordinator, _MID_FRAME)
    await hass.async_block_till_done()
    assert _NEW_KEY in coordinator.pending
    assert _MID_KEY not in coordinator.pending

    # Clearing an already-empty list is a no-op that reports it honestly.
    coordinator.pending.clear()
    reply, _ = await _call(
        client, {"type": "rtl_433/devices/clear", "entry_id": hub.entry_id}
    )
    assert reply["result"] == {"cleared": 0}


async def test_hubs_lists_every_configured_receiver_loaded_or_not(
    hass, hub, hub_entry_builder, hass_ws_client
):
    """A panel opened from the sidebar has to be able to name a hub to address.

    Every other command needs an ``entry_id`` the panel cannot invent, so this is
    the entry point. An unloaded hub is listed and flagged rather than hidden: a
    user with an unreachable receiver should see it named and explained instead of
    silently absent while they wonder where it went.
    """
    unloaded = hub_entry_builder(host="unreachable.local")
    unloaded.add_to_hass(hass)  # deliberately never set up

    client = await hass_ws_client(hass)
    reply, _ = await _call(client, {"type": "rtl_433/hubs"})

    assert reply["success"]
    by_id = {entry["entry_id"]: entry for entry in reply["result"]["hubs"]}
    assert set(by_id) == {hub.entry_id, unloaded.entry_id}
    assert by_id[hub.entry_id]["loaded"] is True
    assert by_id[hub.entry_id]["title"] == hub.title
    assert by_id[unloaded.entry_id]["loaded"] is False


# --------------------------------------------------------------------------- #
# Gating and error handling: one sweep each, not one test per command.        #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("command", _ALL_COMMANDS)
async def test_every_command_rejects_a_non_admin(
    hass, hub, hass_ws_client, hass_read_only_access_token, command
):
    """A non-admin gets ``unauthorized`` from all six, and changes nothing.

    Approving a device creates entities and writes the config entry, and the
    ignore list is a persistent hub setting — neither is a read-only user's to
    change, and even the *list* names devices in radio range of the home. The
    gate has to sit ahead of the work rather than beside it, which is why the
    entry is checked for side effects afterwards: a decorator applied in the
    wrong order would still reject the caller while having already acted.
    """
    client = await hass_ws_client(hass, hass_read_only_access_token)
    # The settings commands write ``entry.options`` and parts of ``entry.data``
    # the approval commands never touch, so the sweep compares whole snapshots
    # rather than a list of keys -- otherwise it would pass while one of them
    # quietly wrote somewhere nobody thought to look.
    before_data = dict(hub.data)
    before_options = dict(hub.options)

    await client.send_json_auto_id(_message(command, hub.entry_id))
    reply = await client.receive_json()

    assert reply["success"] is False
    assert reply["error"]["code"] == "unauthorized"
    assert hub.data.get(CONF_DEVICES, {}) == {}
    assert CONF_IGNORED_DEVICES not in hub.data
    assert dict(hub.data) == before_data
    assert dict(hub.options) == before_options
    assert _registry_device(hass, hub, _NEW_KEY) is None


@pytest.mark.parametrize("command", _ENTRY_COMMANDS)
async def test_an_unusable_entry_id_is_an_error_not_an_exception(
    hass, hub, hub_entry_builder, hass_ws_client, command
):
    """Three unusable ids, three errors, no traceback — for every entry command.

    A panel left open across a hub reload, a stale bookmark, or a script with a
    typo will all send commands for an entry that cannot be served, and that is a
    normal condition to report rather than a crash to log. The cases are
    distinguishable on purpose:

    * an id that names nothing, and one that names another integration's entry,
      are both ``not_found`` — this integration must never reach into an entry it
      does not own;
    * an id whose rtl_433 entry exists but is not set up is ``not_loaded``, which
      is a hub to wait for or repair rather than a mistake. The pending list lives
      only in the coordinator's memory, so there is nothing to answer with either
      way, but the two deserve different answers.
    """
    unloaded = hub_entry_builder(host="unreachable.local")
    unloaded.add_to_hass(hass)  # deliberately never set up
    foreign = MockConfigEntry(domain="light", title="Someone else's entry")
    foreign.add_to_hass(hass)

    client = await hass_ws_client(hass)

    for entry_id, expected in (
        ("no-such-entry-id", "not_found"),
        (foreign.entry_id, "not_found"),
        (unloaded.entry_id, "not_loaded"),
    ):
        reply, _ = await _call(client, _message(command, entry_id))
        assert reply["success"] is False, (command, entry_id)
        assert reply["error"]["code"] == expected, (command, entry_id)


# --------------------------------------------------------------------------- #
# The subscription.                                                           #
# --------------------------------------------------------------------------- #
async def test_subscription_pushes_membership_changes_and_stops_when_unsubscribed(
    hass, hub, hass_ws_client
):
    """Subscribe, see the list, see it change three ways, then see it go quiet.

    The pending list changes continuously by design — that is exactly what makes
    a config-flow form the wrong shape for it — so the panel subscribes instead
    of polling and a device heard while the page is open appears without a
    reload. The three membership changes walked here are the three that matter: a
    candidate appearing, one being adopted, and one being ignored. All three go
    out immediately rather than waiting for the coalescing timer, because they are
    the answer to "is there something new for me?".

    The unsubscribe half guards a leak with real teeth: the subscription arms both
    a dispatcher connection *and* a repeating timer, and a teardown that dropped
    only the first would leave an interval firing against a closed connection for
    the rest of the Home Assistant run. Silence is asserted exactly — a sentinel
    command's reply with no events queued ahead of it — rather than by waiting.
    """
    client = await hass_ws_client(hass)
    coordinator = _coordinator(hass, hub)

    await client.send_json_auto_id(
        {"type": "rtl_433/devices/subscribe", "entry_id": hub.entry_id}
    )
    ack = await client.receive_json()
    assert ack["success"]
    subscription = ack["id"]

    # The current list arrives at once, so a freshly opened panel is never blank
    # while it waits for the next transmission.
    snapshot = await client.receive_json()
    assert snapshot["id"] == subscription
    assert snapshot["type"] == "event"
    assert [row["key"] for row in snapshot["event"]["pending"]] == [
        _NEW_KEY,
        _MID_KEY,
        _OLD_KEY,
    ]

    # A device nobody has heard before transmits.
    _hear(coordinator, _EXTRA_FRAME)
    await hass.async_block_till_done()
    pushed = await client.receive_json()
    assert pushed["type"] == "event"
    assert _EXTRA_KEY in {row["key"] for row in pushed["event"]["pending"]}

    # Adopting one removes it from the list, for this client and any other.
    reply, events = await _call(
        client,
        {
            "type": "rtl_433/devices/add",
            "entry_id": hub.entry_id,
            "device_keys": [_NEW_KEY],
        },
    )
    assert reply["success"]
    assert events, "adopting a candidate must repaint an open panel"
    assert _NEW_KEY not in {row["key"] for row in events[-1]["event"]["pending"]}

    # So does ignoring one, which also lands it in the payload's second half.
    reply, events = await _call(
        client,
        {
            "type": "rtl_433/devices/ignore",
            "entry_id": hub.entry_id,
            "device_keys": [_MID_KEY],
        },
    )
    assert reply["success"]
    assert events, "ignoring a candidate must repaint an open panel"
    assert _MID_KEY not in {row["key"] for row in events[-1]["event"]["pending"]}
    assert events[-1]["event"]["ignored"] == [
        {"key": _MID_KEY, "model": _MID_FRAME["model"]}
    ]

    reply, _ = await _call(
        client, {"type": "unsubscribe_events", "subscription": subscription}
    )
    assert reply["success"]

    # A membership change and several coalescing intervals later, still silent.
    start = dt_util.utcnow()
    with freeze_time(start) as frozen:
        _hear(coordinator, {"model": "Nexus-TH", "id": 3, "temperature_C": 9.0})
        for tick in (10, 20, 30):
            frozen.move_to(start + timedelta(seconds=tick))
            async_fire_time_changed(hass, dt_util.utcnow())
            await hass.async_block_till_done()

    reply, events = await _call(client, {"type": "rtl_433/hubs"})
    assert reply["success"]
    assert events == [], "an unsubscribed client must never be pushed to again"


async def test_repeat_sightings_are_coalesced_instead_of_one_push_per_frame(
    hass, hub, hass_ws_client
):
    """Twenty frames for a known candidate must not be twenty WebSocket messages.

    This is the flooding guard, and the failure it protects against only ever
    appears under the load of a real receiver: in a dense neighbourhood the
    decoder runs constantly, and the naive wiring — push the list on every frame —
    would send a full payload down every open socket so that one row's sighting
    count could tick up by one. Nothing in ordinary development surfaces that; a
    user with 77 devices in range finds it immediately.

    So a repeat sighting deliberately fires no membership signal, and the drift it
    *does* cause (count and last-seen ageing on a row already on screen) is picked
    up by a slow interval that re-renders and sends only when the payload actually
    differs. Twenty frames spread over twenty seconds therefore cost a handful of
    coalesced repaints rather than twenty.

    The upper bound is asserted as a bound, not an exact count: the claim being
    protected is "not one per frame", and pinning the exact number would make this
    fail the next time the interval is legitimately retuned. The lower bound
    matters just as much in the other direction — a throttle that never delivered
    would leave the panel's counts frozen, and would pass a one-sided assertion.
    """
    client = await hass_ws_client(hass)
    coordinator = _coordinator(hass, hub)

    await client.send_json_auto_id(
        {"type": "rtl_433/devices/subscribe", "entry_id": hub.entry_id}
    )
    ack = await client.receive_json()
    assert ack["success"]
    snapshot = await client.receive_json()
    assert snapshot["type"] == "event"

    frames = 20
    before = coordinator.pending[_NEW_KEY].count
    start = dt_util.utcnow()
    with freeze_time(start) as frozen:
        for tick in range(1, frames + 1):
            frozen.move_to(start + timedelta(seconds=tick))
            _hear(coordinator, _NEW_FRAME)
            async_fire_time_changed(hass, dt_util.utcnow())
            await hass.async_block_till_done()

    reply, events = await _call(client, {"type": "rtl_433/hubs"})
    assert reply["success"]

    # Every frame landed on the candidate...
    assert coordinator.pending[_NEW_KEY].count == before + frames
    # ...but the socket saw a few coalesced repaints rather than one per frame.
    assert 1 <= len(events) <= frames // 4, (
        f"{frames} repeat sightings produced {len(events)} pushes"
    )

    # The device now goes quiet for two whole intervals. Coalescing is only
    # acceptable if it also *converges*: whichever repaint happened to be the
    # last one during the flood, the client must end up holding the final count
    # rather than a row frozen partway through — and then be left alone, because
    # a payload that has not changed must cost nothing.
    with freeze_time(start + timedelta(seconds=frames)) as frozen:
        for tick in (frames + 6, frames + 12):
            frozen.move_to(start + timedelta(seconds=tick))
            async_fire_time_changed(hass, dt_util.utcnow())
            await hass.async_block_till_done()

    reply, idle = await _call(client, {"type": "rtl_433/hubs"})
    assert reply["success"]
    assert len(idle) <= 1, "an idle hub must not be repainted every interval"
    latest = {row["key"]: row for row in (events + idle)[-1]["event"]["pending"]}
    assert latest[_NEW_KEY]["count"] == before + frames


# --------------------------------------------------------------------------- #
# One adoption path behind two surfaces.                                      #
# --------------------------------------------------------------------------- #
def _adopted_snapshot(hass, entry, device_key) -> dict[str, Any]:
    """Describe an adopted device in terms independent of which hub owns it.

    Everything identifying — ``entry_id``, the registry ids, the ``entity_id``
    Home Assistant disambiguates when two hubs produce identically-named devices
    — is stripped or normalised out, so two snapshots are comparable if and only
    if the two surfaces really produced the same device.
    """
    device = _registry_device(hass, entry, device_key)
    assert device is not None
    prefix = f"{entry.entry_id}:"
    return {
        "model": device.model,
        "manufacturer": device.manufacturer,
        "name": device.name,
        "entry_type": device.entry_type,
        "linked_to_its_hub": device.via_device_id is not None,
        "entities": {
            (
                registry_entry.domain,
                registry_entry.unique_id.removeprefix(prefix),
                registry_entry.original_name,
                registry_entry.original_device_class,
                registry_entry.unit_of_measurement,
                registry_entry.entity_category,
                registry_entry.disabled_by,
            )
            for registry_entry in er.async_entries_for_device(
                er.async_get(hass), device.id, include_disabled_entities=True
            )
        },
        "stored": entry.data[CONF_DEVICES][device_key],
    }


async def test_adopting_over_the_socket_matches_adopting_from_the_options_flow(
    hass, hub_entry_builder, hass_ws_client, no_socket
):
    """The panel and the options form must produce the same device, not a similar one.

    Three surfaces now reach one registration path — a live first sighting, the
    options flow, and this API — and the whole reason
    ``custom_components/rtl_433/adoption.py`` exists is that two surfaces must not
    mean two implementations. A user who adds one device from the panel and the
    next from the form would otherwise end up with two subtly different
    integrations, and the difference would show up as a missing entity or a wrong
    unit long after the fact.

    Two identical hubs hear the identical frame; one is adopted over the socket
    and the other through the form, and the resulting device metadata, entity set
    (names, device classes, units, categories, enabled-ness) and persisted record
    are compared. This is the mirror of the equivalence check the options flow
    already carries, extended to the third surface.
    """
    socket_hub = await _setup_hub(hass, hub_entry_builder, host="socket-hub.local")
    form_hub = await _setup_hub(hass, hub_entry_builder, host="form-hub.local")
    for entry in (socket_hub, form_hub):
        _hear(_coordinator(hass, entry), _NEW_FRAME)
    await hass.async_block_till_done()

    client = await hass_ws_client(hass)
    reply, _ = await _call(
        client,
        {
            "type": "rtl_433/devices/add",
            "entry_id": socket_hub.entry_id,
            "device_keys": [_NEW_KEY],
        },
    )
    assert reply["success"]
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(form_hub.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "add_devices"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"add": [_NEW_KEY]}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY

    socket_device = _adopted_snapshot(hass, socket_hub, _NEW_KEY)
    assert socket_device["entities"], "adoption produced no entities at all"
    assert socket_device == _adopted_snapshot(hass, form_hub, _NEW_KEY)


# --------------------------------------------------------------------------- #
# The panel itself.                                                           #
# --------------------------------------------------------------------------- #
async def test_the_panel_registers_once_and_serves_its_module(
    hass, hub_entry_builder, hass_client, no_socket
):
    """Two receivers, one panel — and the module is really on disk and served.

    Panel and static-path registration are per Home Assistant *run*, but this
    integration has no ``async_setup`` and so registers from every hub's
    ``async_setup_entry``. Both underlying APIs refuse a duplicate — the frontend
    raises ``Overwriting panel`` and aiohttp refuses a second route on the same
    prefix — so without the guard a user's *second* receiver simply fails to set
    up. Asserting both entries are loaded is what states that.

    The panel's stored config is asserted because its values are the whole reason
    the approach works and none of them is checked anywhere else.
    ``embed_iframe=False`` is what gets ``hass`` handed to the element as a
    property (an iframe would cut it off from the frontend's connection *and* its
    theme), and ``require_admin`` matches the gate on the commands the page calls.

    ``config_panel_domain`` is set, which is what puts the panel behind this
    integration's entry in Settings rather than in a top-level sidebar slot. The
    trade it makes is asserted here because it is easy to make by accident and
    expensive to notice: it does not *add* a route, it replaces one. The entry's
    settings control opens this panel, and the options flow behind it has no
    other entry point -- the row's overflow menu offers reload, rename,
    reconfigure, enable/disable and delete, and nothing that opens options. So
    every options step must be reachable from the panel, or it is unreachable.

    No ``sidebar_title``/``sidebar_icon`` for the same reason: with them the
    panel would take a top-level slot *as well*, which is the placement this
    replaced.

    Finally the module is fetched over HTTP. The element name in the served body
    has to match ``webcomponent_name`` exactly — Home Assistant loads the module
    and then looks for that tag, and a mismatch is a blank page with nothing in
    the log — and a fetch is also the only thing that catches the file failing to
    ship at all, which no amount of Python-side assertion would notice.
    """
    first = await _setup_hub(hass, hub_entry_builder, host="hub-one.local")
    second = await _setup_hub(hass, hub_entry_builder, host="hub-two.local")
    assert first.state is ConfigEntryState.LOADED
    assert second.state is ConfigEntryState.LOADED

    panels = hass.data[DATA_PANELS]
    assert [url_path for url_path in panels if url_path == DOMAIN] == [DOMAIN]

    panel = panels[DOMAIN]
    # Set, so the entry's settings control opens this panel.
    assert panel.config_panel_domain == DOMAIN
    # ...and no sidebar slot is taken alongside it.
    assert panel.sidebar_title is None
    assert panel.sidebar_icon is None
    assert panel.require_admin is True
    assert panel.component_name == "custom"
    custom = panel.config["_panel_custom"]
    assert custom["name"] == PANEL_ELEMENT_NAME
    assert custom["embed_iframe"] is False
    assert custom["module_url"] == f"{PANEL_URL_BASE}/{PANEL_MODULE_NAME}"

    http_client = await hass_client()
    response = await http_client.get(f"{PANEL_URL_BASE}/{PANEL_MODULE_NAME}")
    assert response.status == 200
    body = await response.text()
    assert f'customElements.define("{PANEL_ELEMENT_NAME}"' in body


# --------------------------------------------------------------------------- #
# Settings: the hub's options, one device's overrides, the mapping document.   #
#                                                                             #
# These are the panel's half of the three forms the options flow also renders. #
# What is worth protecting is not the transport but the *rules underneath* --  #
# which submitted value means "clear this", which means "use the default and   #
# store nothing" -- because those are exactly what a second surface tends to   #
# get subtly differently, and a difference is invisible until a device stops   #
# behaving the way its form says it should.                                    #
# --------------------------------------------------------------------------- #
_METER_KEY = "SCM-12345"
_METER_MODEL = "SCM"


@pytest.fixture
async def settings_hub(hass, hub_entry_builder, no_socket):
    """A loaded hub with one adopted meter, ready for the settings forms."""
    return await _setup_hub(
        hass,
        hub_entry_builder,
        devices={
            _METER_KEY: {
                CONF_MODEL: _METER_MODEL,
                DEVICE_FIELDS: ["consumption_data"],
            }
        },
    )


async def test_settings_get_answers_with_what_the_three_forms_render(
    hass, settings_hub, hass_ws_client
):
    """One call fills all three dialogs: hub values, device rows, and the tables.

    The commodity tables travel in the payload rather than living in the panel
    because which units Home Assistant will convert for a gas meter is a fact
    about :mod:`~custom_components.rtl_433.calibration`, not about a screen. A
    panel carrying its own copy would eventually offer a unit the entity build
    refuses, and nothing would fail until someone looked at their energy
    dashboard.
    """
    client = await hass_ws_client(hass)

    reply, _ = await _call(
        client, {"type": "rtl_433/settings/get", "entry_id": settings_hub.entry_id}
    )

    assert reply["success"] is True
    result = reply["result"]
    # The hub's effective values, not the raw entry: 600 is what the fixture set.
    assert result["hub"][CONF_AVAILABILITY_TIMEOUT] == 600
    assert result["hub"][CONF_MANAGE_SETTINGS] is True
    assert result["defaults"][CONF_AVAILABILITY_TIMEOUT] == DEFAULT_AVAILABILITY_TIMEOUT

    # One row per adopted device, carrying everything its form needs.
    (device,) = result["devices"]
    assert device["device_key"] == _METER_KEY
    assert _METER_MODEL in device["label"]
    assert device[DEVICE_TIMEOUT_OVERRIDE] is None
    assert device["calibration"] is None
    assert device["motion"] is False

    # The tables the calibration control is built from.
    assert "gas" in result["commodities"]
    assert "m³" in result["commodity_units"]["gas"]
    # Nothing overridden yet -> an empty editor, not a document holding "{}".
    assert result["mappings"] == ""
    assert result["mappings_docs_url"].startswith("https://")


async def test_the_hub_default_timeout_is_dropped_rather_than_stored(
    hass, settings_hub, hass_ws_client
):
    """Saving the plain default stores no timeout; any other value is kept.

    Persisting the default as an *explicit* hub-wide timeout would mask the
    per-device-class defaults, and the device that suffers is the one that
    should never expire on silence: a doorbell that has not rung in ten minutes
    is not unavailable, it is a doorbell. So the sentinel is dropped, and ``0``
    -- "never expire", a deliberate choice that happens to be falsy -- is not.
    """
    client = await hass_ws_client(hass)

    reply, _ = await _call(
        client,
        {
            "type": "rtl_433/settings/hub",
            "entry_id": settings_hub.entry_id,
            CONF_AVAILABILITY_TIMEOUT: DEFAULT_AVAILABILITY_TIMEOUT,
            CONF_MANAGE_SETTINGS: True,
        },
    )
    await hass.async_block_till_done()
    assert reply["success"] is True
    assert CONF_AVAILABILITY_TIMEOUT not in settings_hub.options
    assert settings_hub.options[CONF_MANAGE_SETTINGS] is True

    reply, _ = await _call(
        client,
        {
            "type": "rtl_433/settings/hub",
            "entry_id": settings_hub.entry_id,
            CONF_AVAILABILITY_TIMEOUT: 0,
            CONF_MANAGE_SETTINGS: True,
        },
    )
    await hass.async_block_till_done()
    assert reply["success"] is True
    assert settings_hub.options[CONF_AVAILABILITY_TIMEOUT] == 0


async def test_a_device_calibration_round_trips_and_then_clears(
    hass, settings_hub, hass_ws_client
):
    """A real commodity is stored; ``none`` clears it, and so does a bad unit.

    The panel cannot store a calibration the entity build would refuse, whatever
    its controls do, because the three parts go through
    :func:`~custom_components.rtl_433.calibration.normalize_calibration` on the
    way in -- and the normalized result comes back in the reply, so a caller
    re-renders from what was stored rather than from what it sent.
    """
    client = await hass_ws_client(hass)
    message = {
        "type": "rtl_433/settings/device",
        "entry_id": settings_hub.entry_id,
        "device_key": _METER_KEY,
        DEVICE_TIMEOUT_OVERRIDE: 1800,
        CALIBRATION_COMMODITY: "gas",
        CALIBRATION_UNIT: "m³",
        CALIBRATION_SCALE: 0.01,
    }

    with patch.object(hass.config_entries, "async_schedule_reload"):
        reply, _ = await _call(client, message)
        await hass.async_block_till_done()

    assert reply["success"] is True
    record = settings_hub.data[CONF_DEVICES][_METER_KEY]
    assert record[DEVICE_TIMEOUT_OVERRIDE] == 1800
    assert record[DEVICE_CALIBRATION] == {
        CALIBRATION_COMMODITY: "gas",
        CALIBRATION_UNIT: "m³",
        CALIBRATION_SCALE: 0.01,
    }
    assert reply["result"]["calibration"] == record[DEVICE_CALIBRATION]

    # A unit that is not convertible for the commodity is not a calibration at
    # all -- it clears, rather than storing something the sensor cannot use.
    with patch.object(hass.config_entries, "async_schedule_reload"):
        reply, _ = await _call(
            client, {**message, CALIBRATION_UNIT: "furlongs", "timeout_override": None}
        )
        await hass.async_block_till_done()

    assert reply["success"] is True
    assert reply["result"]["calibration"] is None
    record = settings_hub.data[CONF_DEVICES][_METER_KEY]
    assert DEVICE_CALIBRATION not in record
    # The blank timeout cleared with it, rather than persisting as a zero.
    assert DEVICE_TIMEOUT_OVERRIDE not in record


async def test_a_device_clear_delay_lands_where_the_resolver_reads_it(
    hass, settings_hub, hass_ws_client
):
    """The clear-delay is written to options, and read back from there.

    It is the one per-device knob that does not live in ``entry.data``, which is
    the whole reason it was silently ignored for so long. Asserting the storage
    location *and* the read-back is what keeps the two ends of that from drifting
    apart again.
    """
    client = await hass_ws_client(hass)

    with patch.object(hass.config_entries, "async_schedule_reload"):
        reply, _ = await _call(
            client,
            {
                "type": "rtl_433/settings/device",
                "entry_id": settings_hub.entry_id,
                "device_key": _METER_KEY,
                DEVICE_MOTION_CLEAR_DELAY: 30,
            },
        )
        await hass.async_block_till_done()

    assert reply["success"] is True
    assert (
        settings_hub.options[CONF_DEVICES][_METER_KEY][DEVICE_MOTION_CLEAR_DELAY] == 30
    )
    assert reply["result"][DEVICE_MOTION_CLEAR_DELAY] == 30

    # Cleared, the device's sub-map goes away entirely rather than being left as
    # an empty dict for every device anyone has ever opened the form on.
    with patch.object(hass.config_entries, "async_schedule_reload"):
        reply, _ = await _call(
            client,
            {
                "type": "rtl_433/settings/device",
                "entry_id": settings_hub.entry_id,
                "device_key": _METER_KEY,
                DEVICE_MOTION_CLEAR_DELAY: None,
            },
        )
        await hass.async_block_till_done()

    assert reply["success"] is True
    assert _METER_KEY not in settings_hub.options.get(CONF_DEVICES, {})


async def test_a_clear_delay_left_in_data_by_the_migration_can_be_cleared(
    hass, hub_entry_builder, hass_ws_client, no_socket
):
    """A hub that came from per-device entries can still blank the delay.

    The migration from per-device config entries writes the clear-delay into
    ``entry.data``; every edit since writes it to ``entry.options``, and
    :func:`~custom_components.rtl_433.settings.device_clear_delay` reads options
    first and falls back to data. Blanking the field only empties the options
    half, so while the leftover stayed in data the old number came straight back
    -- the user cleared it, saved, re-opened the form, and there it was again.

    Saving now retires the leftover, which is what makes the second half of this
    test the interesting one: the form reads back empty, so the device falls back
    to the descriptor's own default.
    """
    migrated = await _setup_hub(
        hass,
        hub_entry_builder,
        devices={
            _METER_KEY: {
                CONF_MODEL: _METER_MODEL,
                DEVICE_FIELDS: ["consumption_data"],
                # What the migration left behind.
                DEVICE_MOTION_CLEAR_DELAY: 90,
            }
        },
    )
    client = await hass_ws_client(hass)

    # The form opens showing the migrated value, so the user sees what they set.
    reply, _ = await _call(
        client, {"type": "rtl_433/settings/get", "entry_id": migrated.entry_id}
    )
    (device,) = reply["result"]["devices"]
    assert device[DEVICE_MOTION_CLEAR_DELAY] == 90

    # Blanking the field and saving really clears it.
    with patch.object(hass.config_entries, "async_schedule_reload"):
        reply, _ = await _call(
            client,
            {
                "type": "rtl_433/settings/device",
                "entry_id": migrated.entry_id,
                "device_key": _METER_KEY,
                DEVICE_MOTION_CLEAR_DELAY: None,
            },
        )
        await hass.async_block_till_done()

    assert reply["success"] is True
    assert reply["result"][DEVICE_MOTION_CLEAR_DELAY] is None
    assert DEVICE_MOTION_CLEAR_DELAY not in migrated.data[CONF_DEVICES][_METER_KEY]
    assert _METER_KEY not in migrated.options.get(CONF_DEVICES, {})

    # And it stays cleared when the form is re-opened.
    reply, _ = await _call(
        client, {"type": "rtl_433/settings/get", "entry_id": migrated.entry_id}
    )
    (device,) = reply["result"]["devices"]
    assert device[DEVICE_MOTION_CLEAR_DELAY] is None


async def test_settings_for_an_unknown_device_are_refused(
    hass, settings_hub, hass_ws_client
):
    """A device key this hub has never adopted is ``not_found``, not a new record.

    Without the check the write would happily create the record, and the hub
    would grow a device it has never heard -- from a stale panel, or a typo in a
    script.
    """
    client = await hass_ws_client(hass)

    reply, _ = await _call(
        client,
        {
            "type": "rtl_433/settings/device",
            "entry_id": settings_hub.entry_id,
            "device_key": "Nothing-Like-This-1",
        },
    )

    assert reply["success"] is False
    assert reply["error"]["code"] == "not_found"
    assert set(settings_hub.data[CONF_DEVICES]) == {_METER_KEY}


async def test_mapping_overrides_round_trip_as_yaml_text(
    hass, settings_hub, hass_ws_client
):
    """Overrides go out and come back as the YAML the documentation writes.

    Text rather than a JSON object because that is the form the user already has
    -- copied from the README, or from someone else's hub -- and because the
    panel has no YAML parser and should not grow one. Parsing on this side is
    also what keeps ``safe_load`` in charge of a string a client submitted.
    """
    client = await hass_ws_client(hass)
    document = (
        "temperature_C:\n"
        "  platform: sensor\n"
        "  name: Outside Temp\n"
        "  object_suffix: outside\n"
        "  unit_of_measurement: \N{DEGREE SIGN}C\n"
    )

    with patch.object(hass.config_entries, "async_schedule_reload"):
        reply, _ = await _call(
            client,
            {
                "type": "rtl_433/settings/mappings",
                "entry_id": settings_hub.entry_id,
                "yaml": document,
            },
        )
        await hass.async_block_till_done()

    assert reply["success"] is True
    stored = settings_hub.data[CONF_USER_MAPPINGS]
    assert stored["temperature_C"]["unit_of_measurement"] == "\N{DEGREE SIGN}C"
    # And it renders back as a document the same editor can re-submit. The unit
    # is deliberately not plain ASCII: units are where non-ASCII characters live,
    # and PyYAML escapes them by default, so without ``allow_unicode`` the user
    # would re-open the editor to ``"\\xB0C"`` and have to decode their own
    # setting before they could edit the line.
    document_back = reply["result"]["mappings"]
    assert "temperature_C:" in document_back
    assert "unit_of_measurement: \N{DEGREE SIGN}C" in document_back
    assert "\\x" not in document_back

    # Clearing the editor removes every override rather than being a parse error.
    with patch.object(hass.config_entries, "async_schedule_reload"):
        reply, _ = await _call(
            client,
            {
                "type": "rtl_433/settings/mappings",
                "entry_id": settings_hub.entry_id,
                "yaml": "   \n",
            },
        )
        await hass.async_block_till_done()

    assert reply["success"] is True
    assert settings_hub.data[CONF_USER_MAPPINGS] == {}


@pytest.mark.parametrize(
    ("document", "why"),
    [
        ("temperature_C: [1,", "not YAML at all"),
        ("- just\n- a list\n", "YAML, but not a mapping"),
        ("bad_field:\n  name: X\n  object_suffix: X\n", "a mapping, but invalid"),
    ],
)
async def test_mappings_that_will_not_store_are_refused_and_change_nothing(
    hass, settings_hub, hass_ws_client, document, why
):
    """Three ways to submit a bad document, one answer, and nothing written.

    The three fail at different depths -- the parser, the shape check, the
    override schema -- and a user fixing a mapping file needs the same treatment
    from all of them: the overrides they already had, still there.
    """
    client = await hass_ws_client(hass)
    before = dict(settings_hub.data)

    reply, _ = await _call(
        client,
        {
            "type": "rtl_433/settings/mappings",
            "entry_id": settings_hub.entry_id,
            "yaml": document,
        },
    )

    assert reply["success"] is False, why
    assert reply["error"]["code"] == "invalid_mappings", why
    assert dict(settings_hub.data) == before, why
