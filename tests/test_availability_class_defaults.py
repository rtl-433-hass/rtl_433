"""Tests for never-expire (0) and device-class-aware default timeout resolution.

These exercise the three-tier resolution order wired in production:

* per-device ``timeout_override`` (``entry.data[CONF_DEVICES][key]``),
* an *explicit* hub ``availability_timeout`` (membership, so ``0`` counts), and
* the device-class default (:func:`class_default_timeout`) from the device's
  latest cached payload when neither explicit tier is set.

Event-driven devices (open/close/motion/button/doorbell) have no periodic
check-in, so their class default is :data:`AVAILABILITY_TIMEOUT_NEVER` (never
marked unavailable once seen); periodic reporters keep the finite
:data:`DEFAULT_AVAILABILITY_TIMEOUT`. The event-driven field keys are derived
from the active device library (:func:`event_driven_field_keys`), so the tests
feed real library keys (``motion``, ``contact_open``, ``button`` …).

The integration-first tests run the real :func:`async_setup_entry` so the actual
``effective_timeout_resolver`` closure and library-derived event-driven key set
are wired onto the coordinator, then drive behaviour through the public
``coordinator._effective_timeout`` / ``_async_watchdog`` / ``entity.available``.
Time travel uses ``freeze_time`` like the existing watchdog tests.
"""

from __future__ import annotations

from datetime import timedelta
import logging
from unittest.mock import patch

from freezegun import freeze_time
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.rtl_433.const import (
    AVAILABILITY_TIMEOUT_NEVER,
    CONF_AVAILABILITY_TIMEOUT,
    CONF_MODEL,
    DEFAULT_AVAILABILITY_TIMEOUT,
    DEVICE_FIELDS,
    DEVICE_TIMEOUT_OVERRIDE,
    DOMAIN,
    class_default_timeout,
)
from custom_components.rtl_433.coordinator import Rtl433Coordinator
from custom_components.rtl_433.coordinator.base import Rtl433Client
from custom_components.rtl_433.mapping import FieldDescriptor, event_driven_field_keys
from custom_components.rtl_433.sensor import Rtl433Sensor
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

# Event-driven field keys derived from the shipped library (no user mappings) —
# the same set the production coordinator computes at setup.
SHIPPED_EVENT_DRIVEN_KEYS = event_driven_field_keys()


@pytest.fixture(autouse=True)
def _no_socket():
    """Stub the connect loop so the real setup never opens a WebSocket."""

    async def _noop(self) -> None:
        return None

    with patch.object(Rtl433Client, "start", _noop):
        yield


def _coordinator(hass, hub: MockConfigEntry) -> Rtl433Coordinator:
    return hass.data[DOMAIN][hub.entry_id]


def _feed(coordinator: Rtl433Coordinator, event: dict) -> None:
    """Push one event dict through the coordinator's frame handler."""
    coordinator._client._process_event(event)


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
        # Event-driven library keys -> never-expire.
        ({"motion": 1}, AVAILABILITY_TIMEOUT_NEVER),
        ({"contact_open": 0, "battery_ok": 1}, AVAILABILITY_TIMEOUT_NEVER),
        ({"closed": 1}, AVAILABILITY_TIMEOUT_NEVER),
        ({"reed_open": 1}, AVAILABILITY_TIMEOUT_NEVER),
        # platform: event fields (button/doorbell) -> never-expire.
        ({"button": "A"}, AVAILABILITY_TIMEOUT_NEVER),
        ({"secret_knock": "0"}, AVAILABILITY_TIMEOUT_NEVER),
        # Periodic reporters and diagnostic-only payloads -> finite default.
        ({"temperature_C": 21.5, "humidity": 50}, DEFAULT_AVAILABILITY_TIMEOUT),
        # A lone tamper/battery bit must NOT make a device never-expire.
        ({"tamper": 1, "battery_ok": 1}, DEFAULT_AVAILABILITY_TIMEOUT),
        ({}, DEFAULT_AVAILABILITY_TIMEOUT),
        (None, DEFAULT_AVAILABILITY_TIMEOUT),
    ],
)
def test_class_default_timeout_classifier(payload, expected):
    """Event-driven payloads never-expire; periodic / empty / missing get 600."""
    assert class_default_timeout(payload, SHIPPED_EVENT_DRIVEN_KEYS) == expected


def test_class_default_timeout_empty_keyset_is_periodic():
    """With no event-driven keys known, even a motion payload is periodic."""
    assert class_default_timeout({"motion": 1}, frozenset()) == (
        DEFAULT_AVAILABILITY_TIMEOUT
    )


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


async def test_class_default_event_driven_device_never_expires(hass, hub_entry_builder):
    """An event-driven (motion) payload resolves to never-expire.

    No per-device override and no explicit hub default, so the device-class
    default applies. A motion device has no periodic check-in, so once seen it
    stays available indefinitely — the watchdog never flips it, even after days
    of silence (under the old flat 600s default it would have been wrongly
    unavailable at 700s).
    """
    device_key = "GS-kw9c-7"
    # availability_timeout=None -> CONF_AVAILABILITY_TIMEOUT is NOT in the entry,
    # so the resolver returns None and the class default kicks in.
    hub = hub_entry_builder(
        availability_timeout=None,
        devices={
            device_key: {
                CONF_MODEL: "GS-kw9c",
                DEVICE_FIELDS: ["motion"],
            }
        },
    )
    assert CONF_AVAILABILITY_TIMEOUT not in hub.data
    coordinator = await _setup(hass, hub)

    start = dt_util.utcnow()
    with freeze_time(start):
        # A motion payload -> event-driven class.
        _feed(coordinator, {"model": "GS-kw9c", "id": 7, "motion": 1})
        await hass.async_block_till_done()

    assert coordinator._effective_timeout(device_key) == AVAILABILITY_TIMEOUT_NEVER

    # Past the old 7200s event default and far beyond -> still available.
    for offset in (timedelta(seconds=7201), timedelta(days=30)):
        with freeze_time(start + offset):
            await coordinator._async_watchdog(dt_util.utcnow())
            await hass.async_block_till_done()
            assert coordinator.available[device_key] is True


async def test_class_default_event_device_never_expires(hass, hub_entry_builder):
    """A ``platform: event`` device (doorbell button) also never-expires.

    The doorbell ``secret_knock`` field maps to ``platform: event``, which the
    classifier treats as event-driven without needing the ``event_driven`` flag.
    """
    device_key = "Honeywell-ActivLink-9"
    hub = hub_entry_builder(
        availability_timeout=None,
        devices={
            device_key: {
                CONF_MODEL: "Honeywell-ActivLink",
                DEVICE_FIELDS: ["secret_knock"],
            }
        },
    )
    coordinator = await _setup(hass, hub)

    start = dt_util.utcnow()
    with freeze_time(start):
        _feed(
            coordinator,
            {"model": "Honeywell-ActivLink", "id": 9, "secret_knock": 0},
        )
        await hass.async_block_till_done()

    assert coordinator._effective_timeout(device_key) == AVAILABILITY_TIMEOUT_NEVER

    with freeze_time(start + timedelta(days=7)):
        await coordinator._async_watchdog(dt_util.utcnow())
        await hass.async_block_till_done()
        assert coordinator.available[device_key] is True


async def test_event_device_silent_since_restart_never_expires(hass, hub_entry_builder):
    """An event device silent since a restart classifies from its adopted fields.

    The restart regression: after a restart the coordinator's live payload cache
    (``self.devices``) is empty until the device next transmits, which for an
    event-driven device (door/window contact, motion, doorbell) may be hours or
    never. The class-default classifier must still resolve never-expire from the
    device's *persisted* adopted fields, so the device — and its battery and other
    sensors — does NOT go unavailable at the periodic timeout while silent. Before
    the fix the classifier read only the live payload and wrongly returned 600s.
    """
    device_key = "GS-kw9c-7"
    hub = hub_entry_builder(
        availability_timeout=None,
        devices={
            device_key: {
                CONF_MODEL: "GS-kw9c",
                # A motion + battery contact sensor adopted before the restart.
                DEVICE_FIELDS: ["motion", "battery_ok"],
            }
        },
    )
    coordinator = await _setup(hass, hub)

    # Simulate "silent since restart": the device was adopted (it is in the
    # devices map) but has not transmitted this session, so there is no live
    # payload cached for it.
    assert device_key not in coordinator.devices

    # Classified never-expire purely from the adopted fields.
    assert coordinator._effective_timeout(device_key) == AVAILABILITY_TIMEOUT_NEVER

    # The entity baselines last_seen to "now" on add; far past the periodic 600s
    # the watchdog must still leave it available (battery sensor stays online).
    start = dt_util.utcnow()
    coordinator.last_seen[device_key] = start
    coordinator.available[device_key] = True
    for offset in (timedelta(seconds=601), timedelta(days=7)):
        with freeze_time(start + offset):
            await coordinator._async_watchdog(dt_util.utcnow())
            await hass.async_block_till_done()
            assert coordinator.available[device_key] is True


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


async def test_class_default_diagnostic_only_device_still_expires(
    hass, hub_entry_builder
):
    """A device reporting only a tamper/battery bit stays periodic (600s).

    Guards the invariant that a lone diagnostic field does NOT flip a device to
    never-expire — only a genuine event-driven state/event field does.
    """
    device_key = "Acurite-606TX-43"
    hub = hub_entry_builder(
        availability_timeout=None,
        devices={
            device_key: {
                CONF_MODEL: "Acurite-606TX",
                DEVICE_FIELDS: ["temperature_C"],
            }
        },
    )
    coordinator = await _setup(hass, hub)

    start = dt_util.utcnow()
    with freeze_time(start):
        _feed(
            coordinator,
            {"model": "Acurite-606TX", "id": 43, "tamper": 1, "battery_ok": 1},
        )
        await hass.async_block_till_done()

    assert coordinator._effective_timeout(device_key) == DEFAULT_AVAILABILITY_TIMEOUT

    with freeze_time(start + timedelta(seconds=601)):
        await coordinator._async_watchdog(dt_util.utcnow())
        await hass.async_block_till_done()
        assert coordinator.available[device_key] is False


# ---------------------------------------------------------------------------
# Resolution precedence
# ---------------------------------------------------------------------------


async def test_precedence_override_beats_hub_and_class(hass, hub_entry_builder):
    """A per-device override wins over both the explicit hub default and class.

    The device is event-driven (class default would be never-expire) and the hub
    sets an explicit 300, but the per-device override of 120 must win.
    """
    device_key = "GS-kw9c-7"
    hub = hub_entry_builder(
        availability_timeout=300,
        devices={
            device_key: {
                CONF_MODEL: "GS-kw9c",
                DEVICE_FIELDS: ["motion"],
                DEVICE_TIMEOUT_OVERRIDE: 120,
            }
        },
    )
    coordinator = await _setup(hass, hub)

    start = dt_util.utcnow()
    with freeze_time(start):
        _feed(coordinator, {"model": "GS-kw9c", "id": 7, "motion": 1})
        await hass.async_block_till_done()

    assert coordinator._effective_timeout(device_key) == 120

    # 121s of silence exceeds the 120s override (class default would never flip).
    with freeze_time(start + timedelta(seconds=121)):
        await coordinator._async_watchdog(dt_util.utcnow())
        await hass.async_block_till_done()
        assert coordinator.available[device_key] is False


async def test_precedence_explicit_hub_number_beats_class(hass, hub_entry_builder):
    """An explicit hub number wins over the class default for an event device.

    Hub=300 -> the event-driven device uses 300, not the never-expire class
    default.
    """
    device_key = "GS-kw9c-7"
    hub = hub_entry_builder(
        availability_timeout=300,
        devices={
            device_key: {
                CONF_MODEL: "GS-kw9c",
                DEVICE_FIELDS: ["motion"],
            }
        },
    )
    coordinator = await _setup(hass, hub)

    start = dt_util.utcnow()
    with freeze_time(start):
        _feed(coordinator, {"model": "GS-kw9c", "id": 7, "motion": 1})
        await hass.async_block_till_done()

    assert coordinator._effective_timeout(device_key) == 300

    # 301s -> unavailable (would still be available under never-expire class).
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


# ---------------------------------------------------------------------------
# Resolved-timeout DEBUG trace (logged once, then on change)
# ---------------------------------------------------------------------------

_TRACE_LOGGER = "custom_components.rtl_433"


def _timeout_lines(caplog) -> list[str]:
    """Return the ``rtl_433 <key> availability timeout=...`` DEBUG lines."""
    return [m for m in caplog.messages if "availability timeout=" in m]


async def test_watchdog_logs_finite_timeout_once(hass, hub_entry_builder, caplog):
    """A periodic reporter logs ``timeout=<N>s`` once; an unchanged tick is silent.

    The explicit hub default (600s) resolves via the ``hub-default`` tier and is
    logged on the first watchdog resolution, then suppressed while unchanged.
    """
    caplog.set_level(logging.DEBUG, logger=_TRACE_LOGGER)
    device_key = "Acurite-606TX-42"
    hub = hub_entry_builder(availability_timeout=600)
    coordinator = await _setup(hass, hub)

    start = dt_util.utcnow()
    with freeze_time(start):
        _feed(coordinator, {"model": "Acurite-606TX", "id": 42, "temperature_C": 21.4})
        await hass.async_block_till_done()
        await coordinator._async_watchdog(dt_util.utcnow())
        await hass.async_block_till_done()

    lines = _timeout_lines(caplog)
    assert len(lines) == 1
    assert device_key in lines[0]
    assert "timeout=600s" in lines[0]
    # The real setup wires an ``effective_timeout_resolver``; an explicit hub
    # default of 600 resolves through it as the ``override-or-hub`` tier.
    assert "source=override-or-hub" in lines[0]

    # A second tick with the same resolved timeout must NOT re-log.
    caplog.clear()
    with freeze_time(start):
        await coordinator._async_watchdog(dt_util.utcnow())
        await hass.async_block_till_done()
    assert _timeout_lines(caplog) == []


async def test_watchdog_logs_never_timeout_for_event_device(
    hass, hub_entry_builder, caplog
):
    """An event-driven device logs ``timeout=never`` (source=class-default).

    A motion device resolves to never-expire via the class default; the opaque
    "never marked unavailable" verdict is surfaced once.
    """
    caplog.set_level(logging.DEBUG, logger=_TRACE_LOGGER)
    device_key = "GS-kw9c-7"
    hub = hub_entry_builder(
        availability_timeout=None,
        devices={
            device_key: {
                CONF_MODEL: "GS-kw9c",
                DEVICE_FIELDS: ["motion"],
            }
        },
    )
    assert CONF_AVAILABILITY_TIMEOUT not in hub.data
    coordinator = await _setup(hass, hub)

    start = dt_util.utcnow()
    with freeze_time(start):
        _feed(coordinator, {"model": "GS-kw9c", "id": 7, "motion": 1})
        await hass.async_block_till_done()
        await coordinator._async_watchdog(dt_util.utcnow())
        await hass.async_block_till_done()

    lines = _timeout_lines(caplog)
    assert len(lines) == 1
    assert device_key in lines[0]
    assert "timeout=never" in lines[0]
    assert "source=class-default" in lines[0]


async def test_log_timeout_change_relogs_on_change(hass, hub_entry_builder, caplog):
    """Calling ``_log_timeout_change`` directly logs once, then only on change.

    A heavy watchdog setup is unnecessary to lock the dedupe-and-relog contract:
    the same timeout is suppressed, a changed timeout re-logs (with its source).
    """
    caplog.set_level(logging.DEBUG, logger=_TRACE_LOGGER)
    entry = hub_entry_builder(availability_timeout=600)
    entry.add_to_hass(hass)
    coordinator = Rtl433Coordinator(hass, entry, host="rtl433.local")
    key = "Acurite-606TX-42"

    coordinator._log_timeout_change(key, 600, "hub-default")
    coordinator._log_timeout_change(key, 600, "hub-default")  # unchanged: silent
    coordinator._log_timeout_change(key, 300, "override-or-hub")  # changed: re-log

    lines = _timeout_lines(caplog)
    assert len(lines) == 2
    assert "timeout=600s" in lines[0]
    assert "source=hub-default" in lines[0]
    assert "timeout=300s" in lines[1]
    assert "source=override-or-hub" in lines[1]
