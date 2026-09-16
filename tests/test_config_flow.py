"""Tests for the rtl_433 config and options flows (single-receiver model).

The connectivity check is patched throughout (no sockets are opened). Coverage:
the receiver user step (success + ``cannot_connect``), the approval steps that turn a
heard device into a Home Assistant device (``add_devices`` / ``ignored_devices``:
the add / ignore / un-ignore round trip, the conflict rejection, and the empty
and receiver-not-loaded aborts), the receiver options step (availability timeout persisted
to ``entry.options``), the device options step (set/clear a per-device
``timeout_override`` in ``entry.data["devices"]``, plus the ``no_devices``
abort), the replace pair, and a direct unit test of
``async_remove_config_entry_device`` (False for the receiver device, True +
map/coordinator eviction for a nested device).

The approval tests populate the pending list by feeding real frames through the
client's own normalize + classify seam rather than assigning
``coordinator.pending`` directly, so a change that stopped routing frames into
the list would fail here instead of quietly passing.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from freezegun import freeze_time

from custom_components.rtl_433 import async_remove_config_entry_device
from custom_components.rtl_433.const import (
    CALIBRATION_COMMODITY,
    CALIBRATION_SCALE,
    CALIBRATION_UNIT,
    COMMODITY_GAS,
    COMMODITY_NONE,
    COMMODITY_WATER,
    CONF_AVAILABILITY_TIMEOUT,
    CONF_DEVICES,
    CONF_HOST,
    CONF_IGNORED_DEVICES,
    CONF_INITIAL_FREQUENCY,
    CONF_MANAGE_SETTINGS,
    CONF_MODEL,
    CONF_PATH,
    CONF_PORT,
    CONF_RADIO_ID,
    DEFAULT_AVAILABILITY_TIMEOUT,
    DEFAULT_INITIAL_FREQUENCY,
    DEFAULT_MANAGE_SETTINGS,
    DEVICE_CALIBRATION,
    DEVICE_FIELDS,
    DEVICE_TIMEOUT_OVERRIDE,
    DOMAIN,
)
from homeassistant.config_entries import SOURCE_HASSIO, SOURCE_USER
from homeassistant.const import UnitOfVolume
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.service_info.hassio import HassioServiceInfo
from homeassistant.util import dt as dt_util
from tests.conftest import (
    build_location_entry,
    receiver_id,
    receiver_scope,
    receiver_subentry,
)

VALIDATE = "custom_components.rtl_433.config_flow.Rtl433Coordinator.validate_connection"


def _schema_default(result, key: str):
    """Return the rendered default for a form field key, or ``None``.

    Voluptuous stores a field's default as a zero-arg callable on the marker; this
    pulls the option flow form's commodity pre-fill out of the shown schema so a
    test can assert the rendered default without re-implementing the flow.
    """
    for marker in result["data_schema"].schema:
        if marker == key:
            default = getattr(marker, "default", None)
            return default() if callable(default) else default
    return None


def _schema_keys(result) -> set[str]:
    """Return the set of field key names present in a form result's schema."""
    keys: set[str] = set()
    for marker in result["data_schema"].schema:
        keys.add(marker.schema if hasattr(marker, "schema") else str(marker))
    return keys


async def test_user_step_success_creates_receiver(hass):
    """A reachable server produces a receiver entry with the connection data."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    with patch(VALIDATE, return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_HOST: "rtl433.local",
                CONF_PORT: 8433,
                CONF_PATH: "/ws",
                "secure": False,
            },
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "rtl_433 (rtl433.local)"
    # One flow, two objects: the location entry, and its first receiver
    # subentry. The connection lives on the receiver; the location's own data
    # starts empty and fills with the devices the user adopts.
    entry = result["result"]
    assert entry.data == {}
    assert entry.unique_id is None
    receiver = receiver_subentry(entry)
    assert receiver.title == "rtl_433 (rtl433.local)"
    assert receiver.unique_id == "hub:rtl433.local:8433"
    assert receiver.data[CONF_HOST] == "rtl433.local"
    assert receiver.data[CONF_PORT] == 8433
    # No per-device entry_type discriminator.
    assert "entry_type" not in receiver.data


async def test_user_step_cannot_connect_shows_error(hass):
    """An unreachable server keeps the form open with a cannot_connect error."""
    from custom_components.rtl_433.coordinator import CannotConnect

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )

    with patch(VALIDATE, side_effect=CannotConnect("nope")):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_HOST: "unreachable",
                CONF_PORT: 8433,
                CONF_PATH: "/ws",
                "secure": False,
            },
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


# --------------------------------------------------------------------------- #
# Options flow — add / ignore discovered devices (the approval steps).         #
# --------------------------------------------------------------------------- #
# The three devices the receiver hears in the fixtures below. Keys are what the
# normalizer derives from ``model`` + ``id``; they are spelled out here so the
# assertions read as the user's picker does.
PENDING_OLD = "Acurite-606TX-42"
PENDING_MID = "GenericDoor-X1-88"
PENDING_NEW = "EnergyMeter-2000-1234"

PENDING_MID_FRAME = {"model": "GenericDoor-X1", "id": 88, "closed": 0, "battery_ok": 1}


def _coordinator(hass, entry):
    """Return the running coordinator for a loaded receiver entry."""
    return hass.data[DOMAIN][receiver_id(entry)]


def _hear(coordinator, frame):
    """Inject one live frame through the client's normalize + classify seam.

    Drives ``_process_event`` -> ``_on_client_event``, the exact path an incoming
    WebSocket frame takes, so these tests exercise the routing code that actually
    builds the pending list rather than a hand-assembled ``coordinator.pending``
    (which would keep passing after a routing regression). The frames carry no
    ``time`` on purpose: a frame with no usable timestamp classifies as a live
    transmission, while a timestamped frame older than the connect anchor is a
    reconnect replay and deliberately never becomes a candidate.
    """
    coordinator._client._process_event(frame)


def _device_entity_unique_ids(hass, entry, device_key) -> set[str]:
    """Return the unique_ids of every registry entity belonging to a device."""
    prefix = f"{receiver_id(entry)}:{device_key}:"
    return {
        registry_entry.unique_id
        for registry_entry in er.async_get(hass).entities.values()
        if registry_entry.unique_id.startswith(prefix)
    }


def _registry_device(hass, entry, device_key):
    """Return the registry device for a device key, or ``None``."""
    return dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, f"{receiver_id(entry)}:{device_key}"), entry.entry_id
    )


async def _receiver_hearing_three_devices(hass, receiver_entry_builder, **kwargs):
    """Set up a receiver that has heard three devices at three distinct times.

    The sightings are frozen a minute apart so "most recently seen first" is a
    real ordering rather than an artefact of insertion order (a stable sort over
    three identical timestamps would silently pass either way), and the newest
    device is heard twice so its sighting count is distinguishable from the
    others'. Only that newest device reports a signal level, which is what makes
    the "omit the level when the device does not report one" branch observable.
    """
    entry = receiver_entry_builder(availability_timeout=600, **kwargs)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    coordinator = _coordinator(hass, entry)
    start = dt_util.utcnow()
    with freeze_time(start):
        _hear(
            coordinator,
            {"model": "Acurite-606TX", "id": 42, "temperature_C": 21.4, "humidity": 55},
        )
    with freeze_time(start + timedelta(minutes=1)):
        _hear(coordinator, PENDING_MID_FRAME)
    with freeze_time(start + timedelta(minutes=2)):
        _hear(
            coordinator,
            {"model": "EnergyMeter-2000", "id": 1234, "power_W": 1450.5, "snr": 11.5},
        )
        _hear(
            coordinator,
            {"model": "EnergyMeter-2000", "id": 1234, "power_W": 1460.5, "snr": 11.5},
        )
    await hass.async_block_till_done()
    return entry


async def _open_add_devices(hass, entry):
    """Walk the options menu to the add-devices step and return the form."""
    result = await hass.config_entries.options.async_init(entry.entry_id)
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "add_devices"}
    )


async def test_options_menu_leads_with_the_two_approval_steps(
    hass, receiver_entry_builder, no_socket
):
    """The menu offers add_devices and ignored_devices, in that order, first.

    Adding a heard device is the only route by which an RF device reaches Home
    Assistant at all, so the pair leads the menu; the settings steps keep their
    established order behind them.
    """
    entry = receiver_entry_builder()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    assert result["menu_options"] == [
        "add_devices",
        "ignored_devices",
        "receiver",
        "device",
        "mappings",
        "replace",
    ]


async def test_add_devices_adds_ignores_and_leaves_the_rest_pending(
    hass, receiver_entry_builder, no_socket
):
    """The whole approval workflow: three heard, one added, one ignored, one left.

    This is the plan's primary contract in a single walk. Nothing reached the
    device registry from being heard; after the submit exactly the added device
    exists (with its entities and a record in ``entry.data["devices"]``), exactly
    the ignored device is on the persistent ignore list and has no device, and
    the unselected candidate is still waiting to be offered again. The form is
    checked before the submit because the ordering and the label content are the
    only things that let a user tell a real sensor from a one-off bad decode
    without leaving the page.
    """
    entry = await _receiver_hearing_three_devices(hass, receiver_entry_builder)

    result = await _open_add_devices(hass, entry)
    assert result["step_id"] == "add_devices"

    # Most recently heard first, and both multi-selects offer the same candidates
    # (one selector serves both fields; only the meaning of a pick differs).
    assert [value for value, _ in _select_options(result, "add")] == [
        PENDING_NEW,
        PENDING_MID,
        PENDING_OLD,
    ]
    assert _select_options(result, "ignore") == _select_options(result, "add")

    labels = dict(_select_options(result, "add"))
    # Model, key, sighting count and signal level, so a weak one-off decode is
    # visibly different from a sensor that keeps checking in.
    assert labels[PENDING_NEW].startswith(
        f"EnergyMeter-2000 ({PENDING_NEW}) — seen 2x — 11.5 dB — last seen "
    )
    assert labels[PENDING_NEW].endswith(" ago")
    # A device whose frames carry no level simply omits that segment rather than
    # rendering a placeholder.
    assert labels[PENDING_OLD].startswith(
        f"Acurite-606TX ({PENDING_OLD}) — seen 1x — last seen "
    )
    assert " dB " not in labels[PENDING_OLD]

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"add": [PENDING_NEW], "ignore": [PENDING_MID]}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY

    # The added device is persisted in the same record shape every other write
    # path produces, so a restart rebuilds it.
    assert set(entry.data[CONF_DEVICES]) == {PENDING_NEW}
    record = entry.data[CONF_DEVICES][PENDING_NEW]
    assert record[CONF_MODEL] == "EnergyMeter-2000"
    assert "power_W" in record[DEVICE_FIELDS]
    # Exactly the ignored device is on the persistent ignore list.
    assert entry.data[CONF_IGNORED_DEVICES] == [PENDING_MID]

    coordinator = _coordinator(hass, entry)
    assert PENDING_NEW in coordinator.adopted
    assert coordinator.ignored == {PENDING_MID}
    # The unselected candidate is untouched and will be offered again.
    assert set(coordinator.pending) == {PENDING_OLD}

    # Only the added device reached the device registry, and it arrived with
    # entities seeded from the frame that was already heard.
    assert _registry_device(hass, entry, PENDING_NEW) is not None
    assert _device_entity_unique_ids(hass, entry, PENDING_NEW)
    for key in (PENDING_MID, PENDING_OLD):
        assert _registry_device(hass, entry, key) is None
        assert _device_entity_unique_ids(hass, entry, key) == set()


async def test_ignored_device_survives_a_reload_and_never_returns_to_pending(
    hass, receiver_entry_builder, no_socket
):
    """Ignoring sticks: across a reload, and against the device's next frame.

    The pending list is in-memory by design, so a reload empties it — but the
    ignore list lives in ``entry.data`` precisely so the device the user dismissed
    does not simply walk back in on its next transmission. Feeding that
    transmission after the reload is what proves the reloaded coordinator seeded
    ``ignored`` from the entry rather than starting clean.
    """
    entry = await _receiver_hearing_three_devices(hass, receiver_entry_builder)

    result = await _open_add_devices(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"ignore": [PENDING_MID]}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY

    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    coordinator = _coordinator(hass, entry)
    assert coordinator.pending == {}  # rebuilt from live traffic, by design
    assert coordinator.ignored == {PENDING_MID}

    _hear(coordinator, PENDING_MID_FRAME)
    await hass.async_block_till_done()

    assert coordinator.pending == {}
    assert _registry_device(hass, entry, PENDING_MID) is None
    assert entry.data[CONF_IGNORED_DEVICES] == [PENDING_MID]


async def test_add_and_ignore_conflict_reshows_the_form_and_applies_nothing(
    hass, receiver_entry_builder, no_socket
):
    """A key in both lists writes nothing at all — not even the unambiguous half.

    "Add this and also ignore it" is a contradiction the flow refuses to resolve
    on the user's behalf. Since the submit is rejected, applying its unambiguous
    part anyway would leave a side effect behind a form the user still has to
    correct and resubmit, so the whole submit is discarded and the form comes
    back with its full candidate list intact.
    """
    entry = await _receiver_hearing_three_devices(hass, receiver_entry_builder)

    result = await _open_add_devices(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        # PENDING_OLD is unambiguously "add" and PENDING_MID unambiguously
        # "ignore"; only PENDING_NEW is contradictory.
        {
            "add": [PENDING_NEW, PENDING_OLD],
            "ignore": [PENDING_NEW, PENDING_MID],
        },
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "add_devices"
    assert result["errors"] == {"base": "add_and_ignore_conflict"}
    # The form is re-shown with every candidate still selectable.
    assert [value for value, _ in _select_options(result, "add")] == [
        PENDING_NEW,
        PENDING_MID,
        PENDING_OLD,
    ]

    # Nothing was applied: no device stored, nothing ignored, nothing adopted,
    # and all three candidates still pending.
    assert entry.data.get(CONF_DEVICES, {}) == {}
    assert CONF_IGNORED_DEVICES not in entry.data
    coordinator = _coordinator(hass, entry)
    assert coordinator.ignored == set()
    assert set(coordinator.pending) == {PENDING_OLD, PENDING_MID, PENDING_NEW}
    for key in (PENDING_OLD, PENDING_MID, PENDING_NEW):
        assert _registry_device(hass, entry, key) is None


async def test_approval_steps_abort_when_there_is_nothing_to_show(
    hass, receiver_entry_builder, no_socket
):
    """Empty lists abort with an explanation instead of a form with no choices.

    An empty pending list is the normal state shortly after a restart (the list
    is rebuilt from live traffic), so the abort has to say so rather than look
    like a broken form.
    """
    entry = receiver_entry_builder()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    for step, reason in (
        ("add_devices", "no_pending_devices"),
        ("ignored_devices", "no_ignored_devices"),
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": step}
        )
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == reason


async def test_approval_steps_abort_when_the_receiver_is_not_loaded(
    hass, receiver_entry_builder
):
    """Both steps degrade to ``receiver_not_loaded`` rather than raising.

    The options flow can be opened while the entry is not loaded (an unreachable
    server, a disabled receiver), and both steps need the running coordinator — the
    pending list lives only in its memory, and un-ignoring has to reach its
    mirrored ``ignored`` set to take effect before a reload. The entry carries a
    non-empty ignore list so the guard is provably checked *before* the
    empty-list abort rather than being masked by it.
    """
    entry = receiver_entry_builder(ignored_devices=[PENDING_MID])
    entry.add_to_hass(hass)  # deliberately never set up

    for step in ("add_devices", "ignored_devices"):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": step}
        )
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "receiver_not_loaded"


async def test_ignored_devices_step_unignores_in_entry_data_and_coordinator(
    hass, receiver_entry_builder, no_socket
):
    """Un-ignoring clears both stores, and takes effect without a reload.

    ``entry.data`` is what survives a restart and the coordinator's ``ignored``
    set is what the very next frame is routed against, so a step that updated
    only one of them would either forget the un-ignore on restart or make the
    user wait for a reload. Feeding the device's next transmission afterwards is
    what distinguishes the two.
    """
    entry = receiver_entry_builder(
        # The ignored device that was once adopted has a stored record, so its
        # row can be named; the other was ignored while pending and never had one.
        devices={
            PENDING_NEW: {
                CONF_MODEL: "EnergyMeter-2000",
                DEVICE_FIELDS: ["power_W"],
            }
        },
        ignored_devices=[PENDING_NEW, PENDING_MID],
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "ignored_devices"}
    )
    assert result["step_id"] == "ignored_devices"
    assert _select_options(result, "unignore") == [
        (PENDING_NEW, f"EnergyMeter-2000 ({PENDING_NEW})"),
        (PENDING_MID, PENDING_MID),
    ]

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"unignore": [PENDING_MID]}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY

    assert entry.data[CONF_IGNORED_DEVICES] == [PENDING_NEW]
    coordinator = _coordinator(hass, entry)
    assert coordinator.ignored == {PENDING_NEW}

    # Not retroactive, but live: the device is offered again from its next frame.
    assert coordinator.pending == {}
    _hear(coordinator, PENDING_MID_FRAME)
    await hass.async_block_till_done()
    assert set(coordinator.pending) == {PENDING_MID}


# --------------------------------------------------------------------------- #
# Options flow — receiver step.                                                     #
# --------------------------------------------------------------------------- #
async def test_receiver_options_step_persists_timeout(hass, receiver_entry_builder):
    """The receiver options step persists the availability timeout to options."""
    entry = receiver_entry_builder()
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "init"

    # Pick the receiver step from the menu.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "receiver"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "receiver"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_MANAGE_SETTINGS: False, CONF_AVAILABILITY_TIMEOUT: 120},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_MANAGE_SETTINGS] is False
    assert entry.options[CONF_AVAILABILITY_TIMEOUT] == 120


async def test_receiver_options_step_prefills_the_timeout_it_will_read_back(
    hass, receiver_entry_builder
):
    """The field opens on the stored timeout, or on the value that means "unset".

    This form has no way to say "no receiver timeout": the field is ``vol.Required``
    and must open on *a* number. So an entry with nothing stored opens on the
    same ``DEFAULT_AVAILABILITY_TIMEOUT`` the submit path reads back as "use the
    per-device-type defaults" — the round trip a user who only came to flip the
    other toggle takes, and it has to leave the entry as it found it. An entry
    that does have a timeout opens on that instead, whatever it is.
    """

    async def timeout_field_default(entry):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "receiver"}
        )
        keys = [
            key
            for key in result["data_schema"].schema
            if key == CONF_AVAILABILITY_TIMEOUT
        ]
        assert keys, "the receiver step has no availability-timeout field"
        return keys[0].default()

    unset = receiver_entry_builder()
    unset.add_to_hass(hass)
    assert await timeout_field_default(unset) == DEFAULT_AVAILABILITY_TIMEOUT

    # A stored timeout that is *not* the plain default, so the assertion cannot
    # pass on the fallback by accident.
    stored = receiver_entry_builder(availability_timeout=45)
    stored.add_to_hass(hass)
    assert await timeout_field_default(stored) == 45

    # And zero, which is a real stored choice that reads as falsy: a fallback
    # written with `or` rather than an `is None` test turns "never expire" back
    # into ten minutes here.
    never = receiver_entry_builder(availability_timeout=0)
    never.add_to_hass(hass)
    assert await timeout_field_default(never) == 0


async def test_receiver_options_step_drops_default_timeout(
    hass, receiver_entry_builder
):
    """Submitting the plain-default timeout does not persist it as an explicit receiver
    default.

    The availability-timeout field is pre-filled with ``DEFAULT_AVAILABILITY_TIMEOUT``,
    so a user who only toggles another setting and saves echoes that default back.
    Persisting it would mask the device-class defaults and wrongly expire
    event-driven devices (doorbells/motion/contacts), so the key is dropped — the
    entry carries no explicit receiver timeout and the per-device-type defaults apply.
    """
    entry = receiver_entry_builder()
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "receiver"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_MANAGE_SETTINGS: False,
            CONF_AVAILABILITY_TIMEOUT: DEFAULT_AVAILABILITY_TIMEOUT,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    # The deliberately-changed toggle is persisted...
    assert entry.options[CONF_MANAGE_SETTINGS] is False
    # ...but the untouched plain-default timeout is not, so class defaults apply.
    assert CONF_AVAILABILITY_TIMEOUT not in entry.options


# --------------------------------------------------------------------------- #
# Options flow — device step.                                                  #
# --------------------------------------------------------------------------- #
async def test_device_options_step_sets_and_clears_timeout_override(
    hass, receiver_entry_builder
):
    """The device step writes, then clears, a per-device timeout override."""
    device_key = "Acurite-606TX-42"
    entry = receiver_entry_builder(
        devices={
            device_key: {
                CONF_MODEL: "Acurite-606TX",
                DEVICE_FIELDS: ["temperature_C"],
            }
        }
    )
    entry.add_to_hass(hass)

    # Menu -> device step.
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "device"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "device"

    # Pick the device -> the per-device settings form.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"device": device_key}
    )
    assert result["step_id"] == "device_settings"

    # Set an override; it lands in entry.data["devices"], not entry.options.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {DEVICE_TIMEOUT_OVERRIDE: 90},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.data[CONF_DEVICES][device_key][DEVICE_TIMEOUT_OVERRIDE] == 90
    assert CONF_AVAILABILITY_TIMEOUT not in entry.options

    # Re-enter and submit with the override blank -> it is cleared.
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "device"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"device": device_key}
    )
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert DEVICE_TIMEOUT_OVERRIDE not in entry.data[CONF_DEVICES][device_key]


async def test_device_options_step_aborts_when_no_devices(hass, receiver_entry_builder):
    """With an empty devices map the device step aborts with no_devices."""
    entry = receiver_entry_builder()  # no devices seeded
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "device"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_devices"


# --------------------------------------------------------------------------- #
# Options flow — per-device calibration (device step -> calibration step).     #
# --------------------------------------------------------------------------- #
async def test_calibration_round_trip_writes_into_device_record(
    hass, receiver_entry_builder
):
    """A water calibration drives device -> calibration step and is persisted.

    Picking a real commodity on the device step advances to the calibration step;
    submitting ``{unit, scale}`` writes the ``{commodity, unit, scale}`` triple
    into ``entry.data[CONF_DEVICES][device_key]["calibration"]``.
    """
    device_key = "ERT-SCM-9001"
    entry = receiver_entry_builder(
        devices={
            device_key: {
                CONF_MODEL: "ERT-SCM",
                DEVICE_FIELDS: ["consumption_data"],
            }
        }
    )
    entry.add_to_hass(hass)

    # Menu -> device step.
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "device"}
    )
    assert result["step_id"] == "device"

    # Pick the device, then choose water -> advance to the calibration step
    # (no record written yet).
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"device": device_key}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CALIBRATION_COMMODITY: COMMODITY_WATER},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "calibration"

    # Submit a convertible volume unit + scale; the triple is persisted.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CALIBRATION_UNIT: UnitOfVolume.LITERS, CALIBRATION_SCALE: 0.1},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY

    calibration = entry.data[CONF_DEVICES][device_key][DEVICE_CALIBRATION]
    assert calibration == {
        CALIBRATION_COMMODITY: COMMODITY_WATER,
        CALIBRATION_UNIT: UnitOfVolume.LITERS,
        CALIBRATION_SCALE: 0.1,
    }
    # The timeout override is untouched (not part of this calibration).
    assert DEVICE_TIMEOUT_OVERRIDE not in entry.data[CONF_DEVICES][device_key]


async def test_device_step_none_commodity_finishes_without_calibration(
    hass, receiver_entry_builder
):
    """Commodity ``none`` writes the record (no calibration) and finishes."""
    device_key = "ERT-SCM-9001"
    entry = receiver_entry_builder(
        devices={
            device_key: {CONF_MODEL: "ERT-SCM", DEVICE_FIELDS: ["consumption_data"]}
        }
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "device"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"device": device_key}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CALIBRATION_COMMODITY: COMMODITY_NONE},
    )
    # No calibration step; finishes immediately with no calibration sub-record.
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert DEVICE_CALIBRATION not in entry.data[CONF_DEVICES][device_key]


# --------------------------------------------------------------------------- #
# Options flow — commodity pre-fill from the device's last decoded event.      #
# --------------------------------------------------------------------------- #
def _seed_coordinator_last_event(hass, entry, fields_by_device):
    """Stand a minimal coordinator with per-device last events into hass.data.

    The device-settings step reads ``coordinator.devices[device_key].fields`` to
    pre-fill the commodity, so a SimpleNamespace coordinator with the right shape
    is enough to exercise the pre-fill path without a full receiver setup.
    """
    coordinator = SimpleNamespace(
        devices={
            key: SimpleNamespace(fields=fields)
            for key, fields in fields_by_device.items()
        }
    )
    hass.data.setdefault(DOMAIN, {})[receiver_id(entry)] = coordinator


async def _open_device_step(hass, entry):
    """Open the options device picker step and return the shown form result."""
    result = await hass.config_entries.options.async_init(entry.entry_id)
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "device"}
    )


async def _open_device_settings(hass, entry, device_key):
    """Pick ``device_key`` and return the per-device settings form result."""
    result = await _open_device_step(hass, entry)
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {"device": device_key}
    )


async def test_commodity_prefill_from_meter_type_string(hass, receiver_entry_builder):
    """A last event with ``MeterType: "Gas"`` pre-fills the commodity to gas."""
    device_key = "IDM-1234"
    entry = receiver_entry_builder(
        devices={device_key: {CONF_MODEL: "IDM", DEVICE_FIELDS: ["consumption"]}}
    )
    entry.add_to_hass(hass)
    _seed_coordinator_last_event(hass, entry, {device_key: {"MeterType": "Gas"}})

    result = await _open_device_settings(hass, entry, device_key)
    assert result["step_id"] == "device_settings"
    assert _schema_default(result, CALIBRATION_COMMODITY) == COMMODITY_GAS


async def test_commodity_prefill_from_ert_type_low_nibble(hass, receiver_entry_builder):
    """An ``ert_type`` whose low nibble denotes gas pre-fills the commodity to gas.

    ``ert_type & 0x0f == 2`` is a gas commodity; 0x12 exercises that the high
    nibble is ignored.
    """
    device_key = "ERT-SCM-9001"
    entry = receiver_entry_builder(
        devices={
            device_key: {CONF_MODEL: "ERT-SCM", DEVICE_FIELDS: ["consumption_data"]}
        }
    )
    entry.add_to_hass(hass)
    _seed_coordinator_last_event(hass, entry, {device_key: {"ert_type": 0x12}})

    result = await _open_device_settings(hass, entry, device_key)
    assert _schema_default(result, CALIBRATION_COMMODITY) == COMMODITY_GAS


async def test_commodity_prefill_defaults_to_none_without_hint(
    hass, receiver_entry_builder
):
    """With no MeterType/ert_type hint, the commodity default stays ``none``."""
    device_key = "ERT-SCM-9001"
    entry = receiver_entry_builder(
        devices={
            device_key: {CONF_MODEL: "ERT-SCM", DEVICE_FIELDS: ["consumption_data"]}
        }
    )
    entry.add_to_hass(hass)
    _seed_coordinator_last_event(hass, entry, {device_key: {"consumption_data": 42}})

    result = await _open_device_settings(hass, entry, device_key)
    assert _schema_default(result, CALIBRATION_COMMODITY) == COMMODITY_NONE


async def test_commodity_prefill_reflects_selected_device_on_multi_device_receiver(
    hass, receiver_entry_builder
):
    """With several meters, the pre-fill follows the *selected* device.

    Regression test: the commodity default used to be derived only when the receiver
    had exactly one device, so a receiver with two SCMplus meters silently fell back
    to ``none`` and the per-device calibration looked like it did not apply.
    """
    gas_key, water_key = "SCMplus-1", "SCMplus-2"
    entry = receiver_entry_builder(
        devices={
            gas_key: {CONF_MODEL: "SCMplus", DEVICE_FIELDS: ["Consumption"]},
            water_key: {CONF_MODEL: "SCMplus", DEVICE_FIELDS: ["Consumption"]},
        }
    )
    entry.add_to_hass(hass)
    _seed_coordinator_last_event(
        hass,
        entry,
        {
            gas_key: {"MeterType": "Gas"},
            water_key: {"MeterType": "Water"},
        },
    )

    result = await _open_device_settings(hass, entry, gas_key)
    assert _schema_default(result, CALIBRATION_COMMODITY) == COMMODITY_GAS

    result = await _open_device_settings(hass, entry, water_key)
    assert _schema_default(result, CALIBRATION_COMMODITY) == COMMODITY_WATER


async def test_device_picker_labels_detected_commodity(hass, receiver_entry_builder):
    """The picker annotates a device whose commodity was detected from the signal.

    This is what makes the per-device calibration discoverable before the user
    has selected anything.
    """
    gas_key, plain_key = "SCMplus-1", "Acurite-1"
    entry = receiver_entry_builder(
        devices={
            gas_key: {CONF_MODEL: "SCMplus", DEVICE_FIELDS: ["Consumption"]},
            plain_key: {CONF_MODEL: "Acurite-606TX", DEVICE_FIELDS: ["temperature_C"]},
        }
    )
    entry.add_to_hass(hass)
    _seed_coordinator_last_event(
        hass, entry, {gas_key: {"MeterType": "Gas"}, plain_key: {"temperature_C": 21}}
    )

    result = await _open_device_step(hass, entry)
    labels = {
        option["value"]: option["label"]
        for option in result["data_schema"].schema["device"].config["options"]
    }
    assert "gas detected" in labels[gas_key]
    assert "detected" not in labels[plain_key]


# --------------------------------------------------------------------------- #
# Options flow — replace step (battery-swap re-key).                           #
# --------------------------------------------------------------------------- #
REPLACE_MODEL = "Acurite-986"
REPLACE_OLD = f"{REPLACE_MODEL}-1a2b"
REPLACE_NEW = f"{REPLACE_MODEL}-9f3c"


def _seen_models(hass, entry, models):
    """Stand a coordinator whose ``devices`` map reports a model per device key.

    ``_replacement_model`` falls back to the coordinator's last event when a key
    has no stored record (or one with a blank model), so the replace picker can
    still name a device the devices map does not describe.
    """
    hass.data.setdefault(DOMAIN, {})[receiver_id(entry)] = SimpleNamespace(
        devices={key: SimpleNamespace(model=model) for key, model in models.items()}
    )


async def test_replace_options_flow_rekeys_device(
    hass, receiver_entry_builder, no_socket
):
    """init -> replace -> replace_target re-keys the device and keeps entity_ids.

    The end-to-end flow assertion: the survivor's ``entity_id`` is untouched (so
    recorder history follows), the devices map is re-keyed with the fields
    unioned, and the flow finishes without clobbering ``entry.options`` — the
    helper has already written ``entry.data``.
    """
    entry = receiver_entry_builder(
        devices={
            REPLACE_OLD: {
                CONF_MODEL: REPLACE_MODEL,
                DEVICE_FIELDS: ["temperature_C"],
                DEVICE_TIMEOUT_OVERRIDE: 900,
            },
            REPLACE_NEW: {
                CONF_MODEL: REPLACE_MODEL,
                DEVICE_FIELDS: ["temperature_C", "battery_ok"],
            },
        },
        options={CONF_MANAGE_SETTINGS: True},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    ent_reg = er.async_get(hass)
    survivor = ent_reg.async_get_entity_id(
        "sensor", DOMAIN, f"{receiver_id(entry)}:{REPLACE_OLD}:T"
    )
    assert survivor is not None

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "replace"}
    )
    assert result["step_id"] == "replace"
    # Both known devices are offered, keyed and labelled from their own records.
    assert _select_options(result, "device") == [
        (REPLACE_OLD, f"{REPLACE_MODEL} ({REPLACE_OLD})"),
        (REPLACE_NEW, f"{REPLACE_MODEL} ({REPLACE_NEW})"),
    ]

    # Picking the survivor advances to the target picker, which excludes it and
    # names it in the prompt so the user can see which device they are keeping.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"device": REPLACE_OLD}
    )
    assert result["step_id"] == "replace_target"
    assert [value for value, _ in _select_options(result, "device")] == [REPLACE_NEW]
    assert result["description_placeholders"] == {
        "device": f"{REPLACE_MODEL} ({REPLACE_OLD})"
    }

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"device": REPLACE_NEW}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY

    # The row moved onto the new unique_id keeping its entity_id...
    assert (
        ent_reg.async_get_entity_id(
            "sensor", DOMAIN, f"{receiver_id(entry)}:{REPLACE_NEW}:T"
        )
        == survivor
    )
    assert (
        ent_reg.async_get_entity_id(
            "sensor", DOMAIN, f"{receiver_id(entry)}:{REPLACE_OLD}:T"
        )
        is None
    )
    # ...the map is re-keyed with the settings carried and the fields unioned...
    devices = entry.data[CONF_DEVICES]
    assert REPLACE_OLD not in devices
    assert devices[REPLACE_NEW][DEVICE_TIMEOUT_OVERRIDE] == 900
    assert devices[REPLACE_NEW][DEVICE_FIELDS] == ["battery_ok", "temperature_C"]
    # ...and options are handed back unchanged.
    assert entry.options == {CONF_MANAGE_SETTINGS: True}


async def test_replace_step_aborts_when_no_devices(hass, receiver_entry_builder):
    """With an empty devices map there is nothing to keep, so the step aborts."""
    entry = receiver_entry_builder()
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "replace"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_devices"


async def test_replace_target_aborts_without_candidates(hass, receiver_entry_builder):
    """A single-device receiver has nothing to adopt, so the target step aborts.

    Better than a dead-end dropdown: the only known device is the one being kept.
    """
    entry = receiver_entry_builder(
        devices={
            REPLACE_OLD: {CONF_MODEL: REPLACE_MODEL, DEVICE_FIELDS: ["temperature_C"]}
        }
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "replace"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"device": REPLACE_OLD}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_replacement_candidates"


async def test_replace_target_offers_unregistered_devices_same_model_first(
    hass, receiver_entry_builder
):
    """Candidates union the devices map with the coordinator's own device keys.

    The coordinator's runtime state is adopted-only, so the two normally agree —
    but a device adopted this session is in that state before its devices-map
    upsert lands, and the upsert is a no-op for a device whose event carried no
    storable fields, so a coordinator-only key must still be offered, labelled
    from its last event. A battery swap keeps the model, so same-model candidates
    sort first even when they sort last alphabetically, and a candidate whose
    model is unknown from both sources degrades to its bare key rather than an
    empty label.
    """
    # Deliberately alphabetically *before* the same-model candidate, so the
    # model-first ordering is distinguishable from a plain sort.
    other_key = "Aaa-Sensor-1"
    unknown_key = "Zz-Unknown-7"
    entry = receiver_entry_builder(
        devices={
            REPLACE_OLD: {CONF_MODEL: REPLACE_MODEL, DEVICE_FIELDS: ["temperature_C"]},
            other_key: {CONF_MODEL: "Aaa-Sensor", DEVICE_FIELDS: ["temperature_C"]},
            unknown_key: {CONF_MODEL: "", DEVICE_FIELDS: ["temperature_C"]},
        }
    )
    entry.add_to_hass(hass)
    # The replacement is in the coordinator's runtime state without a stored
    # record (adopted this session, upsert not yet landed).
    _seen_models(hass, entry, {REPLACE_NEW: REPLACE_MODEL})

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "replace"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"device": REPLACE_OLD}
    )
    assert result["step_id"] == "replace_target"

    assert _select_options(result, "device") == [
        (REPLACE_NEW, f"{REPLACE_MODEL} ({REPLACE_NEW})"),
        (other_key, f"Aaa-Sensor ({other_key})"),
        (unknown_key, unknown_key),
    ]


async def test_replace_target_offers_a_pending_device_and_consumes_it(
    hass, receiver_entry_builder, no_socket
):
    """A battery swap's new identity is *pending*, so replace must still offer it.

    This is the regression the approval flow introduces: a sensor that draws a
    new transmitter id when its batteries are changed is heard under that new id
    and nothing more — it is never added automatically — so a candidate set drawn
    only from the stored devices map and the coordinator's adopted runtime state
    would exclude the very device this step exists to adopt. The row is marked
    "not added yet" because picking it means something different from picking an
    adopted device: the user is folding a device they have never added onto the
    history of one they have.

    The other half of the regression is what is left behind afterwards. A replace
    reloads the entry, which rebuilds the coordinator and with it the in-memory
    pending list, so the adopted candidate must not linger as a stale "add me"
    offer sitting next to the device it was just merged into. The coordinator is
    re-fetched after the submit for exactly that reason: the object captured
    before the replace was discarded by the reload.
    """
    pending_key = "Acurite-986-9999"
    entry = receiver_entry_builder(
        availability_timeout=600,
        devices={
            REPLACE_OLD: {CONF_MODEL: REPLACE_MODEL, DEVICE_FIELDS: ["temperature_C"]}
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    ent_reg = er.async_get(hass)
    survivor = ent_reg.async_get_entity_id(
        "sensor", DOMAIN, f"{receiver_id(entry)}:{REPLACE_OLD}:T"
    )
    assert survivor is not None

    # The swapped sensor checks in under its new id: heard, not added.
    coordinator = _coordinator(hass, entry)
    _hear(coordinator, {"model": REPLACE_MODEL, "id": 9999, "temperature_C": 21.9})
    await hass.async_block_till_done()
    assert set(coordinator.pending) == {pending_key}
    assert _registry_device(hass, entry, pending_key) is None

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "replace"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"device": REPLACE_OLD}
    )
    assert result["step_id"] == "replace_target"

    # The pending device is the only candidate, described as the add step
    # describes it and then explicitly marked as not yet added.
    [(value, label)] = _select_options(result, "device")
    assert value == pending_key
    assert label.startswith(f"{REPLACE_MODEL} ({pending_key}) — seen 1x — last seen ")
    assert label.endswith(" — not added yet")

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"device": pending_key}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY

    # The kept device was re-keyed onto the new identity, history intact...
    assert set(entry.data[CONF_DEVICES]) == {pending_key}
    assert (
        ent_reg.async_get_entity_id(
            "sensor", DOMAIN, f"{receiver_id(entry)}:{pending_key}:T"
        )
        == survivor
    )
    # ...and the candidate is gone, not left offering itself for a second add.
    # The replace reloaded the entry, so this is a different coordinator object.
    assert _coordinator(hass, entry).pending == {}


async def test_replace_target_shows_error_when_the_picker_goes_stale(
    hass, receiver_entry_builder
):
    """A replace that can no longer run re-shows the form with ``replace_failed``.

    The options flow can sit open while the entry changes underneath it (another
    session finishing a replace, a reload); the device being kept is then gone
    from the map. That is a user-facing outcome, so it is rendered as a form
    error rather than escaping as a traceback.
    """
    entry = receiver_entry_builder(
        devices={
            REPLACE_OLD: {CONF_MODEL: REPLACE_MODEL, DEVICE_FIELDS: ["temperature_C"]},
            REPLACE_NEW: {CONF_MODEL: REPLACE_MODEL, DEVICE_FIELDS: ["temperature_C"]},
        }
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "replace"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"device": REPLACE_OLD}
    )
    assert result["step_id"] == "replace_target"

    # The device being kept disappears from the map while the form is open.
    hass.config_entries.async_update_entry(
        entry,
        data={
            **entry.data,
            CONF_DEVICES: {
                k: v for k, v in entry.data[CONF_DEVICES].items() if k != REPLACE_OLD
            },
        },
    )

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"device": REPLACE_NEW}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "replace_target"
    assert result["errors"] == {"base": "replace_failed"}
    # Nothing was written, and the now-recordless device still names itself.
    assert REPLACE_OLD not in entry.data[CONF_DEVICES]
    assert result["description_placeholders"] == {
        "device": f"{REPLACE_OLD} ({REPLACE_OLD})"
    }


# --------------------------------------------------------------------------- #
# async_remove_config_entry_device (direct unit test).                         #
# --------------------------------------------------------------------------- #
async def test_remove_receiver_device_is_refused(hass, receiver_entry_builder):
    """Removing a receiver device returns False (it belongs to its subentry)."""
    entry = receiver_entry_builder()
    entry.add_to_hass(hass)

    receiver_device = SimpleNamespace(identifiers={(DOMAIN, receiver_scope(entry))})

    assert await async_remove_config_entry_device(hass, entry, receiver_device) is False


async def test_remove_nested_device_evicts_map_and_coordinator(
    hass, receiver_entry_builder
):
    """Removing a nested device returns True and drops it from map + coordinator."""
    device_key = "Acurite-606TX-42"
    entry = receiver_entry_builder(
        devices={
            device_key: {CONF_MODEL: "Acurite-606TX", DEVICE_FIELDS: ["temperature_C"]}
        }
    )
    entry.add_to_hass(hass)

    # Stand in a fake coordinator so we can observe forget_device and the
    # per-platform device removers being called (both are the Clarification #4
    # re-add path: coordinator state eviction + platform dedup-cache pruning).
    forgotten: list[str] = []
    removed: list[str] = []
    coordinator = SimpleNamespace(
        forget_device=forgotten.append, device_removers=[removed.append]
    )
    hass.data.setdefault(DOMAIN, {})[receiver_id(entry)] = coordinator

    nested_device = SimpleNamespace(
        identifiers={(DOMAIN, f"{receiver_id(entry)}:{device_key}")}
    )

    assert await async_remove_config_entry_device(hass, entry, nested_device) is True
    # The device_key is gone from the receiver devices map...
    assert device_key not in entry.data.get(CONF_DEVICES, {})
    # ...and the coordinator was told to forget it, and the platform removers ran.
    assert forgotten == [device_key]
    assert removed == [device_key]


# --------------------------------------------------------------------------- #
# Reconfigure flow.                                                            #
# --------------------------------------------------------------------------- #
async def test_reconfigure_updates_data_and_preserves_devices(
    hass, receiver_entry_builder
):
    """A changed-and-reachable target updates entry.data in place.

    The same flow exercise proves the headline guarantees: aborts with
    ``reconfigure_successful``, host/port/path/secure are rewritten,
    ``manage_settings`` and the seeded ``data["devices"]`` map survive untouched,
    ``entry_id`` is stable, and the unique_id is reconciled to the new host:port.
    """
    device_key = "Acurite-606TX-42"
    seeded_devices = {
        device_key: {CONF_MODEL: "Acurite-606TX", DEVICE_FIELDS: ["temperature_C"]}
    }
    entry = receiver_entry_builder(
        host="old.local",
        port=8433,
        path="/ws",
        devices=seeded_devices,
    )
    entry.add_to_hass(hass)
    # manage_settings is owned by the options flow; stamp it on so we can assert
    # the reconfigure data_updates merge leaves it (and the devices map) intact.
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_MANAGE_SETTINGS: True}
    )

    original_entry_id = entry.entry_id
    devices_snapshot = deepcopy(entry.data[CONF_DEVICES])

    result = await entry.start_subentry_reconfigure_flow(hass, receiver_id(entry))
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"

    # Suppress the framework-scheduled reload so it does not try a real socket
    # setup; we only need to confirm the update + abort behaviour here.
    with (
        patch(VALIDATE, return_value=True),
        patch.object(hass.config_entries, "async_schedule_reload"),
    ):
        result = await hass.config_entries.subentries.async_configure(
            result["flow_id"],
            {
                CONF_HOST: "new.local",
                CONF_PORT: 9000,
                CONF_PATH: "/socket",
                "secure": True,
            },
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"

    # Connection params updated in place.
    assert receiver_subentry(entry).data[CONF_HOST] == "new.local"
    assert receiver_subentry(entry).data[CONF_PORT] == 9000
    assert receiver_subentry(entry).data[CONF_PATH] == "/socket"
    assert receiver_subentry(entry).data["secure"] is True

    # Same entry, preserved nested state and manage_settings (data_updates merge).
    assert entry.entry_id == original_entry_id
    assert entry.data[CONF_DEVICES] == devices_snapshot
    assert receiver_subentry(entry).data[CONF_MANAGE_SETTINGS] is True

    # unique_id reconciled to the new host:port, and the title follows the host.
    assert receiver_subentry(entry).unique_id == "hub:new.local:9000"
    assert receiver_subentry(entry).title == "rtl_433 (new.local)"


async def test_reconfigure_cannot_connect_keeps_form_and_data(
    hass, receiver_entry_builder
):
    """An unreachable target re-shows the form and leaves entry.data unchanged."""
    from custom_components.rtl_433.coordinator import CannotConnect

    entry = receiver_entry_builder(host="old.local", port=8433, path="/ws")
    entry.add_to_hass(hass)
    data_snapshot = deepcopy(dict(receiver_subentry(entry).data))
    unique_id_snapshot = receiver_subentry(entry).unique_id

    result = await entry.start_subentry_reconfigure_flow(hass, receiver_id(entry))
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"

    with patch(VALIDATE, side_effect=CannotConnect("nope")):
        result = await hass.config_entries.subentries.async_configure(
            result["flow_id"],
            {
                CONF_HOST: "unreachable",
                CONF_PORT: 9999,
                CONF_PATH: "/ws",
                "secure": False,
            },
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"
    assert result["errors"] == {"base": "cannot_connect"}

    # Nothing persisted.
    assert dict(receiver_subentry(entry).data) == data_snapshot
    assert receiver_subentry(entry).unique_id == unique_id_snapshot


async def test_reconfigure_collision_aborts_and_mutates_neither(
    hass, receiver_entry_builder
):
    """Reconfiguring one receiver onto another's host:port aborts as already_configured."""
    entry_a = receiver_entry_builder(host="a.local", port=8433, path="/ws")
    entry_b = receiver_entry_builder(host="b.local", port=9000, path="/ws")
    entry_a.add_to_hass(hass)
    entry_b.add_to_hass(hass)

    a_data_snapshot = deepcopy(dict(receiver_subentry(entry_a).data))
    a_unique_id_snapshot = receiver_subentry(entry_a).unique_id
    b_data_snapshot = deepcopy(dict(receiver_subentry(entry_b).data))
    b_unique_id_snapshot = receiver_subentry(entry_b).unique_id

    result = await entry_a.start_subentry_reconfigure_flow(hass, receiver_id(entry_a))
    assert result["step_id"] == "reconfigure"

    # Validation passes, but the new host:port collides with entry_b.
    with patch(VALIDATE, return_value=True):
        result = await hass.config_entries.subentries.async_configure(
            result["flow_id"],
            {
                CONF_HOST: "b.local",
                CONF_PORT: 9000,
                CONF_PATH: "/ws",
                "secure": False,
            },
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"

    # Neither entry's data changed.
    assert dict(receiver_subentry(entry_a).data) == a_data_snapshot
    assert receiver_subentry(entry_a).unique_id == a_unique_id_snapshot
    assert dict(receiver_subentry(entry_b).data) == b_data_snapshot
    assert receiver_subentry(entry_b).unique_id == b_unique_id_snapshot


async def test_reconfigure_reloads_entry_exactly_once(
    hass, receiver_entry_builder, no_socket, caplog
):
    """A successful reconfigure reloads the running receiver exactly once.

    The flow deliberately uses the *non*-reloading ``async_update_and_abort``:
    Home Assistant deprecated combining a config-entry update listener with the
    reloading flow helpers (it double-reloads and races; breaks in 2026.12). The
    write alone re-points the receiver — ``_async_update_listener`` sees the changed
    connection target and performs the single reload — and no deprecation report
    is logged.
    """
    entry = receiver_entry_builder(host="old.local", port=8433, path="/ws")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    with (
        patch(VALIDATE, return_value=True),
        patch.object(
            hass.config_entries, "async_reload", wraps=hass.config_entries.async_reload
        ) as reload_spy,
    ):
        result = await entry.start_subentry_reconfigure_flow(hass, receiver_id(entry))
        result = await hass.config_entries.subentries.async_configure(
            result["flow_id"],
            {
                CONF_HOST: "new.local",
                CONF_PORT: 9000,
                CONF_PATH: "/ws",
                "secure": False,
            },
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    reload_spy.assert_called_once_with(entry.entry_id)
    assert receiver_subentry(entry).data[CONF_HOST] == "new.local"
    assert receiver_subentry(entry).data[CONF_PORT] == 9000
    assert "should use it for scheduling a reload" not in caplog.text


# --------------------------------------------------------------------------- #
# Options flow — mappings step (per-receiver user-mapping overrides).               #
# --------------------------------------------------------------------------- #
async def test_mappings_step_invalid_submit_reshows_form_and_stores_nothing(
    hass, receiver_entry_builder
):
    """A schema-invalid mappings object re-shows the form and stores nothing."""
    from custom_components.rtl_433.const import CONF_USER_MAPPINGS

    entry = receiver_entry_builder()
    entry.add_to_hass(hass)
    data_snapshot = deepcopy(dict(entry.data))

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "mappings"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "mappings"

    # An entry missing the required ``platform`` is rejected by the validator.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_USER_MAPPINGS: {"bad_field": {"name": "X", "object_suffix": "X"}}},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "mappings"
    assert result["errors"]
    # Nothing was persisted: entry.data is unchanged (no CONF_USER_MAPPINGS).
    assert dict(entry.data) == data_snapshot
    assert CONF_USER_MAPPINGS not in entry.data


async def test_mappings_step_valid_submit_writes_data_leaves_options_and_devices(
    hass, receiver_entry_builder
):
    """A valid mappings object lands in entry.data, leaving options + devices intact."""
    from custom_components.rtl_433.const import CONF_USER_MAPPINGS

    device_key = "Acurite-606TX-42"
    entry = receiver_entry_builder(
        devices={
            device_key: {CONF_MODEL: "Acurite-606TX", DEVICE_FIELDS: ["temperature_C"]}
        },
        options={CONF_MANAGE_SETTINGS: True},
    )
    entry.add_to_hass(hass)
    options_snapshot = deepcopy(dict(entry.options))
    devices_snapshot = deepcopy(entry.data[CONF_DEVICES])

    # The update listener reloads the receiver when CONF_USER_MAPPINGS changes; the
    # entry is not actually set up here, so suppress the scheduled reload.
    with patch.object(hass.config_entries, "async_schedule_reload"):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "mappings"}
        )
        assert result["step_id"] == "mappings"

        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                CONF_USER_MAPPINGS: {
                    "temperature_C": {
                        "platform": "sensor",
                        "name": "Kelvin Temp",
                        "object_suffix": "K",
                        "unit_of_measurement": "K",
                    }
                }
            },
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    # The override was written (normalized) into entry.data.
    stored = entry.data[CONF_USER_MAPPINGS]
    assert stored["temperature_C"]["unit_of_measurement"] == "K"
    # Options and the devices map are untouched.
    assert dict(entry.options) == options_snapshot
    assert entry.data[CONF_DEVICES] == devices_snapshot


# --------------------------------------------------------------------------- #
# Supervisor (hassio) discovery flow.                                          #
# --------------------------------------------------------------------------- #
def _disc(host="core-rtl433", port=8433, uid="serial:0123"):
    """Build a Supervisor add-on discovery payload for one radio."""
    return HassioServiceInfo(
        config={
            CONF_HOST: host,
            CONF_PORT: port,
            CONF_PATH: "/ws",
            "secure": False,
            "unique_id": uid,
            "addon": "rtl_433",
        },
        name="rtl_433",
        slug="abc123",
        uuid="deadbeef",
    )


def _radio_entry(host="core-rtl433", port=8433, uid="serial:0123", devices=None):
    """Build a discovered-style receiver entry keyed by a stable radio unique_id.

    ``devices`` (when given) seeds ``data["devices"]`` so collision/orphan and
    rebind-preservation scenarios can assert the nested-device map survives (or
    that an empty-devices entry counts as an orphan).
    """
    data = {
        CONF_HOST: host,
        CONF_PORT: port,
        CONF_PATH: "/ws",
        "secure": False,
        CONF_MANAGE_SETTINGS: False,
    }
    # The adopted devices belong to the *location*, not to the receiver that
    # happened to decode them, so they are seeded on the entry.
    return build_location_entry(
        receiver_data=data,
        receiver_unique_id=uid,
        title=f"rtl_433 ({host})",
        receiver_title=f"rtl_433 ({host})",
        data={} if devices is None else {CONF_DEVICES: devices},
        version=2,
    )


def _flow_title_placeholders(hass):
    """Return the in-progress flow's context title_placeholders."""
    flow = next(iter(hass.config_entries.flow._progress.values()))
    return flow.context.get("title_placeholders")


async def test_hassio_discovery_happy_path_creates_entry(hass):
    """Discovery -> confirm form -> create entry keyed by the advertised radio id."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_HASSIO}, data=_disc()
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "hassio_confirm"
    # The confirm form surfaces exactly which add-on/radio it is.
    assert result["description_placeholders"] == {
        "addon": "rtl_433",
        "host": "core-rtl433",
        "port": "8433",
    }
    # The discovered card title is set from the add-on name and host:port.
    assert _flow_title_placeholders(hass) == {"name": "rtl_433 (core-rtl433:8433)"}

    with patch(VALIDATE, return_value=True) as validate:
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        await hass.async_block_till_done()

    # Connectivity is revalidated against the discovered target before adoption.
    validate.assert_called_once_with(hass, "core-rtl433", 8433, "/ws", secure=False)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry = result["result"]
    receiver = receiver_subentry(entry)
    # The stable radio id describes the *receiver*, so it keys the subentry; the
    # location it lands in has no hardware identity of its own.
    assert entry.unique_id is None
    assert receiver.unique_id == "serial:0123"
    assert entry.title == "rtl_433 (core-rtl433:8433)"
    assert receiver.title == "rtl_433 (core-rtl433:8433)"
    # Exact data shape: connection params + the default toggles. Submitting the
    # empty confirm form applies the schema defaults (manage-settings default +
    # the pre-filled 433.92 MHz initial frequency, which is persisted because
    # manage-settings is on).
    assert receiver.data == {
        CONF_HOST: "core-rtl433",
        CONF_PORT: 8433,
        CONF_PATH: "/ws",
        "secure": False,
        CONF_MANAGE_SETTINGS: DEFAULT_MANAGE_SETTINGS,
        CONF_INITIAL_FREQUENCY: DEFAULT_INITIAL_FREQUENCY,
    }


async def test_hassio_discovery_non_default_fields_propagate(hass):
    """A discovery carrying non-default path/secure/addon propagates them through."""
    disc = HassioServiceInfo(
        config={
            CONF_HOST: "core-rtl433",
            CONF_PORT: 8500,
            CONF_PATH: "/socket",
            "secure": True,
            "unique_id": "usbpath:1-1.4",
            "addon": "Custom rtl_433",
        },
        name="rtl_433",
        slug="abc123",
        uuid="deadbeef",
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_HASSIO}, data=disc
    )
    assert result["description_placeholders"] == {
        "addon": "Custom rtl_433",
        "host": "core-rtl433",
        "port": "8500",
    }

    with patch(VALIDATE, return_value=True) as validate:
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        await hass.async_block_till_done()

    validate.assert_called_once_with(hass, "core-rtl433", 8500, "/socket", secure=True)
    receiver = receiver_subentry(result["result"])
    assert receiver.unique_id == "usbpath:1-1.4"
    assert receiver.data[CONF_PATH] == "/socket"
    assert receiver.data["secure"] is True


async def test_hassio_discovery_missing_optional_fields_use_defaults(hass):
    """A discovery with only host/port/unique_id falls back to path/secure/addon defaults."""
    disc = HassioServiceInfo(
        config={CONF_HOST: "core-rtl433", CONF_PORT: 8433, "unique_id": "serial:0123"},
        name="rtl_433",
        slug="abc123",
        uuid="deadbeef",
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_HASSIO}, data=disc
    )
    # addon name falls back to "rtl_433" when the message omits it.
    assert result["description_placeholders"] == {
        "addon": "rtl_433",
        "host": "core-rtl433",
        "port": "8433",
    }

    with patch(VALIDATE, return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        await hass.async_block_till_done()

    receiver = receiver_subentry(result["result"])
    assert receiver.data[CONF_PATH] == "/ws"
    assert receiver.data["secure"] is False


async def test_hassio_discovery_missing_unique_id_aborts(hass):
    """A discovery message without a unique_id is rejected as malformed."""
    disc = HassioServiceInfo(
        config={CONF_HOST: "core-rtl433", CONF_PORT: 8433},
        name="rtl_433",
        slug="abc123",
        uuid="deadbeef",
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_HASSIO}, data=disc
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "invalid_discovery_info"


async def test_hassio_discovery_adopts_manual_entry(hass, receiver_entry_builder):
    """Discovery of a manually-added host:port re-keys it to the radio id and aborts."""
    entry = receiver_entry_builder(
        host="core-rtl433", port=8433, path="/ws", availability_timeout=42
    )
    entry.add_to_hass(hass)
    assert receiver_subentry(entry).unique_id == "hub:core-rtl433:8433"

    # Advertise a different path/secure so we can prove the connection data is
    # refreshed (not just the unique_id re-keyed) during adoption.
    disc = HassioServiceInfo(
        config={
            CONF_HOST: "core-rtl433",
            CONF_PORT: 8433,
            CONF_PATH: "/ws2",
            "secure": True,
            "unique_id": "serial:0123",
            "addon": "rtl_433",
        },
        name="rtl_433",
        slug="abc123",
        uuid="deadbeef",
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_HASSIO}, data=disc
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"

    entries = hass.config_entries.async_entries(DOMAIN)
    assert len(entries) == 1
    adopted = entries[0]
    adopted_receiver = receiver_subentry(adopted)
    assert adopted_receiver.unique_id == "serial:0123"
    # Connection data refreshed from discovery...
    assert adopted_receiver.data[CONF_PATH] == "/ws2"
    assert adopted_receiver.data["secure"] is True
    # ...while the location's own settings (its availability timeout) survive.
    assert adopted.data[CONF_AVAILABILITY_TIMEOUT] == 42


async def test_hassio_readvertisement_reloads_running_receiver(hass, no_socket, caplog):
    """A re-advertised radio on a new host reloads the running receiver, once.

    The discovery step passes ``reload_on_update=False`` (core scheduling a reload
    here *and* an update listener is the deprecated combination), so the reload is
    the update listener's, driven by the changed connection target.
    """
    entry = _radio_entry(host="core-rtl433", port=8433)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    with patch.object(
        hass.config_entries, "async_reload", wraps=hass.config_entries.async_reload
    ) as reload_spy:
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_HASSIO},
            data=_disc(host="core-rtl433-2", port=8500),
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    readvertised = receiver_subentry(entry)
    assert readvertised.data[CONF_HOST] == "core-rtl433-2"
    assert readvertised.data[CONF_PORT] == 8500
    reload_spy.assert_called_once_with(entry.entry_id)
    assert "should use it for scheduling a reload" not in caplog.text


async def test_hassio_discovery_does_not_rekey_populated_different_radio(hass):
    """A populated entry bound to another radio id on the same host:port is not re-keyed.

    Two radios sharing one host:port: discovery for a new stable id must NOT
    silently overwrite the existing radio's identity. It falls through to the
    guided replace step, leaving the existing entry's unique_id and devices
    intact.
    """
    existing = _radio_entry(uid="serial:AAA", devices={"Acurite-606TX-1": {}})
    existing.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_HASSIO}, data=_disc(uid="serial:BBB")
    )
    # Routed to the guided replace step, NOT a silent adopt/abort.
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "hassio_replace"

    # The existing radio's identity and devices are untouched.
    entries = hass.config_entries.async_entries(DOMAIN)
    assert len(entries) == 1
    assert receiver_subentry(entries[0]).unique_id == "serial:AAA"
    assert entries[0].data[CONF_DEVICES] == {"Acurite-606TX-1": {}}


async def test_hassio_discovery_adopt_aborts_on_unique_id_collision(
    hass, receiver_entry_builder
):
    """Adopting onto a stable id already owned by a populated entry creates no duplicate.

    A placeholder manual entry matches the discovered host:port, but another
    populated entry already owns the radio's stable id (on a different host:port).
    The adopt must abort without re-keying, so no two entries share a unique_id.
    """
    placeholder = receiver_entry_builder(host="core-rtl433", port=8433)
    placeholder.add_to_hass(hass)
    assert receiver_subentry(placeholder).unique_id == "hub:core-rtl433:8433"

    owner = _radio_entry(
        host="other-host", port=8433, uid="serial:0123", devices={"Acurite-606TX-1": {}}
    )
    owner.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_HASSIO},
        data=_disc(host="core-rtl433", port=8433, uid="serial:0123"),
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"

    # Placeholder NOT re-keyed (would collide); owner untouched; no duplicates.
    entries = hass.config_entries.async_entries(DOMAIN)
    assert sorted(receiver_subentry(e).unique_id for e in entries) == [
        "hub:core-rtl433:8433",
        "serial:0123",
    ]


async def test_hassio_discovery_distinct_port_can_become_new_radio(hass):
    """With a receiver present, an unknown radio offers replace; '__new__' adds it new.

    When a receiver already exists, discovery of an unknown radio on a distinct
    host:port routes to the guided ``hassio_replace`` step (the "replacement
    landed on a new host:port" case). Choosing ``__new__`` preserves the original
    behavior — the distinct-port unknown radio still becomes its own new entry.
    """
    entry = _radio_entry(host="core-rtl433", port=8433, uid="serial:0123")
    entry.add_to_hass(hass)

    # Same host, different port, different stable id -> no host:port match, but a
    # receiver already exists, so the flow offers the replace choice first.
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_HASSIO},
        data=_disc(port=9999, uid="serial:NEW"),
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "hassio_replace"

    # Choosing "__new__" advances to the confirm step (a genuinely new radio)...
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"replaces": "__new__"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "hassio_confirm"

    # ...and creates a second, distinct entry keyed by the new radio id.
    with patch(VALIDATE, return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert receiver_subentry(result["result"]).unique_id == "serial:NEW"
    entries = hass.config_entries.async_entries(DOMAIN)
    assert {receiver_subentry(e).unique_id for e in entries} == {
        "serial:0123",
        "serial:NEW",
    }


async def test_hassio_discovery_updates_changed_port_in_place(hass):
    """A re-advertised radio on a new port updates the stored connection and aborts."""
    entry = _radio_entry(port=8433)
    entry.add_to_hass(hass)

    # Same stable id, new port + path so we prove every field is updated.
    disc = HassioServiceInfo(
        config={
            CONF_HOST: "core-rtl433",
            CONF_PORT: 8434,
            CONF_PATH: "/ws9",
            "secure": True,
            "unique_id": "serial:0123",
            "addon": "rtl_433",
        },
        name="rtl_433",
        slug="abc123",
        uuid="deadbeef",
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_HASSIO}, data=disc
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"

    entries = hass.config_entries.async_entries(DOMAIN)
    assert len(entries) == 1
    updated = receiver_subentry(entries[0])
    assert updated.data[CONF_PORT] == 8434
    assert updated.data[CONF_PATH] == "/ws9"
    assert updated.data["secure"] is True


async def test_user_step_dedups_against_discovered_entry(hass):
    """The manual user step aborts when host:port is already owned by a radio entry."""
    entry = _radio_entry(host="core-rtl433", port=8433)
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    with patch(VALIDATE, return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_HOST: "core-rtl433",
                CONF_PORT: 8433,
                CONF_PATH: "/ws",
                "secure": False,
            },
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def test_reconfigure_preserves_stable_radio_unique_id(hass):
    """Reconfiguring a discovered entry keeps its stable radio id (not hub:...)."""
    entry = _radio_entry(host="core-rtl433", port=8433, uid="serial:0123")
    entry.add_to_hass(hass)

    result = await entry.start_subentry_reconfigure_flow(hass, receiver_id(entry))
    assert result["step_id"] == "reconfigure"

    with (
        patch(VALIDATE, return_value=True),
        patch.object(hass.config_entries, "async_schedule_reload"),
    ):
        result = await hass.config_entries.subentries.async_configure(
            result["flow_id"],
            {
                CONF_HOST: "new.local",
                CONF_PORT: 9000,
                CONF_PATH: "/socket",
                "secure": True,
            },
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    # Stable radio id preserved, not rewritten to hub:host:port.
    assert receiver_subentry(entry).unique_id == "serial:0123"
    assert receiver_subentry(entry).data[CONF_HOST] == "new.local"
    assert receiver_subentry(entry).data[CONF_PORT] == 9000
    assert receiver_subentry(entry).data[CONF_PATH] == "/socket"
    assert receiver_subentry(entry).data["secure"] is True


async def test_reconfigure_rebinds_to_new_radio_id_preserving_entry(hass):
    """Reconfiguring a discovered entry to a new radio_id rebinds it in place.

    The unique_id moves to the supplied radio_id, the host/port are updated, and
    the entry_id + the seeded ``data["devices"]`` map survive (the additive-only
    rebind preserves nested device/entity history).
    """
    devices = {
        "Acurite-606TX-42": {
            CONF_MODEL: "Acurite-606TX",
            DEVICE_FIELDS: ["temperature_C"],
        }
    }
    entry = _radio_entry(host="old.local", port=8433, uid="radio-old", devices=devices)
    entry.add_to_hass(hass)
    original_entry_id = entry.entry_id
    devices_snapshot = deepcopy(entry.data[CONF_DEVICES])

    result = await entry.start_subentry_reconfigure_flow(hass, receiver_id(entry))
    assert result["step_id"] == "reconfigure"

    with (
        patch(VALIDATE, return_value=True),
        patch.object(hass.config_entries, "async_reload"),
    ):
        result = await hass.config_entries.subentries.async_configure(
            result["flow_id"],
            {
                CONF_RADIO_ID: "radio-new",
                CONF_HOST: "new.local",
                CONF_PORT: 9000,
                CONF_PATH: "/ws",
                "secure": False,
            },
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"

    # Rebound to the new stable id, in place (same entry_id), devices preserved.
    assert receiver_subentry(entry).unique_id == "radio-new"
    assert entry.entry_id == original_entry_id
    assert entry.data[CONF_DEVICES] == devices_snapshot
    assert receiver_subentry(entry).data[CONF_HOST] == "new.local"
    assert receiver_subentry(entry).data[CONF_PORT] == 9000
    # Exactly one entry remains (no orphan/clone created).
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def test_reconfigure_rebind_collision_with_populated_entry_aborts(hass):
    """Rebinding onto a populated entry's id aborts already_configured, no change."""
    entry = _radio_entry(host="old.local", port=8433, uid="radio-old")
    other = _radio_entry(
        host="other.local",
        port=8433,
        uid="radio-new",
        devices={"Foo-1": {CONF_MODEL: "Foo", DEVICE_FIELDS: ["temperature_C"]}},
    )
    entry.add_to_hass(hass)
    other.add_to_hass(hass)

    entry_data_snapshot = deepcopy(dict(receiver_subentry(entry).data))
    other_data_snapshot = deepcopy(dict(receiver_subentry(other).data))

    result = await entry.start_subentry_reconfigure_flow(hass, receiver_id(entry))
    assert result["step_id"] == "reconfigure"

    with patch(VALIDATE, return_value=True):
        result = await hass.config_entries.subentries.async_configure(
            result["flow_id"],
            {
                CONF_RADIO_ID: "radio-new",
                CONF_HOST: "new.local",
                CONF_PORT: 9000,
                CONF_PATH: "/ws",
                "secure": False,
            },
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"

    # Nothing changed: both entries still present with their original ids + data.
    assert receiver_subentry(entry).unique_id == "radio-old"
    assert dict(receiver_subentry(entry).data) == entry_data_snapshot
    assert receiver_subentry(other).unique_id == "radio-new"
    assert dict(receiver_subentry(other).data) == other_data_snapshot
    assert len(hass.config_entries.async_entries(DOMAIN)) == 2


async def test_reconfigure_rebind_collision_with_empty_orphan_succeeds(hass):
    """Rebinding onto an empty-orphan entry deletes the orphan and rebinds."""
    devices = {"Foo-1": {CONF_MODEL: "Foo", DEVICE_FIELDS: ["temperature_C"]}}
    entry = _radio_entry(host="old.local", port=8433, uid="radio-old", devices=devices)
    # Orphan owns "radio-new" but has no devices (an auto-created duplicate).
    orphan = _radio_entry(host="other.local", port=8433, uid="radio-new")
    entry.add_to_hass(hass)
    orphan.add_to_hass(hass)
    orphan_id = orphan.entry_id
    devices_snapshot = deepcopy(entry.data[CONF_DEVICES])

    result = await entry.start_subentry_reconfigure_flow(hass, receiver_id(entry))
    assert result["step_id"] == "reconfigure"

    with (
        patch(VALIDATE, return_value=True),
        patch.object(hass.config_entries, "async_reload"),
    ):
        result = await hass.config_entries.subentries.async_configure(
            result["flow_id"],
            {
                CONF_RADIO_ID: "radio-new",
                CONF_HOST: "new.local",
                CONF_PORT: 9000,
                CONF_PATH: "/ws",
                "secure": False,
            },
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"

    # Orphan removed; the reconfigured entry took over "radio-new" with devices.
    assert hass.config_entries.async_get_entry(orphan_id) is None
    assert receiver_subentry(entry).unique_id == "radio-new"
    assert entry.data[CONF_DEVICES] == devices_snapshot
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def test_hassio_replace_rebinds_chosen_receiver(hass):
    """Discovery of an unknown radio while a receiver exists rebinds the chosen receiver.

    The replace step lists the existing receiver; submitting its entry_id rebinds that
    receiver onto the discovered radio id and connection target, in place.
    """
    devices = {"Foo-1": {CONF_MODEL: "Foo", DEVICE_FIELDS: ["temperature_C"]}}
    receiver = _radio_entry(
        host="old.local", port=8433, uid="radio-old", devices=devices
    )
    receiver.add_to_hass(hass)
    location_id = receiver.entry_id
    replaces = receiver_id(receiver)
    devices_snapshot = deepcopy(receiver.data[CONF_DEVICES])

    # An unknown radio on a distinct host:port -> the replace step is shown.
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_HASSIO},
        data=_disc(host="new.local", port=9000, uid="radio-new"),
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "hassio_replace"

    with (
        patch(VALIDATE, return_value=True),
        patch.object(hass.config_entries, "async_reload"),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"replaces": replaces}
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "rebind_successful"

    # The same receiver subentry was rebound onto the new radio + connection
    # target, inside the same location -- so its devices are untouched.
    rebound = hass.config_entries.async_get_entry(location_id)
    assert rebound is not None
    rebound_receiver = receiver_subentry(rebound)
    assert rebound_receiver.unique_id == "radio-new"
    assert rebound_receiver.data[CONF_HOST] == "new.local"
    assert rebound_receiver.data[CONF_PORT] == 9000
    assert rebound.data[CONF_DEVICES] == devices_snapshot
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def test_legacy_receiver_reconfigure_rebinds_via_host_port(
    hass, receiver_entry_builder
):
    """A legacy receiver: entry still rebinds its unique_id via host:port on reconfigure."""
    entry = receiver_entry_builder(host="old.local", port=8433, path="/ws")
    entry.add_to_hass(hass)
    assert receiver_subentry(entry).unique_id == "hub:old.local:8433"

    result = await entry.start_subentry_reconfigure_flow(hass, receiver_id(entry))
    assert result["step_id"] == "reconfigure"
    # No radio_id field is offered for a legacy receiver: entry.
    assert CONF_RADIO_ID not in _schema_keys(result)

    with (
        patch(VALIDATE, return_value=True),
        patch.object(hass.config_entries, "async_schedule_reload"),
    ):
        result = await hass.config_entries.subentries.async_configure(
            result["flow_id"],
            {
                CONF_HOST: "new.local",
                CONF_PORT: 9000,
                CONF_PATH: "/ws",
                "secure": False,
            },
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert receiver_subentry(entry).unique_id == "hub:new.local:9000"


async def test_hassio_confirm_cannot_connect_reshows_form(hass):
    """A failed validation on confirm re-shows the form with cannot_connect."""
    from custom_components.rtl_433.coordinator import CannotConnect

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_HASSIO}, data=_disc()
    )
    assert result["step_id"] == "hassio_confirm"

    with patch(VALIDATE, side_effect=CannotConnect("nope")):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "hassio_confirm"
    assert result["errors"] == {"base": "cannot_connect"}
    # The re-shown form keeps the addon/host/port context.
    assert result["description_placeholders"] == {
        "addon": "rtl_433",
        "host": "core-rtl433",
        "port": "8433",
    }


# --------------------------------------------------------------------------- #
# Setup toggles + initial frequency (manual user step and discovery confirm).  #
# --------------------------------------------------------------------------- #
async def test_user_step_persists_initial_frequency(hass):
    """A managed add with an explicit frequency persists it into entry.data."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )

    with patch(VALIDATE, return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_HOST: "rtl433.local",
                CONF_PORT: 8433,
                CONF_PATH: "/ws",
                "secure": False,
                CONF_MANAGE_SETTINGS: True,
                CONF_INITIAL_FREQUENCY: 868.3,
            },
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry = result["result"]
    receiver = receiver_subentry(entry)
    # The frequency rides the managed path; persisted as a float (MHz).
    assert receiver.data[CONF_INITIAL_FREQUENCY] == 868.3


async def test_user_step_applies_default_initial_frequency(hass):
    """Leaving the frequency field untouched seeds the pre-filled 433.92 MHz default."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )

    with patch(VALIDATE, return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_HOST: "rtl433.local",
                CONF_PORT: 8433,
                CONF_PATH: "/ws",
                "secure": False,
                CONF_MANAGE_SETTINGS: True,
            },
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry = result["result"]
    receiver = receiver_subentry(entry)
    assert receiver.data[CONF_INITIAL_FREQUENCY] == DEFAULT_INITIAL_FREQUENCY


async def test_user_step_drops_initial_frequency_when_unmanaged(hass):
    """A frequency entered with management off is not persisted (managed-only path)."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )

    with patch(VALIDATE, return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_HOST: "rtl433.local",
                CONF_PORT: 8433,
                CONF_PATH: "/ws",
                "secure": False,
                CONF_MANAGE_SETTINGS: False,
                CONF_INITIAL_FREQUENCY: 868.3,
            },
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry = result["result"]
    assert CONF_INITIAL_FREQUENCY not in entry.data


async def test_hassio_confirm_persists_toggles_and_frequency(hass):
    """The discovery confirm form persists manage-settings + frequency into entry.data."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_HASSIO}, data=_disc()
    )
    assert result["step_id"] == "hassio_confirm"

    with patch(VALIDATE, return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_MANAGE_SETTINGS: True,
                CONF_INITIAL_FREQUENCY: 915.0,
            },
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry = result["result"]
    receiver = receiver_subentry(entry)
    assert receiver.data[CONF_MANAGE_SETTINGS] is True
    assert receiver.data[CONF_INITIAL_FREQUENCY] == 915.0


# --------------------------------------------------------------------------- #
# Mutation coverage: rebind form contents, titles, and identity branches.      #
# --------------------------------------------------------------------------- #
def _select_options(result, key: str) -> list[tuple[str, str]]:
    """Return [(value, label), ...] for a SelectSelector field in a form schema."""
    for marker, validator in result["data_schema"].schema.items():
        if marker == key:
            return [(opt["value"], opt["label"]) for opt in validator.config["options"]]
    raise AssertionError(f"no select field {key!r} in schema")


async def test_hassio_replace_form_lists_receivers_and_new_option(hass):
    """The replace form offers each existing receiver (id->title) plus an 'add new' choice."""
    receiver = _radio_entry(host="old.local", port=8433, uid="radio-old")
    receiver.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_HASSIO},
        data=_disc(host="new.local", port=9000, uid="radio-new"),
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "hassio_replace"
    # Placeholders describe the *newly discovered* radio (addon/host/port as str).
    assert result["description_placeholders"] == {
        "addon": "rtl_433",
        "host": "new.local",
        "port": "9000",
    }
    # Options: each existing receiver keyed by its subentry id, labelled with the
    # location it sits in, then the new-radio choice.
    options = _select_options(result, "replaces")
    assert (
        receiver_id(receiver),
        "rtl_433 (old.local) / rtl_433 (old.local)",
    ) in options
    assert ("__new__", "It's a new radio") in options
    # The form defaults to "add as new" so a careless submit never rebinds a receiver.
    assert _schema_default(result, "replaces") == "__new__"


async def test_hassio_replace_rebind_sets_title_to_new_host(hass):
    """Rebinding via the replace step retitles the receiver to the new host."""
    receiver = _radio_entry(host="old.local", port=8433, uid="radio-old")
    receiver.add_to_hass(hass)
    location_id = receiver.entry_id
    replaces = receiver_id(receiver)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_HASSIO},
        data=_disc(host="new.local", port=9000, uid="radio-new"),
    )
    with (
        patch(VALIDATE, return_value=True),
        patch.object(hass.config_entries, "async_reload"),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"replaces": replaces}
        )
        await hass.async_block_till_done()

    rebound = receiver_subentry(hass.config_entries.async_get_entry(location_id))
    assert rebound.unique_id == "radio-new"
    assert rebound.title == "rtl_433 (new.local)"


async def test_reconfigure_connection_only_keeps_id_and_updates_title(hass):
    """An empty radio_id reconfigure updates the connection + title, keeps the id."""
    devices = {"Foo-1": {CONF_MODEL: "Foo", DEVICE_FIELDS: ["temperature_C"]}}
    entry = _radio_entry(
        host="old.local", port=8433, uid="serial:0123", devices=devices
    )
    entry.add_to_hass(hass)
    devices_snapshot = deepcopy(entry.data[CONF_DEVICES])

    result = await entry.start_subentry_reconfigure_flow(hass, receiver_id(entry))
    assert result["step_id"] == "reconfigure"

    with (
        patch(VALIDATE, return_value=True),
        patch.object(hass.config_entries, "async_schedule_reload"),
    ):
        result = await hass.config_entries.subentries.async_configure(
            result["flow_id"],
            {
                CONF_RADIO_ID: "",  # explicitly cleared -> connection-only update
                CONF_HOST: "new.local",
                CONF_PORT: 9000,
                CONF_PATH: "/ws",
                "secure": False,
            },
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    # Stable id untouched (no rebind), connection + title updated, devices kept.
    assert receiver_subentry(entry).unique_id == "serial:0123"
    assert receiver_subentry(entry).title == "rtl_433 (new.local)"
    assert receiver_subentry(entry).data[CONF_HOST] == "new.local"
    assert receiver_subentry(entry).data[CONF_PORT] == 9000
    assert entry.data[CONF_DEVICES] == devices_snapshot
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def test_reconfigure_rebind_strips_whitespace_and_sets_title(hass):
    """A padded radio_id is stripped before rebinding; the title follows the host."""
    entry = _radio_entry(host="old.local", port=8433, uid="radio-old")
    entry.add_to_hass(hass)

    result = await entry.start_subentry_reconfigure_flow(hass, receiver_id(entry))
    with (
        patch(VALIDATE, return_value=True),
        patch.object(hass.config_entries, "async_reload"),
    ):
        result = await hass.config_entries.subentries.async_configure(
            result["flow_id"],
            {
                CONF_RADIO_ID: "  radio-new  ",
                CONF_HOST: "new.local",
                CONF_PORT: 9000,
                CONF_PATH: "/ws",
                "secure": False,
            },
        )
        await hass.async_block_till_done()

    assert result["reason"] == "reconfigure_successful"
    assert receiver_subentry(entry).unique_id == "radio-new"
    assert receiver_subentry(entry).title == "rtl_433 (new.local)"


async def test_reconfigure_empty_unique_id_uses_legacy_receiver_scheme(hass):
    """An entry with no unique_id reconfigures via the legacy hub:host:port scheme."""
    entry = build_location_entry(
        receiver_data={
            CONF_HOST: "old.local",
            CONF_PORT: 8433,
            CONF_PATH: "/ws",
            "secure": False,
            CONF_MANAGE_SETTINGS: False,
        },
        receiver_unique_id=None,
        title="rtl_433 (old.local)",
        receiver_title="rtl_433 (old.local)",
        version=2,
    )
    entry.add_to_hass(hass)

    result = await entry.start_subentry_reconfigure_flow(hass, receiver_id(entry))
    assert result["step_id"] == "reconfigure"
    # No stable radio id -> no radio_id field offered.
    assert CONF_RADIO_ID not in _schema_keys(result)

    with (
        patch(VALIDATE, return_value=True),
        patch.object(hass.config_entries, "async_schedule_reload"),
    ):
        result = await hass.config_entries.subentries.async_configure(
            result["flow_id"],
            {
                CONF_HOST: "new.local",
                CONF_PORT: 9000,
                CONF_PATH: "/ws",
                "secure": False,
            },
        )
        await hass.async_block_till_done()

    assert result["reason"] == "reconfigure_successful"
    assert receiver_subentry(entry).unique_id == "hub:new.local:9000"


# --------------------------------------------------------------------------- #
# Receiver subentry flow — adding a second receiver to a location.             #
# --------------------------------------------------------------------------- #
async def _add_receiver(hass, entry, **overrides):
    """Drive the receiver subentry's add flow and return the result."""
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "receiver"), context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    user_input = {
        CONF_HOST: "garage.local",
        CONF_PORT: 8433,
        CONF_PATH: "/ws",
        "secure": False,
        CONF_MANAGE_SETTINGS: True,
    }
    user_input.update(overrides)
    return await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input
    )


async def test_subentry_flow_adds_a_second_receiver_to_a_location(
    hass, receiver_entry_builder
):
    """Adding a receiver is a subentry of the location, not a second entry.

    This is the primary path into the device union: both servers feed one config
    entry, which is what lets their views of a sensor be merged later.
    """
    entry = receiver_entry_builder(host="attic.local")
    entry.add_to_hass(hass)

    with patch(VALIDATE, return_value=True):
        result = await _add_receiver(hass, entry)
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1
    receivers = [
        subentry
        for subentry in entry.subentries.values()
        if subentry.subentry_type == "receiver"
    ]
    assert [r.data[CONF_HOST] for r in receivers] == ["attic.local", "garage.local"]
    assert receivers[1].unique_id == "hub:garage.local:8433"
    assert receivers[1].title == "rtl_433 (garage.local)"


async def test_subentry_flow_refuses_a_server_already_configured(
    hass, receiver_entry_builder
):
    """The same endpoint cannot be added twice, even into a different location.

    Home Assistant only enforces subentry unique_ids within one entry, so the
    guard scans every location: two coordinators against one server would be two
    sockets reading the same frames.
    """
    entry = receiver_entry_builder(host="attic.local")
    entry.add_to_hass(hass)
    other = receiver_entry_builder(host="garage.local")
    other.add_to_hass(hass)

    with patch(VALIDATE, return_value=True):
        result = await _add_receiver(hass, entry)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_subentry_flow_reports_an_unreachable_server(
    hass, receiver_entry_builder
):
    """A failed connection re-shows the form rather than adding a dead receiver."""
    from custom_components.rtl_433.coordinator import CannotConnect

    entry = receiver_entry_builder(host="attic.local")
    entry.add_to_hass(hass)

    with patch(VALIDATE, side_effect=CannotConnect("nope")):
        result = await _add_receiver(hass, entry)

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}
    assert len(entry.subentries) == 1


async def test_subentry_flow_offers_the_whole_connection_form(
    hass, receiver_entry_builder
):
    """The add-a-receiver form asks for everything a receiver needs.

    A form shown with no schema would submit whatever the caller happened to
    send, so the fields are asserted rather than the fact that a form appeared.
    """
    entry = receiver_entry_builder(host="attic.local")
    entry.add_to_hass(hass)

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "receiver"), context={"source": SOURCE_USER}
    )
    assert _schema_keys(result) == {
        CONF_HOST,
        CONF_PORT,
        CONF_PATH,
        "secure",
        CONF_MANAGE_SETTINGS,
        CONF_INITIAL_FREQUENCY,
    }


async def test_subentry_flow_stores_every_connection_field_it_was_given(
    hass, receiver_entry_builder
):
    """The added receiver records the exact endpoint and radio policy typed in.

    The stored subentry data is what the coordinator dials and what the settings
    manager reads on the next start, so each field is asserted by value; a
    receiver that silently kept a default would connect somewhere else.
    """
    entry = receiver_entry_builder(host="attic.local")
    entry.add_to_hass(hass)

    with patch(VALIDATE, return_value=True):
        result = await _add_receiver(
            hass,
            entry,
            **{
                CONF_PORT: 9443,
                CONF_PATH: "/stream",
                "secure": True,
                CONF_MANAGE_SETTINGS: True,
                CONF_INITIAL_FREQUENCY: 915000000.0,
            },
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    added = receiver_subentry(entry, 1)
    assert added.data == {
        CONF_HOST: "garage.local",
        CONF_PORT: 9443,
        CONF_PATH: "/stream",
        "secure": True,
        CONF_MANAGE_SETTINGS: True,
        CONF_INITIAL_FREQUENCY: 915000000.0,
    }
    assert added.unique_id == "hub:garage.local:9443"


async def test_subentry_flow_stores_no_frequency_when_not_managing_the_radio(
    hass, receiver_entry_builder
):
    """An initial frequency is only persisted when Home Assistant drives the radio.

    The frequency rides the managed desired-state path; storing one for an
    unmanaged receiver would promise a tuning that nothing ever applies.
    """
    entry = receiver_entry_builder(host="attic.local")
    entry.add_to_hass(hass)

    with patch(VALIDATE, return_value=True):
        await _add_receiver(
            hass,
            entry,
            **{
                CONF_MANAGE_SETTINGS: False,
                CONF_INITIAL_FREQUENCY: 915000000.0,
            },
        )
        await hass.async_block_till_done()

    added = receiver_subentry(entry, 1)
    assert added.data[CONF_MANAGE_SETTINGS] is False
    assert CONF_INITIAL_FREQUENCY not in added.data


async def test_subentry_flow_probes_the_server_the_user_typed(
    hass, receiver_entry_builder
):
    """The reachability check dials the typed endpoint, not some other one.

    Probing the wrong target would either add a receiver nobody verified or
    refuse a healthy one, and both look identical from the form.
    """
    entry = receiver_entry_builder(host="attic.local")
    entry.add_to_hass(hass)

    probed: list[tuple] = []

    async def _probe(probe_hass, host, port, path, *, secure):
        probed.append((probe_hass is hass, host, port, path, secure))
        return True

    with patch(VALIDATE, new=_probe):
        result = await _add_receiver(
            hass,
            entry,
            **{CONF_PORT: 9443, CONF_PATH: "/stream", "secure": True},
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert probed == [(True, "garage.local", 9443, "/stream", True)]


async def test_subentry_flow_refuses_a_server_already_keyed_by_its_radio_id(
    hass, receiver_entry_builder
):
    """A discovered receiver blocks a manual add of the same endpoint.

    Its unique_id is the radio's stable id, so only the host:port half of the
    guard can recognise it -- and without that half a second coordinator would
    open a second socket onto the same server.
    """
    entry = receiver_entry_builder(host="attic.local")
    entry.add_to_hass(hass)
    discovered = _radio_entry(host="garage.local", port=8433, uid="serial:0123")
    discovered.add_to_hass(hass)

    with patch(VALIDATE, return_value=True):
        result = await _add_receiver(hass, entry)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert len(entry.subentries) == 1


async def test_discovery_rekeys_a_manual_receiver_that_already_has_devices(hass):
    """A placeholder identity is adopted however many devices the location holds.

    ``hub:host:port`` means "added by hand, awaiting a stable radio id" -- it is
    not a claim about a *different* radio, so adoption is safe even once the
    location has adopted sensors. Only a receiver already bound to another stable
    id is left alone.
    """
    entry = build_location_entry(
        receiver_data={
            CONF_HOST: "core-rtl433",
            CONF_PORT: 8433,
            CONF_PATH: "/ws",
            "secure": False,
            CONF_MANAGE_SETTINGS: False,
        },
        receiver_unique_id="hub:core-rtl433:8433",
        data={CONF_DEVICES: {"Acurite-606TX-1": {CONF_MODEL: "Acurite-606TX"}}},
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_HASSIO}, data=_disc(uid="serial:0123")
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1
    assert receiver_subentry(entry).unique_id == "serial:0123"
    # The adopted device is untouched by the re-key.
    assert entry.data[CONF_DEVICES] == {
        "Acurite-606TX-1": {CONF_MODEL: "Acurite-606TX"}
    }


async def test_hassio_confirm_offers_only_this_integrations_locations(hass):
    """The "which location?" choices never include another integration's entries.

    The list is built by scanning config entries, and an unscoped scan would
    offer to attach an rtl_433 receiver to, say, a Hue bridge.
    """
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    mine = _radio_entry(host="old.local", port=8433, uid="radio-old")
    mine.add_to_hass(hass)
    stranger = MockConfigEntry(domain="hue", title="Hue Bridge")
    stranger.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_HASSIO},
        data=_disc(host="new.local", port=9000, uid="radio-new"),
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"replaces": "__new__"}
    )
    options = _select_options(result, "location")
    assert ("__new__", "A new location") in options
    assert (mine.entry_id, mine.title) in options
    assert stranger.entry_id not in {value for value, _ in options}


async def test_hassio_confirm_defaults_to_a_new_location(hass):
    """Discovery offers the existing locations but defaults to a new one.

    A discovered server is usually a second site, and merging two sites' devices
    is the one outcome a user cannot undo by editing a form.
    """
    existing = _radio_entry(host="old.local", port=8433, uid="radio-old")
    existing.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_HASSIO},
        data=_disc(host="new.local", port=9000, uid="radio-new"),
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"replaces": "__new__"}
    )
    assert result["step_id"] == "hassio_confirm"
    options = _select_options(result, "location")
    assert ("__new__", "A new location") in options
    assert (existing.entry_id, existing.title) in options
    assert _schema_default(result, "location") == "__new__"


async def test_hassio_confirm_can_attach_to_an_existing_location(hass):
    """Choosing a location adds the discovered server to it as a receiver.

    That is the opt-in: the receiver now feeds the same entry as its neighbour,
    which is what puts their devices in one place.
    """
    existing = _radio_entry(host="old.local", port=8433, uid="radio-old")
    existing.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_HASSIO},
        data=_disc(host="new.local", port=9000, uid="radio-new"),
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"replaces": "__new__"}
    )
    with patch(VALIDATE, return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"location": existing.entry_id}
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "receiver_added"
    # One location, two receivers -- no second config entry was created.
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1
    receivers = [
        subentry
        for subentry in existing.subentries.values()
        if subentry.subentry_type == "receiver"
    ]
    assert [r.unique_id for r in receivers] == ["radio-old", "radio-new"]
    assert receivers[1].data[CONF_HOST] == "new.local"
