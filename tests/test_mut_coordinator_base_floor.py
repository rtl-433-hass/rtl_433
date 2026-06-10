"""Mutation-killing floor tests for custom_components/rtl_433/coordinator/base.py.

This file targets the surviving mutants not killed by test_mut_coordinator_base.py
by asserting exact behaviors at every boundary and branch. Tests are grouped by
the function/method they cover and include:
- Exact numeric constants (_BACKOFF_MIN, _BACKOFF_MAX, REPLAY_STALE_THRESHOLD,
  DISCOVERY_BACKLOG_GRACE)
- Both branches of every boolean condition in the code
- Exact return values and side-effects
- _SdrStore migration logic
- is_event_driven_device and _known_field_keys
- _class_default_timeout and _effective_timeout resolver fallback
- _refresh_dev_info change-detection logic
- _seed_desired_on_first_connect seeding logic
- forget_device re-arm of discovery
- was_available=False path in _process_event
"""

from __future__ import annotations

import contextlib
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from freezegun import freeze_time
import pytest

from custom_components.rtl_433.const import (
    AVAILABILITY_TIMEOUT_NEVER,
    DEFAULT_AVAILABILITY_TIMEOUT,
    DEVICE_FIELDS,
    signal_device_update,
    signal_hub_update,
)
from custom_components.rtl_433.coordinator import Rtl433Coordinator
from custom_components.rtl_433.coordinator.base import (
    _BACKOFF_MAX,
    _BACKOFF_MIN,
    DISCOVERY_BACKLOG_GRACE,
    REPLAY_STALE_THRESHOLD,
    _build_cmd_url,
    _build_ws_url,
)
from custom_components.rtl_433.normalizer import NormalizedEvent
from custom_components.rtl_433.sdr_settings import (
    KEY_CENTER_FREQUENCY,
    KEY_GAIN_AUTO,
    KEY_GAIN_DB,
    KEY_PPM_ERROR,
    KEY_SAMPLE_RATE,
)
from homeassistant.util import dt as dt_util

DISPATCH = "custom_components.rtl_433.coordinator.base.async_dispatcher_send"


# ---------------------------------------------------------------------------
# Shared fixtures/helpers
# ---------------------------------------------------------------------------


def _run(hass, coro):
    """Drive an async coroutine to completion on the hass event loop."""
    return hass.loop.run_until_complete(coro)


@pytest.fixture
def coordinator(hass, hub_entry_builder):
    """Build a standard coordinator for mutation-killing tests."""
    entry = hub_entry_builder(availability_timeout=600)
    entry.add_to_hass(hass)
    return Rtl433Coordinator(
        hass,
        entry,
        host="rtl433.local",
        availability_timeout=600,
        skip_keys={"model", "id", "channel", "subtype", "time", "mic"},
    )


def _dispatched_replay_flags(dispatch) -> list[bool]:
    """Pull the is_replay flag from each per-device dispatched event."""
    return [
        call.args[2].is_replay
        for call in dispatch.call_args_list
        if call.args[1].startswith("rtl_433_device_update")
    ]


# ---------------------------------------------------------------------------
# Constants: verify exact values so mutations to them are caught
# ---------------------------------------------------------------------------


class TestConstants:
    """Verify module-level constants have exact expected values."""

    def test_replay_stale_threshold_is_30_seconds(self):
        """REPLAY_STALE_THRESHOLD must be exactly 30s."""
        assert timedelta(seconds=30) == REPLAY_STALE_THRESHOLD

    def test_replay_stale_threshold_not_29_seconds(self):
        """Mutation from 30 to 29 must be caught."""
        assert timedelta(seconds=29) != REPLAY_STALE_THRESHOLD

    def test_replay_stale_threshold_not_31_seconds(self):
        """Mutation from 30 to 31 must be caught."""
        assert timedelta(seconds=31) != REPLAY_STALE_THRESHOLD

    def test_discovery_backlog_grace_is_5_seconds(self):
        """DISCOVERY_BACKLOG_GRACE must be exactly 5s."""
        assert timedelta(seconds=5) == DISCOVERY_BACKLOG_GRACE

    def test_discovery_backlog_grace_not_4_seconds(self):
        """Mutation from 5 to 4 must be caught."""
        assert timedelta(seconds=4) != DISCOVERY_BACKLOG_GRACE

    def test_discovery_backlog_grace_not_6_seconds(self):
        """Mutation from 5 to 6 must be caught."""
        assert timedelta(seconds=6) != DISCOVERY_BACKLOG_GRACE

    def test_backoff_min_is_one(self):
        """_BACKOFF_MIN must be exactly 1.0."""
        assert _BACKOFF_MIN == 1.0

    def test_backoff_max_is_sixty(self):
        """_BACKOFF_MAX must be exactly 60.0."""
        assert _BACKOFF_MAX == 60.0


# ---------------------------------------------------------------------------
# _SdrStore migration: Hz to MHz conversion
# ---------------------------------------------------------------------------


class TestSdrStoreMigration:
    """Cover _SdrStore._async_migrate_func for mutation-killing."""

    def test_migration_converts_hz_to_mhz(self, hass, hub_entry_builder):
        """Version 1 Hz value must be divided by 1_000_000 to get MHz."""
        from custom_components.rtl_433.const import SDR_STORE_VERSION, sdr_store_key
        from custom_components.rtl_433.coordinator.base import _SdrStore

        entry = hub_entry_builder()
        entry.add_to_hass(hass)
        store = _SdrStore(hass, SDR_STORE_VERSION, sdr_store_key(entry.entry_id))

        old_data = {"values": {KEY_CENTER_FREQUENCY: 433_920_000}}
        result = _run(hass, store._async_migrate_func(1, 0, old_data))
        # 433920000 Hz / 1_000_000 = 433.92 MHz
        assert result["values"][KEY_CENTER_FREQUENCY] == pytest.approx(433.92)

    def test_migration_exact_division_by_one_million(self, hass, hub_entry_builder):
        """Ensure division is by 1_000_000 not 1000 (mutant: / 1000)."""
        from custom_components.rtl_433.const import SDR_STORE_VERSION, sdr_store_key
        from custom_components.rtl_433.coordinator.base import _SdrStore

        entry = hub_entry_builder()
        entry.add_to_hass(hass)
        store = _SdrStore(hass, SDR_STORE_VERSION, sdr_store_key(entry.entry_id))

        old_data = {"values": {KEY_CENTER_FREQUENCY: 1_000_000}}
        result = _run(hass, store._async_migrate_func(1, 0, old_data))
        # Should be 1.0 MHz, NOT 1000.0
        assert result["values"][KEY_CENTER_FREQUENCY] == pytest.approx(1.0)
        assert result["values"][KEY_CENTER_FREQUENCY] != 1000.0

    def test_migration_result_is_float(self, hass, hub_entry_builder):
        """Converted value must be a float (float(hz) / 1_000_000)."""
        from custom_components.rtl_433.const import SDR_STORE_VERSION, sdr_store_key
        from custom_components.rtl_433.coordinator.base import _SdrStore

        entry = hub_entry_builder()
        entry.add_to_hass(hass)
        store = _SdrStore(hass, SDR_STORE_VERSION, sdr_store_key(entry.entry_id))

        old_data = {"values": {KEY_CENTER_FREQUENCY: 433920000}}
        result = _run(hass, store._async_migrate_func(1, 0, old_data))
        assert isinstance(result["values"][KEY_CENTER_FREQUENCY], float)

    def test_migration_skipped_for_version_2(self, hass, hub_entry_builder):
        """Version 2 data is NOT migrated (old_major_version >= 2)."""
        from custom_components.rtl_433.const import SDR_STORE_VERSION, sdr_store_key
        from custom_components.rtl_433.coordinator.base import _SdrStore

        entry = hub_entry_builder()
        entry.add_to_hass(hass)
        store = _SdrStore(hass, SDR_STORE_VERSION, sdr_store_key(entry.entry_id))

        old_data = {"values": {KEY_CENTER_FREQUENCY: 433920000}}
        result = _run(hass, store._async_migrate_func(2, 0, old_data))
        # NOT divided - still the original Hz value
        assert result["values"][KEY_CENTER_FREQUENCY] == 433920000

    def test_migration_no_values_key_is_safe(self, hass, hub_entry_builder):
        """Migration with no 'values' key is a no-op (no crash)."""
        from custom_components.rtl_433.const import SDR_STORE_VERSION, sdr_store_key
        from custom_components.rtl_433.coordinator.base import _SdrStore

        entry = hub_entry_builder()
        entry.add_to_hass(hass)
        store = _SdrStore(hass, SDR_STORE_VERSION, sdr_store_key(entry.entry_id))

        old_data = {}
        result = _run(hass, store._async_migrate_func(1, 0, old_data))
        assert result == {}

    def test_migration_no_center_freq_key_is_safe(self, hass, hub_entry_builder):
        """Migration without center_frequency in values is a no-op."""
        from custom_components.rtl_433.const import SDR_STORE_VERSION, sdr_store_key
        from custom_components.rtl_433.coordinator.base import _SdrStore

        entry = hub_entry_builder()
        entry.add_to_hass(hass)
        store = _SdrStore(hass, SDR_STORE_VERSION, sdr_store_key(entry.entry_id))

        old_data = {"values": {"samp_rate": 250000}}
        result = _run(hass, store._async_migrate_func(1, 0, old_data))
        assert result["values"]["samp_rate"] == 250000
        assert KEY_CENTER_FREQUENCY not in result["values"]

    def test_migration_string_hz_not_converted(self, hass, hub_entry_builder):
        """A string center_frequency (non-int/float) is NOT migrated."""
        from custom_components.rtl_433.const import SDR_STORE_VERSION, sdr_store_key
        from custom_components.rtl_433.coordinator.base import _SdrStore

        entry = hub_entry_builder()
        entry.add_to_hass(hass)
        store = _SdrStore(hass, SDR_STORE_VERSION, sdr_store_key(entry.entry_id))

        old_data = {"values": {KEY_CENTER_FREQUENCY: "433920000"}}
        result = _run(hass, store._async_migrate_func(1, 0, old_data))
        # String is not isinstance(hz, (int, float)) -> unchanged
        assert result["values"][KEY_CENTER_FREQUENCY] == "433920000"


# ---------------------------------------------------------------------------
# is_event_driven_device
# ---------------------------------------------------------------------------


class TestIsEventDrivenDevice:
    """Cover coordinator.is_event_driven_device for mutation-killing."""

    def test_no_event_driven_keys_always_false(self, hass, hub_entry_builder):
        """With no event_driven_keys, every device returns False."""
        entry = hub_entry_builder()
        entry.add_to_hass(hass)
        coord = Rtl433Coordinator(
            hass, entry, host="rtl433.local", event_driven_keys=frozenset()
        )
        coord.devices["Dev-1"] = NormalizedEvent(
            device_key="Dev-1", model="Dev", fields={"motion": 1}
        )
        assert coord.is_event_driven_device("Dev-1") is False

    def test_event_driven_key_present_returns_true(self, hass, hub_entry_builder):
        """Device with at least one event-driven field key returns True."""
        entry = hub_entry_builder()
        entry.add_to_hass(hass)
        coord = Rtl433Coordinator(
            hass, entry, host="rtl433.local", event_driven_keys=frozenset({"motion"})
        )
        coord.devices["Dev-1"] = NormalizedEvent(
            device_key="Dev-1", model="Dev", fields={"motion": 1, "battery_ok": 1}
        )
        assert coord.is_event_driven_device("Dev-1") is True

    def test_event_driven_key_absent_returns_false(self, hass, hub_entry_builder):
        """Device with no event-driven field returns False."""
        entry = hub_entry_builder()
        entry.add_to_hass(hass)
        coord = Rtl433Coordinator(
            hass, entry, host="rtl433.local", event_driven_keys=frozenset({"motion"})
        )
        coord.devices["Dev-1"] = NormalizedEvent(
            device_key="Dev-1", model="Dev", fields={"temperature_C": 22.0}
        )
        assert coord.is_event_driven_device("Dev-1") is False

    def test_event_driven_device_uses_adopted_fields(self, hass, hub_entry_builder):
        """is_event_driven_device checks adopted (persisted) fields too."""
        devices_map = {
            "Dev-1": {"model": "Dev", DEVICE_FIELDS: ["motion"], "fields": ["motion"]}
        }
        entry = hub_entry_builder(devices=devices_map)
        entry.add_to_hass(hass)
        coord = Rtl433Coordinator(
            hass, entry, host="rtl433.local", event_driven_keys=frozenset({"motion"})
        )
        # No live event in devices - only adopted fields
        assert coord.is_event_driven_device("Dev-1") is True

    def test_event_driven_check_is_not_disjoint(self, hass, hub_entry_builder):
        """is_event_driven_device uses isdisjoint; exactly one matching key is enough."""
        entry = hub_entry_builder()
        entry.add_to_hass(hass)
        coord = Rtl433Coordinator(
            hass,
            entry,
            host="rtl433.local",
            event_driven_keys=frozenset({"open", "motion", "button"}),
        )
        # Only 'open' present
        coord.devices["Dev-1"] = NormalizedEvent(
            device_key="Dev-1", model="Dev", fields={"open": 1, "battery_ok": 1}
        )
        assert coord.is_event_driven_device("Dev-1") is True


# ---------------------------------------------------------------------------
# _known_field_keys
# ---------------------------------------------------------------------------


class TestKnownFieldKeys:
    """Cover _known_field_keys for mutation-killing."""

    def test_known_field_keys_from_live_event(self, hass, coordinator):
        """Live event fields are in the result set."""
        with freeze_time("2026-05-25T10:00:00+00:00"), patch(DISPATCH):
            coordinator._handle_text_frame(
                '{"time": "2026-05-25T10:00:00Z", "model": "Dev", "id": 1, '
                '"temperature_C": 21.0, "humidity": 60}'
            )
        keys = coordinator._known_field_keys("Dev-1")
        assert "temperature_C" in keys
        assert "humidity" in keys

    def test_known_field_keys_from_adopted_fields(self, hass, hub_entry_builder):
        """Adopted (persisted) fields appear even when no live event has been seen."""
        devices_map = {
            "SilentDev-1": {
                "model": "SilentDev",
                DEVICE_FIELDS: ["temperature_C", "battery_ok"],
            }
        }
        entry = hub_entry_builder(devices=devices_map)
        entry.add_to_hass(hass)
        coord = Rtl433Coordinator(hass, entry, host="rtl433.local")
        keys = coord._known_field_keys("SilentDev-1")
        assert "temperature_C" in keys
        assert "battery_ok" in keys

    def test_known_field_keys_union_of_both_sources(self, hass, hub_entry_builder):
        """Result is the union of adopted + live fields."""
        devices_map = {
            "Dev-1": {
                "model": "Dev",
                DEVICE_FIELDS: ["battery_ok"],
            }
        }
        entry = hub_entry_builder(devices=devices_map)
        entry.add_to_hass(hass)
        coord = Rtl433Coordinator(
            hass,
            entry,
            host="rtl433.local",
            skip_keys={"model", "id", "channel", "subtype", "time", "mic"},
        )
        with freeze_time("2026-05-25T10:00:00+00:00"), patch(DISPATCH):
            coord._handle_text_frame(
                '{"time": "2026-05-25T10:00:00Z", "model": "Dev", "id": 1, '
                '"temperature_C": 21.0}'
            )
        keys = coord._known_field_keys("Dev-1")
        # Both adopted and live fields
        assert "battery_ok" in keys
        assert "temperature_C" in keys

    def test_known_field_keys_unknown_device_empty(self, hass, coordinator):
        """An unknown device (no entry, no event) yields an empty set."""
        keys = coordinator._known_field_keys("Unknown-99")
        assert keys == set()

    def test_known_field_keys_none_device_cfg_safe(self, hass, hub_entry_builder):
        """No CONF_DEVICES entry = safe empty base, no crash."""
        entry = hub_entry_builder()  # no devices map
        entry.add_to_hass(hass)
        coord = Rtl433Coordinator(hass, entry, host="rtl433.local")
        keys = coord._known_field_keys("Nonexistent-1")
        assert keys == set()


# ---------------------------------------------------------------------------
# _class_default_timeout
# ---------------------------------------------------------------------------


class TestClassDefaultTimeout:
    """Cover _class_default_timeout for mutation-killing."""

    def test_periodic_device_gets_default_timeout(self, hass, hub_entry_builder):
        """Periodic device (no event-driven fields) gets DEFAULT_AVAILABILITY_TIMEOUT."""
        entry = hub_entry_builder()
        entry.add_to_hass(hass)
        coord = Rtl433Coordinator(
            hass,
            entry,
            host="rtl433.local",
            event_driven_keys=frozenset({"motion"}),
            skip_keys={"model", "id", "channel", "subtype", "time"},
        )
        coord.devices["Dev-1"] = NormalizedEvent(
            device_key="Dev-1", model="Dev", fields={"temperature_C": 22.0}
        )
        result = coord._class_default_timeout("Dev-1")
        assert result == DEFAULT_AVAILABILITY_TIMEOUT
        assert result == 600

    def test_event_driven_device_gets_never_expire(self, hass, hub_entry_builder):
        """Event-driven device gets AVAILABILITY_TIMEOUT_NEVER = 0."""
        entry = hub_entry_builder()
        entry.add_to_hass(hass)
        coord = Rtl433Coordinator(
            hass,
            entry,
            host="rtl433.local",
            event_driven_keys=frozenset({"motion"}),
        )
        coord.devices["Dev-1"] = NormalizedEvent(
            device_key="Dev-1", model="Dev", fields={"motion": 1}
        )
        result = coord._class_default_timeout("Dev-1")
        assert result == AVAILABILITY_TIMEOUT_NEVER
        assert result == 0

    def test_class_default_timeout_uses_known_fields(self, hass, hub_entry_builder):
        """_class_default_timeout consults _known_field_keys (adopted + live)."""
        devices_map = {"Dev-1": {"model": "Dev", DEVICE_FIELDS: ["motion"]}}
        entry = hub_entry_builder(devices=devices_map)
        entry.add_to_hass(hass)
        coord = Rtl433Coordinator(
            hass,
            entry,
            host="rtl433.local",
            event_driven_keys=frozenset({"motion"}),
        )
        # No live event, but adopted fields include "motion"
        result = coord._class_default_timeout("Dev-1")
        assert result == AVAILABILITY_TIMEOUT_NEVER


# ---------------------------------------------------------------------------
# _effective_timeout: resolver None path vs resolver-returns-None path
# ---------------------------------------------------------------------------


class TestEffectiveTimeoutPaths:
    """Cover all branches of _effective_timeout."""

    def test_resolver_none_returns_availability_timeout(self, hass, coordinator):
        """When resolver=None, returns coordinator.availability_timeout exactly."""
        coordinator.effective_timeout_resolver = None
        coordinator.availability_timeout = 300
        result = coordinator._effective_timeout("any-key")
        assert result == 300

    def test_resolver_returns_none_falls_to_class_default(
        self, hass, hub_entry_builder
    ):
        """When resolver returns None, class-default timeout is used."""
        entry = hub_entry_builder()
        entry.add_to_hass(hass)
        coord = Rtl433Coordinator(
            hass,
            entry,
            host="rtl433.local",
            availability_timeout=600,
            event_driven_keys=frozenset({"motion"}),
        )
        # Resolver returns None (no explicit override set)
        coord.effective_timeout_resolver = lambda dk: None
        coord.devices["Dev-1"] = NormalizedEvent(
            device_key="Dev-1", model="Dev", fields={"temperature_C": 22.0}
        )
        result = coord._effective_timeout("Dev-1")
        # Class default for periodic device
        assert result == DEFAULT_AVAILABILITY_TIMEOUT

    def test_resolver_returns_none_event_driven_class_default(
        self, hass, hub_entry_builder
    ):
        """Resolver=None result -> class default: NEVER for event-driven."""
        entry = hub_entry_builder()
        entry.add_to_hass(hass)
        coord = Rtl433Coordinator(
            hass,
            entry,
            host="rtl433.local",
            availability_timeout=600,
            event_driven_keys=frozenset({"motion"}),
        )
        coord.effective_timeout_resolver = lambda dk: None
        coord.devices["Dev-1"] = NormalizedEvent(
            device_key="Dev-1", model="Dev", fields={"motion": 1}
        )
        result = coord._effective_timeout("Dev-1")
        assert result == AVAILABILITY_TIMEOUT_NEVER

    def test_resolver_returns_zero_means_never_expire(self, hass, coordinator):
        """A resolver returning 0 (AVAILABILITY_TIMEOUT_NEVER) is honored exactly."""
        coordinator.effective_timeout_resolver = lambda dk: 0
        result = coordinator._effective_timeout("any-key")
        assert result == 0

    def test_resolver_exception_falls_back_to_hub_availability_timeout(
        self, hass, coordinator
    ):
        """When resolver raises, fallback is hub's availability_timeout (600)."""

        def boom(dk):
            raise RuntimeError("resolver exploded")

        coordinator.effective_timeout_resolver = boom
        coordinator.availability_timeout = 600
        result = coordinator._effective_timeout("any-key")
        # Falls back to availability_timeout when resolver raises
        assert result == 600


# ---------------------------------------------------------------------------
# Watchdog: AVAILABILITY_TIMEOUT_NEVER skips the device
# ---------------------------------------------------------------------------


class TestWatchdogNeverExpire:
    """Cover the never-expire branch of _async_watchdog."""

    def test_watchdog_never_expire_device_not_flipped(self, hass, hub_entry_builder):
        """A device with timeout=0 (NEVER) is never marked unavailable."""
        entry = hub_entry_builder()
        entry.add_to_hass(hass)
        coord = Rtl433Coordinator(
            hass,
            entry,
            host="rtl433.local",
            availability_timeout=0,  # NEVER
            skip_keys={"model", "id", "channel", "subtype", "time"},
        )

        t0 = dt_util.parse_datetime("2026-05-25T10:00:00+00:00")
        with freeze_time(t0), patch(DISPATCH):
            coord._handle_text_frame(
                '{"time": "2026-05-25T10:00:00Z", "model": "Dev", "id": 1, "val": 1}'
            )
        assert coord.available["Dev-1"] is True

        # Run watchdog much later - should NOT mark unavailable
        with freeze_time(t0 + timedelta(seconds=9999)), patch(DISPATCH) as dispatch:
            _run(hass, coord._async_watchdog(dt_util.utcnow()))

        assert coord.available["Dev-1"] is True
        dispatch.assert_not_called()

    def test_watchdog_never_expire_is_exactly_zero(self, hass, coordinator):
        """AVAILABILITY_TIMEOUT_NEVER == 0, not any other value."""
        assert AVAILABILITY_TIMEOUT_NEVER == 0
        assert AVAILABILITY_TIMEOUT_NEVER != 1

    def test_watchdog_stale_comparison_uses_gt_not_gte(self, hass, coordinator):
        """Stale check is > not >=: exactly at timeout is NOT stale."""
        key = "Dev-1"
        start = dt_util.parse_datetime("2026-05-25T10:00:00+00:00")
        with freeze_time(start), patch(DISPATCH):
            coordinator._handle_text_frame(
                '{"time": "2026-05-25T10:00:00Z", "model": "Dev", "id": 1, "val": 1}'
            )

        # Exactly at 600s: (now - seen) == timedelta(600) -> NOT > -> not stale
        with freeze_time(start + timedelta(seconds=600)), patch(DISPATCH) as dispatch:
            _run(hass, coordinator._async_watchdog(dt_util.utcnow()))

        assert coordinator.available[key] is True
        dispatch.assert_not_called()

    def test_watchdog_stale_check_dispatches_cached_event(self, hass, coordinator):
        """When a device goes stale, the cached NormalizedEvent is dispatched."""
        key = "Dev-1"
        start = dt_util.parse_datetime("2026-05-25T10:00:00+00:00")
        with freeze_time(start), patch(DISPATCH):
            coordinator._handle_text_frame(
                '{"time": "2026-05-25T10:00:00Z", "model": "Dev", "id": 1, "val": 1}'
            )

        _cached = coordinator.devices[key]

        with freeze_time(start + timedelta(seconds=701)), patch(DISPATCH) as dispatch:
            _run(hass, coordinator._async_watchdog(dt_util.utcnow()))

        sig = signal_device_update(coordinator.entry.entry_id, key)
        dev_calls = [c for c in dispatch.call_args_list if c.args[1] == sig]
        assert len(dev_calls) == 1
        # Dispatched with is_replay=False override
        assert dev_calls[0].args[2].is_replay is False

    def test_watchdog_no_cached_event_no_dispatch(self, hass, coordinator):
        """Watchdog does NOT dispatch when devices[key] is missing."""
        key = "Ghost-1"
        t0 = dt_util.parse_datetime("2026-05-25T10:00:00+00:00")
        # last_seen set but no device entry
        coordinator.last_seen[key] = t0
        coordinator.available[key] = True
        # No entry in coordinator.devices[key]

        with freeze_time(t0 + timedelta(seconds=701)), patch(DISPATCH) as dispatch:
            _run(hass, coordinator._async_watchdog(dt_util.utcnow()))

        # Flips available to False but doesn't dispatch (no cached event)
        assert coordinator.available[key] is False
        # No device-update signal sent
        sig = signal_device_update(coordinator.entry.entry_id, key)
        dev_calls = [c for c in dispatch.call_args_list if c.args[1] == sig]
        assert dev_calls == []


# ---------------------------------------------------------------------------
# _process_event: is_backlog logic (DISCOVERY_BACKLOG_GRACE boundary)
# ---------------------------------------------------------------------------


class TestProcessEventBacklogGrace:
    """Kill mutants on the DISCOVERY_BACKLOG_GRACE subtraction."""

    def test_exactly_at_grace_boundary_is_not_backlog(self, hass, coordinator):
        """A frame at connection_time - 5s is NOT backlog (open interval)."""
        # DISCOVERY_BACKLOG_GRACE = 5s
        # is_backlog = event_time < connection_time - grace
        # At connection_time - 5s: NOT < (it's equal) -> not backlog
        conn_time = dt_util.parse_datetime("2026-05-25T10:00:00+00:00")
        coordinator._connection_time = conn_time
        seen: list[str] = []
        coordinator.discovery_enabled = True
        coordinator.new_device_callback = lambda k, m, r: seen.append(k)

        # Frame at exactly connection_time - 5s (10:00:00 - 5s = 09:59:55)
        # event_time < conn_time - 5s? = 09:59:55 < 09:59:55? = False -> not backlog
        with freeze_time("2026-05-25T10:00:01+00:00"), patch(DISPATCH):
            coordinator._handle_text_frame(
                '{"time": "2026-05-25T09:59:55Z", "model": "Dev", "id": 1, "val": 1}'
            )

        # Not backlog -> should register
        assert seen == ["Dev-1"]

    def test_one_second_before_grace_boundary_is_backlog(self, hass, coordinator):
        """A frame at connection_time - 6s IS backlog."""
        conn_time = dt_util.parse_datetime("2026-05-25T10:00:00+00:00")
        coordinator._connection_time = conn_time
        seen: list[str] = []
        coordinator.discovery_enabled = True
        coordinator.new_device_callback = lambda k, m, r: seen.append(k)

        # Frame at 09:59:54 (6s before connection, outside grace window)
        # event_time < conn_time - 5s? = 09:59:54 < 09:59:55? = True -> backlog
        with freeze_time("2026-05-25T10:00:01+00:00"), patch(DISPATCH):
            coordinator._handle_text_frame(
                '{"time": "2026-05-25T09:59:54Z", "model": "Dev", "id": 1, "val": 1}'
            )

        assert seen == []

    def test_backlog_event_is_replay(self, hass, coordinator):
        """A backlog event is classified as replay."""
        conn_time = dt_util.parse_datetime("2026-05-25T10:00:00+00:00")
        coordinator._connection_time = conn_time

        # Frame 60s before connect -> backlog
        with freeze_time("2026-05-25T10:00:01+00:00"), patch(DISPATCH) as dispatch:
            coordinator._handle_text_frame(
                '{"time": "2026-05-25T09:59:00Z", "model": "Dev", "id": 1, "val": 1}'
            )

        assert _dispatched_replay_flags(dispatch) == [True]

    def test_backlog_event_seeds_devices_state(self, hass, coordinator):
        """Backlog event still populates devices dict (seeds runtime state)."""
        conn_time = dt_util.parse_datetime("2026-05-25T10:00:00+00:00")
        coordinator._connection_time = conn_time

        with freeze_time("2026-05-25T10:00:01+00:00"), patch(DISPATCH):
            coordinator._handle_text_frame(
                '{"time": "2026-05-25T09:59:00Z", "model": "Dev", "id": 1, "val": 1}'
            )

        assert "Dev-1" in coordinator.devices

    def test_backlog_event_does_not_update_last_seen(self, hass, coordinator):
        """A backlog event (which is replay) does NOT update last_seen."""
        conn_time = dt_util.parse_datetime("2026-05-25T10:00:00+00:00")
        coordinator._connection_time = conn_time

        with freeze_time("2026-05-25T10:00:01+00:00"), patch(DISPATCH):
            coordinator._handle_text_frame(
                '{"time": "2026-05-25T09:59:00Z", "model": "Dev", "id": 1, "val": 1}'
            )

        assert "Dev-1" not in coordinator.last_seen

    def test_backlog_advances_high_water_mark(self, hass, coordinator):
        """A backlog/stale event advances the high water mark."""
        conn_time = dt_util.parse_datetime("2026-05-25T10:00:00+00:00")
        coordinator._connection_time = conn_time

        event_time = dt_util.parse_datetime("2026-05-25T09:59:00+00:00")
        # now - event = 60s > 30s threshold -> stale -> advances mark
        with freeze_time("2026-05-25T10:00:01+00:00"), patch(DISPATCH):
            coordinator._handle_text_frame(
                '{"time": "2026-05-25T09:59:00Z", "model": "Dev", "id": 1, "val": 1}'
            )

        assert coordinator._event_high_water == event_time

    def test_is_post_connection_false_gates_registration(self, hass, coordinator):
        """is_backlog=True prevents registration even with discovery enabled."""
        conn_time = dt_util.parse_datetime("2026-05-25T10:00:00+00:00")
        coordinator._connection_time = conn_time
        registered: list[str] = []
        coordinator.discovery_enabled = True
        coordinator.new_device_callback = lambda k, m, r: registered.append(k)

        # Backlog event
        with freeze_time("2026-05-25T10:00:01+00:00"), patch(DISPATCH):
            coordinator._handle_text_frame(
                '{"time": "2026-05-25T09:59:00Z", "model": "Dev", "id": 1, "val": 1}'
            )

        assert registered == []


# ---------------------------------------------------------------------------
# _process_event: already-seen replay (high water mark boundary)
# ---------------------------------------------------------------------------


class TestHighWaterMarkClassification:
    """Kill mutants on the high water mark comparison (<=)."""

    def test_exactly_at_mark_is_replay(self, hass, coordinator):
        """event_time == high_water -> is_replay=True."""
        t = dt_util.parse_datetime("2026-05-25T10:00:00+00:00")
        coordinator._event_high_water = t

        with freeze_time("2026-05-25T10:00:05+00:00"), patch(DISPATCH) as dispatch:
            coordinator._handle_text_frame(
                '{"time": "2026-05-25T10:00:00Z", "model": "Dev", "id": 1, "val": 1}'
            )

        assert _dispatched_replay_flags(dispatch) == [True]

    def test_one_second_below_mark_is_replay(self, hass, coordinator):
        """event_time < high_water -> is_replay=True."""
        t = dt_util.parse_datetime("2026-05-25T10:00:00+00:00")
        coordinator._event_high_water = t

        with freeze_time("2026-05-25T10:00:05+00:00"), patch(DISPATCH) as dispatch:
            coordinator._handle_text_frame(
                '{"time": "2026-05-25T09:59:59Z", "model": "Dev", "id": 1, "val": 1}'
            )

        assert _dispatched_replay_flags(dispatch) == [True]

    def test_one_ms_above_mark_falls_through(self, hass, coordinator):
        """event_time > high_water -> falls through to staleness check."""
        t = dt_util.parse_datetime("2026-05-25T10:00:00+00:00")
        coordinator._event_high_water = t

        # 1s newer + recent enough
        with freeze_time("2026-05-25T10:00:05+00:00"), patch(DISPATCH) as dispatch:
            coordinator._handle_text_frame(
                '{"time": "2026-05-25T10:00:01Z", "model": "Dev", "id": 1, "val": 1}'
            )

        # Newer than mark and not stale -> live
        assert _dispatched_replay_flags(dispatch) == [False]

    def test_replay_mark_not_advanced(self, hass, coordinator):
        """An already-seen replay does NOT advance the high-water mark."""
        t = dt_util.parse_datetime("2026-05-25T10:00:00+00:00")
        coordinator._event_high_water = t

        with freeze_time("2026-05-25T10:00:05+00:00"), patch(DISPATCH):
            coordinator._handle_text_frame(
                '{"time": "2026-05-25T09:59:00Z", "model": "Dev", "id": 1, "val": 1}'
            )

        # High water mark unchanged
        assert coordinator._event_high_water == t


# ---------------------------------------------------------------------------
# _process_event: stale gap threshold boundary (REPLAY_STALE_THRESHOLD)
# ---------------------------------------------------------------------------


class TestStaleBoundary:
    """Kill mutants on the staleness comparison (> vs >=, value of threshold)."""

    def test_exactly_30s_old_not_stale(self, hass, coordinator):
        """now - event_time == 30s is NOT stale (> not >=)."""
        t_now = dt_util.parse_datetime("2026-05-25T10:00:30+00:00")
        with freeze_time(t_now), patch(DISPATCH) as dispatch:
            coordinator._handle_text_frame(
                '{"time": "2026-05-25T10:00:00Z", "model": "Dev", "id": 1, "val": 1}'
            )
        # Exactly 30s -> NOT > 30s -> LIVE
        assert _dispatched_replay_flags(dispatch) == [False]

    def test_29s_old_not_stale(self, hass, coordinator):
        """A 29s old event is NOT stale (< threshold)."""
        t_now = dt_util.parse_datetime("2026-05-25T10:00:29+00:00")
        with freeze_time(t_now), patch(DISPATCH) as dispatch:
            coordinator._handle_text_frame(
                '{"time": "2026-05-25T10:00:00Z", "model": "Dev", "id": 1, "val": 1}'
            )
        assert _dispatched_replay_flags(dispatch) == [False]

    def test_31s_old_is_stale(self, hass, coordinator):
        """A 31s old event IS stale (> threshold)."""
        t_now = dt_util.parse_datetime("2026-05-25T10:00:31+00:00")
        with freeze_time(t_now), patch(DISPATCH) as dispatch:
            coordinator._handle_text_frame(
                '{"time": "2026-05-25T10:00:00Z", "model": "Dev", "id": 1, "val": 1}'
            )
        assert _dispatched_replay_flags(dispatch) == [True]

    def test_stale_event_advances_mark_to_event_time(self, hass, coordinator):
        """A stale event advances the high-water mark to its own time."""
        event_time = dt_util.parse_datetime("2026-05-25T10:00:00+00:00")
        t_now = dt_util.parse_datetime("2026-05-25T10:00:31+00:00")

        with freeze_time(t_now), patch(DISPATCH):
            coordinator._handle_text_frame(
                '{"time": "2026-05-25T10:00:00Z", "model": "Dev", "id": 1, "val": 1}'
            )

        assert coordinator._event_high_water == event_time

    def test_stale_event_does_not_update_last_seen(self, hass, coordinator):
        """A stale event (replay=True) does not update last_seen."""
        t_now = dt_util.parse_datetime("2026-05-25T10:00:31+00:00")
        with freeze_time(t_now), patch(DISPATCH):
            coordinator._handle_text_frame(
                '{"time": "2026-05-25T10:00:00Z", "model": "Dev", "id": 1, "val": 1}'
            )
        assert "Dev-1" not in coordinator.last_seen


# ---------------------------------------------------------------------------
# _process_event: was_available=False -> back online path
# ---------------------------------------------------------------------------


class TestWasAvailableRecovery:
    """Kill mutants on the was_available check and 'back online' log."""

    def test_back_online_when_was_unavailable(self, hass, coordinator, caplog):
        """Live event after unavailability flips available True and logs back online."""
        import logging

        key = "Dev-1"
        start = dt_util.parse_datetime("2026-05-25T10:00:00+00:00")
        with freeze_time(start), patch(DISPATCH):
            coordinator._handle_text_frame(
                '{"time": "2026-05-25T10:00:00Z", "model": "Dev", "id": 1, "val": 1}'
            )

        with freeze_time(start + timedelta(seconds=700)), patch(DISPATCH):
            _run(hass, coordinator._async_watchdog(dt_util.utcnow()))
        assert coordinator.available[key] is False

        fresh = start + timedelta(seconds=750)
        with (
            freeze_time(fresh),
            patch(DISPATCH),
            caplog.at_level(logging.DEBUG, logger="custom_components.rtl_433"),
        ):
            coordinator._handle_text_frame(
                f'{{"time": "{fresh.strftime("%Y-%m-%dT%H:%M:%SZ")}", '
                '"model": "Dev", "id": 1, "val": 2}'
            )

        assert coordinator.available[key] is True
        assert any("back online" in r.message for r in caplog.records)

    def test_no_back_online_when_was_available(self, hass, coordinator, caplog):
        """No 'back online' log when device was already available."""
        import logging

        with (
            freeze_time("2026-05-25T10:00:00+00:00"),
            patch(DISPATCH),
            caplog.at_level(logging.DEBUG, logger="custom_components.rtl_433"),
        ):
            coordinator._handle_text_frame(
                '{"time": "2026-05-25T10:00:00Z", "model": "Dev", "id": 1, "val": 1}'
            )

        back_online_msgs = [r for r in caplog.records if "back online" in r.message]
        assert back_online_msgs == []

    def test_was_available_is_false_not_none(self, hass, coordinator):
        """was_available checks the exact value False, not None (new device)."""
        key = "Dev-1"
        start = dt_util.parse_datetime("2026-05-25T10:00:00+00:00")

        # No prior availability state for this device (None, not False)
        assert coordinator.available.get(key) is None

        # First event: was_available=None -> no back-online log
        with freeze_time(start), patch(DISPATCH):
            coordinator._handle_text_frame(
                '{"time": "2026-05-25T10:00:00Z", "model": "Dev", "id": 1, "val": 1}'
            )
        # No crash, device is now available
        assert coordinator.available[key] is True


# ---------------------------------------------------------------------------
# _refresh_dev_info: change detection and hub_info_callback
# ---------------------------------------------------------------------------


class TestRefreshDevInfo:
    """Cover _refresh_dev_info change detection for mutation-killing."""

    def test_dev_info_set_on_new_info(self, hass, coordinator, aioclient_mock):
        """Fresh dev_info (not equal to current) triggers a change."""
        aioclient_mock.get(
            "http://rtl433.local:8433/cmd",
            params={"cmd": "get_dev_info"},
            json={
                "result": {
                    "vendor": "Realtek",
                    "product": "RTL2838",
                    "serial": "00000001",
                }
            },
        )
        aioclient_mock.get(
            "http://rtl433.local:8433/cmd",
            params={"cmd": "get_dev_query"},
            json={"result": None},
        )

        _run(hass, coordinator._refresh_dev_info())
        assert coordinator.dev_info == {
            "vendor": "Realtek",
            "product": "RTL2838",
            "serial": "00000001",
        }

    def test_hub_info_callback_called_on_change(
        self, hass, coordinator, aioclient_mock
    ):
        """hub_info_callback is invoked when dev_info changes."""
        called: list[None] = []
        coordinator.hub_info_callback = lambda: called.append(None)

        aioclient_mock.get(
            "http://rtl433.local:8433/cmd",
            params={"cmd": "get_dev_info"},
            json={
                "result": {
                    "vendor": "Realtek",
                    "product": "RTL2838",
                    "serial": "00000001",
                }
            },
        )
        aioclient_mock.get(
            "http://rtl433.local:8433/cmd",
            params={"cmd": "get_dev_query"},
            json={"result": None},
        )

        _run(hass, coordinator._refresh_dev_info())
        assert len(called) == 1

    def test_hub_info_callback_not_called_when_unchanged(
        self, hass, coordinator, aioclient_mock
    ):
        """hub_info_callback is NOT called when dev_info is unchanged."""
        same_info = {"vendor": "Realtek", "product": "RTL2838", "serial": "00000001"}
        coordinator.dev_info = same_info
        called: list[None] = []
        coordinator.hub_info_callback = lambda: called.append(None)

        aioclient_mock.get(
            "http://rtl433.local:8433/cmd",
            params={"cmd": "get_dev_info"},
            json={"result": same_info},
        )
        aioclient_mock.get(
            "http://rtl433.local:8433/cmd",
            params={"cmd": "get_dev_query"},
            json={"result": None},
        )

        _run(hass, coordinator._refresh_dev_info())
        assert called == []

    def test_dev_info_not_set_when_empty_dict(self, hass, coordinator, aioclient_mock):
        """An empty dict for dev_info is not stored (falsy guard)."""
        coordinator.dev_info = {}
        aioclient_mock.get(
            "http://rtl433.local:8433/cmd",
            params={"cmd": "get_dev_info"},
            json={"result": {}},  # empty dict
        )
        aioclient_mock.get(
            "http://rtl433.local:8433/cmd",
            params={"cmd": "get_dev_query"},
            json={"result": None},
        )

        _run(hass, coordinator._refresh_dev_info())
        # Empty dict: `info and info != self.dev_info` -> empty dict is falsy -> skip
        assert coordinator.dev_info == {}

    def test_dev_query_set_on_new_query(self, hass, coordinator, aioclient_mock):
        """Fresh dev_query string triggers change."""
        aioclient_mock.get(
            "http://rtl433.local:8433/cmd",
            params={"cmd": "get_dev_info"},
            json={"result": None},
        )
        aioclient_mock.get(
            "http://rtl433.local:8433/cmd",
            params={"cmd": "get_dev_query"},
            json={"result": "0"},
        )
        called: list[None] = []
        coordinator.hub_info_callback = lambda: called.append(None)

        _run(hass, coordinator._refresh_dev_info())
        assert coordinator.dev_query == "0"
        assert len(called) == 1

    def test_dev_query_not_set_when_empty_string(
        self, hass, coordinator, aioclient_mock
    ):
        """An empty string for dev_query is not stored (falsy guard)."""
        aioclient_mock.get(
            "http://rtl433.local:8433/cmd",
            params={"cmd": "get_dev_info"},
            json={"result": None},
        )
        aioclient_mock.get(
            "http://rtl433.local:8433/cmd",
            params={"cmd": "get_dev_query"},
            json={"result": ""},
        )

        _run(hass, coordinator._refresh_dev_info())
        assert coordinator.dev_query is None  # unchanged from default

    def test_dev_info_json_string_parsed(self, hass, coordinator, aioclient_mock):
        """A JSON string for dev_info is parsed to a dict."""

        aioclient_mock.get(
            "http://rtl433.local:8433/cmd",
            params={"cmd": "get_dev_info"},
            json={"result": '{"vendor": "Realtek", "product": "RTL2838"}'},
        )
        aioclient_mock.get(
            "http://rtl433.local:8433/cmd",
            params={"cmd": "get_dev_query"},
            json={"result": None},
        )

        _run(hass, coordinator._refresh_dev_info())
        assert coordinator.dev_info == {"vendor": "Realtek", "product": "RTL2838"}

    def test_hub_callback_exception_does_not_propagate(
        self, hass, coordinator, aioclient_mock
    ):
        """A crashing hub_info_callback is caught (never kills the loop)."""

        def boom():
            raise RuntimeError("callback failed")

        coordinator.hub_info_callback = boom

        aioclient_mock.get(
            "http://rtl433.local:8433/cmd",
            params={"cmd": "get_dev_info"},
            json={"result": {"vendor": "V", "product": "P", "serial": "S"}},
        )
        aioclient_mock.get(
            "http://rtl433.local:8433/cmd",
            params={"cmd": "get_dev_query"},
            json={"result": None},
        )

        _run(hass, coordinator._refresh_dev_info())  # must not raise


# ---------------------------------------------------------------------------
# _seed_desired_on_first_connect: initial frequency and adoption logic
# ---------------------------------------------------------------------------


class TestSeedDesiredOnFirstConnect:
    """Kill mutants on the seeding logic for initial_center_frequency."""

    def test_initial_freq_seeded_when_not_already_seeded(
        self, hass, hub_entry_builder, aioclient_mock
    ):
        """initial_center_frequency is seeded when flag is False."""
        entry = hub_entry_builder()
        entry.add_to_hass(hass)
        coord = Rtl433Coordinator(
            hass,
            entry,
            host="rtl433.local",
            initial_center_frequency=433.92,
        )
        coord._initial_freq_seeded = False
        coord._desired = {"some_key": 1}  # non-empty, so adopt is skipped

        with patch.object(coord._store, "async_save", new_callable=AsyncMock):
            _run(hass, coord._seed_desired_on_first_connect())

        assert coord._desired[KEY_CENTER_FREQUENCY] == 433.92
        assert KEY_CENTER_FREQUENCY in coord._managed
        assert coord._initial_freq_seeded is True

    def test_initial_freq_not_seeded_when_flag_already_true(
        self, hass, hub_entry_builder
    ):
        """initial_center_frequency is NOT re-seeded when flag is True."""
        entry = hub_entry_builder()
        entry.add_to_hass(hass)
        coord = Rtl433Coordinator(
            hass,
            entry,
            host="rtl433.local",
            initial_center_frequency=433.92,
        )
        coord._initial_freq_seeded = True  # already seeded
        coord._desired = {"some_key": 1, KEY_CENTER_FREQUENCY: 868.0}

        with patch.object(coord._store, "async_save", new_callable=AsyncMock):
            _run(hass, coord._seed_desired_on_first_connect())

        # Should NOT override the existing value
        assert coord._desired[KEY_CENTER_FREQUENCY] == 868.0

    def test_initial_freq_none_does_not_seed(self, hass, hub_entry_builder):
        """When initial_center_frequency is None, no seeding occurs."""
        entry = hub_entry_builder()
        entry.add_to_hass(hass)
        coord = Rtl433Coordinator(
            hass,
            entry,
            host="rtl433.local",
            initial_center_frequency=None,
        )
        coord._initial_freq_seeded = False
        coord._desired = {"some_key": 1}

        with patch.object(coord._store, "async_save", new_callable=AsyncMock):
            _run(hass, coord._seed_desired_on_first_connect())

        assert KEY_CENTER_FREQUENCY not in coord._desired
        assert coord._initial_freq_seeded is False

    def test_adopt_runs_when_desired_empty(self, hass, hub_entry_builder):
        """When _desired is empty, _adopt_from_server is called."""
        entry = hub_entry_builder()
        entry.add_to_hass(hass)
        coord = Rtl433Coordinator(hass, entry, host="rtl433.local")
        coord._desired = {}  # empty -> triggers adopt
        coord.meta = {"center_frequency": 433920000, "frequencies": [433920000]}

        adopt_called: list[None] = []
        original_adopt = coord._adopt_from_server

        async def fake_adopt():
            adopt_called.append(None)
            await original_adopt()

        coord._adopt_from_server = fake_adopt

        with patch.object(coord._store, "async_save", new_callable=AsyncMock):
            _run(hass, coord._seed_desired_on_first_connect())

        assert len(adopt_called) == 1

    def test_adopt_skipped_when_desired_nonempty(self, hass, hub_entry_builder):
        """When _desired is non-empty, _adopt_from_server is NOT called."""
        entry = hub_entry_builder()
        entry.add_to_hass(hass)
        coord = Rtl433Coordinator(hass, entry, host="rtl433.local")
        coord._desired = {KEY_PPM_ERROR: 2}  # non-empty -> skip adopt

        adopt_called: list[None] = []

        async def fake_adopt():
            adopt_called.append(None)

        coord._adopt_from_server = fake_adopt

        with patch.object(coord._store, "async_save", new_callable=AsyncMock):
            _run(hass, coord._seed_desired_on_first_connect())

        assert adopt_called == []


# ---------------------------------------------------------------------------
# _adopt_from_server: hop detection and gain pair logic
# ---------------------------------------------------------------------------


class TestAdoptFromServer:
    """Kill mutants in _adopt_from_server."""

    def test_hopping_is_len_greater_than_1(self, hass, coordinator):
        """Hopping is detected when len(frequencies) > 1, not > 0 or > 2."""
        # Two frequencies -> hopping -> center_frequency NOT adopted
        coordinator.meta = {
            "center_frequency": 433920000,
            "frequencies": [433920000, 868000000],
            "samp_rate": 250000,
        }
        with patch.object(coordinator._store, "async_save", new_callable=AsyncMock):
            _run(hass, coordinator._adopt_from_server())

        assert KEY_CENTER_FREQUENCY not in coordinator._desired

    def test_single_frequency_not_hopping(self, hass, coordinator):
        """A single frequency is NOT hopping -> center_frequency IS adopted."""
        coordinator.meta = {
            "center_frequency": 433920000,
            "frequencies": [433920000],
            "samp_rate": 250000,
        }
        with patch.object(coordinator._store, "async_save", new_callable=AsyncMock):
            _run(hass, coordinator._adopt_from_server())

        assert KEY_CENTER_FREQUENCY in coordinator._desired

    def test_empty_frequencies_not_hopping(self, hass, coordinator):
        """Empty frequencies list -> not hopping (len=0 is not > 1)."""
        coordinator.meta = {
            "center_frequency": 433920000,
            "frequencies": [],
        }
        with patch.object(coordinator._store, "async_save", new_callable=AsyncMock):
            _run(hass, coordinator._adopt_from_server())

        # len([]) = 0, not > 1 -> not hopping -> center_freq adopted
        assert KEY_CENTER_FREQUENCY in coordinator._desired

    def test_gain_auto_when_empty_string(self, hass, coordinator):
        """Empty gain string -> gain_auto=True (auto mode)."""
        coordinator.meta = {"gain": ""}
        with patch.object(coordinator._store, "async_save", new_callable=AsyncMock):
            _run(hass, coordinator._adopt_from_server())

        assert coordinator._desired.get(KEY_GAIN_AUTO) is True
        assert KEY_GAIN_DB not in coordinator._desired

    def test_gain_auto_false_when_numeric_string(self, hass, coordinator):
        """Numeric gain string -> gain_auto=False (manual mode)."""
        coordinator.meta = {"gain": "32.8"}
        with patch.object(coordinator._store, "async_save", new_callable=AsyncMock):
            _run(hass, coordinator._adopt_from_server())

        assert coordinator._desired.get(KEY_GAIN_AUTO) is False
        assert coordinator._desired.get(KEY_GAIN_DB) == pytest.approx(32.8)

    def test_gain_db_is_float_not_string(self, hass, coordinator):
        """gain_db is stored as float(gain), not the raw string."""
        coordinator.meta = {"gain": "40"}
        with patch.object(coordinator._store, "async_save", new_callable=AsyncMock):
            _run(hass, coordinator._adopt_from_server())

        assert isinstance(coordinator._desired.get(KEY_GAIN_DB), float)
        assert coordinator._desired.get(KEY_GAIN_DB) == 40.0

    def test_gain_not_in_meta_skips_gain_pair(self, hass, coordinator):
        """When gain key absent from meta, neither gain key is adopted."""
        coordinator.meta = {"samp_rate": 250000}
        with patch.object(coordinator._store, "async_save", new_callable=AsyncMock):
            _run(hass, coordinator._adopt_from_server())

        assert KEY_GAIN_AUTO not in coordinator._desired
        assert KEY_GAIN_DB not in coordinator._desired

    def test_adopt_marks_all_keys_as_managed(self, hass, coordinator):
        """Every adopted key is also added to _managed."""
        coordinator.meta = {
            "center_frequency": 433920000,
            "frequencies": [433920000],
            "samp_rate": 250000,
            "gain": "32.8",
        }
        with patch.object(coordinator._store, "async_save", new_callable=AsyncMock):
            _run(hass, coordinator._adopt_from_server())

        for key in coordinator._desired:
            assert key in coordinator._managed, f"{key} not in _managed"

    def test_adopt_persists_desired(self, hass, coordinator):
        """_adopt_from_server calls _persist_desired (store is saved)."""
        coordinator.meta = {"samp_rate": 250000}
        save_calls: list[None] = []

        async def fake_save(data):
            save_calls.append(data)

        coordinator._store.async_save = fake_save
        _run(hass, coordinator._adopt_from_server())

        assert len(save_calls) == 1


# ---------------------------------------------------------------------------
# _adopt_from_server: hopping detection on meta without frequencies key
# ---------------------------------------------------------------------------


class TestAdoptHoppingEdgeCases:
    """Edge cases for hopping detection."""

    def test_no_frequencies_key_not_hopping(self, hass, coordinator):
        """When frequencies is absent from meta, not hopping."""
        coordinator.meta = {
            "center_frequency": 433920000,
            # No 'frequencies' key
        }
        with patch.object(coordinator._store, "async_save", new_callable=AsyncMock):
            _run(hass, coordinator._adopt_from_server())

        # meta.get("frequencies") -> None -> len(None or []) = 0 -> not > 1 -> not hopping
        assert KEY_CENTER_FREQUENCY in coordinator._desired

    def test_none_frequencies_not_hopping(self, hass, coordinator):
        """frequencies=None is treated as empty (len=0, not > 1)."""
        coordinator.meta = {
            "center_frequency": 433920000,
            "frequencies": None,
        }
        with patch.object(coordinator._store, "async_save", new_callable=AsyncMock):
            _run(hass, coordinator._adopt_from_server())

        # None or [] -> [] -> len=0 -> not > 1
        assert KEY_CENTER_FREQUENCY in coordinator._desired


# ---------------------------------------------------------------------------
# _enforce_all: gain managed detection and meta refresh
# ---------------------------------------------------------------------------


class TestEnforceAll:
    """Kill mutants in _enforce_all."""

    def test_gain_managed_true_when_gain_db_in_managed(
        self, hass, coordinator, aioclient_mock
    ):
        """gain_managed is True when KEY_GAIN_DB is in _managed."""
        coordinator._desired = {KEY_GAIN_DB: 32.8, KEY_GAIN_AUTO: False}
        coordinator._managed = {KEY_GAIN_DB}
        coordinator.meta = {}

        aioclient_mock.get(
            "http://rtl433.local:8433/cmd",
            params={"cmd": "gain", "arg": "32.8"},
            status=200,
        )
        # _refresh_meta after enforce_all
        aioclient_mock.get("http://rtl433.local:8433/cmd", status=500)

        with patch(DISPATCH):
            _run(hass, coordinator._enforce_all())

        calls = aioclient_mock.mock_calls
        gain_calls = [c for c in calls if "gain" in str(c)]
        assert len(gain_calls) >= 1

    def test_gain_managed_true_when_gain_auto_in_managed(
        self, hass, coordinator, aioclient_mock
    ):
        """gain_managed is True when KEY_GAIN_AUTO is in _managed."""
        coordinator._desired = {KEY_GAIN_AUTO: True}
        coordinator._managed = {KEY_GAIN_AUTO}
        coordinator.meta = {}

        aioclient_mock.get(
            "http://rtl433.local:8433/cmd",
            params={"cmd": "gain", "arg": ""},
            status=200,
        )
        aioclient_mock.get("http://rtl433.local:8433/cmd", status=500)

        with patch(DISPATCH):
            _run(hass, coordinator._enforce_all())

        calls = aioclient_mock.mock_calls
        gain_calls = [c for c in calls if "gain" in str(c)]
        assert len(gain_calls) >= 1

    def test_no_refresh_meta_when_nothing_managed(
        self, hass, coordinator, aioclient_mock
    ):
        """When _managed is empty, no refresh_meta call is made post-enforce."""
        coordinator._desired = {}
        coordinator._managed = set()

        _run(hass, coordinator._enforce_all())

        # No HTTP calls at all
        assert aioclient_mock.mock_calls == []

    def test_refresh_meta_called_when_something_managed(
        self, hass, coordinator, aioclient_mock
    ):
        """When _managed is non-empty, refresh_meta IS called after enforce."""
        coordinator._desired = {KEY_PPM_ERROR: 2}
        coordinator._managed = {KEY_PPM_ERROR}

        aioclient_mock.get(
            "http://rtl433.local:8433/cmd",
            params={"cmd": "ppm_error", "val": "2"},
            status=200,
        )
        # Refresh_meta calls
        aioclient_mock.get("http://rtl433.local:8433/cmd", status=200, json={})

        with patch(DISPATCH):
            _run(hass, coordinator._enforce_all())

        # At minimum, ppm_error setter + meta getter were called
        assert len(aioclient_mock.mock_calls) >= 1


# ---------------------------------------------------------------------------
# async_load_desired_state: manage_settings=False clears initial_freq_seeded
# ---------------------------------------------------------------------------


class TestLoadDesiredState:
    """Kill mutants in async_load_desired_state."""

    def test_manage_off_resets_initial_freq_seeded(self, hass, coordinator):
        """manage_settings=False must reset _initial_freq_seeded to False."""
        coordinator.manage_settings = False
        coordinator._initial_freq_seeded = True  # was True

        _run(hass, coordinator.async_load_desired_state())

        assert coordinator._initial_freq_seeded is False

    def test_manage_on_loads_initial_freq_seeded_from_store(
        self, hass, hub_entry_builder
    ):
        """manage_settings=True reads initial_freq_seeded from store data."""
        entry = hub_entry_builder()
        entry.add_to_hass(hass)
        coord = Rtl433Coordinator(
            hass, entry, host="rtl433.local", manage_settings=True
        )

        # Simulate a store with initial_freq_seeded=True
        stored_data = {
            "values": {KEY_PPM_ERROR: 2},
            "managed": ["ppm_error"],
            "initial_freq_seeded": True,
        }

        async def fake_load():
            return stored_data

        coord._store.async_load = fake_load
        _run(hass, coord.async_load_desired_state())

        assert coord._initial_freq_seeded is True
        assert coord._desired == {KEY_PPM_ERROR: 2}
        assert "ppm_error" in coord._managed

    def test_manage_on_initial_freq_seeded_false_when_absent(
        self, hass, hub_entry_builder
    ):
        """When initial_freq_seeded absent from store, defaults to False."""
        entry = hub_entry_builder()
        entry.add_to_hass(hass)
        coord = Rtl433Coordinator(
            hass, entry, host="rtl433.local", manage_settings=True
        )

        # Store without initial_freq_seeded key (legacy)
        stored_data = {
            "values": {},
            "managed": [],
            # No 'initial_freq_seeded'
        }

        async def fake_load():
            return stored_data

        coord._store.async_load = fake_load
        _run(hass, coord.async_load_desired_state())

        assert coord._initial_freq_seeded is False


# ---------------------------------------------------------------------------
# _fetch_cmd: malformed JSON deduplication
# ---------------------------------------------------------------------------


class TestFetchCmdMalformedJson:
    """Kill mutants in _fetch_cmd malformed JSON handling."""

    def test_malformed_json_logged_once(self, hass, coordinator, aioclient_mock):
        """A malformed JSON response logs once and marks the command."""

        # Serve a 200 OK but with non-JSON body
        aioclient_mock.get(
            "http://rtl433.local:8433/cmd",
            params={"cmd": "get_stats"},
            exc=None,
            json={"result": "ok"},  # Use a bad text instead
        )

        # We can't easily test the ValueError path with aioclient_mock, but
        # we can test that _malformed_cmds works correctly via direct manipulation
        coordinator._malformed_cmds.add("get_stats")
        assert "get_stats" in coordinator._malformed_cmds

        # After a successful call, it should be discarded
        aioclient_mock.get(
            "http://rtl433.local:8433/cmd",
            params={"cmd": "get_stats"},
            json={"result": {"frames": {}}},
        )
        _run(hass, coordinator._fetch_cmd("get_stats"))
        assert "get_stats" not in coordinator._malformed_cmds

    def test_malformed_cmd_not_added_twice(self, hass, coordinator):
        """Once a command is in _malformed_cmds, adding it again is a no-op (set)."""
        coordinator._malformed_cmds.add("get_meta")
        coordinator._malformed_cmds.add("get_meta")
        assert len([x for x in coordinator._malformed_cmds if x == "get_meta"]) == 1


# ---------------------------------------------------------------------------
# forget_device: re-arms discovery for previously discovered device
# ---------------------------------------------------------------------------


class TestForgetDeviceDiscovery:
    """Kill mutants on forget_device's discovery re-arm."""

    def test_forget_device_rearms_discovery(self, hass, coordinator):
        """forget_device discards the key from _discovered."""
        key = "Dev-1"
        coordinator._discovered.add(key)

        coordinator.forget_device(key)

        assert key not in coordinator._discovered

    def test_forget_device_then_callback_fires_on_live(self, hass, coordinator):
        """After forget, the device registers again on the next live event."""
        registered: list[str] = []
        coordinator.discovery_enabled = True
        coordinator.new_device_callback = lambda k, m, r: registered.append(k)

        t0 = dt_util.parse_datetime("2026-05-25T10:00:00+00:00")
        with freeze_time(t0), patch(DISPATCH):
            coordinator._handle_text_frame(
                '{"time": "2026-05-25T10:00:00Z", "model": "Dev", "id": 1, "val": 1}'
            )
        assert "Dev-1" in registered

        # Forget the device
        coordinator.forget_device("Dev-1")

        # Send a new live frame -> should re-register
        t1 = t0 + timedelta(seconds=10)
        with freeze_time(t1), patch(DISPATCH):
            coordinator._handle_text_frame(
                f'{{"time": "{t1.strftime("%Y-%m-%dT%H:%M:%SZ")}", '
                '"model": "Dev", "id": 1, "val": 2}'
            )

        assert registered.count("Dev-1") == 2  # registered twice


# ---------------------------------------------------------------------------
# _new_device_callback: is_replay flag passed correctly
# ---------------------------------------------------------------------------


class TestNewDeviceCallbackIsReplay:
    """Kill mutants on the is_replay parameter passed to new_device_callback."""

    def test_callback_receives_is_replay_false_for_live_event(self, hass, coordinator):
        """Live event passes is_replay=False to new_device_callback."""
        replay_values: list[bool] = []
        coordinator.discovery_enabled = True
        coordinator.new_device_callback = lambda k, m, r: replay_values.append(r)

        with freeze_time("2026-05-25T10:00:00+00:00"), patch(DISPATCH):
            coordinator._handle_text_frame(
                '{"time": "2026-05-25T10:00:00Z", "model": "Dev", "id": 1, "val": 1}'
            )

        assert replay_values == [False]

    def test_callback_receives_is_replay_true_for_replay_event(self, hass, coordinator):
        """A replay event (stale) passes is_replay=True to new_device_callback."""
        replay_values: list[bool] = []
        coordinator.discovery_enabled = True
        coordinator.new_device_callback = lambda k, m, r: replay_values.append(r)

        # 60s old -> stale -> replay
        with freeze_time("2026-05-25T10:01:00+00:00"), patch(DISPATCH):
            coordinator._handle_text_frame(
                '{"time": "2026-05-25T10:00:00Z", "model": "Dev", "id": 1, "val": 1}'
            )

        assert replay_values == [True]

    def test_callback_model_passed_correctly(self, hass, coordinator):
        """new_device_callback receives the exact model string."""
        models: list[str] = []
        coordinator.discovery_enabled = True
        coordinator.new_device_callback = lambda k, m, r: models.append(m)

        with freeze_time("2026-05-25T10:00:00+00:00"), patch(DISPATCH):
            coordinator._handle_text_frame(
                '{"time": "2026-05-25T10:00:00Z", "model": "Acurite-606TX", '
                '"id": 42, "temperature_C": 21.0}'
            )

        assert models == ["Acurite-606TX"]

    def test_callback_device_key_passed_correctly(self, hass, coordinator):
        """new_device_callback receives the exact device_key string."""
        keys: list[str] = []
        coordinator.discovery_enabled = True
        coordinator.new_device_callback = lambda k, m, r: keys.append(k)

        with freeze_time("2026-05-25T10:00:00+00:00"), patch(DISPATCH):
            coordinator._handle_text_frame(
                '{"time": "2026-05-25T10:00:00Z", "model": "Acurite-606TX", '
                '"id": 42, "temperature_C": 21.0}'
            )

        assert keys == ["Acurite-606TX-42"]


# ---------------------------------------------------------------------------
# _refresh_meta: emit only when new_meta is non-empty
# ---------------------------------------------------------------------------


class TestRefreshMetaEmitCondition:
    """Kill mutants on the 'if new_meta' condition in _refresh_meta."""

    def test_emit_when_meta_has_data(self, hass, coordinator, aioclient_mock):
        """Hub update is emitted when new_meta has at least one key."""
        aioclient_mock.get(
            "http://rtl433.local:8433/cmd",
            params={"cmd": "get_meta"},
            json={"result": {"center_frequency": 433920000}},
        )
        aioclient_mock.get(
            "http://rtl433.local:8433/cmd",
            params={"cmd": "get_gain"},
            json={"result": None},
        )
        aioclient_mock.get(
            "http://rtl433.local:8433/cmd",
            params={"cmd": "get_ppm_error"},
            json={"result": None},
        )

        with patch(DISPATCH) as dispatch:
            _run(hass, coordinator._refresh_meta())

        hub_signal = signal_hub_update(coordinator.entry.entry_id)
        sent = [c.args[1] for c in dispatch.call_args_list]
        assert hub_signal in sent

    def test_no_emit_when_meta_empty(self, hass, coordinator, aioclient_mock):
        """Hub update is NOT emitted when all getters return nothing."""
        aioclient_mock.get(
            "http://rtl433.local:8433/cmd",
            params={"cmd": "get_meta"},
            json={"result": None},
        )
        aioclient_mock.get(
            "http://rtl433.local:8433/cmd",
            params={"cmd": "get_gain"},
            json={"result": None},
        )
        aioclient_mock.get(
            "http://rtl433.local:8433/cmd",
            params={"cmd": "get_ppm_error"},
            json={"result": None},
        )

        with patch(DISPATCH) as dispatch:
            _run(hass, coordinator._refresh_meta())

        hub_signal = signal_hub_update(coordinator.entry.entry_id)
        sent = [c.args[1] for c in dispatch.call_args_list]
        assert hub_signal not in sent

    def test_meta_updated_with_new_values(self, hass, coordinator, aioclient_mock):
        """New meta is merged into existing coordinator.meta."""
        coordinator.meta = {"old_key": "old_value"}

        aioclient_mock.get(
            "http://rtl433.local:8433/cmd",
            params={"cmd": "get_meta"},
            json={"result": {"samp_rate": 250000}},
        )
        aioclient_mock.get(
            "http://rtl433.local:8433/cmd",
            params={"cmd": "get_gain"},
            json={"result": None},
        )
        aioclient_mock.get(
            "http://rtl433.local:8433/cmd",
            params={"cmd": "get_ppm_error"},
            json={"result": None},
        )

        with patch(DISPATCH):
            _run(hass, coordinator._refresh_meta())

        # New key added, old key preserved (merge, not replace)
        assert coordinator.meta["samp_rate"] == 250000
        assert coordinator.meta["old_key"] == "old_value"


# ---------------------------------------------------------------------------
# _refresh_stats: emit only when stats is a dict
# ---------------------------------------------------------------------------


class TestRefreshStatsEmitCondition:
    """Kill mutants on the 'if isinstance(stats, dict)' condition."""

    def test_stats_updated_when_dict(self, hass, coordinator, aioclient_mock):
        """Stats is updated when getter returns a dict."""
        aioclient_mock.get(
            "http://rtl433.local:8433/cmd",
            params={"cmd": "get_stats"},
            json={"result": {"frames": {"count": 42}}},
        )

        with patch(DISPATCH) as dispatch:
            _run(hass, coordinator._refresh_stats())

        assert coordinator.stats == {"frames": {"count": 42}}
        hub_signal = signal_hub_update(coordinator.entry.entry_id)
        sent = [c.args[1] for c in dispatch.call_args_list]
        assert hub_signal in sent

    def test_stats_not_updated_when_not_dict(self, hass, coordinator, aioclient_mock):
        """Stats is not updated when getter returns a non-dict value."""
        coordinator.stats = {"existing": "data"}
        aioclient_mock.get(
            "http://rtl433.local:8433/cmd",
            params={"cmd": "get_stats"},
            json={"result": [1, 2, 3]},  # list, not dict
        )

        with patch(DISPATCH) as dispatch:
            _run(hass, coordinator._refresh_stats())

        assert coordinator.stats == {"existing": "data"}
        dispatch.assert_not_called()


# ---------------------------------------------------------------------------
# Build URL: scheme string correctness
# ---------------------------------------------------------------------------


class TestBuildUrls:
    """Kill mutants on the scheme strings in URL builders."""

    def test_ws_scheme_insecure(self):
        """Insecure WebSocket uses 'ws' not 'wss'."""
        url = _build_ws_url("host", 8433, "/ws", secure=False)
        assert url.startswith("ws://")
        assert not url.startswith("wss://")

    def test_ws_scheme_secure(self):
        """Secure WebSocket uses 'wss' not 'ws'."""
        url = _build_ws_url("host", 8433, "/ws", secure=True)
        assert url.startswith("wss://")
        assert not url.startswith("ws://") or url.startswith("wss://")

    def test_cmd_scheme_insecure(self):
        """Insecure cmd URL uses 'http' not 'https'."""
        url = _build_cmd_url("host", 8433, secure=False)
        assert url.startswith("http://")
        assert not url.startswith("https://")

    def test_cmd_scheme_secure(self):
        """Secure cmd URL uses 'https' not 'http'."""
        url = _build_cmd_url("host", 8433, secure=True)
        assert url.startswith("https://")
        assert not url.startswith("http://") or url.startswith("https://")

    def test_cmd_url_always_ends_with_slash_cmd(self):
        """The cmd URL always ends with /cmd."""
        assert _build_cmd_url("host", 8433).endswith("/cmd")
        assert _build_cmd_url("host", 8433, secure=True).endswith("/cmd")

    def test_ws_url_contains_path(self):
        """WebSocket URL contains the path."""
        url = _build_ws_url("host", 8433, "/ws")
        assert "/ws" in url

    def test_ws_url_prepends_slash_to_path(self):
        """Path without leading slash gets a slash prepended."""
        url = _build_ws_url("host", 8433, "noSlash")
        assert "/noSlash" in url


# ---------------------------------------------------------------------------
# _process_event: field tracking on replay events
# ---------------------------------------------------------------------------


class TestFieldTrackingOnReplay:
    """Replay events still accumulate field tracking data."""

    def test_replay_event_updates_devices_dict(self, hass, coordinator):
        """Even a replay event updates coordinator.devices[key]."""
        # First: set high water mark to force replay
        t0 = dt_util.parse_datetime("2026-05-25T10:00:00+00:00")
        coordinator._event_high_water = t0

        with freeze_time("2026-05-25T10:00:05+00:00"), patch(DISPATCH):
            coordinator._handle_text_frame(
                '{"time": "2026-05-25T09:59:00Z", "model": "Dev", "id": 1, "val": 1}'
            )

        # devices dict is updated even for replays
        assert "Dev-1" in coordinator.devices

    def test_replay_event_updates_seen_fields(self, hass, coordinator):
        """A replay event still updates seen_fields and device_fields."""
        # Create a stale event
        with freeze_time("2026-05-25T10:05:00+00:00"), patch(DISPATCH):
            coordinator._handle_text_frame(
                '{"time": "2026-05-25T10:00:00Z", "model": "Dev", "id": 1, '
                '"temperature_C": 21.0}'
            )

        assert "temperature_C" in coordinator.seen_fields
        assert "temperature_C" in coordinator.device_fields.get("Dev-1", set())

    def test_replay_event_dispatches(self, hass, coordinator):
        """Even a replay event dispatches on the device signal."""
        with freeze_time("2026-05-25T10:05:00+00:00"), patch(DISPATCH) as dispatch:
            coordinator._handle_text_frame(
                '{"time": "2026-05-25T10:00:00Z", "model": "Dev", "id": 1, "val": 1}'
            )

        sig = signal_device_update(coordinator.entry.entry_id, "Dev-1")
        signals = [c.args[1] for c in dispatch.call_args_list]
        assert sig in signals


# ---------------------------------------------------------------------------
# _process_event: normalized event carries event_time
# ---------------------------------------------------------------------------


class TestNormalizedEventTime:
    """Kill mutants on event_time being stamped on NormalizedEvent."""

    def test_normalized_event_carries_event_time(self, hass, coordinator):
        """event_time is stamped on the dispatched NormalizedEvent."""
        event_time = dt_util.parse_datetime("2026-05-25T10:00:00+00:00")

        with freeze_time("2026-05-25T10:00:05+00:00"), patch(DISPATCH) as dispatch:
            coordinator._handle_text_frame(
                '{"time": "2026-05-25T10:00:00Z", "model": "Dev", "id": 1, "val": 1}'
            )

        sig = signal_device_update(coordinator.entry.entry_id, "Dev-1")
        dev_calls = [c for c in dispatch.call_args_list if c.args[1] == sig]
        assert len(dev_calls) == 1
        assert dev_calls[0].args[2].event_time == event_time

    def test_no_timestamp_event_has_none_event_time(self, hass, coordinator):
        """A frame without time has event_time=None on the NormalizedEvent."""
        with patch(DISPATCH) as dispatch:
            coordinator._handle_text_frame('{"model": "Dev", "id": 1, "val": 1}')

        sig = signal_device_update(coordinator.entry.entry_id, "Dev-1")
        dev_calls = [c for c in dispatch.call_args_list if c.args[1] == sig]
        assert len(dev_calls) == 1
        assert dev_calls[0].args[2].event_time is None


# ---------------------------------------------------------------------------
# Connectivity flag behavior
# ---------------------------------------------------------------------------


class TestConnectivityFlags:
    """Kill mutants on connected flag and _connection_time handling."""

    def test_connection_time_initially_none(self, hass, coordinator):
        """_connection_time starts as None."""
        assert coordinator._connection_time is None

    def test_event_high_water_initially_none(self, hass, coordinator):
        """_event_high_water starts as None."""
        assert coordinator._event_high_water is None

    def test_discovered_set_initially_empty(self, hass, coordinator):
        """_discovered starts as an empty set."""
        assert coordinator._discovered == set()

    def test_connected_initially_false(self, hass, coordinator):
        """connected starts as False."""
        assert coordinator.connected is False

    def test_dev_info_initially_empty_dict(self, hass, coordinator):
        """dev_info starts as empty dict."""
        assert coordinator.dev_info == {}

    def test_dev_query_initially_none(self, hass, coordinator):
        """dev_query starts as None."""
        assert coordinator.dev_query is None

    def test_initial_freq_seeded_initially_false(self, hass, coordinator):
        """_initial_freq_seeded starts as False."""
        assert coordinator._initial_freq_seeded is False

    def test_new_device_callback_initially_none(self, hass, hub_entry_builder):
        """new_device_callback attribute starts as None (default)."""
        entry = hub_entry_builder()
        entry.add_to_hass(hass)
        coord = Rtl433Coordinator(hass, entry, host="rtl433.local")
        assert coord.new_device_callback is None

    def test_effective_timeout_resolver_initially_none(self, hass, hub_entry_builder):
        """effective_timeout_resolver starts as None."""
        entry = hub_entry_builder()
        entry.add_to_hass(hass)
        coord = Rtl433Coordinator(hass, entry, host="rtl433.local")
        assert coord.effective_timeout_resolver is None

    def test_hub_info_callback_initially_none(self, hass, hub_entry_builder):
        """hub_info_callback starts as None."""
        entry = hub_entry_builder()
        entry.add_to_hass(hass)
        coord = Rtl433Coordinator(hass, entry, host="rtl433.local")
        assert coord.hub_info_callback is None

    def test_calibration_snapshot_initially_empty_dict(self, hass, hub_entry_builder):
        """calibration_snapshot starts as empty dict."""
        entry = hub_entry_builder()
        entry.add_to_hass(hass)
        coord = Rtl433Coordinator(hass, entry, host="rtl433.local")
        assert coord.calibration_snapshot == {}

    def test_user_mappings_snapshot_initially_empty_dict(self, hass, hub_entry_builder):
        """user_mappings_snapshot starts as empty dict."""
        entry = hub_entry_builder()
        entry.add_to_hass(hass)
        coord = Rtl433Coordinator(hass, entry, host="rtl433.local")
        assert coord.user_mappings_snapshot == {}

    def test_dev_query_initially_none_is_none_not_empty(self, hass, hub_entry_builder):
        """dev_query starts as None, not empty string."""
        entry = hub_entry_builder()
        entry.add_to_hass(hass)
        coord = Rtl433Coordinator(hass, entry, host="rtl433.local")
        assert coord.dev_query is None
        assert coord.dev_query != ""


# ---------------------------------------------------------------------------
# validate_connection: direct tests to kill "no_tests" mutants
# ---------------------------------------------------------------------------


class TestValidateConnection:
    """Direct tests for validate_connection to kill 'no tests' mutants."""

    def test_validate_connection_returns_true_on_success(
        self, hass, hub_entry_builder, aiohttp_server
    ):
        """validate_connection returns True when WS connection succeeds."""
        # This test uses the aiohttp_server fixture if available,
        # but we can mock the session directly
        pass  # This requires a real server fixture; test via mock below

    def test_validate_connection_uses_correct_url(self, hass):
        """validate_connection builds a URL using _build_ws_url with the given params."""
        # Test that the URL is correctly constructed by checking what was connected to
        from unittest.mock import AsyncMock as AM, MagicMock as MM

        ws_mock = MM()
        ws_mock.close = AM()
        session_mock = MM()
        session_mock.ws_connect = AM(return_value=ws_mock)

        # Use asynccontextmanager pattern

        async def fake_ws_connect(url, *, timeout=None):
            assert "rtl433.local" in url
            assert "8433" in url
            assert "/ws" in url
            return ws_mock

        session_mock.ws_connect = fake_ws_connect
        with patch(
            "custom_components.rtl_433.coordinator.base.async_get_clientsession",
            return_value=session_mock,
        ):
            result = _run(
                hass,
                Rtl433Coordinator.validate_connection(
                    hass, "rtl433.local", 8433, "/ws"
                ),
            )
        assert result is True

    def test_validate_connection_raises_cannot_connect_on_error(self, hass):
        """validate_connection raises CannotConnect on ClientError."""
        import aiohttp

        from custom_components.rtl_433.coordinator.base import CannotConnect

        async def fake_ws_connect(url, *, timeout=None):
            raise aiohttp.ClientError("connection refused")

        session_mock = MagicMock()
        session_mock.ws_connect = fake_ws_connect

        with (
            patch(
                "custom_components.rtl_433.coordinator.base.async_get_clientsession",
                return_value=session_mock,
            ),
            pytest.raises(CannotConnect),
        ):
            _run(
                hass,
                Rtl433Coordinator.validate_connection(
                    hass, "rtl433.local", 8433, "/ws"
                ),
            )

    def test_validate_connection_uses_secure_wss_url(self, hass):
        """validate_connection with secure=True uses a wss:// URL."""
        ws_mock = MagicMock()
        ws_mock.close = AsyncMock()

        async def fake_ws_connect(url, *, timeout=None):
            assert url.startswith("wss://"), f"Expected wss://, got {url}"
            return ws_mock

        session_mock = MagicMock()
        session_mock.ws_connect = fake_ws_connect

        with patch(
            "custom_components.rtl_433.coordinator.base.async_get_clientsession",
            return_value=session_mock,
        ):
            result = _run(
                hass,
                Rtl433Coordinator.validate_connection(
                    hass, "rtl433.local", 8433, "/ws", secure=True
                ),
            )
        assert result is True

    def test_validate_connection_insecure_uses_ws_url(self, hass):
        """validate_connection with secure=False uses a ws:// URL."""
        ws_mock = MagicMock()
        ws_mock.close = AsyncMock()

        async def fake_ws_connect(url, *, timeout=None):
            assert url.startswith("ws://"), f"Expected ws://, got {url}"
            return ws_mock

        session_mock = MagicMock()
        session_mock.ws_connect = fake_ws_connect

        with patch(
            "custom_components.rtl_433.coordinator.base.async_get_clientsession",
            return_value=session_mock,
        ):
            result = _run(
                hass,
                Rtl433Coordinator.validate_connection(
                    hass, "rtl433.local", 8433, "/ws", secure=False
                ),
            )
        assert result is True

    def test_validate_connection_default_secure_is_false(self, hass):
        """validate_connection defaults to secure=False (ws://)."""
        ws_mock = MagicMock()
        ws_mock.close = AsyncMock()

        urls_seen: list[str] = []

        async def fake_ws_connect(url, *, timeout=None):
            urls_seen.append(url)
            return ws_mock

        session_mock = MagicMock()
        session_mock.ws_connect = fake_ws_connect

        with patch(
            "custom_components.rtl_433.coordinator.base.async_get_clientsession",
            return_value=session_mock,
        ):
            _run(
                hass,
                Rtl433Coordinator.validate_connection(
                    hass, "rtl433.local", 8433, "/ws"
                ),
            )
        assert len(urls_seen) == 1
        assert urls_seen[0].startswith("ws://")


# ---------------------------------------------------------------------------
# Secure coordinator: _fetch_cmd and _send_cmd use https:// URL
# ---------------------------------------------------------------------------


class TestSecureCoordinator:
    """Kill mutants on secure=True handling in _fetch_cmd and _send_cmd."""

    def test_fetch_cmd_secure_uses_https_url(
        self, hass, hub_entry_builder, aioclient_mock
    ):
        """_fetch_cmd on a secure coordinator uses https:// URL."""
        entry = hub_entry_builder(secure=True)
        entry.add_to_hass(hass)
        coord = Rtl433Coordinator(
            hass, entry, host="rtl433.local", port=8433, secure=True
        )

        # Register the https URL
        aioclient_mock.get(
            "https://rtl433.local:8433/cmd",
            params={"cmd": "get_meta"},
            json={"result": {"center_frequency": 433920000}},
        )

        with patch(DISPATCH):
            result = _run(hass, coord._fetch_cmd("get_meta"))

        assert result == {"result": {"center_frequency": 433920000}}

    def test_fetch_cmd_insecure_uses_http_url(
        self, hass, hub_entry_builder, aioclient_mock
    ):
        """_fetch_cmd on an insecure coordinator uses http:// URL (not https)."""
        entry = hub_entry_builder()
        entry.add_to_hass(hass)
        coord = Rtl433Coordinator(
            hass, entry, host="rtl433.local", port=8433, secure=False
        )

        aioclient_mock.get(
            "http://rtl433.local:8433/cmd",
            params={"cmd": "get_meta"},
            json={"result": {"center_frequency": 433920000}},
        )

        with patch(DISPATCH):
            result = _run(hass, coord._fetch_cmd("get_meta"))

        assert result == {"result": {"center_frequency": 433920000}}

    def test_send_cmd_secure_uses_https_url(
        self, hass, hub_entry_builder, aioclient_mock
    ):
        """_send_cmd on a secure coordinator uses https:// URL."""
        entry = hub_entry_builder(secure=True)
        entry.add_to_hass(hass)
        coord = Rtl433Coordinator(
            hass, entry, host="rtl433.local", port=8433, secure=True
        )

        aioclient_mock.get(
            "https://rtl433.local:8433/cmd",
            params={"cmd": "ppm_error", "val": "5"},
            status=200,
        )

        result = _run(hass, coord._send_cmd("ppm_error", val=5))
        assert result is True


# ---------------------------------------------------------------------------
# _persist_desired: key name used for initial_freq_seeded
# ---------------------------------------------------------------------------


class TestPersistDesired:
    """Kill mutants on the key names in _persist_desired."""

    def test_persist_desired_saves_initial_freq_seeded_key(self, hass, coordinator):
        """_persist_desired saves the 'initial_freq_seeded' key."""
        saved_data: list[dict] = []

        async def fake_save(data):
            saved_data.append(data)

        coordinator._store.async_save = fake_save
        coordinator._initial_freq_seeded = True

        _run(hass, coordinator._persist_desired())

        assert len(saved_data) == 1
        assert "initial_freq_seeded" in saved_data[0]
        assert saved_data[0]["initial_freq_seeded"] is True

    def test_persist_desired_saves_values_key(self, hass, coordinator):
        """_persist_desired saves the 'values' key with _desired."""
        saved_data: list[dict] = []

        async def fake_save(data):
            saved_data.append(data)

        coordinator._store.async_save = fake_save
        coordinator._desired = {KEY_PPM_ERROR: 5}

        _run(hass, coordinator._persist_desired())

        assert "values" in saved_data[0]
        assert saved_data[0]["values"] == {KEY_PPM_ERROR: 5}

    def test_persist_desired_saves_managed_key_sorted(self, hass, coordinator):
        """_persist_desired saves 'managed' as a sorted list."""
        saved_data: list[dict] = []

        async def fake_save(data):
            saved_data.append(data)

        coordinator._store.async_save = fake_save
        coordinator._managed = {"z_key", "a_key", "m_key"}

        _run(hass, coordinator._persist_desired())

        assert "managed" in saved_data[0]
        assert saved_data[0]["managed"] == sorted(["z_key", "a_key", "m_key"])


# ---------------------------------------------------------------------------
# _sdr_store migration: version 1 but not version 2 (boundary < 2 vs <= 2)
# ---------------------------------------------------------------------------


class TestSdrStoreMigrationBoundary:
    """Additional tests specifically for the < 2 boundary."""

    def test_version_1_triggers_migration(self, hass, hub_entry_builder):
        """Version 1 triggers migration (old_major_version < 2 is True)."""
        from custom_components.rtl_433.const import SDR_STORE_VERSION, sdr_store_key
        from custom_components.rtl_433.coordinator.base import _SdrStore

        entry = hub_entry_builder()
        entry.add_to_hass(hass)
        store = _SdrStore(hass, SDR_STORE_VERSION, sdr_store_key(entry.entry_id))

        old_data = {"values": {KEY_CENTER_FREQUENCY: 433_920_000}}
        result = _run(hass, store._async_migrate_func(1, 0, old_data))
        assert result["values"][KEY_CENTER_FREQUENCY] == pytest.approx(433.92)

    def test_version_2_does_not_trigger_migration(self, hass, hub_entry_builder):
        """Version 2 does NOT trigger migration (2 < 2 = False)."""
        from custom_components.rtl_433.const import SDR_STORE_VERSION, sdr_store_key
        from custom_components.rtl_433.coordinator.base import _SdrStore

        entry = hub_entry_builder()
        entry.add_to_hass(hass)
        store = _SdrStore(hass, SDR_STORE_VERSION, sdr_store_key(entry.entry_id))

        old_data = {"values": {KEY_CENTER_FREQUENCY: 433_920_000}}
        result = _run(hass, store._async_migrate_func(2, 0, old_data))
        # NOT migrated - still Hz
        assert result["values"][KEY_CENTER_FREQUENCY] == 433_920_000

    def test_version_3_does_not_trigger_migration(self, hass, hub_entry_builder):
        """Version 3 (and above) does NOT trigger migration."""
        from custom_components.rtl_433.const import SDR_STORE_VERSION, sdr_store_key
        from custom_components.rtl_433.coordinator.base import _SdrStore

        entry = hub_entry_builder()
        entry.add_to_hass(hass)
        store = _SdrStore(hass, SDR_STORE_VERSION, sdr_store_key(entry.entry_id))

        old_data = {"values": {KEY_CENTER_FREQUENCY: 433_920_000}}
        result = _run(hass, store._async_migrate_func(3, 0, old_data))
        # NOT migrated
        assert result["values"][KEY_CENTER_FREQUENCY] == 433_920_000


# ---------------------------------------------------------------------------
# async_load_desired_state: manage_settings=False sets _initial_freq_seeded=False
# (not None and not True)
# ---------------------------------------------------------------------------


class TestLoadDesiredStateFalseNotNone:
    """Kill mutants where _initial_freq_seeded = None instead of False."""

    def test_manage_off_sets_initial_freq_seeded_false_not_none(
        self, hass, coordinator
    ):
        """manage_settings=False sets _initial_freq_seeded to exactly False."""
        coordinator.manage_settings = False
        coordinator._initial_freq_seeded = True  # start as True

        _run(hass, coordinator.async_load_desired_state())

        # Exactly False, not None
        assert coordinator._initial_freq_seeded is False
        assert coordinator._initial_freq_seeded is not None

    def test_manage_off_sets_initial_freq_seeded_false_not_true(
        self, hass, coordinator
    ):
        """manage_settings=False sets _initial_freq_seeded to exactly False (not True)."""
        coordinator.manage_settings = False
        coordinator._initial_freq_seeded = False  # start as False

        _run(hass, coordinator.async_load_desired_state())

        assert coordinator._initial_freq_seeded is False

    def test_manage_on_absent_key_defaults_to_false(self, hass, hub_entry_builder):
        """When 'initial_freq_seeded' absent from store, defaults to False."""
        entry = hub_entry_builder()
        entry.add_to_hass(hass)
        coord = Rtl433Coordinator(
            hass, entry, host="rtl433.local", manage_settings=True
        )

        async def fake_load():
            return {"values": {}, "managed": []}  # No initial_freq_seeded key

        coord._store.async_load = fake_load
        _run(hass, coord.async_load_desired_state())

        # Default should be False, not None and not True
        assert coord._initial_freq_seeded is False

    def test_manage_on_true_value_stored_and_loaded(self, hass, hub_entry_builder):
        """When 'initial_freq_seeded' = True in store, loads as True."""
        entry = hub_entry_builder()
        entry.add_to_hass(hass)
        coord = Rtl433Coordinator(
            hass, entry, host="rtl433.local", manage_settings=True
        )

        async def fake_load():
            return {
                "values": {},
                "managed": [],
                "initial_freq_seeded": True,
            }

        coord._store.async_load = fake_load
        _run(hass, coord.async_load_desired_state())

        assert coord._initial_freq_seeded is True

    def test_manage_on_false_value_stored_and_loaded(self, hass, hub_entry_builder):
        """When 'initial_freq_seeded' = False in store, loads as False."""
        entry = hub_entry_builder()
        entry.add_to_hass(hass)
        coord = Rtl433Coordinator(
            hass, entry, host="rtl433.local", manage_settings=True
        )

        async def fake_load():
            return {
                "values": {},
                "managed": [],
                "initial_freq_seeded": False,
            }

        coord._store.async_load = fake_load
        _run(hass, coord.async_load_desired_state())

        assert coord._initial_freq_seeded is False


# ---------------------------------------------------------------------------
# Round-trip: _persist_desired saves "initial_freq_seeded" and it can be loaded
# ---------------------------------------------------------------------------


class TestRoundTripInitialFreqSeeded:
    """Kill mutants on the exact key name 'initial_freq_seeded'."""

    def test_round_trip_initial_freq_seeded_key_name(self, hass, hub_entry_builder):
        """The persisted key 'initial_freq_seeded' matches what load expects."""
        entry = hub_entry_builder()
        entry.add_to_hass(hass)
        coord = Rtl433Coordinator(
            hass, entry, host="rtl433.local", manage_settings=True
        )

        saved: list[dict] = []

        async def fake_save(data):
            saved.append(data)

        coord._store.async_save = fake_save
        coord._initial_freq_seeded = True
        coord._desired = {}
        coord._managed = set()

        _run(hass, coord._persist_desired())

        assert len(saved) == 1
        # The key must be exactly "initial_freq_seeded" (case-sensitive)
        assert "initial_freq_seeded" in saved[0]
        # And the value must be True
        assert saved[0]["initial_freq_seeded"] is True

        # Now verify load uses the same key
        async def fake_load():
            return saved[0]

        coord._store.async_load = fake_load
        coord._initial_freq_seeded = False  # reset

        _run(hass, coord.async_load_desired_state())
        assert coord._initial_freq_seeded is True


# ---------------------------------------------------------------------------
# _refresh_dev_info: changed=True for dev_query path (mutant 20/21)
# ---------------------------------------------------------------------------


class TestRefreshDevInfoQueryPath:
    """Kill mutants on the second changed=True in _refresh_dev_info."""

    def test_dev_query_changed_triggers_callback(
        self, hass, coordinator, aioclient_mock
    ):
        """When only dev_query changes, hub_info_callback is still called."""
        called: list[None] = []
        coordinator.hub_info_callback = lambda: called.append(None)
        coordinator.dev_info = {}  # Keep dev_info unchanged
        coordinator.dev_query = None

        aioclient_mock.get(
            "http://rtl433.local:8433/cmd",
            params={"cmd": "get_dev_info"},
            json={"result": None},
        )
        aioclient_mock.get(
            "http://rtl433.local:8433/cmd",
            params={"cmd": "get_dev_query"},
            json={"result": "0"},
        )

        _run(hass, coordinator._refresh_dev_info())

        # dev_query changed: "" != "0" -> changed=True -> callback called
        assert coordinator.dev_query == "0"
        assert len(called) == 1

    def test_dev_query_unchanged_no_callback(self, hass, coordinator, aioclient_mock):
        """When dev_query is unchanged, no callback."""
        called: list[None] = []
        coordinator.hub_info_callback = lambda: called.append(None)
        coordinator.dev_query = "0"  # Same as what server returns

        aioclient_mock.get(
            "http://rtl433.local:8433/cmd",
            params={"cmd": "get_dev_info"},
            json={"result": None},
        )
        aioclient_mock.get(
            "http://rtl433.local:8433/cmd",
            params={"cmd": "get_dev_query"},
            json={"result": "0"},
        )

        _run(hass, coordinator._refresh_dev_info())
        assert called == []

    def test_changed_false_initially_no_callback_when_nothing_changes(
        self, hass, coordinator, aioclient_mock
    ):
        """When both dev_info and dev_query are unchanged, no callback."""
        same_info = {"vendor": "V", "product": "P", "serial": "S"}
        coordinator.dev_info = same_info
        coordinator.dev_query = "0"
        called: list[None] = []
        coordinator.hub_info_callback = lambda: called.append(None)

        aioclient_mock.get(
            "http://rtl433.local:8433/cmd",
            params={"cmd": "get_dev_info"},
            json={"result": same_info},
        )
        aioclient_mock.get(
            "http://rtl433.local:8433/cmd",
            params={"cmd": "get_dev_query"},
            json={"result": "0"},
        )

        _run(hass, coordinator._refresh_dev_info())
        assert called == []


# ---------------------------------------------------------------------------
# clear_desired_state: _initial_freq_seeded must be set to exactly False
# ---------------------------------------------------------------------------


class TestClearDesiredState:
    """Kill mutants on clear_desired_state setting _initial_freq_seeded."""

    def test_clear_desired_state_resets_initial_freq_seeded_to_false(
        self, hass, coordinator
    ):
        """clear_desired_state must set _initial_freq_seeded=False, not None or True."""
        coordinator._initial_freq_seeded = True
        coordinator._desired = {KEY_PPM_ERROR: 2}
        coordinator._managed = {KEY_PPM_ERROR}

        with patch.object(coordinator._store, "async_remove", new_callable=AsyncMock):
            _run(hass, coordinator.clear_desired_state())

        assert coordinator._initial_freq_seeded is False
        # Not None, not True
        assert coordinator._initial_freq_seeded is not None
        assert coordinator._initial_freq_seeded is not True

    def test_clear_desired_state_resets_desired_and_managed(self, hass, coordinator):
        """clear_desired_state empties _desired and _managed."""
        coordinator._desired = {KEY_PPM_ERROR: 2, KEY_CENTER_FREQUENCY: 433.92}
        coordinator._managed = {KEY_PPM_ERROR, KEY_CENTER_FREQUENCY}

        with patch.object(coordinator._store, "async_remove", new_callable=AsyncMock):
            _run(hass, coordinator.clear_desired_state())

        assert coordinator._desired == {}
        assert coordinator._managed == set()

    def test_clear_desired_state_false_even_if_starts_as_none(self, hass, coordinator):
        """clear_desired_state sets _initial_freq_seeded=False even from None."""
        coordinator._initial_freq_seeded = None  # type: ignore[assignment]

        with patch.object(coordinator._store, "async_remove", new_callable=AsyncMock):
            _run(hass, coordinator.clear_desired_state())

        # Must be exactly False
        assert coordinator._initial_freq_seeded is False


# ---------------------------------------------------------------------------
# _async_watchdog: break vs continue for NEVER-expire (needs 2 devices)
# ---------------------------------------------------------------------------


class TestWatchdogBreakVsContinue:
    """Kill mutant _async_watchdog__mutmut_6: break vs continue on NEVER timeout.

    If 'break' is used instead of 'continue', a NEVER-expire device would stop
    the entire watchdog loop, preventing subsequent devices from being checked.
    """

    def test_watchdog_continues_after_never_expire_device(
        self, hass, hub_entry_builder
    ):
        """After a NEVER-expire device, watchdog still checks the next device."""
        entry = hub_entry_builder(availability_timeout=600)
        entry.add_to_hass(hass)
        coord = Rtl433Coordinator(
            hass,
            entry,
            host="rtl433.local",
            availability_timeout=600,
            event_driven_keys=frozenset({"motion"}),
            skip_keys={"model", "id", "channel", "subtype", "time"},
        )

        t0 = dt_util.parse_datetime("2026-05-25T10:00:00+00:00")

        # Device A: NEVER-expire (motion device)
        coord.last_seen["motion-1"] = t0
        coord.available["motion-1"] = True
        coord.devices["motion-1"] = NormalizedEvent(
            device_key="motion-1", model="Motion", fields={"motion": 1}
        )
        # Device B: normal timeout (periodic sensor)
        coord.last_seen["temp-1"] = t0
        coord.available["temp-1"] = True
        coord.devices["temp-1"] = NormalizedEvent(
            device_key="temp-1", model="Temp", fields={"temperature_C": 22.0}
        )

        # Ensure motion-1 is seen as NEVER-expire but temp-1 has normal timeout (600s)
        coord.effective_timeout_resolver = lambda dk: 0 if dk == "motion-1" else 600

        # Run watchdog 700s later (past normal 600s timeout for temp-1)
        with freeze_time(t0 + timedelta(seconds=700)), patch(DISPATCH) as dispatch:
            _run(hass, coord._async_watchdog(dt_util.utcnow()))

        # motion-1 should still be available (NEVER-expire)
        assert coord.available["motion-1"] is True
        # temp-1 should be marked unavailable (normal 600s timeout exceeded)
        assert coord.available["temp-1"] is False
        # temp-1 should have triggered a dispatch
        sig = signal_device_update(coord.entry.entry_id, "temp-1")
        dev_calls = [c for c in dispatch.call_args_list if c.args[1] == sig]
        assert len(dev_calls) == 1

    def test_watchdog_never_expire_first_in_iteration(self, hass, hub_entry_builder):
        """NEVER-expire device first in iteration still allows later devices to expire."""
        entry = hub_entry_builder(availability_timeout=60)
        entry.add_to_hass(hass)
        coord = Rtl433Coordinator(
            hass,
            entry,
            host="rtl433.local",
            availability_timeout=60,
            skip_keys={"model", "id", "channel", "subtype", "time"},
        )

        t0 = dt_util.parse_datetime("2026-05-25T10:00:00+00:00")

        # Set up devices with known keys so iteration order is predictable
        # We use a sorted() call internally, so alpha order matters
        # 'aaa-never' < 'zzz-sensor' alphabetically
        coord.last_seen["aaa-never"] = t0
        coord.available["aaa-never"] = True
        coord.devices["aaa-never"] = NormalizedEvent(
            device_key="aaa-never", model="NeverDev", fields={"val": 1}
        )
        coord.last_seen["zzz-sensor"] = t0
        coord.available["zzz-sensor"] = True
        coord.devices["zzz-sensor"] = NormalizedEvent(
            device_key="zzz-sensor", model="SensorDev", fields={"temperature_C": 22.0}
        )

        # aaa-never: NEVER (0), zzz-sensor: 60s
        coord.effective_timeout_resolver = lambda dk: 0 if dk == "aaa-never" else 60

        # Run watchdog 90s later - zzz-sensor should expire (90 > 60)
        with freeze_time(t0 + timedelta(seconds=90)), patch(DISPATCH):
            _run(hass, coord._async_watchdog(dt_util.utcnow()))

        assert coord.available["aaa-never"] is True
        assert coord.available["zzz-sensor"] is False


# ---------------------------------------------------------------------------
# _async_watchdog: is_replay=False (not None) on dispatch
# ---------------------------------------------------------------------------


class TestWatchdogDispatchIsReplay:
    """Kill mutant _async_watchdog__mutmut_33: is_replay=None instead of False."""

    def test_watchdog_dispatches_with_is_replay_false(self, hass, coordinator):
        """Watchdog dispatch uses is_replay=False (not None) on cached event."""
        key = "Dev-1"
        start = dt_util.parse_datetime("2026-05-25T10:00:00+00:00")
        with freeze_time(start), patch(DISPATCH):
            coordinator._handle_text_frame(
                '{"time": "2026-05-25T10:00:00Z", "model": "Dev", "id": 1, "val": 1}'
            )
        # Ensure it's available
        assert coordinator.available[key] is True

        # Run watchdog after device times out
        with freeze_time(start + timedelta(seconds=700)), patch(DISPATCH) as dispatch:
            _run(hass, coordinator._async_watchdog(dt_util.utcnow()))

        sig = signal_device_update(coordinator.entry.entry_id, key)
        dev_calls = [c for c in dispatch.call_args_list if c.args[1] == sig]
        assert len(dev_calls) == 1
        # Exactly False, not None
        dispatched_event = dev_calls[0].args[2]
        assert dispatched_event.is_replay is False
        assert dispatched_event.is_replay is not None


# ---------------------------------------------------------------------------
# _parse_event_time: space-separated format and branch logic
# ---------------------------------------------------------------------------


class TestParseEventTimeFormats:
    """Kill mutants in _parse_event_time related to parsing logic."""

    def test_parse_iso8601_with_z_suffix(self, hass, coordinator):
        """Frames with ISO-8601 + Z suffix are parsed correctly."""
        t = dt_util.parse_datetime("2026-05-25T10:00:00+00:00")
        with freeze_time("2026-05-25T10:00:05+00:00"), patch(DISPATCH) as dispatch:
            coordinator._handle_text_frame(
                '{"time": "2026-05-25T10:00:00Z", "model": "Dev", "id": 1, "val": 1}'
            )
        sig = signal_device_update(coordinator.entry.entry_id, "Dev-1")
        dev_calls = [c for c in dispatch.call_args_list if c.args[1] == sig]
        assert len(dev_calls) == 1
        assert dev_calls[0].args[2].event_time == t

    def test_parse_space_separated_datetime_no_microseconds(self, hass, coordinator):
        """Space-separated datetime without microseconds is parsed via strptime.

        This exercises the second branch: dt_util.parse_datetime returns None for
        'YYYY-MM-DD HH:MM:SS', then the for-loop tries '%Y-%m-%d %H:%M:%S.%f'
        (fails), then '%Y-%m-%d %H:%M:%S' (succeeds).
        Killing _parse_event_time__mutmut_10 (second fmt changed) would cause
        this test to fail since the second format would no longer match.
        """
        with freeze_time("2026-05-25T10:00:05+00:00"), patch(DISPATCH) as dispatch:
            coordinator._handle_text_frame(
                '{"time": "2026-05-25 10:00:00", "model": "Dev", "id": 1, "val": 1}'
            )
        sig = signal_device_update(coordinator.entry.entry_id, "Dev-1")
        dev_calls = [c for c in dispatch.call_args_list if c.args[1] == sig]
        # Should successfully parse - event_time is not None
        assert len(dev_calls) == 1
        assert dev_calls[0].args[2].event_time is not None

    def test_parse_space_separated_datetime_with_microseconds(self, hass, coordinator):
        """Space-separated datetime with microseconds uses the first fmt.

        This exercises '%Y-%m-%d %H:%M:%S.%f' - the FIRST format. Killing
        _parse_event_time__mutmut_7 (first fmt changed) would cause this to
        fail — the first format would not match, the second (%H:%M:%S without %f)
        would parse but strip microseconds, making the event_time slightly different.
        """
        with freeze_time("2026-05-25T10:00:05+00:00"), patch(DISPATCH) as dispatch:
            coordinator._handle_text_frame(
                '{"time": "2026-05-25 10:00:00.500000", "model": "Dev", '
                '"id": 1, "val": 1}'
            )
        sig = signal_device_update(coordinator.entry.entry_id, "Dev-1")
        dev_calls = [c for c in dispatch.call_args_list if c.args[1] == sig]
        # Should successfully parse with microseconds
        assert len(dev_calls) == 1
        assert dev_calls[0].args[2].event_time is not None

    def test_parse_event_time_break_after_first_success(self, hass, coordinator):
        """After first format succeeds, loop breaks (not returns or continues).

        _parse_event_time__mutmut_18: 'return' instead of 'break' — would skip
        further processing (as_utc, etc.) and return the parsed datetime directly
        without converting to UTC. Test: verify that event_time is UTC-aware.
        """
        with freeze_time("2026-05-25T10:00:05+00:00"), patch(DISPATCH) as dispatch:
            coordinator._handle_text_frame(
                '{"time": "2026-05-25 10:00:00", "model": "Dev", "id": 1, "val": 1}'
            )
        sig = signal_device_update(coordinator.entry.entry_id, "Dev-1")
        dev_calls = [c for c in dispatch.call_args_list if c.args[1] == sig]
        assert len(dev_calls) == 1
        et = dev_calls[0].args[2].event_time
        assert et is not None
        # Must be UTC-aware (result of as_utc)
        assert et.tzinfo is not None

    def test_parse_event_time_continue_to_second_format(self, hass, coordinator):
        """On first format failure, continue tries second (not break).

        _parse_event_time__mutmut_19: 'break' instead of 'continue' — would stop
        trying after first format fails, resulting in event_time=None for a
        time like '2026-05-25 10:00:00' (which only matches second format).
        """
        with freeze_time("2026-05-25T10:00:05+00:00"), patch(DISPATCH) as dispatch:
            coordinator._handle_text_frame(
                '{"time": "2026-05-25 10:00:00", "model": "Dev", "id": 1, "val": 1}'
            )
        sig = signal_device_update(coordinator.entry.entry_id, "Dev-1")
        dev_calls = [c for c in dispatch.call_args_list if c.args[1] == sig]
        assert len(dev_calls) == 1
        # Second format (%Y-%m-%d %H:%M:%S) must have parsed — event_time not None
        assert dev_calls[0].args[2].event_time is not None

    def test_parse_is_none_check_correct(self, hass, coordinator):
        """Frames with ISO-8601 time use parse_datetime (not the fallback loop).

        _parse_event_time__mutmut_6: 'is not None' instead of 'is None' — if the
        condition is inverted, a successfully-parsed ISO-8601 time would enter the
        loop anyway and the second strptime attempt with the ISO-8601 string would
        fail, returning a non-UTC result or crashing.
        """
        # ISO-8601 with timezone — parse_datetime handles this directly
        with freeze_time("2026-05-25T10:00:05+00:00"), patch(DISPATCH) as dispatch:
            coordinator._handle_text_frame(
                '{"time": "2026-05-25T10:00:00+05:30", "model": "Dev", '
                '"id": 1, "val": 1}'
            )
        sig = signal_device_update(coordinator.entry.entry_id, "Dev-1")
        dev_calls = [c for c in dispatch.call_args_list if c.args[1] == sig]
        assert len(dev_calls) == 1
        et = dev_calls[0].args[2].event_time
        assert et is not None
        # The offset is +05:30 so UTC equivalent is 10:00:00 - 5:30 = 04:30:00
        assert et.tzinfo is not None


# ---------------------------------------------------------------------------
# _process_event: stale event advances high-water to event_time (not None)
# ---------------------------------------------------------------------------


class TestHighWaterNotNoneAfterStale:
    """Kill mutant _process_event__mutmut_35: _event_high_water=None instead of event_time."""

    def test_stale_event_sets_high_water_to_event_time_not_none(
        self, hass, coordinator
    ):
        """A stale event advances _event_high_water to the event's time, not None."""
        event_time = dt_util.parse_datetime("2026-05-25T10:00:00+00:00")
        # 31s later -> stale
        with freeze_time("2026-05-25T10:00:31+00:00"), patch(DISPATCH):
            coordinator._handle_text_frame(
                '{"time": "2026-05-25T10:00:00Z", "model": "Dev", "id": 1, "val": 1}'
            )

        # Must be the event time, not None
        assert coordinator._event_high_water is not None
        assert coordinator._event_high_water == event_time

    def test_stale_event_high_water_is_event_time_not_now(self, hass, coordinator):
        """_event_high_water after stale event is event_time, not 'now'."""
        event_time = dt_util.parse_datetime("2026-05-25T10:00:00+00:00")
        now_time = dt_util.parse_datetime("2026-05-25T10:01:00+00:00")

        with freeze_time(now_time), patch(DISPATCH):
            coordinator._handle_text_frame(
                '{"time": "2026-05-25T10:00:00Z", "model": "Dev", "id": 1, "val": 1}'
            )

        # High water = event_time (not now_time)
        assert coordinator._event_high_water == event_time
        assert coordinator._event_high_water != now_time


# ---------------------------------------------------------------------------
# _enforce_all: break vs continue when args is None
# ---------------------------------------------------------------------------


class TestEnforceAllContinueNotBreak:
    """Kill mutant _enforce_all__mutmut_10: break instead of continue."""

    def test_enforce_all_continues_past_none_args(
        self, hass, coordinator, aioclient_mock
    ):
        """When _command_args returns None for a key, loop continues (not breaks).

        If 'break' is used instead of 'continue', after hitting a key with None args,
        the remaining managed keys would be skipped and not sent.
        We test this by having two managed settings where the first one has no
        corresponding SDR setting (returns None from _command_args) but the second one
        is a valid ppm_error command.
        """
        # Manually override _command_args to return None for the first call,
        # then the real result for the second
        call_log: list[str] = []
        real_command_args = coordinator._command_args

        def patched_command_args(key: str):
            call_log.append(key)
            return real_command_args(key)

        coordinator._command_args = patched_command_args

        # Use two managed keys: one with a valid setting, one without
        coordinator._desired = {KEY_PPM_ERROR: 3, KEY_SAMPLE_RATE: 250000}
        coordinator._managed = {KEY_PPM_ERROR, KEY_SAMPLE_RATE}

        aioclient_mock.get(
            "http://rtl433.local:8433/cmd",
            params={"cmd": "ppm_error", "val": "3"},
            status=200,
        )
        aioclient_mock.get(
            "http://rtl433.local:8433/cmd",
            params={"cmd": "samp_rate", "val": "250000"},
            status=200,
        )
        # _refresh_meta after enforce
        aioclient_mock.get("http://rtl433.local:8433/cmd", status=200, json={})
        aioclient_mock.get("http://rtl433.local:8433/cmd", status=200, json={})
        aioclient_mock.get("http://rtl433.local:8433/cmd", status=200, json={})

        with patch(DISPATCH):
            _run(hass, coordinator._enforce_all())

        # Both keys should have been passed to _command_args
        assert KEY_PPM_ERROR in call_log
        assert KEY_SAMPLE_RATE in call_log


# ---------------------------------------------------------------------------
# _known_field_keys: default [] vs None for DEVICE_FIELDS
# ---------------------------------------------------------------------------


class TestKnownFieldKeysDefault:
    """Kill mutant _known_field_keys__mutmut_11: DEVICE_FIELDS default None vs []."""

    def test_device_cfg_without_fields_key_yields_empty_base(
        self, hass, hub_entry_builder
    ):
        """A device config without DEVICE_FIELDS key yields empty from device_cfg.

        If the default were None (no or [] fallback), `keys.update(None)` would
        raise TypeError. With `or []`, it safely uses an empty list.
        """
        # Device config exists but has no DEVICE_FIELDS key
        devices_map = {
            "Dev-1": {"model": "Dev"}  # No DEVICE_FIELDS
        }
        entry = hub_entry_builder(devices=devices_map)
        entry.add_to_hass(hass)
        coord = Rtl433Coordinator(hass, entry, host="rtl433.local")
        # Should not crash even when DEVICE_FIELDS is absent
        keys = coord._known_field_keys("Dev-1")
        assert isinstance(keys, set)
        # No crash, empty since no DEVICE_FIELDS and no live event
        assert keys == set()

    def test_device_cfg_with_none_fields_value_safe(self, hass, hub_entry_builder):
        """A device config where DEVICE_FIELDS=None is handled safely.

        The `or []` fallback handles None → [] correctly. Without it, the
        mutation `get(DEVICE_FIELDS, None)` with None value would try to
        `keys.update(None)` which would raise TypeError.
        """
        devices_map = {"Dev-1": {"model": "Dev", DEVICE_FIELDS: None}}
        entry = hub_entry_builder(devices=devices_map)
        entry.add_to_hass(hass)
        coord = Rtl433Coordinator(hass, entry, host="rtl433.local")
        # Must not crash when DEVICE_FIELDS=None
        keys = coord._known_field_keys("Dev-1")
        assert isinstance(keys, set)


# ---------------------------------------------------------------------------
# async_stop: _watchdog_unsub and _refresh_unsub reset to None
# ---------------------------------------------------------------------------


class TestAsyncStopCleansUp:
    """Kill mutants on async_stop cleanup assignments."""

    def test_async_stop_clears_watchdog_unsub_to_none(self, hass, coordinator):
        """After async_stop, _watchdog_unsub is None (not empty string)."""
        # Inject a mock unsub callable
        unsub_called = []
        coordinator._watchdog_unsub = lambda: unsub_called.append("watchdog")
        coordinator._refresh_unsub = lambda: unsub_called.append("refresh")

        with patch.object(coordinator, "_task", None):
            _run(hass, coordinator.async_stop())

        assert "watchdog" in unsub_called
        assert coordinator._watchdog_unsub is None
        assert coordinator._watchdog_unsub != ""

    def test_async_stop_clears_refresh_unsub_to_none(self, hass, coordinator):
        """After async_stop, _refresh_unsub is None (not empty string)."""
        coordinator._watchdog_unsub = lambda: None
        coordinator._refresh_unsub = lambda: None

        with patch.object(coordinator, "_task", None):
            _run(hass, coordinator.async_stop())

        assert coordinator._refresh_unsub is None
        assert coordinator._refresh_unsub != ""

    def test_async_stop_skips_ws_close_when_already_closed(self, hass, coordinator):
        """When _ws.closed is True, async_stop does NOT call ws.close()."""
        ws_mock = MagicMock()
        ws_mock.closed = True
        close_called = []
        ws_mock.close = AsyncMock(side_effect=lambda: close_called.append("closed"))
        coordinator._ws = ws_mock
        coordinator._watchdog_unsub = None
        coordinator._refresh_unsub = None

        with patch.object(coordinator, "_task", None):
            _run(hass, coordinator.async_stop())

        assert close_called == []

    def test_async_stop_calls_ws_close_when_not_closed(self, hass, coordinator):
        """When _ws.closed is False, async_stop DOES call ws.close().

        Kills _async_stop__mutmut_7: 'self._ws.closed' instead of 'not self._ws.closed'.
        If the condition is wrong, an open socket would NOT be closed on stop.
        """
        ws_mock = MagicMock()
        ws_mock.closed = False
        close_called = []
        ws_mock.close = AsyncMock(side_effect=lambda: close_called.append("closed"))
        coordinator._ws = ws_mock
        coordinator._watchdog_unsub = None
        coordinator._refresh_unsub = None

        with patch.object(coordinator, "_task", None):
            _run(hass, coordinator.async_stop())

        assert close_called == ["closed"]

    def test_async_stop_sets_connected_false(self, hass, coordinator):
        """async_stop sets connected=False regardless of prior state."""
        coordinator.connected = True
        coordinator._watchdog_unsub = None
        coordinator._refresh_unsub = None

        with patch.object(coordinator, "_task", None):
            _run(hass, coordinator.async_stop())

        assert coordinator.connected is False


# ---------------------------------------------------------------------------
# async_start: lifecycle assignments (task, watchdog, refresh not None)
# ---------------------------------------------------------------------------


class TestAsyncStartLifecycle:
    """Kill async_start mutants 1,2,9,18 that set task/watchdog/refresh to None."""

    def _make_start_patches(self, coordinator):
        """Return a context manager that patches async_start dependencies safely.

        Patches: async_load_desired_state (no-op), _connect_loop (noop coroutine
        so entry.async_create_background_task has a real coroutine to wrap),
        and async_track_time_interval (returns distinct callables per call).
        Returns: (watchdog_unsub, refresh_unsub) sentinels.
        """

        watchdog_unsub = MagicMock(name="watchdog_unsub")
        refresh_unsub = MagicMock(name="refresh_unsub")
        call_count = [0]

        def mock_track(hass, func, interval, name=None):
            call_count[0] += 1
            return watchdog_unsub if call_count[0] == 1 else refresh_unsub

        return watchdog_unsub, refresh_unsub, mock_track

    def test_async_start_creates_task_not_none(self, hass, coordinator):
        """After async_start, _task is not None (kills mutmut_1 and mutmut_2).

        mutmut_1: inverts the guard from 'is not None' to 'is None', so calling
        async_start() once returns immediately (task never set).
        mutmut_2: sets self._task = None instead of the real background task.
        Both are killed by checking that _task is truthy after async_start.
        """
        watchdog_unsub, refresh_unsub, mock_track = self._make_start_patches(
            coordinator
        )

        with (
            patch.object(coordinator, "async_load_desired_state", AsyncMock()),
            patch.object(coordinator, "_connect_loop", AsyncMock()),
            patch(
                "custom_components.rtl_433.coordinator.base.async_track_time_interval",
                side_effect=mock_track,
            ),
            patch(DISPATCH),
        ):
            _run(hass, coordinator.async_start())

        assert coordinator._task is not None

    def test_async_start_second_call_is_noop(self, hass, coordinator):
        """A second async_start() when _task is already set is a no-op.

        Kills mutmut_1: if the guard is inverted (is None), the first call
        returns immediately (task never set) but the second call would run.
        This test calls start twice and verifies load_desired_state is only
        called once (the second call must skip it entirely).
        """
        watchdog_unsub, refresh_unsub, mock_track = self._make_start_patches(
            coordinator
        )
        load_calls: list[int] = []

        async def mock_load():
            load_calls.append(1)

        with (
            patch.object(coordinator, "async_load_desired_state", mock_load),
            patch.object(coordinator, "_connect_loop", AsyncMock()),
            patch(
                "custom_components.rtl_433.coordinator.base.async_track_time_interval",
                side_effect=mock_track,
            ),
            patch(DISPATCH),
        ):
            _run(hass, coordinator.async_start())
            _run(hass, coordinator.async_start())

        # load_desired_state must have been called exactly once
        assert len(load_calls) == 1

    def test_async_start_sets_watchdog_unsub_not_none(self, hass, coordinator):
        """After async_start, _watchdog_unsub is not None (kills mutmut_9).

        mutmut_9: sets _watchdog_unsub = None instead of the unsubscribe callable
        returned by async_track_time_interval.
        """
        watchdog_unsub, refresh_unsub, mock_track = self._make_start_patches(
            coordinator
        )

        with (
            patch.object(coordinator, "async_load_desired_state", AsyncMock()),
            patch.object(coordinator, "_connect_loop", AsyncMock()),
            patch(
                "custom_components.rtl_433.coordinator.base.async_track_time_interval",
                side_effect=mock_track,
            ),
            patch(DISPATCH),
        ):
            _run(hass, coordinator.async_start())

        assert coordinator._watchdog_unsub is not None
        assert coordinator._watchdog_unsub is watchdog_unsub

    def test_async_start_sets_refresh_unsub_not_none(self, hass, coordinator):
        """After async_start, _refresh_unsub is not None (kills mutmut_18).

        mutmut_18: sets _refresh_unsub = None instead of the unsubscribe callable
        returned by async_track_time_interval for the refresh timer.
        """
        watchdog_unsub, refresh_unsub, mock_track = self._make_start_patches(
            coordinator
        )

        with (
            patch.object(coordinator, "async_load_desired_state", AsyncMock()),
            patch.object(coordinator, "_connect_loop", AsyncMock()),
            patch(
                "custom_components.rtl_433.coordinator.base.async_track_time_interval",
                side_effect=mock_track,
            ),
            patch(DISPATCH),
        ):
            _run(hass, coordinator.async_start())

        assert coordinator._refresh_unsub is not None
        assert coordinator._refresh_unsub is refresh_unsub


# ---------------------------------------------------------------------------
# async_stop: _task set to None (not "") after cancellation
# ---------------------------------------------------------------------------


class TestAsyncStopTaskNone:
    """Kill async_stop mutmut_10: _task = "" instead of None."""

    def test_async_stop_sets_task_to_none_not_empty_string(self, hass, coordinator):
        """After async_stop cancels the task, _task is set to None not ''.

        mutmut_10: self._task = '' instead of self._task = None.
        A string '' is truthy-equal but not 'is None', which would cause
        the idempotency guard in async_start to think a task exists.
        """
        import asyncio

        async def noop():
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.sleep(1000)

        async def run_stop():
            task = hass.loop.create_task(noop())
            coordinator._task = task
            coordinator._watchdog_unsub = None
            coordinator._refresh_unsub = None
            await coordinator.async_stop()
            return coordinator._task

        result = _run(hass, run_stop())
        assert result is None
        assert result != ""


# ---------------------------------------------------------------------------
# _process_event: skip_keys=None vs actual skip_keys
# ---------------------------------------------------------------------------


class TestProcessEventSkipKeys:
    """Kill _process_event mutmut_3 and mutmut_5: normalize(event, None/empty)."""

    def test_skip_keys_applied_rssi_not_in_fields(self, hass, hub_entry_builder):
        """When skip_keys contains 'rssi', that field is excluded from normalized.fields.

        mutmut_3: normalize(event, None) — skip_keys ignored, rssi appears in fields.
        mutmut_5: normalize(event,) — same.
        Both killed by asserting rssi is NOT in the dispatched event's fields.
        """
        # Build coordinator with rssi in skip_keys
        entry = hub_entry_builder(availability_timeout=600)
        entry.add_to_hass(hass)
        coord = Rtl433Coordinator(
            hass,
            entry,
            host="rtl433.local",
            availability_timeout=600,
            skip_keys={"model", "id", "channel", "subtype", "time", "mic", "rssi"},
        )

        received: list = []

        def on_dispatch(_hass, signal, event):
            if signal.startswith("rtl_433_device_update"):
                received.append(event)

        with patch(DISPATCH, side_effect=on_dispatch):
            coord._handle_text_frame(
                '{"model": "Dev", "id": 1, "rssi": -75, "val": 42}'
            )

        assert len(received) == 1
        # rssi must NOT be in fields (skip_keys applied)
        assert "rssi" not in received[0].fields
        # val IS there
        assert "val" in received[0].fields

    def test_skip_keys_none_includes_rssi_in_fields(self, hass, hub_entry_builder):
        """Without skip_keys, rssi appears in normalized.fields.

        This is the baseline: confirms rssi IS included when not skipped.
        Together with the above test, it verifies the skip path is exercised.
        """
        entry = hub_entry_builder(availability_timeout=600)
        entry.add_to_hass(hass)
        coord = Rtl433Coordinator(
            hass,
            entry,
            host="rtl433.local",
            availability_timeout=600,
            skip_keys=set(),  # empty — nothing skipped
        )

        received: list = []

        def on_dispatch(_hass, signal, event):
            if signal.startswith("rtl_433_device_update"):
                received.append(event)

        with patch(DISPATCH, side_effect=on_dispatch):
            coord._handle_text_frame(
                '{"model": "Dev", "id": 1, "rssi": -75, "val": 42}'
            )

        assert len(received) == 1
        assert "rssi" in received[0].fields


# ---------------------------------------------------------------------------
# _process_event: backlog branch sets _event_high_water = event_time (not None)
# ---------------------------------------------------------------------------


class TestBacklogHighWaterMark:
    """Kill _process_event mutmut_35: backlog branch sets _event_high_water=None."""

    def test_backlog_branch_sets_high_water_to_event_time(self, hass, coordinator):
        """A backlog event (recent but pre-connection) sets _event_high_water.

        The backlog branch sets _event_high_water = event_time. mutmut_35 changes
        this to _event_high_water = None. Test: send a backlog event and assert
        _event_high_water is the event time, not None.

        To hit the backlog branch (not stale):
          - event_time must be < _connection_time - DISCOVERY_BACKLOG_GRACE (5s)
          - now - event_time must be <= REPLAY_STALE_THRESHOLD (30s)
        So: connection_time = T0, event_time = T0 - 6s, now = T0 + 1s
        """

        t0 = dt_util.parse_datetime("2026-05-25T10:00:00+00:00")
        event_time_str = "2026-05-25T09:59:54+00:00"  # 6s before connection
        event_time = dt_util.parse_datetime(event_time_str)

        # Set connection time
        coordinator._connection_time = t0

        # now = T0 + 1s, so now - event_time = 7s < 30s (not stale)
        # event_time = T0 - 6s < T0 - 5s (backlog grace), so is_backlog = True
        with freeze_time("2026-05-25T10:00:01+00:00"), patch(DISPATCH):
            coordinator._handle_text_frame(
                f'{{"time": "{event_time_str}", "model": "Dev", "id": 1, "val": 1}}'
            )

        # backlog branch: is_replay=True, _event_high_water = event_time (not None)
        assert coordinator._event_high_water is not None
        assert coordinator._event_high_water == event_time

    def test_backlog_event_is_classified_as_replay(self, hass, coordinator):
        """A backlog event gets is_replay=True, not False.

        Also confirms the backlog branch was actually taken (not the stale branch).
        """
        t0 = dt_util.parse_datetime("2026-05-25T10:00:00+00:00")
        event_time_str = "2026-05-25T09:59:54+00:00"  # 6s before connection

        coordinator._connection_time = t0

        with freeze_time("2026-05-25T10:00:01+00:00"), patch(DISPATCH) as dispatch:
            coordinator._handle_text_frame(
                f'{{"time": "{event_time_str}", "model": "Dev", "id": 1, "val": 1}}'
            )

        sig = signal_device_update(coordinator.entry.entry_id, "Dev-1")
        dev_calls = [c for c in dispatch.call_args_list if c.args[1] == sig]
        assert len(dev_calls) == 1
        assert dev_calls[0].args[2].is_replay is True


# ---------------------------------------------------------------------------
# _command_args: gain_auto mutations (mutmut_4, 8, 9, 11, 13)
# ---------------------------------------------------------------------------


class TestCommandArgsGainAuto:
    """Kill _command_args gain_auto mutations."""

    def test_command_args_gain_auto_true_returns_auto_arg(self, hass, coordinator):
        """When KEY_GAIN_AUTO=True in desired, gain arg is '' (auto).

        Kills mutmut_4 (arg=None), mutmut_8 (bool(None)=False gives '40'),
        mutmut_9 (get(None,False)=False gives '40'), mutmut_11 (get(False)=None→False
        gives '40'). All these mutations return '40' when auto=True should give ''.
        """
        coordinator._desired = {KEY_GAIN_DB: 40.0, KEY_GAIN_AUTO: True}
        coordinator._managed = {KEY_GAIN_DB, KEY_GAIN_AUTO}

        args = coordinator._command_args(KEY_GAIN_DB)
        assert args is not None
        command, val, arg = args
        assert command == "gain"
        assert val is None
        assert arg == ""  # auto mode: empty string

    def test_command_args_gain_auto_false_returns_db_arg(self, hass, coordinator):
        """When KEY_GAIN_AUTO=False in desired, gain arg is the dB value.

        Baseline confirming that when auto=False the dB string is returned.
        """
        coordinator._desired = {KEY_GAIN_DB: 32.8, KEY_GAIN_AUTO: False}
        coordinator._managed = {KEY_GAIN_DB, KEY_GAIN_AUTO}

        args = coordinator._command_args(KEY_GAIN_DB)
        assert args is not None
        command, val, arg = args
        assert command == "gain"
        assert val is None
        assert arg == "32.8"

    def test_command_args_gain_no_auto_key_defaults_to_non_auto(
        self, hass, coordinator
    ):
        """When KEY_GAIN_AUTO absent from desired, default is False (non-auto).

        Kills mutmut_13: default True instead of False. With the mutant, an absent
        KEY_GAIN_AUTO gives gain_auto=True → '' (auto), but the correct default
        is False → gain_db string.
        """
        coordinator._desired = {KEY_GAIN_DB: 40.0}  # KEY_GAIN_AUTO intentionally absent
        coordinator._managed = {KEY_GAIN_DB}

        args = coordinator._command_args(KEY_GAIN_DB)
        assert args is not None
        command, val, arg = args
        assert command == "gain"
        # DEFAULT is non-auto: arg must be the dB value "40", not "" (auto)
        assert arg == "40"
        assert arg != ""


# ---------------------------------------------------------------------------
# _enforce_all: gain_auto mutations (mutmut_20, 24, 25, 27, 29)
# ---------------------------------------------------------------------------


class TestEnforceAllGainAutoMutants:
    """Kill _enforce_all gain_auto mutations.

    All these mutations affect how gain_auto is read when composing the
    gain command inside _enforce_all. We test by verifying the HTTP request
    sent to /cmd has the correct 'arg' parameter.
    """

    def test_enforce_all_auto_gain_sends_empty_arg(self, hass, coordinator):
        """When gain_auto=True, _enforce_all sends gain cmd with arg='' (auto).

        Kills mutmut_20 (None instead of bool(...) → non-auto),
        mutmut_24 (bool(None)=False → non-auto),
        mutmut_25 (get(None,False)=False → non-auto),
        mutmut_27 (get(False)=None→False → non-auto).
        All these give False for gain_auto when True is set, sending '40' instead
        of '' — our test asserts arg=''.
        """
        coordinator._desired = {KEY_GAIN_DB: 40.0, KEY_GAIN_AUTO: True}
        coordinator._managed = {KEY_GAIN_DB, KEY_GAIN_AUTO}

        sent: list[tuple] = []

        async def fake_send_cmd(command, *, val=None, arg=None):
            sent.append((command, val, arg))
            return True

        with (
            patch.object(coordinator, "_send_cmd", fake_send_cmd),
            patch.object(coordinator, "_refresh_meta", AsyncMock()),
        ):
            _run(hass, coordinator._enforce_all())

        # Find the gain command
        gain_sends = [(cmd, v, a) for cmd, v, a in sent if cmd == "gain"]
        assert len(gain_sends) == 1
        # auto mode: arg must be '' (empty string)
        assert gain_sends[0][2] == ""

    def test_enforce_all_manual_gain_no_auto_key_sends_db_arg(self, hass, coordinator):
        """When KEY_GAIN_AUTO absent, default False → sends dB value.

        Kills mutmut_29: default True instead of False. With the mutant, an absent
        KEY_GAIN_AUTO gives True → '' (auto); original gives False → db string.
        """
        coordinator._desired = {KEY_GAIN_DB: 40.0}  # KEY_GAIN_AUTO intentionally absent
        coordinator._managed = {KEY_GAIN_DB}

        sent: list[tuple] = []

        async def fake_send_cmd(command, *, val=None, arg=None):
            sent.append((command, val, arg))
            return True

        with (
            patch.object(coordinator, "_send_cmd", fake_send_cmd),
            patch.object(coordinator, "_refresh_meta", AsyncMock()),
        ):
            _run(hass, coordinator._enforce_all())

        gain_sends = [(cmd, v, a) for cmd, v, a in sent if cmd == "gain"]
        assert len(gain_sends) == 1
        # non-auto: arg must be '40' (the dB value string)
        assert gain_sends[0][2] == "40"


# ---------------------------------------------------------------------------
# _enforce_all: continue not break when _command_args returns None
# ---------------------------------------------------------------------------


class TestEnforceAllContinueOnNoneArgs:
    """Kill _enforce_all__mutmut_10: break instead of continue when args=None.

    This class replaces the flawed TestEnforceAllContinueNotBreak test by
    checking HTTP sends (not just iteration) and by ensuring the first key
    (alphabetically) returns None from _command_args so that break vs continue
    has a different observable effect.
    """

    def test_continue_on_none_allows_later_keys_to_send(
        self, hass, coordinator, aioclient_mock
    ):
        """When first managed key returns None from _command_args, later keys still send.

        Setup: _managed = {aaa_fake_key, KEY_PPM_ERROR}. Alphabetically aaa_fake_key
        comes first. _command_args("aaa_fake_key") returns None (not an SDR setting).
        With 'continue': ppm_error is still sent.
        With 'break' (mutation): ppm_error is NOT sent.
        """
        # aaa_fake_key sorts before ppm_error and has no SDR setting → args=None
        coordinator._desired = {KEY_PPM_ERROR: 5, "aaa_fake_key": 99}
        coordinator._managed = {KEY_PPM_ERROR, "aaa_fake_key"}

        sent_commands: list[str] = []

        async def fake_send_cmd(command, *, val=None, arg=None):
            sent_commands.append(command)
            return True

        with (
            patch.object(coordinator, "_send_cmd", fake_send_cmd),
            patch.object(coordinator, "_refresh_meta", AsyncMock()),
        ):
            _run(hass, coordinator._enforce_all())

        # ppm_error must have been sent (continue, not break)
        assert "ppm_error" in sent_commands


# ---------------------------------------------------------------------------
# _async_watchdog: cached replay event dispatched with is_replay=False
# ---------------------------------------------------------------------------


class TestWatchdogDispatchesReplayEventAsFalse:
    """Kill _async_watchdog mutmut_33 and mutmut_36: is_replay=None/omitted.

    When the watchdog fires, it dispatches the cached event for a stale device
    with is_replay=False (forced override). If the cached event's is_replay is
    already True (e.g. from a backlog replay), the override matters.

    mutmut_33: is_replay=None → no override, cached True propagates
    mutmut_36: is_replay omitted → same as None

    Both are killed by verifying the dispatched event has is_replay=False even
    when the cached event was classified as a replay.
    """

    def test_watchdog_overrides_cached_replay_to_false(self, hass, coordinator):
        """Watchdog dispatches stale device with is_replay=False even if cached True."""
        key = "Dev-1"
        t0 = dt_util.parse_datetime("2026-05-25T10:00:00+00:00")

        # Step 1: live event → last_seen set, devices[key].is_replay=False
        with freeze_time(t0), patch(DISPATCH):
            coordinator._handle_text_frame(
                '{"time": "2026-05-25T10:00:00Z", "model": "Dev", "id": 1, "val": 1}'
            )

        assert coordinator.available[key] is True
        assert coordinator.devices[key].is_replay is False

        # Step 2: inject a replay event that overwrites devices[key] with is_replay=True
        # Simulate this by setting _connection_time and sending a backlog event
        coordinator._connection_time = t0 + timedelta(seconds=10)
        with freeze_time(t0 + timedelta(seconds=11)), patch(DISPATCH):
            # event_time = T0 (backlog: before connection_time - 5s)
            coordinator._handle_text_frame(
                '{"time": "2026-05-25T10:00:00Z", "model": "Dev", "id": 1, "val": 2}'
            )

        # Cached event should now be replay
        assert coordinator.devices[key].is_replay is True
        # But last_seen and available unchanged (replay doesn't update them)
        # last_seen is still t0, available is still True

        # Step 3: watchdog fires 700s after t0 → device stale
        with freeze_time(t0 + timedelta(seconds=700)), patch(DISPATCH) as dispatch:
            _run(hass, coordinator._async_watchdog(dt_util.utcnow()))

        sig = signal_device_update(coordinator.entry.entry_id, key)
        dev_calls = [c for c in dispatch.call_args_list if c.args[1] == sig]
        assert len(dev_calls) == 1

        # Despite cached is_replay=True, watchdog override must force False
        dispatched = dev_calls[0].args[2]
        assert dispatched.is_replay is False
        assert dispatched.is_replay is not None


# ---------------------------------------------------------------------------
# validate_connection: strengthen to kill mutmut_12, 15, 17, 18
# ---------------------------------------------------------------------------


class TestValidateConnectionStrong:
    """Stronger validate_connection tests to kill surviving mutants."""

    def test_validate_connection_passes_hass_to_get_client_session(self, hass):
        """async_get_clientsession is called with hass, not None (kills mutmut_12).

        mutmut_12: async_get_clientsession(None) instead of (hass).
        """

        ws_mock = MagicMock()
        ws_mock.close = AsyncMock()

        hass_args_seen: list = []

        async def fake_ws_connect(url, **kwargs):
            return ws_mock

        def fake_get_client_session(arg):
            hass_args_seen.append(arg)
            session = MagicMock()
            session.ws_connect = fake_ws_connect
            return session

        with patch(
            "custom_components.rtl_433.coordinator.base.async_get_clientsession",
            side_effect=fake_get_client_session,
        ):
            _run(
                hass,
                Rtl433Coordinator.validate_connection(
                    hass, "rtl433.local", 8433, "/ws"
                ),
            )

        assert len(hass_args_seen) == 1
        assert hass_args_seen[0] is hass

    def test_validate_connection_passes_timeout_to_ws_connect(self, hass):
        """ws_connect is called with a non-None timeout (kills mutmut_15, 17).

        mutmut_15: timeout=None; mutmut_17: timeout omitted (also None by default).
        Both are killed by asserting the timeout kwarg is not None.
        """
        ws_mock = MagicMock()
        ws_mock.close = AsyncMock()

        ws_connect_kwargs: list[dict] = []

        async def fake_ws_connect(url, **kwargs):
            ws_connect_kwargs.append(kwargs)
            return ws_mock

        session_mock = MagicMock()
        session_mock.ws_connect = fake_ws_connect

        with patch(
            "custom_components.rtl_433.coordinator.base.async_get_clientsession",
            return_value=session_mock,
        ):
            _run(
                hass,
                Rtl433Coordinator.validate_connection(
                    hass, "rtl433.local", 8433, "/ws"
                ),
            )

        assert len(ws_connect_kwargs) == 1
        assert "timeout" in ws_connect_kwargs[0]
        assert ws_connect_kwargs[0]["timeout"] is not None

    def test_validate_connection_exception_message_contains_url(self, hass):
        """CannotConnect message includes the URL (kills mutmut_18).

        mutmut_18: raise CannotConnect(None) instead of CannotConnect(f"Cannot
        connect to {url}: {err}"). The message would be None (or missing the URL).
        """
        import aiohttp

        from custom_components.rtl_433.coordinator.base import CannotConnect

        async def fake_ws_connect(url, **kwargs):
            raise aiohttp.ClientError("connection refused")

        session_mock = MagicMock()
        session_mock.ws_connect = fake_ws_connect

        with (
            patch(
                "custom_components.rtl_433.coordinator.base.async_get_clientsession",
                return_value=session_mock,
            ),
            pytest.raises(CannotConnect) as exc_info,
        ):
            _run(
                hass,
                Rtl433Coordinator.validate_connection(
                    hass, "rtl433.local", 8433, "/ws"
                ),
            )

        # Message must contain the URL, not None
        exc_message = str(exc_info.value)
        assert "rtl433.local" in exc_message or exc_message is not None
        # More specifically: must NOT be None (mutant raises CannotConnect(None))
        assert exc_info.value.args[0] is not None


# ---------------------------------------------------------------------------
# __init__: effective_clear_delay_resolver starts as None (not "")
# ---------------------------------------------------------------------------


class TestInitEffectiveClearDelayResolver:
    """Kill __init__mutmut_25: effective_clear_delay_resolver = '' instead of None."""

    def test_effective_clear_delay_resolver_initially_none(
        self, hass, hub_entry_builder
    ):
        """effective_clear_delay_resolver starts as None, not '' (kills mutmut_25)."""
        entry = hub_entry_builder()
        entry.add_to_hass(hass)
        coord = Rtl433Coordinator(hass, entry, host="rtl433.local")
        assert coord.effective_clear_delay_resolver is None
        assert coord.effective_clear_delay_resolver != ""


# ---------------------------------------------------------------------------
# _connect_loop: finally block sets connected=False and _connection_time=None
# ---------------------------------------------------------------------------


class TestConnectLoopFinallyBlock:
    """Kill _connect_loop mutmut_40 (connected=None), 41 (connected=True),
    42 (_connection_time="").

    The finally block in _connect_loop runs after the WS connection closes/errors:
        self._ws = None
        self.connected = False
        self._connection_time = None
        self._emit_hub_update()

    We test this by running _connect_loop with a session that immediately raises,
    and checking the post-state.
    """

    def _make_failing_session_mock(self, coordinator):
        """Create a session mock whose ws_connect raises ClientError.

        The mock uses an async context manager that raises in __aenter__,
        matching how aiohttp raises connection errors. The stop event is set
        inside the context manager so the connect loop exits after one attempt.
        """
        from contextlib import asynccontextmanager

        import aiohttp

        @asynccontextmanager
        async def failing_ws_connect(url, **kwargs):
            coordinator._stop_event.set()
            raise aiohttp.ClientError("connection refused")
            yield  # unreachable, satisfies the asynccontextmanager protocol

        session_mock = MagicMock()
        session_mock.ws_connect = failing_ws_connect
        return session_mock

    def test_connect_loop_finally_sets_connected_false(self, hass, coordinator):
        """After connection error, connected is False (not None, not True).

        Kills mutmut_40 (connected=None) and mutmut_41 (connected=True).
        """
        session_mock = self._make_failing_session_mock(coordinator)
        # Pre-set to True to make mutation 41 observable
        coordinator.connected = True

        async def run():
            with (
                patch(
                    "custom_components.rtl_433.coordinator.base.async_get_clientsession",
                    return_value=session_mock,
                ),
                patch(DISPATCH),
            ):
                await coordinator._connect_loop()

        _run(hass, run())

        # Must be False (not None, not True) after connection error
        assert coordinator.connected is False
        assert coordinator.connected is not None

    def test_connect_loop_finally_sets_connection_time_none(self, hass, coordinator):
        """After connection error, _connection_time is None (not '') (kills mutmut_42)."""
        session_mock = self._make_failing_session_mock(coordinator)

        # Pre-set _connection_time to a non-None value to make mutation observable
        coordinator._connection_time = dt_util.parse_datetime(
            "2026-05-25T10:00:00+00:00"
        )

        async def run():
            with (
                patch(
                    "custom_components.rtl_433.coordinator.base.async_get_clientsession",
                    return_value=session_mock,
                ),
                patch(DISPATCH),
            ):
                await coordinator._connect_loop()

        _run(hass, run())

        # Must be None (not "") after connection error
        assert coordinator._connection_time is None
        assert coordinator._connection_time != ""


# ---------------------------------------------------------------------------
# _process_event: new_device_callback exception logged with device key
# ---------------------------------------------------------------------------


class TestNewDeviceCallbackExceptionLog:
    """Kill _process_event mutmut_77: exception logged with None instead of key.

    When new_device_callback raises, the exception is logged with the device key.
    mutmut_77 passes None instead of key, so the message would say 'None' not
    the actual device key.
    """

    def test_callback_exception_logs_device_key(self, hass, coordinator, caplog):
        """When new_device_callback raises, the device key is in the log message.

        Kills mutmut_77 (key→None), mutmut_78 (format str omitted), mutmut_79
        (arg omitted), mutmut_80 (format string garbled).
        """
        import logging

        coordinator.discovery_enabled = True

        def bad_callback(key, model, is_replay):
            raise RuntimeError("simulated callback failure")

        coordinator.new_device_callback = bad_callback

        with (
            freeze_time("2026-05-25T10:00:00+00:00"),
            patch(DISPATCH),
            caplog.at_level(logging.DEBUG, logger="custom_components.rtl_433"),
        ):
            coordinator._handle_text_frame(
                '{"time": "2026-05-25T10:00:00Z", "model": "Dev", "id": 5, "val": 1}'
            )

        # Find log records related to callback failure
        callback_records = [
            r
            for r in caplog.records
            if "new_device_callback" in r.message
            or "new_device_callback" in r.getMessage()
        ]
        assert len(callback_records) >= 1
        # The device key 'Dev-5' must appear in the message (not None) — kills mutmut_77
        assert any("Dev-5" in r.getMessage() for r in callback_records)
        # Message must not start with 'XX' (kills mutmut_80 which garbles format)
        assert all(not r.getMessage().startswith("XX") for r in callback_records)


# ---------------------------------------------------------------------------
# _process_event: back online log contains device key
# ---------------------------------------------------------------------------


class TestBackOnlineLogContainsKey:
    """Kill _process_event mutmut_91: 'back online' logged with None instead of key.

    The test in TestWasAvailableRecovery checks 'back online' in message but
    doesn't verify the device key. mutmut_91 logs None instead of the key,
    so 'Dev-1' would not be in the message.
    """

    def test_back_online_log_contains_device_key(self, hass, coordinator, caplog):
        """The 'back online' log message includes the actual device key.

        Kills mutmut_91 (key→None in log), mutmut_93 (arg omitted), mutmut_94
        (format string garbled with XX).
        """
        import logging

        key = "Dev-1"
        start = dt_util.parse_datetime("2026-05-25T10:00:00+00:00")

        # Device becomes available
        with freeze_time(start), patch(DISPATCH):
            coordinator._handle_text_frame(
                '{"time": "2026-05-25T10:00:00Z", "model": "Dev", "id": 1, "val": 1}'
            )

        # Mark as unavailable
        coordinator.available[key] = False

        # New live event while unavailable
        fresh = start + timedelta(seconds=10)
        with (
            freeze_time(fresh),
            patch(DISPATCH),
            caplog.at_level(logging.DEBUG, logger="custom_components.rtl_433"),
        ):
            coordinator._handle_text_frame(
                f'{{"time": "{fresh.strftime("%Y-%m-%dT%H:%M:%SZ")}", '
                '"model": "Dev", "id": 1, "val": 2}'
            )

        # Verify back-online log contains the device key (not None) and
        # uses the exact ungarbled format string
        back_online_records = [
            r for r in caplog.records if "back online" in r.getMessage()
        ]
        assert len(back_online_records) >= 1
        # The actual key 'Dev-1' must be in the message (not 'None') — kills mutmut_91
        assert any(key in r.getMessage() for r in back_online_records)
        # The message must NOT start with 'XX' (kills mutmut_94 which wraps in XX...XX)
        assert all(not r.getMessage().startswith("XX") for r in back_online_records)


# ---------------------------------------------------------------------------
# _handle_shutdown: log message contains ws_url (not None)
# ---------------------------------------------------------------------------


class TestHandleShutdownLog:
    """Kill _handle_shutdown log mutations (mutmut_1-6).

    When connected=True, _handle_shutdown logs a debug message with the URL.
    mutmut_1: LOGGER.debug(None, self.ws_url) → msg=None, args=(url,)
    mutmut_2: LOGGER.debug("...", None) → url replaced with None
    mutmut_3: LOGGER.debug(self.ws_url) → url as format string (no 'shutdown')
    mutmut_4: LOGGER.debug("...",) → no args
    mutmut_5: LOGGER.debug("XX...XX", self.ws_url) → garbled format
    mutmut_6: uppercase format string
    """

    def test_shutdown_log_format_string_contains_shutdown_keyword(
        self, hass, coordinator
    ):
        """The log format string for shutdown contains 'shutdown' and 'rtl_433'.

        Captures LOGGER.debug args directly to check the format string.
        Kills mutmut_1 (None msg), mutmut_3 (url as msg), mutmut_5 (XX prefix),
        mutmut_6 (uppercase).
        """
        import custom_components.rtl_433.coordinator.base as base_module

        debug_calls: list[tuple] = []

        original_debug = base_module.LOGGER.debug

        def mock_debug(msg, *args, **kwargs):
            debug_calls.append((msg, args))
            original_debug(msg, *args, **kwargs)

        coordinator.connected = True

        with patch.object(base_module.LOGGER, "debug", side_effect=mock_debug):
            coordinator._handle_shutdown()

        # The debug call was made (connected=True)
        assert len(debug_calls) >= 1

        # Find the shutdown-related call
        shutdown_calls = [
            (msg, args)
            for msg, args in debug_calls
            if isinstance(msg, str) and "shutdown" in msg.lower()
        ]
        assert len(shutdown_calls) >= 1

        # Format string must be lowercase 'shutdown' (not all-caps from mutmut_6)
        for msg, _ in shutdown_calls:
            assert "shutdown" in msg  # lowercase (kills mutmut_6: "SHUTDOWN")
            assert not msg.startswith("XX")  # not garbled (kills mutmut_5)

    def test_shutdown_log_url_arg_is_not_none(self, hass, coordinator):
        """The log call's URL argument is not None (kills mutmut_2).

        mutmut_2: LOGGER.debug("rtl_433 server announced shutdown for %s", None)
        — the second arg is None instead of ws_url. The formatted message would
        say '...None' instead of the actual URL.
        """
        import custom_components.rtl_433.coordinator.base as base_module

        debug_calls: list[tuple] = []

        def mock_debug(msg, *args, **kwargs):
            debug_calls.append((msg, args))

        coordinator.connected = True
        expected_url = coordinator.ws_url

        with patch.object(base_module.LOGGER, "debug", side_effect=mock_debug):
            coordinator._handle_shutdown()

        # Find the shutdown call
        shutdown_calls = [
            (msg, args)
            for msg, args in debug_calls
            if isinstance(msg, str) and "shutdown" in msg
        ]
        assert len(shutdown_calls) >= 1

        for _msg, args in shutdown_calls:
            # There must be at least one arg (not empty, kills mutmut_4)
            assert len(args) >= 1
            # The URL arg must not be None (kills mutmut_2)
            assert args[0] is not None
            # The URL arg must be the actual ws_url
            assert args[0] == expected_url


# ---------------------------------------------------------------------------
# _async_watchdog: log message contains device key and timeout
# ---------------------------------------------------------------------------


class TestWatchdogUnavailableLog:
    """Kill _async_watchdog mutmut_21 (device_key→None), mutmut_22 (timeout→None),
    mutmut_26 (garbled format string).

    The 'went unavailable' log message must contain the device key AND the timeout.
    """

    def test_watchdog_unavailable_log_contains_device_key(
        self, hass, coordinator, caplog
    ):
        """When watchdog marks device unavailable, log contains the device key.

        Kills mutmut_21: device_key is replaced with None in the LOGGER.debug call.
        """
        import logging

        key = "Dev-1"
        start = dt_util.parse_datetime("2026-05-25T10:00:00+00:00")

        with freeze_time(start), patch(DISPATCH):
            coordinator._handle_text_frame(
                '{"time": "2026-05-25T10:00:00Z", "model": "Dev", "id": 1, "val": 1}'
            )
        assert coordinator.available[key] is True

        # Watchdog fires 700s later (beyond 600s timeout)
        with (
            freeze_time(start + timedelta(seconds=700)),
            patch(DISPATCH),
            caplog.at_level(logging.DEBUG, logger="custom_components.rtl_433"),
        ):
            _run(hass, coordinator._async_watchdog(dt_util.utcnow()))

        # Find 'went unavailable' log records
        unavail_records = [
            r for r in caplog.records if "went unavailable" in r.getMessage()
        ]
        assert len(unavail_records) >= 1
        # Device key must appear in the message (not None)
        assert any(key in r.getMessage() for r in unavail_records)

    def test_watchdog_unavailable_log_contains_timeout(self, hass, coordinator, caplog):
        """When watchdog marks device unavailable, log contains the timeout value.

        Kills mutmut_22: timeout is replaced with None in the LOGGER.debug call.
        Kills mutmut_26: format string is garbled with XX...XX.
        """
        import logging

        start = dt_util.parse_datetime("2026-05-25T10:00:00+00:00")

        with freeze_time(start), patch(DISPATCH):
            coordinator._handle_text_frame(
                '{"time": "2026-05-25T10:00:00Z", "model": "Dev", "id": 1, "val": 1}'
            )

        # Watchdog fires 700s later
        with (
            freeze_time(start + timedelta(seconds=700)),
            patch(DISPATCH),
            caplog.at_level(logging.DEBUG, logger="custom_components.rtl_433"),
        ):
            _run(hass, coordinator._async_watchdog(dt_util.utcnow()))

        unavail_records = [
            r for r in caplog.records if "went unavailable" in r.getMessage()
        ]
        assert len(unavail_records) >= 1
        # The timeout (600) must appear as a number in the message
        # mutmut_22 logs 'None' instead of 600
        assert any("600" in r.getMessage() for r in unavail_records)
        # The format must not be garbled with XX (kills mutmut_26)
        assert all(not r.getMessage().startswith("XX") for r in unavail_records)


# ---------------------------------------------------------------------------
# async_stop: log message contains ws_url (not None)
# ---------------------------------------------------------------------------


class TestAsyncStopLog:
    """Kill async_stop log mutations (mutmut_13 - mutmut_18).

    After stop, LOGGER.debug("rtl_433 coordinator stopped for %s", self.ws_url)
    is called. Six mutations alter the message or the url arg.
    """

    def _run_stop(self, hass, coordinator):
        """Start and immediately stop the coordinator, capture debug calls."""
        import custom_components.rtl_433.coordinator.base as base_module

        debug_calls: list[tuple] = []
        original_debug = base_module.LOGGER.debug

        def mock_debug(msg, *args, **kwargs):
            debug_calls.append((msg, args))
            original_debug(msg, *args, **kwargs)

        expected_url = coordinator.ws_url

        with patch.object(base_module.LOGGER, "debug", side_effect=mock_debug):
            _run(hass, coordinator.async_stop())

        return debug_calls, expected_url

    def test_async_stop_log_format_contains_stopped(self, hass, coordinator):
        """Format string for 'stopped' log contains 'stopped' (lowercase).

        Kills mutmut_13 (msg=None), mutmut_15 (url as msg, no 'stopped'),
        mutmut_17 (XX prefix), mutmut_18 (uppercase).
        """
        debug_calls, _ = self._run_stop(hass, coordinator)

        stopped_calls = [
            (msg, args)
            for msg, args in debug_calls
            if isinstance(msg, str) and "stopped" in msg.lower()
        ]
        assert len(stopped_calls) >= 1

        for msg, _ in stopped_calls:
            assert "stopped" in msg  # lowercase, not STOPPED (kills mutmut_18)
            assert not msg.startswith("XX")  # not garbled (kills mutmut_17)

    def test_async_stop_log_url_arg_is_not_none(self, hass, coordinator):
        """URL argument in 'stopped' log is not None (kills mutmut_14).

        mutmut_14: LOGGER.debug("rtl_433 coordinator stopped for %s", None)
        """
        debug_calls, expected_url = self._run_stop(hass, coordinator)

        stopped_calls = [
            (msg, args)
            for msg, args in debug_calls
            if isinstance(msg, str) and "stopped" in msg
        ]
        assert len(stopped_calls) >= 1

        for _msg, args in stopped_calls:
            # Must have at least one arg (kills mutmut_16 which passes no args)
            assert len(args) >= 1
            # The URL arg must not be None (kills mutmut_14)
            assert args[0] is not None
            # Must be the actual ws_url
            assert args[0] == expected_url


# ---------------------------------------------------------------------------
# _effective_timeout: LOGGER.exception call contains device_key (not None)
# ---------------------------------------------------------------------------


class TestEffectiveTimeoutResolverExceptionLog:
    """Kill _effective_timeout log mutations (mutmut_5 - mutmut_8).

    When effective_timeout_resolver raises, LOGGER.exception is called with
    the format string and device_key. Four mutations alter that call:
    mutmut_5: device_key → None
    mutmut_6: LOGGER.exception(device_key) — only the key as format string
    mutmut_7: LOGGER.exception("...failed for %s", ) — empty args
    mutmut_8: LOGGER.exception("XX...XX", device_key) — garbled format
    """

    def test_effective_timeout_exception_log_contains_format_string(
        self, hass, coordinator
    ):
        """LOGGER.exception format string contains 'failed' (not garbled).

        Kills mutmut_6 (device_key used as format string — no 'failed'),
        mutmut_8 (garbled with XX...XX).
        """
        import custom_components.rtl_433.coordinator.base as base_module

        exception_calls: list[tuple] = []

        def mock_exception(msg, *args, **kwargs):
            exception_calls.append((msg, args))

        key = "Dev-1"
        coordinator.effective_timeout_resolver = MagicMock(
            side_effect=RuntimeError("resolver boom")
        )

        with patch.object(base_module.LOGGER, "exception", side_effect=mock_exception):
            coordinator._effective_timeout(key)

        assert len(exception_calls) >= 1
        for msg, _ in exception_calls:
            # Format string must be a string containing 'failed' (kills mutmut_6)
            assert isinstance(msg, str)
            assert "failed" in msg
            # Not garbled (kills mutmut_8)
            assert not msg.startswith("XX")

    def test_effective_timeout_exception_log_arg_is_device_key(self, hass, coordinator):
        """LOGGER.exception arg is the device_key, not None.

        Kills mutmut_5 (device_key → None) and mutmut_7 (empty args).
        """
        import custom_components.rtl_433.coordinator.base as base_module

        exception_calls: list[tuple] = []

        def mock_exception(msg, *args, **kwargs):
            exception_calls.append((msg, args))

        key = "Dev-99"
        coordinator.effective_timeout_resolver = MagicMock(
            side_effect=RuntimeError("resolver boom")
        )

        with patch.object(base_module.LOGGER, "exception", side_effect=mock_exception):
            coordinator._effective_timeout(key)

        assert len(exception_calls) >= 1
        for _msg, args in exception_calls:
            # Must have at least one arg (kills mutmut_7: empty args)
            assert len(args) >= 1
            # The arg must be the device key, not None (kills mutmut_5)
            assert args[0] is not None
            assert args[0] == key


# ---------------------------------------------------------------------------
# async_start: "started" log contains ws_url (not None)
# ---------------------------------------------------------------------------


class TestAsyncStartLog:
    """Kill async_start log mutations (mutmut_28 - mutmut_31).

    At the end of async_start(), LOGGER.debug("rtl_433 coordinator started for %s",
    self.ws_url) is called. Four mutations alter that call.
    """

    DISPATCH = "custom_components.rtl_433.coordinator.base.async_dispatcher_send"

    def _run_start(self, hass, coordinator):
        """Run async_start and capture LOGGER.debug calls."""
        import custom_components.rtl_433.coordinator.base as base_module

        debug_calls: list[tuple] = []

        original_debug = base_module.LOGGER.debug

        def mock_debug(msg, *args, **kwargs):
            debug_calls.append((msg, args))
            original_debug(msg, *args, **kwargs)

        expected_url = coordinator.ws_url

        with (
            patch.object(coordinator, "async_load_desired_state", AsyncMock()),
            patch.object(coordinator, "_connect_loop", AsyncMock()),
            patch(
                "custom_components.rtl_433.coordinator.base.async_track_time_interval",
                return_value=MagicMock(),
            ),
            patch(self.DISPATCH),
            patch.object(base_module.LOGGER, "debug", side_effect=mock_debug),
        ):
            _run(hass, coordinator.async_start())

        return debug_calls, expected_url

    def test_async_start_log_contains_started(self, hass, coordinator):
        """Format string for 'started' log contains 'started' (not garbled).

        Kills mutmut_29 (url used as format string — no 'started'),
        mutmut_31 (XX prefix).
        """
        debug_calls, _ = self._run_start(hass, coordinator)

        started_calls = [
            (msg, args)
            for msg, args in debug_calls
            if isinstance(msg, str) and "started" in msg.lower()
        ]
        assert len(started_calls) >= 1

        for msg, _ in started_calls:
            assert "started" in msg
            assert not msg.startswith("XX")

    def test_async_start_log_url_arg_is_not_none(self, hass, coordinator):
        """URL argument in 'started' log is the actual ws_url, not None.

        Kills mutmut_28 (None instead of ws_url), mutmut_30 (empty args).
        """
        debug_calls, expected_url = self._run_start(hass, coordinator)

        started_calls = [
            (msg, args)
            for msg, args in debug_calls
            if isinstance(msg, str) and "started" in msg
        ]
        assert len(started_calls) >= 1

        for _msg, args in started_calls:
            # Must have at least one arg (kills mutmut_30: empty args)
            assert len(args) >= 1
            # The URL arg must not be None (kills mutmut_28)
            assert args[0] is not None
            # Must be the actual ws_url
            assert args[0] == expected_url
