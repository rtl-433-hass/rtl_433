"""Declarative settings registry for the managed SDR controls.

This module describes each controllable rtl_433 SDR field *once*: how to read its
current value out of ``coordinator.meta`` (the Plan 3 ``_refresh_meta`` result),
which ``/cmd`` command sets it (and whether the desired value rides on ``val`` as
an integer or ``arg`` as a string), the value transform, and the Home Assistant
platform + entity-description metadata needed to build the entity. The coordinator
(adopt/enforce), the control platforms (number/select/switch), and the config flow
all import from here, so this is the single source of truth for the control set.

It sits *below* the coordinator and the platforms in the dependency graph (mirrors
how ``mapping.py`` is import-disjoint): it imports only from the standard library,
Home Assistant helpers, and ``.const`` — never from ``coordinator/*``, ``entity``,
or the platform modules.

Outbound ``/cmd`` commands follow ``WEBSOCKET_API.md`` exactly (no invented
fields):

- center frequency -> ``center_frequency``, ``val`` = Hz (live).
- sample rate -> ``sample_rate``, ``val`` = Hz (live).
- ppm -> ``ppm_error``, ``val`` = integer (live).
- gain -> ``gain``, ``arg`` = dB string, empty string = auto (live).
- conversion mode -> ``convert``, ``val`` = integer 0/1/2 (config-setter).
- hop interval -> ``hop_interval``, ``val`` = seconds (config-setter).

Gain is modelled as the clarified *Number (dB) + "Auto gain" Switch* pair: two
registry entries that share the ``gain`` command. The coordinator stores the two
desired-state keys (``gain`` dB float, ``gain_auto`` bool) but issues exactly one
``gain`` ``/cmd`` per write, composing its ``arg`` via :func:`gain_command_arg`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from homeassistant.components.number import NumberDeviceClass, NumberMode
from homeassistant.const import UnitOfFrequency
from homeassistant.helpers.entity import EntityCategory

# --------------------------------------------------------------------------- #
# Stable internal keys.                                                         #
# --------------------------------------------------------------------------- #
# Each key is the desired-state Store key for its field and the registry's
# stable identity token; they are referenced by the coordinator and platforms.
KEY_CENTER_FREQUENCY = "center_frequency"
KEY_SAMPLE_RATE = "sample_rate"
KEY_PPM_ERROR = "ppm_error"
KEY_GAIN_DB = "gain"
KEY_GAIN_AUTO = "gain_auto"
KEY_CONVERSION_MODE = "conversion_mode"
KEY_HOP_INTERVAL = "hop_interval"


# --------------------------------------------------------------------------- #
# Conversion-mode label <-> integer mapping.                                    #
# --------------------------------------------------------------------------- #
# The select's option index *is* the ``convert`` command ``val``, so the tuple
# order is load-bearing: native -> 0, si -> 1, customary -> 2.
CONVERSION_MODES: tuple[str, ...] = ("native", "si", "customary")


def conversion_label_to_val(label: str) -> int:
    """Map a conversion-mode label to its ``convert`` command ``val``.

    Raises ``ValueError`` for an unknown label (the select only ever offers the
    three known options, so an unknown label signals a programming error).
    """
    return CONVERSION_MODES.index(label)


def conversion_val_to_label(val: int) -> str | None:
    """Map a ``conversion_mode`` integer to its label, or ``None`` if unknown."""
    return CONVERSION_MODES[val] if 0 <= val < len(CONVERSION_MODES) else None


# --------------------------------------------------------------------------- #
# Gain command argument composer.                                               #
# --------------------------------------------------------------------------- #
def gain_command_arg(gain_db: float | None, gain_auto: bool) -> str:
    """Compose the single outbound ``gain`` ``/cmd`` ``arg`` from the gain pair.

    An empty string means auto; otherwise the dB value as a string (rtl_433
    accepts e.g. ``"32.8"``). ``%g`` trims a trailing ``.0`` so clean integers
    read as ``"40"`` rather than ``"40.0"``.
    """
    if gain_auto or gain_db is None:
        return ""
    return f"{gain_db:g}"


def _always(_meta: dict[str, Any]) -> bool:
    """Capability gate that is always satisfied (today every field is supported)."""
    return True


def _frequency_count(meta: dict[str, Any]) -> int | None:
    """Number of configured frequencies, or None when unknown (pre-connect)."""
    freqs = meta.get("frequencies")
    return len(freqs) if isinstance(freqs, list) else None


def _available_when_not_hopping(meta: dict[str, Any]) -> bool:
    """Center frequency is meaningful only with a single configured frequency.

    Under hop mode (more than one frequency) the API cannot represent the hop
    list and setting a single ``center_frequency`` would collapse hopping, so the
    control is hidden. Unknown frequency count (before the first connect) defaults
    to available.
    """
    count = _frequency_count(meta)
    return count is None or count <= 1


def _available_when_hopping(meta: dict[str, Any]) -> bool:
    """Hop interval only takes effect when more than one frequency is configured.

    With a single frequency there is nothing to hop between, so the control is
    hidden. Unknown frequency count (before the first connect) defaults to
    available.
    """
    count = _frequency_count(meta)
    return count is None or count > 1


@dataclass(frozen=True, kw_only=True)
class SdrSetting:
    """One controllable SDR field, described once for all consumers.

    Pure data plus tiny callables so the coordinator and the platforms can both
    iterate :data:`SDR_SETTINGS` without importing each other. ``read`` extracts
    the current value from ``coordinator.meta``; ``to_command`` maps a desired
    Python value to the value/arg actually sent on ``/cmd`` (carried as ``val``
    when ``arg_kind == "val"``, else as ``arg``). ``capability`` gates whether the
    field is offered for a given meta (today always ``True``). ``available`` is a
    runtime-state gate read on every ``signal_hub_update`` (vs ``capability``,
    evaluated once at setup): it decides whether the *created* control entity
    reports as available for the current ``meta`` — used to hide ``hop_interval``
    when the server is not hopping and ``center_frequency`` when it is.
    """

    # Identity / contract.
    key: str  # stable internal key (also the desired-state Store key)
    name: str  # entity name (has_entity_name relative)
    object_suffix: str  # unique-id token -> f"{entry_id}:hub:{object_suffix}"
    platform: str  # "number" | "select" | "switch"
    command: str  # /cmd command name
    arg_kind: str  # "val" (integer) | "arg" (string)

    # Read current value out of coordinator.meta.
    read: Callable[[dict[str, Any]], Any]
    # Map a desired python value -> the value/arg sent on /cmd.
    to_command: Callable[[Any], Any]

    # number-only entity-description parameters.
    native_min: float | None = None
    native_max: float | None = None
    native_step: float | None = None
    native_unit: str | None = None
    mode: NumberMode | None = None
    device_class: str | None = None

    # select-only entity-description parameters.
    options: tuple[str, ...] | None = None

    # Capability gate (today always True; future: per-server capability).
    capability: Callable[[dict[str, Any]], bool] = field(default=_always)
    # Runtime availability gate (always available unless a field overrides it).
    available: Callable[[dict[str, Any]], bool] = field(default=_always)


# --------------------------------------------------------------------------- #
# Per-field read helpers (defensive: a missing meta key reads as None).         #
# --------------------------------------------------------------------------- #
def _read_center_frequency(meta: dict[str, Any]) -> Any:
    return meta.get("center_frequency")


def _read_sample_rate(meta: dict[str, Any]) -> Any:
    # The meta object names this ``samp_rate``; the registry key is sample_rate.
    return meta.get("samp_rate")


def _read_ppm_error(meta: dict[str, Any]) -> Any:
    return meta.get("ppm_error")


def _read_gain_db(meta: dict[str, Any]) -> float | None:
    """Read the gain dB value out of meta's gain string ("" -> None for auto)."""
    gain = meta.get("gain")
    if gain is None or gain == "":
        return None
    try:
        return float(gain)
    except TypeError, ValueError:
        return None


def _read_gain_auto(meta: dict[str, Any]) -> bool | None:
    """Read whether auto gain is active ("" -> True; a value -> False)."""
    gain = meta.get("gain")
    if gain is None:
        return None
    return gain == ""


def _read_conversion_mode(meta: dict[str, Any]) -> int | None:
    """Read the conversion mode as the integer ``convert`` ``val``.

    The desired value is stored as the integer the ``convert`` command takes
    (and that ``meta`` natively reports); only the Select entity maps it to/from
    a human label at its UI boundary.
    """
    raw = meta.get("conversion_mode")
    if raw is None:
        return None
    try:
        return int(raw)
    except TypeError, ValueError:
        return None


def _read_hop_interval(meta: dict[str, Any]) -> Any:
    # Plan 3's _refresh_meta exposes hop_interval (= hop_times[0]).
    return meta.get("hop_interval")


# --------------------------------------------------------------------------- #
# Outbound value transforms (desired python value -> /cmd val/arg).             #
# --------------------------------------------------------------------------- #
def _int_command(value: Any) -> int:
    """Coerce a desired numeric value to the integer sent on ``val``."""
    return int(value)


# --------------------------------------------------------------------------- #
# The registry.                                                                 #
# --------------------------------------------------------------------------- #
# Defensibly wide BOX-mode bounds: rtl_433 servers vary widely by SDR hardware,
# so we offer generous ranges rather than guessing a device's true limits. The
# server clamps/rejects out-of-range values; HA is not the authority here.
SDR_SETTINGS: tuple[SdrSetting, ...] = (
    SdrSetting(
        key=KEY_CENTER_FREQUENCY,
        name="Center frequency",
        object_suffix="center_frequency",
        platform="number",
        command="center_frequency",
        arg_kind="val",
        read=_read_center_frequency,
        to_command=_int_command,
        # 0 .. 6 GHz covers RTL-SDR through wideband front-ends.
        native_min=0,
        native_max=6_000_000_000,
        native_step=1,
        native_unit=UnitOfFrequency.HERTZ,
        mode=NumberMode.BOX,
        device_class=NumberDeviceClass.FREQUENCY,
        # Hidden under hop mode: a single value cannot represent the hop list and
        # setting it would collapse hopping (also why adoption leaves it unmanaged).
        available=_available_when_not_hopping,
    ),
    SdrSetting(
        key=KEY_SAMPLE_RATE,
        name="Sample rate",
        object_suffix="sample_rate",
        platform="number",
        command="sample_rate",
        arg_kind="val",
        read=_read_sample_rate,
        to_command=_int_command,
        # 0 .. 20 MS/s comfortably spans common rtl_433 sample rates.
        native_min=0,
        native_max=20_000_000,
        native_step=1,
        native_unit=UnitOfFrequency.HERTZ,
        mode=NumberMode.BOX,
    ),
    SdrSetting(
        key=KEY_PPM_ERROR,
        name="Frequency correction",
        object_suffix="ppm_error",
        platform="number",
        command="ppm_error",
        arg_kind="val",
        read=_read_ppm_error,
        to_command=_int_command,
        # -1000 .. 1000 ppm; real crystals are well inside this.
        native_min=-1000,
        native_max=1000,
        native_step=1,
        mode=NumberMode.BOX,
    ),
    # --- Gain pair: a Number (dB) + a Switch ("Auto gain"), sharing "gain". --- #
    SdrSetting(
        key=KEY_GAIN_DB,
        name="Gain",
        object_suffix="gain",
        platform="number",
        command="gain",
        arg_kind="arg",
        read=_read_gain_db,
        # The actual outbound arg is composed by gain_command_arg() from the
        # *combined* desired state; this float->str maps the dB value alone.
        to_command=lambda value: gain_command_arg(value, gain_auto=False),
        # 0 .. 100 dB; rtl_433 accepts fractional dB (e.g. "32.8").
        native_min=0,
        native_max=100,
        native_step=0.1,
        native_unit="dB",
        mode=NumberMode.BOX,
    ),
    SdrSetting(
        key=KEY_GAIN_AUTO,
        name="Auto gain",
        object_suffix="gain_auto",
        platform="switch",
        command="gain",
        arg_kind="arg",
        read=_read_gain_auto,
        # On -> empty arg (auto); off -> defer to the dB value at write time.
        to_command=lambda auto: gain_command_arg(None, gain_auto=bool(auto)),
    ),
    SdrSetting(
        key=KEY_CONVERSION_MODE,
        name="Conversion mode",
        object_suffix="conversion_mode",
        platform="select",
        command="convert",
        arg_kind="val",
        read=_read_conversion_mode,
        to_command=_int_command,
        options=CONVERSION_MODES,
    ),
    SdrSetting(
        key=KEY_HOP_INTERVAL,
        name="Hop interval",
        object_suffix="hop_interval",
        platform="number",
        command="hop_interval",
        arg_kind="val",
        read=_read_hop_interval,
        to_command=_int_command,
        # 0 .. 86400 s (one day); 0 disables hopping.
        native_min=0,
        native_max=86400,
        native_step=1,
        native_unit="s",
        mode=NumberMode.BOX,
        # Only meaningful with more than one configured frequency; hidden when the
        # server is not hopping (a single frequency has nothing to hop between).
        available=_available_when_hopping,
    ),
)

# Convenience: registry indexed by stable key for O(1) lookup by consumers.
SDR_SETTINGS_BY_KEY: dict[str, SdrSetting] = {s.key: s for s in SDR_SETTINGS}

# The Home Assistant control platforms this registry can populate. Task 4 wires
# these into PLATFORMS; the registry only declares which ones it uses.
CONTROL_PLATFORMS: tuple[str, ...] = ("number", "select", "switch")

# EntityCategory all managed controls belong to; re-exported so the platforms
# need not import EntityCategory themselves.
CONTROL_ENTITY_CATEGORY = EntityCategory.CONFIG
