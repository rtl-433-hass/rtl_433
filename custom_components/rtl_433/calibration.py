"""Per-device utility-meter calibration helpers.

A calibration is a user-supplied ``{commodity, unit, scale}`` triple stored in
the hub's per-device record (``entry.data[CONF_DEVICES][device_key]
[DEVICE_CALIBRATION]``). It turns a unitless consumption counter into an
Energy-dashboard-eligible sensor by attaching a real ``device_class`` (from the
commodity), a Home-Assistant-convertible base unit, ``state_class:
total_increasing``, and a scale on the raw counter.

This module owns the small, pure mappings shared by the options flow (which
constrains the base-unit selector per commodity and pre-fills the commodity from
decoded ``MeterType`` / ``ert_type`` hints) and the entity build (which overlays
the calibration onto the looked-up consumption descriptor — precedence #1).

The integration intentionally owns **no** unit-conversion logic: supplying a
real device_class + a convertible base unit + ``total_increasing`` is sufficient
for Energy-dashboard eligibility and unlocks Home Assistant's own per-entity
display-unit conversion.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.const import UnitOfEnergy, UnitOfVolume

from .const import (
    CALIBRATION_COMMODITY,
    CALIBRATION_SCALE,
    CALIBRATION_UNIT,
    COMMODITY_ENERGY,
    COMMODITY_GAS,
    COMMODITY_NONE,
    COMMODITY_WATER,
)

# Commodity -> sensor device_class. ``none`` has no device_class (it clears the
# calibration and falls back to the library descriptor).
COMMODITY_DEVICE_CLASS: dict[str, SensorDeviceClass] = {
    COMMODITY_ENERGY: SensorDeviceClass.ENERGY,
    COMMODITY_GAS: SensorDeviceClass.GAS,
    COMMODITY_WATER: SensorDeviceClass.WATER,
}

# Commodity -> the base units offered in the options flow. Each is a unit Home
# Assistant recognizes as convertible for that commodity's device_class, so the
# resulting sensor is both Energy-dashboard-eligible and gets HA's built-in
# per-entity display-unit conversion. Energy uses energy units; gas/water use
# volume units (gas omits ``gal`` — not a valid gas device_class unit).
COMMODITY_UNITS: dict[str, tuple[str, ...]] = {
    COMMODITY_ENERGY: (
        UnitOfEnergy.WATT_HOUR,
        UnitOfEnergy.KILO_WATT_HOUR,
        UnitOfEnergy.MEGA_WATT_HOUR,
    ),
    COMMODITY_GAS: (
        UnitOfVolume.CUBIC_METERS,
        UnitOfVolume.CUBIC_FEET,
        UnitOfVolume.LITERS,
        UnitOfVolume.CENTUM_CUBIC_FEET,
    ),
    COMMODITY_WATER: (
        UnitOfVolume.CUBIC_METERS,
        UnitOfVolume.CUBIC_FEET,
        UnitOfVolume.LITERS,
        UnitOfVolume.GALLONS,
        UnitOfVolume.CENTUM_CUBIC_FEET,
    ),
}


def default_unit(commodity: str) -> str | None:
    """Return the first (default) base unit for a commodity, or ``None``."""
    units = COMMODITY_UNITS.get(commodity)
    return units[0] if units else None


def normalize_calibration(raw: Any) -> dict[str, Any] | None:
    """Return a clean ``{commodity, unit, scale}`` calibration, or ``None``.

    Treats a missing record, a non-mapping, a ``commodity`` of ``none`` /
    unknown, or a missing/invalid unit-for-commodity as "no calibration"
    (returns ``None``) so callers can use a simple truthiness test. The scale
    defaults to ``1.0`` and is coerced to ``float`` (a bad value falls back to
    ``1.0`` rather than raising).
    """
    if not isinstance(raw, dict):
        return None
    commodity = raw.get(CALIBRATION_COMMODITY)
    if commodity not in COMMODITY_DEVICE_CLASS:
        return None
    unit = raw.get(CALIBRATION_UNIT)
    if unit not in COMMODITY_UNITS.get(commodity, ()):  # also rejects None
        return None
    try:
        scale = float(raw.get(CALIBRATION_SCALE, 1.0))
    except (TypeError, ValueError):
        scale = 1.0
    return {
        CALIBRATION_COMMODITY: commodity,
        CALIBRATION_UNIT: unit,
        CALIBRATION_SCALE: scale,
    }


def commodity_from_fields(fields: dict[str, Any] | None) -> str:
    """Best-effort commodity hint from a device's last decoded event fields.

    Maps a ``MeterType`` string (``"Electric"`` -> energy, ``"Gas"`` -> gas,
    ``"Water"`` -> water) when present; otherwise the low nibble of an
    ``ert_type`` integer (a utility-dependent convention: 4/5/7/8 are commonly
    electric, 2/9/12 gas, 11/13 water). Never raises; defaults to ``none``.
    """
    if not isinstance(fields, dict):
        return COMMODITY_NONE

    meter_type = fields.get("MeterType")
    if isinstance(meter_type, str):
        mapped = {
            "electric": COMMODITY_ENERGY,
            "gas": COMMODITY_GAS,
            "water": COMMODITY_WATER,
        }.get(meter_type.strip().lower())
        if mapped is not None:
            return mapped

    ert_type = fields.get("ert_type")
    try:
        nibble = int(ert_type) & 0x0F
    except (TypeError, ValueError):
        return COMMODITY_NONE
    return {
        2: COMMODITY_GAS,
        4: COMMODITY_ENERGY,
        5: COMMODITY_ENERGY,
        7: COMMODITY_ENERGY,
        8: COMMODITY_ENERGY,
        9: COMMODITY_GAS,
        11: COMMODITY_WATER,
        12: COMMODITY_GAS,
        13: COMMODITY_WATER,
    }.get(nibble, COMMODITY_NONE)
