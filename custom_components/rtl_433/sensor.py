"""Sensor platform for the rtl_433 hub config entry.

``async_setup_entry`` runs once for the hub config entry. It registers the
hub-level DIAGNOSTIC sensors (SDR/meta configuration and server statistics that
the coordinator sources over HTTP) and then delegates to the shared
:func:`~custom_components.rtl_433.entity.async_setup_hub_platform` helper, which
resolves the hub coordinator, builds a :class:`Rtl433Sensor` for every device's
observed mapped fields whose descriptor ``platform == "sensor"``, adds new
devices/fields at runtime, and keeps the hub's devices map current.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import (
    DOMAIN as SENSOR_DOMAIN,
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import MATCH_ALL, UnitOfFrequency, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util
from homeassistant.util.enum import try_parse_enum

from .const import DOMAIN
from .entity import Rtl433Entity, Rtl433HubEntity, async_setup_hub_platform
from .mapping import FieldDescriptor, apply_transform

if TYPE_CHECKING:
    from .coordinator import Rtl433Coordinator
    from .normalizer import NormalizedEvent

# This platform owns only descriptors whose ``platform`` attribute equals this.
PLATFORM = "sensor"

# States that should not overwrite a fresh value when restoring.
_NON_RESTORABLE = (None, "unknown", "unavailable")

# Entity-registry option Home Assistant's sensor base writes to freeze a sensor's
# previously-shown unit when the integration later reports a different one, so an
# existing sensor's displayed unit does not change under the user
# (``SensorEntity.add_to_platform_start``). Stored under the sensor platform's
# private options key.
_SENSOR_PRIVATE_OPTIONS = f"{SENSOR_DOMAIN}.private"
_SUGGESTED_UNIT_OPTION = "suggested_unit_of_measurement"
# HA never derives a unit-system suggested unit for temperature (it converts
# temperature via a display-time legacy path instead), so a ``°C``/``°F`` pin on
# a temperature entity is only ever the freeze-the-old-unit artifact above and is
# safe to drop.
_TEMPERATURE_UNITS = frozenset(
    {UnitOfTemperature.CELSIUS, UnitOfTemperature.FAHRENHEIT}
)


class Rtl433Sensor(Rtl433Entity, SensorEntity):
    """A single measurement field of an rtl_433 device."""

    def __init__(
        self,
        coordinator: Rtl433Coordinator,
        hub_entry_id: str,
        device_key: str,
        model: str,
        descriptor: FieldDescriptor,
    ) -> None:
        """Initialize sensor-specific description fields."""
        super().__init__(coordinator, hub_entry_id, device_key, model, descriptor)
        # Coerce the device-library's plain-string ``device_class`` /
        # ``state_class`` into their canonical enum members. Home Assistant's
        # sensor base performs its legacy temperature unit conversion behind an
        # identity check (``self.device_class is SensorDeviceClass.TEMPERATURE``),
        # which a bare ``"temperature"`` string fails -- so without this the
        # native unit (e.g. Acurite-986 °F) is shown verbatim instead of being
        # converted to the user's unit system. ``try_parse_enum`` returns the
        # singleton member for a valid value and ``None`` for an unknown one
        # (equivalent to leaving the attribute unset).
        self._attr_device_class = try_parse_enum(
            SensorDeviceClass, descriptor.device_class
        )
        self._attr_state_class = try_parse_enum(
            SensorStateClass, descriptor.state_class
        )
        self._attr_native_unit_of_measurement = descriptor.unit_of_measurement
        self._attr_force_update = descriptor.force_update

        # Seed from the coordinator's last event if it already carries the field
        # (covers a device that transmitted before its entity was added).
        last_event = coordinator.devices.get(device_key)
        if last_event is not None and descriptor.field_key in last_event.fields:
            self._apply_value(last_event.fields[descriptor.field_key])

    async def async_added_to_hass(self) -> None:
        """Restore state, then drop any stale frozen temperature unit."""
        await super().async_added_to_hass()
        self._drop_stale_temperature_unit_override()

    def _drop_stale_temperature_unit_override(self) -> None:
        """Clear Home Assistant's freeze-the-old-unit pin for temperature.

        When an integration starts reporting a different displayed unit for an
        existing sensor, HA's sensor base freezes the previously-shown unit into
        the entity registry as ``sensor.private.suggested_unit_of_measurement``
        so the unit does not change under the user. For a temperature sensor that
        pin is only ever this artifact -- HA has no unit-system suggested unit for
        temperature -- and here it freezes the pre-fix native unit (e.g. the
        Acurite-986's ``°F``) on a metric install, defeating the unit-system
        conversion. HA also restores the pin from its deleted-entities store when
        a device is removed and rediscovered, so a one-shot config migration would
        miss a later rediscovery; clearing it as each temperature entity is added
        catches every path. A user's explicit per-entity unit override lives under
        a different key (``sensor.unit_of_measurement``) and is left untouched.
        """
        if self.device_class is not SensorDeviceClass.TEMPERATURE:
            return
        registry = er.async_get(self.hass)
        entry = registry.async_get(self.entity_id)
        if entry is None:
            return
        private = entry.options.get(_SENSOR_PRIVATE_OPTIONS)
        if not private or private.get(_SUGGESTED_UNIT_OPTION) not in _TEMPERATURE_UNITS:
            return
        remaining = {
            key: value
            for key, value in private.items()
            if key != _SUGGESTED_UNIT_OPTION
        }
        registry.async_update_entity_options(
            self.entity_id, _SENSOR_PRIVATE_OPTIONS, remaining or None
        )

    def _apply_value(self, raw_value: Any) -> None:
        """Transform and store a raw value as the sensor's native value."""
        self._attr_native_value = apply_transform(self._descriptor, raw_value)

    async def _async_restore_state(self) -> None:
        """Restore the last known native value on startup.

        A live value already seeded from the coordinator's last event wins over a
        restored one.
        """
        if self._attr_native_value is not None:
            return
        last_state = await self.async_get_last_state()
        if last_state is not None and last_state.state not in _NON_RESTORABLE:
            self._attr_native_value = last_state.state


# --------------------------------------------------------------------------- #
# Per-device synthetic "Last seen" timestamp sensor.                           #
# --------------------------------------------------------------------------- #
# Sentinel field_key that no rtl_433 event can carry, so the base's
# field-driven _apply_value path is never triggered for this entity.
_LAST_SEEN_FIELD = "__last_seen__"

LAST_SEEN_DESCRIPTOR = FieldDescriptor(
    field_key=_LAST_SEEN_FIELD,
    platform="sensor",
    name="Last seen",
    object_suffix="last_seen",
    device_class="timestamp",
    entity_category="diagnostic",
    enabled_by_default=False,
)


class Rtl433LastSeenSensor(Rtl433Entity, SensorEntity):
    """Per-device diagnostic timestamp of when the device was last heard from.

    Synthetic (not field-driven): holds its own ``native_value``, seeded from a
    real event when one exists, restored otherwise, and updated on dispatch.
    Stays available once it has a value even after the device falls silent, so
    "last_seen older than X" staleness automations keep working.

    Ships disabled-by-default (a diagnostic timestamp is redundant for periodic
    devices, where ``available`` already conveys freshness) **except** for
    event-driven devices: those never expire, so their availability no longer
    signals freshness and this timestamp becomes the only such signal — so it is
    enabled by default for them.
    """

    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(
        self,
        coordinator: Rtl433Coordinator,
        hub_entry_id: str,
        device_key: str,
        model: str,
    ) -> None:
        """Initialize the synthetic last-seen sensor and seed a live value."""
        super().__init__(
            coordinator, hub_entry_id, device_key, model, LAST_SEEN_DESCRIPTOR
        )
        # Enable by default for event-driven devices (no reliable check-in), for
        # which availability never expires and this timestamp is the only
        # freshness signal. Periodic devices keep LAST_SEEN_DESCRIPTOR's
        # disabled-by-default.
        if coordinator.is_event_driven_device(device_key):
            self._attr_entity_registry_enabled_default = True
        # Seed only when a *real* event has been seen this session; the presence
        # of a devices-map entry distinguishes a true timestamp from the base's
        # startup baseline (which never sets coordinator.devices).
        if coordinator.devices.get(device_key) is not None:
            self._attr_native_value = coordinator.last_seen.get(device_key)

    def _apply_value(self, raw_value: Any) -> None:
        """No-op: the sentinel field_key never appears in an event."""

    async def _async_restore_state(self) -> None:
        """Restore the prior timestamp as a tz-aware datetime, if not seeded.

        A live value seeded from a real event wins over a restored one.
        """
        if self._attr_native_value is not None:
            return
        last_state = await self.async_get_last_state()
        if last_state is None or last_state.state in _NON_RESTORABLE:
            return
        restored = dt_util.parse_datetime(last_state.state)
        if restored is not None:
            self._attr_native_value = restored

    @callback
    def _handle_dispatch(self, event: NormalizedEvent) -> None:
        """Adopt the coordinator's last_seen on a real/watchdog dispatch.

        Overrides the base: the synthetic field is never in ``event.fields``, so
        instead of the field-driven path we read coordinator.last_seen, which the
        non-dispatching baseline never reaches.
        """
        self._attr_native_value = self._coordinator.last_seen.get(self._device_key)
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        """Available once a real/restored timestamp exists, ignoring the timeout."""
        return self._attr_native_value is not None


# --------------------------------------------------------------------------- #
# Hub-level DIAGNOSTIC sensors (SDR/meta + server stats).                       #
# --------------------------------------------------------------------------- #
def _meta(coordinator: Rtl433Coordinator, key: str) -> Any:
    """Read a key from ``coordinator.meta`` defensively (missing -> None)."""
    return coordinator.meta.get(key)


def _center_frequency_mhz(coordinator: Rtl433Coordinator) -> Any:
    """Read ``center_frequency`` from meta (Hz) as MHz, or None when absent.

    Mirrors the MHz presentation of the editable Center-frequency control; the
    raw per-frequency Hz list stays in the sensor's attributes.
    """
    hz = coordinator.meta.get("center_frequency")
    if hz is None:
        return None
    try:
        return float(hz) / 1_000_000
    except TypeError, ValueError:
        return None


def _frames(coordinator: Rtl433Coordinator, key: str) -> Any:
    """Read a key from the ``frames`` sub-dict of ``coordinator.stats``."""
    frames = coordinator.stats.get("frames")
    return frames.get(key) if isinstance(frames, dict) else None


def _gain(coordinator: Rtl433Coordinator) -> Any:
    """Render the SDR gain, mapping an empty string to ``"auto"``."""
    gain = coordinator.meta.get("gain")
    if gain is None:
        return None
    return "auto" if gain == "" else gain


@dataclass(frozen=True, kw_only=True)
class HubSensorDesc:
    """Lightweight description of one hub diagnostic sensor.

    ``value`` extracts the native value from the coordinator; ``attrs`` (when
    set) extracts extra-state attributes. Both read live coordinator state so
    the entity always reflects the latest HTTP-sourced hub data.

    ``folded_when_managing`` marks a sensor whose concept is folded into a managed
    control (number/select/switch) in managed mode; its diagnostic sensor is then
    suppressed so each concept has exactly one entity. Center frequency is
    intentionally NOT folded
    (its actual can diverge from the desired value under hopping), and the
    server-stats sensors are never folded.
    """

    suffix: str
    name: str
    value: Callable[[Rtl433Coordinator], Any]
    device_class: SensorDeviceClass | None = None
    native_unit: str | None = None
    state_class: SensorStateClass | None = None
    attrs: Callable[[Rtl433Coordinator], dict[str, Any] | None] | None = None
    folded_when_managing: bool = False


HUB_SENSORS: tuple[HubSensorDesc, ...] = (
    # --- SDR/meta configuration (from coordinator.meta) ------------------- #
    HubSensorDesc(
        suffix="center_frequency",
        name="Center frequency",
        value=_center_frequency_mhz,
        device_class=SensorDeviceClass.FREQUENCY,
        native_unit=UnitOfFrequency.MEGAHERTZ,
        attrs=lambda c: {
            "frequencies": c.meta.get("frequencies"),
            "hop_times": c.meta.get("hop_times"),
        },
    ),
    HubSensorDesc(
        suffix="sample_rate",
        name="Sample rate",
        value=lambda c: _meta(c, "samp_rate"),
        native_unit="Hz",
        folded_when_managing=True,
    ),
    HubSensorDesc(
        suffix="conversion_mode",
        name="Conversion mode",
        value=lambda c: _meta(c, "conversion_mode"),
        folded_when_managing=True,
    ),
    HubSensorDesc(
        suffix="hop_interval",
        name="Hop interval",
        value=lambda c: _meta(c, "hop_interval"),
        native_unit="s",
        folded_when_managing=True,
    ),
    HubSensorDesc(suffix="gain", name="Gain", value=_gain, folded_when_managing=True),
    HubSensorDesc(
        suffix="ppm_error",
        name="Frequency correction",
        value=lambda c: _meta(c, "ppm_error"),
        folded_when_managing=True,
    ),
    # --- Server statistics (from coordinator.stats) ----------------------- #
    HubSensorDesc(
        suffix="decoded_events",
        name="Decoded events",
        value=lambda c: _frames(c, "events"),
        state_class=SensorStateClass.TOTAL_INCREASING,
        attrs=lambda c: {
            "stats": c.stats.get("stats"),
            "since": c.stats.get("since"),
        },
    ),
    HubSensorDesc(
        suffix="ook_frames",
        name="OOK frames",
        value=lambda c: _frames(c, "count"),
        # Cumulative since the server started; resets when it restarts, which
        # TOTAL_INCREASING tolerates (same shape as decoded events).
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    HubSensorDesc(
        suffix="fsk_frames",
        name="FSK frames",
        value=lambda c: _frames(c, "fsk"),
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    HubSensorDesc(
        suffix="enabled_decoders",
        name="Enabled decoders",
        value=lambda c: c.stats.get("enabled"),
        # A current count of enabled decoders (a gauge that moves up/down as
        # decoders are toggled), not a running total -> MEASUREMENT.
        state_class=SensorStateClass.MEASUREMENT,
    ),
)


class Rtl433HubSensor(Rtl433HubEntity, SensorEntity):
    """A diagnostic sensor on the hub device, driven by a :class:`HubSensorDesc`.

    Reads live coordinator state via the description's callables and refreshes on
    ``signal_hub_update`` (handled by :class:`Rtl433HubEntity`). A missing key
    yields a ``None`` native value (state ``unknown``) rather than raising.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    # These diagnostic sensors expose live config/stats detail as state
    # attributes: ``decoded_events`` carries the server's per-protocol ``stats``
    # array -- one entry per enabled rtl_433 decoder (potentially hundreds), which
    # alone overflows the recorder's 16 KiB attribute limit -- and
    # ``center_frequency`` carries the ``frequencies``/``hop_times`` lists that
    # grow with the hop set. None of it is time-series worth persisting, so keep
    # every hub sensor's attributes out of the recorder: the live state still
    # shows them, but they are never written to the database (avoiding the
    # "State attributes ... exceed maximum size" warning and the DB churn it
    # warns about). MATCH_ALL future-proofs any later hub sensor that adds a
    # large attribute.
    _unrecorded_attributes = frozenset({MATCH_ALL})

    def __init__(
        self,
        coordinator: Rtl433Coordinator,
        hub_entry_id: str,
        desc: HubSensorDesc,
    ) -> None:
        """Initialize identity and entity-description fields from ``desc``."""
        super().__init__(coordinator, hub_entry_id)
        self._desc = desc
        self._attr_unique_id = f"{hub_entry_id}:hub:{desc.suffix}"
        self._attr_name = desc.name
        self._attr_device_class = desc.device_class
        self._attr_native_unit_of_measurement = desc.native_unit
        self._attr_state_class = desc.state_class

    @property
    def available(self) -> bool:
        """Always available: a missing value reads ``unknown``, not unavailable."""
        return True

    @property
    def native_value(self) -> Any:
        """Return the latest value extracted from the coordinator."""
        return self._desc.value(self._coordinator)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return extra-state attributes, dropping any with a ``None`` value."""
        if self._desc.attrs is None:
            return None
        attrs = self._desc.attrs(self._coordinator)
        return {k: v for k, v in (attrs or {}).items() if v is not None} or None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up rtl_433 sensors for the hub config entry.

    Registers the hub-level diagnostic sensors and then the per-device sensors.
    """
    coordinator = hass.data[DOMAIN][entry.entry_id]
    managed = coordinator.manage_settings
    async_add_entities(
        Rtl433HubSensor(coordinator, entry.entry_id, desc)
        for desc in HUB_SENSORS
        if not (managed and desc.folded_when_managing)
    )
    await async_setup_hub_platform(
        hass,
        entry,
        async_add_entities,
        PLATFORM,
        Rtl433Sensor,
        per_device_factory=Rtl433LastSeenSensor,
    )
