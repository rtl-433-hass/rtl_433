"""Mutation tests for the dispatcher-signal helpers in ``const.py``.

These helpers exist so the coordinator and entities format dispatcher signal
strings identically. A mutation that drops an argument (e.g. ``hub_entry_id=None``)
produces a signal string that no longer round-trips, so we assert the exact
formatted output and that distinct inputs produce distinct, correctly-embedded
strings.
"""

from __future__ import annotations

from custom_components.rtl_433.const import (
    SIGNAL_DEVICE_UPDATE,
    SIGNAL_HUB_UPDATE,
    SIGNAL_NEW_DEVICE,
    signal_device_update,
    signal_hub_update,
    signal_new_device,
)


def test_signal_device_update_embeds_both_arguments_exactly():
    assert signal_device_update("hubA", "devX") == "rtl_433_device_update_hubA_devX"


def test_signal_device_update_distinguishes_hub_and_device():
    # If either argument were dropped/swapped, two of these would collide.
    a = signal_device_update("hub1", "dev1")
    b = signal_device_update("hub2", "dev1")
    c = signal_device_update("hub1", "dev2")
    assert a != b and a != c and b != c
    assert "hub1" in a and "dev1" in a
    assert a == SIGNAL_DEVICE_UPDATE.format(hub_entry_id="hub1", device_key="dev1")


def test_signal_device_update_device_key_is_used():
    # Kills the mutant that formats device_key=None.
    assert signal_device_update("h", "the_device").endswith("_the_device")


def test_signal_hub_update_embeds_hub_id_exactly():
    assert signal_hub_update("hubA") == "rtl_433_hub_update_hubA"
    assert signal_hub_update("hubA") == SIGNAL_HUB_UPDATE.format(hub_entry_id="hubA")


def test_signal_hub_update_distinct_per_hub():
    assert signal_hub_update("h1") != signal_hub_update("h2")
    assert "h1" in signal_hub_update("h1")


def test_signal_new_device_embeds_hub_id_exactly():
    assert signal_new_device("hubA") == "rtl_433_new_device_hubA"
    assert signal_new_device("hubA") == SIGNAL_NEW_DEVICE.format(hub_entry_id="hubA")


def test_signal_new_device_distinct_per_hub():
    assert signal_new_device("h1") != signal_new_device("h2")
    assert "h1" in signal_new_device("h1")
