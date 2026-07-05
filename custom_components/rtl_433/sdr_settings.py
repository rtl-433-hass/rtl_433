"""Home Assistant adapter over the ``pyrtl_433.sdr`` managed-control registry.

The *wire-protocol* half of the managed SDR controls — how to read each field out
of the server's ``meta`` payload, which ``/cmd`` command sets it (and whether the
desired value rides on ``val`` as an integer or ``arg`` as a string), the value
transforms, and the capability/availability gates — now lives in the standalone
``pyrtl_433.sdr`` module (:class:`~pyrtl_433.sdr.SdrCommand` / ``SDR_COMMANDS``).
That module intentionally drops all Home Assistant entity-description metadata,
keeping only the protocol contract.

This module is the thin *integration adapter* that re-supplies exactly that
dropped HA metadata (entity name, unique-id token, platform, Number min/max/step/
unit + mode + device class, Select options) and merges it with the library's
protocol contract to produce the :class:`SdrSetting` records the coordinator and
the number/select/switch platforms consume. It re-exports the library's helper
functions and stable keys unchanged, so every existing consumer keeps importing
the same names from ``custom_components.rtl_433.sdr_settings``.

Because the protocol callables (``read`` / ``to_command`` / ``capability`` /
``available``) are taken *by reference* from the library's own ``SDR_COMMANDS``,
entity generation and ``/cmd`` argument composition are byte-identical to the
pre-extraction behaviour.

It imports only from the standard library, Home Assistant helpers, and
``pyrtl_433.sdr`` — never from ``coordinator/*``, ``entity``, or the platform
modules — mirroring how ``mapping.py`` sits import-disjoint below them.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

# The pure protocol half, sourced from the standalone library. ``pyrtl_433.sdr``
# is a submodule and is *not* re-exported from the package top level, so it is
# imported directly from the submodule path.
from pyrtl_433.sdr import (
    CONVERSION_MODES,
    KEY_CENTER_FREQUENCY,
    KEY_CONVERSION_MODE,
    KEY_GAIN_AUTO,
    KEY_GAIN_DB,
    KEY_HOP_INTERVAL,
    KEY_PPM_ERROR,
    KEY_SAMPLE_RATE,
    SDR_COMMANDS_BY_KEY,
    conversion_label_to_val,
    conversion_val_to_label,
    gain_command_arg,
)

from homeassistant.components.number import NumberDeviceClass, NumberMode
from homeassistant.const import UnitOfFrequency
from homeassistant.helpers.entity import EntityCategory

# Re-exported so consumers keep importing every name from this module path. The
# ``KEY_*`` constants, ``CONVERSION_MODES`` tuple, and the three helper functions
# are the library's originals, surfaced unchanged.
__all__ = [
    "CONTROL_ENTITY_CATEGORY",
    "CONTROL_PLATFORMS",
    "CONVERSION_MODES",
    "KEY_CENTER_FREQUENCY",
    "KEY_CONVERSION_MODE",
    "KEY_GAIN_AUTO",
    "KEY_GAIN_DB",
    "KEY_HOP_INTERVAL",
    "KEY_PPM_ERROR",
    "KEY_SAMPLE_RATE",
    "SDR_SETTINGS",
    "SDR_SETTINGS_BY_KEY",
    "SdrSetting",
    "conversion_label_to_val",
    "conversion_val_to_label",
    "gain_command_arg",
]


@dataclass(frozen=True, kw_only=True)
class SdrSetting:
    """One controllable SDR field: the library's protocol plus HA metadata.

    The protocol fields (``key``, ``command``, ``arg_kind``, ``read``,
    ``to_command``, ``capability``, ``available``) mirror
    :class:`pyrtl_433.sdr.SdrCommand` and are populated *by reference* from the
    corresponding library command, so their behaviour is identical. The remaining
    fields are the Home Assistant entity-description metadata the library drops —
    supplied here because only the integration needs them to build entities.
    """

    # Protocol contract (mirrors pyrtl_433.sdr.SdrCommand).
    key: str  # stable internal key (also the desired-state Store key)
    command: str  # /cmd command name
    arg_kind: str  # "val" (integer) | "arg" (string)
    read: Callable[[dict[str, Any]], Any]
    to_command: Callable[[Any], Any]
    capability: Callable[[dict[str, Any]], bool]
    available: Callable[[dict[str, Any]], bool]

    # Identity / platform routing (HA-only).
    name: str  # entity name (has_entity_name relative)
    object_suffix: str  # unique-id token -> f"{entry_id}:hub:{object_suffix}"
    platform: str  # "number" | "select" | "switch"

    # number-only entity-description parameters.
    native_min: float | None = None
    native_max: float | None = None
    native_step: float | None = None
    native_unit: str | None = None
    mode: NumberMode | None = None
    device_class: str | None = None

    # select-only entity-description parameters.
    options: tuple[str, ...] | None = None


def _adapt(key: str, **ha_metadata: Any) -> SdrSetting:
    """Build an :class:`SdrSetting` from a library command plus HA metadata.

    The protocol fields are copied by reference off the library's
    :data:`pyrtl_433.sdr.SDR_COMMANDS_BY_KEY` entry for ``key`` (so the exact same
    read/transform/gate callables are used); ``ha_metadata`` supplies the
    entity-description fields the library does not carry.
    """
    command = SDR_COMMANDS_BY_KEY[key]
    return SdrSetting(
        key=command.key,
        command=command.command,
        arg_kind=command.arg_kind,
        read=command.read,
        to_command=command.to_command,
        capability=command.capability,
        available=command.available,
        **ha_metadata,
    )


# --------------------------------------------------------------------------- #
# The registry: library protocol + HA entity-description metadata.              #
# --------------------------------------------------------------------------- #
# Defensibly wide BOX-mode bounds: rtl_433 servers vary widely by SDR hardware,
# so we offer generous ranges rather than guessing a device's true limits. The
# server clamps/rejects out-of-range values; HA is not the authority here.
SDR_SETTINGS: tuple[SdrSetting, ...] = (
    _adapt(
        KEY_CENTER_FREQUENCY,
        name="Center frequency",
        object_suffix="center_frequency",
        platform="number",
        # Presented in MHz (converted to Hz on the wire). 0 .. 6000 MHz covers
        # RTL-SDR through wideband front-ends; 0.001 MHz = 1 kHz resolution.
        native_min=0,
        native_max=6000,
        native_step=0.001,
        native_unit=UnitOfFrequency.MEGAHERTZ,
        mode=NumberMode.BOX,
        device_class=NumberDeviceClass.FREQUENCY,
    ),
    _adapt(
        KEY_SAMPLE_RATE,
        name="Sample rate",
        object_suffix="sample_rate",
        platform="number",
        # 0 .. 20 MS/s comfortably spans common rtl_433 sample rates.
        native_min=0,
        native_max=20_000_000,
        native_step=1,
        native_unit=UnitOfFrequency.HERTZ,
        mode=NumberMode.BOX,
    ),
    _adapt(
        KEY_PPM_ERROR,
        name="Frequency correction",
        object_suffix="ppm_error",
        platform="number",
        # -1000 .. 1000 ppm; real crystals are well inside this.
        native_min=-1000,
        native_max=1000,
        native_step=1,
        mode=NumberMode.BOX,
    ),
    # --- Gain pair: a Number (dB) + a Switch ("Auto gain"), sharing "gain". --- #
    _adapt(
        KEY_GAIN_DB,
        name="Gain",
        object_suffix="gain",
        platform="number",
        # 0 .. 100 dB; rtl_433 accepts fractional dB (e.g. "32.8").
        native_min=0,
        native_max=100,
        native_step=0.1,
        native_unit="dB",
        mode=NumberMode.BOX,
    ),
    _adapt(
        KEY_GAIN_AUTO,
        name="Auto gain",
        object_suffix="gain_auto",
        platform="switch",
    ),
    _adapt(
        KEY_CONVERSION_MODE,
        name="Conversion mode",
        object_suffix="conversion_mode",
        platform="select",
        options=CONVERSION_MODES,
    ),
    _adapt(
        KEY_HOP_INTERVAL,
        name="Hop interval",
        object_suffix="hop_interval",
        platform="number",
        # 0 .. 86400 s (one day); 0 disables hopping.
        native_min=0,
        native_max=86400,
        native_step=1,
        native_unit="s",
        mode=NumberMode.BOX,
    ),
)

# Convenience: registry indexed by stable key for O(1) lookup by consumers.
SDR_SETTINGS_BY_KEY: dict[str, SdrSetting] = {s.key: s for s in SDR_SETTINGS}

# The Home Assistant control platforms this registry can populate (wired into
# PLATFORMS in const.py); the registry only declares which ones it uses.
CONTROL_PLATFORMS: tuple[str, ...] = ("number", "select", "switch")

# EntityCategory all managed controls belong to; re-exported so the platforms
# need not import EntityCategory themselves.
CONTROL_ENTITY_CATEGORY = EntityCategory.CONFIG
