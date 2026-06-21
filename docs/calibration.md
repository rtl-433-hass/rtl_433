# Utility-Meter Calibration

Utility meters decoded by SCM, ERT, and SCMplus protocols report a raw
consumption counter, but the RF signal does not carry the counter's unit or
scale. Different meters report in different granularities, so the integration
cannot derive Energy-dashboard-ready values automatically.

Out of the box, consumption is a plain unitless `total_increasing` counter. To
make it eligible for Home Assistant's Energy dashboard, calibrate the device.

## Calibration Flow

Open **Settings → Devices & Services → rtl_433 → Configure → Device settings**,
pick the meter, and set its consumption calibration.

| Field | Meaning |
| --- | --- |
| **Commodity** | `none`, `energy`, `gas`, or `water`. This sets the sensor's device class. Choosing `none` clears calibration. |
| **Base unit** | The unit the calibrated counter is expressed in, constrained to Home Assistant units for that commodity. |
| **Scale** | Multiplier applied to the raw counter so the stored value is in the chosen base unit. |

When the meter reports a `MeterType` or `ert_type` hint, the commodity is
pre-filled from it. You can override it.

![The calibration step for an energy meter, with a base-unit selector and a scale multiplier](images/12-calibration.png)

Once calibrated, the consumption sensor gets a real device class, native unit,
and `state_class: total_increasing`. You do not need to pick the display unit in
the calibration flow: Home Assistant can convert convertible units in the entity
settings.

## Statistics Caveat

Changing commodity, base unit, or scale changes the sensor's native unit or
device class. Home Assistant treats that as a non-convertible change for long
term statistics.

The entity keeps its ID, but previous long-term statistics are orphaned. Calibrate
intentionally, ideally once. Saving a calibration reloads the hub so the sensor is
rebuilt with the new unit and class.

## Model-Scoped Mappings

For models whose unit and scale are authoritatively known, a contributor can ship
a model-scoped mapping in the [device library](device-library.md#model-scoped-mappings-models)
so those meters work without per-device calibration.

The shipped library does not include speculative real-meter consumption mappings.
A wrong scale would silently corrupt Energy data.
