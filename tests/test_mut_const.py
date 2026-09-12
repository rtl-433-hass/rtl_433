"""Mutation tests for the dispatcher-signal helpers and identity grammar in ``const.py``.

The signal helpers exist so the coordinator and entities format dispatcher
signal strings identically. A mutation that drops an argument (e.g.
``receiver_id=None``) produces a signal string that no longer round-trips,
so we assert the exact formatted output and that distinct inputs produce
distinct, correctly-embedded strings.

The identity-grammar tests pin the reserved ``receiver`` marker against the real
``pyrtl_433.naming.safe_token`` output, because that is what makes the marker a
*reservation* rather than a convention: the token builder every ``device_key`` is
made of would otherwise happily emit it.
"""

from __future__ import annotations

from pyrtl_433.naming import safe_token
import pytest

from custom_components.rtl_433.const import (
    RECEIVER_SEGMENT,
    RESERVED_DEVICE_KEYS,
    SIGNAL_DEVICE_UPDATE,
    SIGNAL_NEW_DEVICE,
    SIGNAL_RECEIVER_UPDATE,
    is_reserved_device_key,
    signal_device_update,
    signal_new_device,
    signal_receiver_update,
)


def test_signal_device_update_embeds_both_arguments_exactly():
    assert (
        signal_device_update("receiverA", "devX")
        == "rtl_433_device_update_receiverA_devX"
    )


def test_signal_device_update_distinguishes_receiver_and_device():
    # If either argument were dropped/swapped, two of these would collide.
    a = signal_device_update("receiver1", "dev1")
    b = signal_device_update("receiver2", "dev1")
    c = signal_device_update("receiver1", "dev2")
    assert a != b and a != c and b != c
    assert "receiver1" in a and "dev1" in a
    assert a == SIGNAL_DEVICE_UPDATE.format(receiver_id="receiver1", device_key="dev1")


def test_signal_device_update_device_key_is_used():
    # Kills the mutant that formats device_key=None.
    assert signal_device_update("h", "the_device").endswith("_the_device")


def test_signal_receiver_update_embeds_receiver_id_exactly():
    assert signal_receiver_update("receiverA") == "rtl_433_receiver_update_receiverA"
    assert signal_receiver_update("receiverA") == SIGNAL_RECEIVER_UPDATE.format(
        receiver_id="receiverA"
    )


def test_signal_receiver_update_distinct_per_receiver():
    assert signal_receiver_update("h1") != signal_receiver_update("h2")
    assert "h1" in signal_receiver_update("h1")


def test_signal_new_device_embeds_receiver_id_exactly():
    assert signal_new_device("receiverA") == "rtl_433_new_device_receiverA"
    assert signal_new_device("receiverA") == SIGNAL_NEW_DEVICE.format(
        receiver_id="receiverA"
    )


def test_signal_new_device_distinct_per_receiver():
    assert signal_new_device("h1") != signal_new_device("h2")
    assert "h1" in signal_new_device("h1")


# ---------------------------------------------------------------------------
# Identity grammar: the reserved ``receiver`` marker
# ---------------------------------------------------------------------------


def test_safe_token_can_emit_the_reserved_marker():
    """The collision the reservation exists for is genuinely reachable.

    ``safe_token`` only rewrites characters that are unsafe in an identifier, so
    a device whose ``model`` is literally ``receiver`` mints the marker verbatim.
    If this ever stopped being true the reservation would be dead code -- and if
    the marker were renamed without re-checking it, silently ineffective.
    """
    assert safe_token("receiver") == RECEIVER_SEGMENT
    assert is_reserved_device_key(safe_token("receiver"))


def test_safe_token_never_emits_a_colon():
    """The whole grammar rests on ``device_key`` never containing a separator.

    Segment counting is only unambiguous because of this; ``safe_token`` maps
    every character outside ``[alnum] - _ .`` to an underscore.
    """
    for value in ("a:b", "Acurite:606TX", "::", "Brand X/Model 1:2"):
        assert ":" not in safe_token(value)


def test_receiver_is_reserved():
    assert RECEIVER_SEGMENT == "receiver"
    assert RECEIVER_SEGMENT in RESERVED_DEVICE_KEYS
    assert is_reserved_device_key(RECEIVER_SEGMENT) is True


@pytest.mark.parametrize(
    "device_key",
    ["Receiver", "receivers", "receiver-1", "my_receiver", "", "Acurite-606TX-42"],
)
def test_reservation_is_an_exact_match_not_a_substring(device_key):
    """Only the exact token is reserved; near misses stay usable device keys."""
    assert is_reserved_device_key(device_key) is False
