"""Tests for never-expire (0) and device-class-aware default timeout resolution.

These exercise the three-tier resolution order wired in production:

* per-device ``timeout_override`` (``entry.data[CONF_DEVICES][key]``),
* an *explicit* hub ``availability_timeout`` (membership, so ``0`` counts), and
* the device-class default (:func:`class_default_timeout`) from the device's
  latest cached payload when neither explicit tier is set.

The integration-first tests run the real :func:`async_setup_entry` so the actual
``effective_timeout_resolver`` closure is wired onto the coordinator (rather than
stubbing a resolver), then drive behaviour through the public
``coordinator._effective_timeout`` / ``_async_watchdog`` / ``entity.available``.
Time travel uses ``freeze_time`` like the existing watchdog tests.
"""

from __future__ import annotations

from datetime import timedelta
import json
from unittest.mock import patch

from freezegun import freeze_time
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.rtl_433.const import (
    AVAILABILITY_TIMEOUT_NEVER,
    CONF_AVAILABILITY_TIMEOUT,
    CONF_MODEL,
    DEFAULT_AVAILABILITY_TIMEOUT,
    DEFAULT_EVENT_DEVICE_TIMEOUT,
    DEVICE_FIELDS,
    DEVICE_TIMEOUT_OVERRIDE,
    DOMAIN,
    class_default_timeout,
)
from custom_components.rtl_433.coordinator import Rtl433Coordinator
from custom_components.rtl_433.mapping import FieldDescriptor
from custom_components.rtl_433.sensor import Rtl433Sensor
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util


@pytest.fixture(autouse=True)
def _no_socket():
    """Stub the connect loop so the real setup never opens a WebSocket."""

    async def _noop(self) -> None:
        return None

    with patch.object(Rtl433Coordinator, "_connect_loop", _noop):
        yield


def _coordinator(hass, hub: MockConfigEntry) -> Rtl433Coordinator:
    return hass.data[DOMAIN][hub.entry_id]


def _feed(coordinator: Rtl433Coordinator, event: dict) -> None:
    """Push one event dict through the coordinator's frame handler."""
    coordinator._handle_text_frame(json.dumps(event))


async def _setup(hass, hub: MockConfigEntry) -> Rtl433Coordinator:
    """Run the real setup (wiring the production resolver) and return the coord."""
    hub.add_to_hass(hass)
    assert await hass.config_entries.async_setup(hub.entry_id)
    await hass.async_block_till_done()
    return _coordinator(hass, hub)


# ---------------------------------------------------------------------------
# Pure classifier (cheap unit coverage)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"motion": 1}, DEFAULT_EVENT_DEVICE_TIMEOUT),
        ({"contact": 0, "battery_ok": 1}, DEFAULT_EVENT_DEVICE_TIMEOUT),
        ({"door": 1}, DEFAULT_EVENT_DEVICE_TIMEOUT),
        ({"temperature_C": 21.5, "humidity": 50}, DEFAULT_AVAILABILITY_TIMEOUT),
        ({}, DEFAULT_AVAILABILITY_TIMEOUT),
        (None, DEFAULT_AVAILABILITY_TIMEOUT),
    ],
)
def test_class_default_timeout_classifier(payload, expected):
    """Event-driven payloads get 7200; periodic / empty / missing get 600."""
    assert class_default_timeout(payload) == expected


# ---------------------------------------------------------------------------
# Never-expire (timeout == 0)
# ---------------------------------------------------------------------------


async def test_never_expire_via_device_override(hass, hub_entry_builder):
    """A per-device override of 0 means the device never goes unavailable.

    With an old ``last_seen`` the watchdog must NOT flip it and the entity's
    ``available`` property must read True (it was seen at least once).
    """
    device_key = "EnergyMeter-2000-1234"
    hub = hub_entry_builder(
        availability_timeout=600,  # hub default is short, but the override wins
        devices={
            device_key: {
                CONF_MODEL: "EnergyMeter-2000",
                DEVICE_FIELDS: ["power_W"],
                DEVICE_TIMEOUT_OVERRIDE: AVAILABILITY_TIMEOUT_NEVER,
            }
        },
    )
    coordinator = await _setup(hass, hub)
    ent_reg = er.async_get(hass)
    watts_eid = ent_reg.async_get_entity_id(
        "sensor", DOMAIN, f"{hub.entry_id}:{device_key}:watts"
    )
    assert watts_eid is not None

    assert coordinator._effective_timeout(device_key) == 0

    start = dt_util.utcnow()
    with freeze_time(start):
        _feed(coordinator, {"model": "EnergyMeter-2000", "id": 1234, "power_W": 5.0})
        await hass.async_block_till_done()

    # Far past any normal timeout: watchdog leaves it available.
    with freeze_time(start + timedelta(days=30)):
        await coordinator._async_watchdog(dt_util.utcnow())
        await hass.async_block_till_done()
        assert coordinator.available[device_key] is True
        assert hass.states.get(watts_eid).state != "unavailable"


async def test_never_expire_via_explicit_hub_default(hass, hub_entry_builder):
    """An explicit hub ``availability_timeout`` of 0 never-expires a plain device."""
    device_key = "EnergyMeter-2000-1234"
    hub = hub_entry_builder(
        availability_timeout=AVAILABILITY_TIMEOUT_NEVER,
        devices={
            device_key: {
                CONF_MODEL: "EnergyMeter-2000",
                DEVICE_FIELDS: ["power_W"],
            }
        },
    )
    coordinator = await _setup(hass, hub)
    ent_reg = er.async_get(hass)
    watts_eid = ent_reg.async_get_entity_id(
        "sensor", DOMAIN, f"{hub.entry_id}:{device_key}:watts"
    )

    assert coordinator._effective_timeout(device_key) == 0

    start = dt_util.utcnow()
    with freeze_time(start):
        _feed(coordinator, {"model": "EnergyMeter-2000", "id": 1234, "power_W": 5.0})
        await hass.async_block_till_done()

    with freeze_time(start + timedelta(days=30)):
        await coordinator._async_watchdog(dt_util.utcnow())
        await hass.async_block_till_done()
        assert coordinator.available[device_key] is True
        assert hass.states.get(watts_eid).state != "unavailable"


# ---------------------------------------------------------------------------
# Device-class defaults (no explicit override / no explicit hub default)
# ---------------------------------------------------------------------------


async def test_class_default_event_driven_device(hass, hub_entry_builder):
    """An event-driven payload resolves to 7200: silent at 1h, gone past 2h.

    No per-device override and no explicit hub default, so the device-class
    default applies. Under the old flat 600s default this device would have been
    wrongly unavailable at 700s.
    """
    device_key = "GS-kw9c-7"
    # availability_timeout=None -> CONF_AVAILABILITY_TIMEOUT is NOT in the entry,
    # so the resolver returns None and the class default kicks in.
    hub = hub_entry_builder(
        availability_timeout=None,
        devices={
            device_key: {
                CONF_MODEL: "GS-kw9c",
                DEVICE_FIELDS: ["contact"],
            }
        },
    )
    assert CONF_AVAILABILITY_TIMEOUT not in hub.data
    coordinator = await _setup(hass, hub)

    start = dt_util.utcnow()
    with freeze_time(start):
        # An open/close payload -> event-driven class.
        _feed(coordinator, {"model": "GS-kw9c", "id": 7, "contact": 0})
        await hass.async_block_till_done()

    assert coordinator._effective_timeout(device_key) == DEFAULT_EVENT_DEVICE_TIMEOUT

    # 700s of silence (> old 600s default but < 7200s) -> still available.
    with freeze_time(start + timedelta(seconds=700)):
        await coordinator._async_watchdog(dt_util.utcnow())
        await hass.async_block_till_done()
        assert coordinator.available[device_key] is True

    # Past 7200s -> watchdog flips it unavailable.
    with freeze_time(start + timedelta(seconds=7201)):
        await coordinator._async_watchdog(dt_util.utcnow())
        await hass.async_block_till_done()
        assert coordinator.available[device_key] is False


async def test_class_default_periodic_device(hass, hub_entry_builder):
    """A periodic payload resolves to 600 and goes unavailable past 600s."""
    device_key = "Acurite-606TX-42"
    hub = hub_entry_builder(
        availability_timeout=None,
        devices={
            device_key: {
                CONF_MODEL: "Acurite-606TX",
                DEVICE_FIELDS: ["temperature_C"],
            }
        },
    )
    assert CONF_AVAILABILITY_TIMEOUT not in hub.data
    coordinator = await _setup(hass, hub)

    start = dt_util.utcnow()
    with freeze_time(start):
        _feed(
            coordinator,
            {"model": "Acurite-606TX", "id": 42, "temperature_C": 21.5, "humidity": 50},
        )
        await hass.async_block_till_done()

    assert coordinator._effective_timeout(device_key) == DEFAULT_AVAILABILITY_TIMEOUT

    # One second past 600s -> unavailable.
    with freeze_time(start + timedelta(seconds=601)):
        await coordinator._async_watchdog(dt_util.utcnow())
        await hass.async_block_till_done()
        assert coordinator.available[device_key] is False


# ---------------------------------------------------------------------------
# Resolution precedence
# ---------------------------------------------------------------------------


async def test_precedence_override_beats_hub_and_class(hass, hub_entry_builder):
    """A per-device override wins over both the explicit hub default and class.

    The device is event-driven (class default would be 7200) and the hub sets an
    explicit 300, but the per-device override of 120 must win.
    """
    device_key = "GS-kw9c-7"
    hub = hub_entry_builder(
        availability_timeout=300,
        devices={
            device_key: {
                CONF_MODEL: "GS-kw9c",
                DEVICE_FIELDS: ["contact"],
                DEVICE_TIMEOUT_OVERRIDE: 120,
            }
        },
    )
    coordinator = await _setup(hass, hub)

    start = dt_util.utcnow()
    with freeze_time(start):
        _feed(coordinator, {"model": "GS-kw9c", "id": 7, "contact": 1})
        await hass.async_block_till_done()

    assert coordinator._effective_timeout(device_key) == 120

    # 121s of silence exceeds the 120s override (but not 300 or 7200).
    with freeze_time(start + timedelta(seconds=121)):
        await coordinator._async_watchdog(dt_util.utcnow())
        await hass.async_block_till_done()
        assert coordinator.available[device_key] is False


async def test_precedence_explicit_hub_number_beats_class(hass, hub_entry_builder):
    """An explicit hub number wins over the class default for an event device.

    Hub=300 -> the event-driven device uses 300, not the 7200 class default.
    """
    device_key = "GS-kw9c-7"
    hub = hub_entry_builder(
        availability_timeout=300,
        devices={
            device_key: {
                CONF_MODEL: "GS-kw9c",
                DEVICE_FIELDS: ["contact"],
            }
        },
    )
    coordinator = await _setup(hass, hub)

    start = dt_util.utcnow()
    with freeze_time(start):
        _feed(coordinator, {"model": "GS-kw9c", "id": 7, "contact": 0})
        await hass.async_block_till_done()

    assert coordinator._effective_timeout(device_key) == 300

    # 301s -> unavailable (would still be available under the 7200 class default).
    with freeze_time(start + timedelta(seconds=301)):
        await coordinator._async_watchdog(dt_util.utcnow())
        await hass.async_block_till_done()
        assert coordinator.available[device_key] is False


async def test_never_seen_is_unavailable_even_when_timeout_zero(
    hass, hub_entry_builder
):
    """``last_seen is None`` reads unavailable even with a never-expire timeout.

    The entity's ``available`` property short-circuits on the missing
    ``last_seen`` BEFORE the never-expire (timeout == 0) shortcut, so a device
    that was configured but has never transmitted reads unavailable.
    """
    device_key = "EnergyMeter-2000-1234"
    hub = hub_entry_builder(
        availability_timeout=AVAILABILITY_TIMEOUT_NEVER,
        devices={
            device_key: {
                CONF_MODEL: "EnergyMeter-2000",
                DEVICE_FIELDS: ["power_W"],
            }
        },
    )
    coordinator = await _setup(hass, hub)

    # Model "never transmitted": ensure no last_seen baseline for this device.
    coordinator.last_seen.pop(device_key, None)
    coordinator.available.pop(device_key, None)

    # Never-expire is in effect for a *seen* device...
    assert coordinator._effective_timeout(device_key) == 0

    # ...but this entity has never been seen, so available is False regardless.
    sensor = Rtl433Sensor(
        coordinator,
        hub.entry_id,
        device_key,
        "EnergyMeter-2000",
        FieldDescriptor(
            field_key="power_W",
            object_suffix="watts",
            name="Power",
            platform="sensor",
        ),
    )
    assert device_key not in coordinator.last_seen
    assert sensor.available is False

    # Sanity: once seen, the same entity with a 0 timeout is available again.
    coordinator.last_seen[device_key] = dt_util.utcnow()
    assert sensor.available is True


# ---------------------------------------------------------------------------
# Edge: no cached payload
# ---------------------------------------------------------------------------


async def test_no_cached_payload_falls_back_to_periodic_default(
    hass, hub_entry_builder
):
    """A device in last_seen but absent from ``devices`` falls back to 600.

    No explicit override / hub default, and no cached NormalizedEvent payload, so
    the class default classifier sees ``None`` and returns the safe periodic
    default without raising.
    """
    device_key = "Phantom-1"
    hub = hub_entry_builder(availability_timeout=None)
    assert CONF_AVAILABILITY_TIMEOUT not in hub.data
    coordinator = await _setup(hass, hub)

    # Present in last_seen, but never processed -> not in coordinator.devices.
    coordinator.last_seen[device_key] = dt_util.utcnow()
    assert device_key not in coordinator.devices

    assert coordinator._effective_timeout(device_key) == DEFAULT_AVAILABILITY_TIMEOUT
    # And the watchdog handles it without raising.
    await coordinator._async_watchdog(dt_util.utcnow())
    await hass.async_block_till_done()
