"""Mutation-killing tests for custom_components/rtl_433/diagnostics.py.

Covers every surviving mutant in diffs_diagnostics.txt:
- TO_REDACT set: host is redacted, port/path are NOT redacted
- _resolve_coordinator: DOMAIN key and entry_id key are exact
- _unmatched_field_keys: registry vs None, skip_keys exclusion, sorted output
- async_get_config_entry_diagnostics: all exact key names and values, both
  coordinator-present and coordinator-absent paths, devices block structure
  (model, identity, fields, available, last_seen as ISO string), seen_field_keys,
  DATA_ENTRY_LIBRARY key for registry lookup
"""

from __future__ import annotations

from datetime import UTC, datetime

from custom_components.rtl_433.const import (
    CONF_HOST,
    CONF_PATH,
    CONF_PORT,
    DATA_ENTRY_LIBRARY,
    DOMAIN,
)
from custom_components.rtl_433.diagnostics import (
    TO_REDACT,
    _resolve_coordinator,
    _unmatched_field_keys,
    async_get_config_entry_diagnostics,
)
from custom_components.rtl_433.mapping import FieldDescriptor, Registry, load_library
from homeassistant.core import HomeAssistant

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeNormalized:
    """Minimal stand-in for a NormalizedEvent as stored in coordinator.devices."""

    def __init__(self, model: str = "TestModel", identity: dict | None = None) -> None:
        self.model = model
        self.identity = identity or {}


class _FakeCoordinator:
    """Minimal stand-in exposing the runtime state diagnostics reads."""

    def __init__(self) -> None:
        self.host = "secret-host.local"
        self.port = 8433
        self.path = "/ws"
        self.secure = False
        self.connected = True
        self.discovery_enabled = True
        self.availability_timeout = 600
        self.seen_fields: set[str] = {"temperature_C", "humidity", "made_up_field"}
        self.devices: dict = {}
        self.device_fields: dict = {}
        self.last_seen: dict = {}
        self.available: dict = {}

    @property
    def ws_url(self) -> str:
        return f"ws://{self.host}:{self.port}{self.path}"


def _make_minimal_registry() -> Registry:
    """Return a Registry with exactly one known field: 'known_field'."""
    descriptor = FieldDescriptor(
        field_key="known_field",
        platform="sensor",
        name="Known Field",
        object_suffix="known",
    )
    return Registry(flat={"known_field": descriptor}, models={})


def _setup_hass_with_coordinator(
    hass: HomeAssistant,
    entry,
    coordinator: _FakeCoordinator,
    registry: Registry | None = None,
    skip_keys: set | None = None,
) -> None:
    """Register domain data and coordinator in hass.

    Diagnostics resolves the library per entry from
    ``hass.data[DOMAIN][DATA_ENTRY_LIBRARY][entry_id]`` (a ``(registry, skip_keys)``
    tuple), so mirror that shape when a registry is supplied.
    """
    hass.data.setdefault(DOMAIN, {})
    if registry is not None:
        hass.data[DOMAIN].setdefault(DATA_ENTRY_LIBRARY, {})[entry.entry_id] = (
            registry,
            skip_keys or set(),
        )
    hass.data[DOMAIN][entry.entry_id] = coordinator


# ---------------------------------------------------------------------------
# TO_REDACT set: host is redacted, port and path are NOT
# ---------------------------------------------------------------------------


def test_to_redact_contains_host() -> None:
    """CONF_HOST must be in TO_REDACT."""
    assert CONF_HOST in TO_REDACT


def test_to_redact_does_not_contain_port() -> None:
    """CONF_PORT must NOT be in TO_REDACT so port is visible in diagnostics."""
    assert CONF_PORT not in TO_REDACT


def test_to_redact_does_not_contain_path() -> None:
    """CONF_PATH must NOT be in TO_REDACT so path is visible in diagnostics."""
    assert CONF_PATH not in TO_REDACT


# ---------------------------------------------------------------------------
# _resolve_coordinator: uses DOMAIN and entry_id exactly
# ---------------------------------------------------------------------------


async def test_resolve_coordinator_returns_coordinator_when_present(
    hass: HomeAssistant, hub_entry_builder
) -> None:
    """_resolve_coordinator returns the coordinator stored under hass.data[DOMAIN][entry_id]."""
    entry = hub_entry_builder(entry_id="test-entry-123")
    entry.add_to_hass(hass)
    coordinator = _FakeCoordinator()
    hass.data[DOMAIN] = {entry.entry_id: coordinator}
    result = _resolve_coordinator(hass, entry)
    assert result is coordinator


async def test_resolve_coordinator_returns_none_when_domain_absent(
    hass: HomeAssistant, hub_entry_builder
) -> None:
    """_resolve_coordinator returns None when DOMAIN not in hass.data."""
    entry = hub_entry_builder(entry_id="test-entry-456")
    entry.add_to_hass(hass)
    # Ensure DOMAIN is not in hass.data
    hass.data.pop(DOMAIN, None)
    result = _resolve_coordinator(hass, entry)
    assert result is None


async def test_resolve_coordinator_returns_none_when_entry_absent(
    hass: HomeAssistant, hub_entry_builder
) -> None:
    """_resolve_coordinator returns None when entry_id not in domain data."""
    entry = hub_entry_builder(entry_id="test-entry-789")
    entry.add_to_hass(hass)
    hass.data[DOMAIN] = {"other-entry": _FakeCoordinator()}
    result = _resolve_coordinator(hass, entry)
    assert result is None


async def test_resolve_coordinator_uses_domain_key(
    hass: HomeAssistant, hub_entry_builder
) -> None:
    """Coordinator must be stored under DOMAIN, not any other key."""
    entry = hub_entry_builder(entry_id="test-entry-domain")
    entry.add_to_hass(hass)
    coordinator = _FakeCoordinator()
    # Store under a wrong key — must not be found
    hass.data["wrong_domain"] = {entry.entry_id: coordinator}
    result = _resolve_coordinator(hass, entry)
    assert result is None


# ---------------------------------------------------------------------------
# _unmatched_field_keys: sorted output, skip_keys exclusion, registry usage
# ---------------------------------------------------------------------------


def test_unmatched_field_keys_sorted() -> None:
    """_unmatched_field_keys returns fields in sorted order."""
    coordinator = _FakeCoordinator()
    coordinator.seen_fields = {"z_field", "a_field", "m_field"}
    result = _unmatched_field_keys(coordinator, None, set())
    assert result == sorted(result)
    assert result == ["a_field", "m_field", "z_field"]


def test_unmatched_field_keys_excludes_skip_keys() -> None:
    """Fields in skip_keys must not appear in the unmatched list."""
    coordinator = _FakeCoordinator()
    coordinator.seen_fields = {"skipped_field", "unknown_field"}
    result = _unmatched_field_keys(coordinator, None, {"skipped_field"})
    assert "skipped_field" not in result
    assert "unknown_field" in result


def test_unmatched_field_keys_excludes_mapped_fields() -> None:
    """Fields known to the registry must not appear in the unmatched list."""
    coordinator = _FakeCoordinator()
    coordinator.seen_fields = {"known_field", "truly_unknown_field"}
    registry = _make_minimal_registry()
    result = _unmatched_field_keys(coordinator, registry, set())
    assert "known_field" not in result
    assert "truly_unknown_field" in result


def test_unmatched_field_keys_uses_registry_not_none() -> None:
    """Passing a real registry vs None must differ when registry has a match.

    mutmut_5 replaces ``registry`` with ``None`` in the lookup call, which
    falls back to the cached default library and may resolve fields that the
    explicit (minimal) registry does not know. This test uses a field that IS
    in the explicit registry but is NOT in the shipped library, so swapping
    registry -> None causes the field to become unmatched (the shipped library
    has no entry for it), whereas the real code with the explicit registry sees
    it as matched.
    """
    coordinator = _FakeCoordinator()
    # "synthetic_test_known_field_xyz" is not in the shipped library
    coordinator.seen_fields = {"synthetic_test_known_field_xyz"}
    descriptor = FieldDescriptor(
        field_key="synthetic_test_known_field_xyz",
        platform="sensor",
        name="Synthetic",
        object_suffix="synthetic",
    )
    registry = Registry(flat={"synthetic_test_known_field_xyz": descriptor}, models={})
    # With the explicit registry: field is matched -> unmatched list is empty
    result_with_registry = _unmatched_field_keys(coordinator, registry, set())
    assert "synthetic_test_known_field_xyz" not in result_with_registry

    # With None registry: falls back to shipped library, field is unknown -> appears
    result_with_none = _unmatched_field_keys(coordinator, None, set())
    assert "synthetic_test_known_field_xyz" in result_with_none


def test_unmatched_field_keys_all_matched_returns_empty() -> None:
    """When all seen fields are in the registry, the result is empty."""
    coordinator = _FakeCoordinator()
    coordinator.seen_fields = {"known_field"}
    registry = _make_minimal_registry()
    result = _unmatched_field_keys(coordinator, registry, set())
    assert result == []


def test_unmatched_field_keys_skip_keys_is_exact_set() -> None:
    """skip_keys must be used as a set membership test (not some other check)."""
    coordinator = _FakeCoordinator()
    coordinator.seen_fields = {"field_a", "field_b"}
    # Only skip field_a; field_b should be unmatched
    result = _unmatched_field_keys(coordinator, None, {"field_a"})
    assert "field_a" not in result
    assert "field_b" in result


# ---------------------------------------------------------------------------
# async_get_config_entry_diagnostics: coordinator-absent path
# ---------------------------------------------------------------------------


async def test_diag_coordinator_absent_has_false_flag(
    hass: HomeAssistant, hub_entry_builder
) -> None:
    """coordinator_loaded must be False when coordinator is absent."""
    entry = hub_entry_builder()
    entry.add_to_hass(hass)
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["coordinator_loaded"] is False


async def test_diag_coordinator_absent_has_no_connection_block(
    hass: HomeAssistant, hub_entry_builder
) -> None:
    """No 'connection' key when coordinator is absent."""
    entry = hub_entry_builder()
    entry.add_to_hass(hass)
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert "connection" not in diag


async def test_diag_coordinator_absent_entry_block_has_entry_id(
    hass: HomeAssistant, hub_entry_builder
) -> None:
    """entry block must include key 'entry_id' with the entry's id value."""
    entry = hub_entry_builder(entry_id="specific-id-for-test")
    entry.add_to_hass(hass)
    diag = await async_get_config_entry_diagnostics(hass, entry)
    # Must use key "entry_id" not "XXentry_idXX" or "ENTRY_ID"
    assert "entry_id" in diag["entry"]
    assert diag["entry"]["entry_id"] == entry.entry_id


async def test_diag_coordinator_absent_entry_block_has_title(
    hass: HomeAssistant, hub_entry_builder
) -> None:
    """entry block must include key 'title' with the entry's title value."""
    entry = hub_entry_builder(host="somehost.local")
    entry.add_to_hass(hass)
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert "title" in diag["entry"]
    assert diag["entry"]["title"] == entry.title


async def test_diag_coordinator_absent_entry_block_has_options(
    hass: HomeAssistant, hub_entry_builder
) -> None:
    """entry block must include key 'options' with the entry's options dict."""
    entry = hub_entry_builder(options={"some_opt": 42})
    entry.add_to_hass(hass)
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert "options" in diag["entry"]
    assert diag["entry"]["options"] == {"some_opt": 42}


async def test_diag_coordinator_absent_uses_domain_key(
    hass: HomeAssistant, hub_entry_builder
) -> None:
    """Domain data must be read with DOMAIN, not None or other key.

    mutmut_2 replaces DOMAIN with None in hass.data.get(DOMAIN, {}).
    We put library data under DOMAIN but not under None; if the mutant
    fires, no library is found, and made_up_field appears as unmatched even
    though it shouldn't matter here. Validate the coordinator is correctly
    resolved (not found) so the absent path fires.
    """
    entry = hub_entry_builder()
    entry.add_to_hass(hass)
    # Store coordinator under DOMAIN — if DOMAIN key is wrong, None is returned
    coordinator = _FakeCoordinator()
    hass.data[DOMAIN] = {entry.entry_id: coordinator}
    diag = await async_get_config_entry_diagnostics(hass, entry)
    # coordinator IS present, so loaded must be True
    assert diag["coordinator_loaded"] is True


async def test_diag_data_library_key_used(
    hass: HomeAssistant, hub_entry_builder
) -> None:
    """Registry must be read using the per-entry DATA_ENTRY_LIBRARY key, not None.

    Diagnostics resolves the merged library from
    ``domain_data.get(DATA_ENTRY_LIBRARY, {}).get(entry_id, (None, set()))``. We
    store the registry there; a mutant that nulls the key (or the entry lookup)
    yields a None registry so every seen field appears unmatched, whereas the real
    code excludes fields that resolve against the registry.
    """
    entry = hub_entry_builder()
    entry.add_to_hass(hass)
    coordinator = _FakeCoordinator()
    coordinator.seen_fields = {"synthetic_test_known_field_xyz"}
    descriptor = FieldDescriptor(
        field_key="synthetic_test_known_field_xyz",
        platform="sensor",
        name="Synthetic",
        object_suffix="synthetic",
    )
    registry = Registry(flat={"synthetic_test_known_field_xyz": descriptor}, models={})
    hass.data.setdefault(DOMAIN, {})
    # Diagnostics resolves the per-entry library from DATA_ENTRY_LIBRARY[entry_id].
    hass.data[DOMAIN][DATA_ENTRY_LIBRARY] = {entry.entry_id: (registry, set())}
    hass.data[DOMAIN][entry.entry_id] = coordinator

    diag = await async_get_config_entry_diagnostics(hass, entry)
    # With real code and correct key: field is matched -> NOT in unmatched_field_keys
    assert "synthetic_test_known_field_xyz" not in diag["unmatched_field_keys"]


# ---------------------------------------------------------------------------
# async_get_config_entry_diagnostics: coordinator-present path, top-level keys
# ---------------------------------------------------------------------------


async def test_diag_coordinator_loaded_true(
    hass: HomeAssistant, hub_entry_builder
) -> None:
    """coordinator_loaded must be True when coordinator is present."""
    entry = hub_entry_builder()
    entry.add_to_hass(hass)
    coordinator = _FakeCoordinator()
    _setup_hass_with_coordinator(hass, entry, coordinator)
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["coordinator_loaded"] is True


async def test_diag_connected_key_exact_name_and_value(
    hass: HomeAssistant, hub_entry_builder
) -> None:
    """diagnostics['connected'] must equal coordinator.connected (True).

    Kills mutmut_56 (value=None), mutmut_57 ('XXconnectedXX'), mutmut_58 ('CONNECTED').
    """
    entry = hub_entry_builder()
    entry.add_to_hass(hass)
    coordinator = _FakeCoordinator()
    coordinator.connected = True
    _setup_hass_with_coordinator(hass, entry, coordinator)
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert "connected" in diag
    assert diag["connected"] is True


async def test_diag_connected_false_value(
    hass: HomeAssistant, hub_entry_builder
) -> None:
    """diagnostics['connected'] must equal coordinator.connected (False)."""
    entry = hub_entry_builder()
    entry.add_to_hass(hass)
    coordinator = _FakeCoordinator()
    coordinator.connected = False
    _setup_hass_with_coordinator(hass, entry, coordinator)
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["connected"] is False


async def test_diag_discovery_enabled_key_exact_name_and_value(
    hass: HomeAssistant, hub_entry_builder
) -> None:
    """diagnostics['discovery_enabled'] must equal coordinator.discovery_enabled.

    Kills mutmut_59 (value=None), mutmut_60 ('XXdiscovery_enabledXX'),
    mutmut_61 ('DISCOVERY_ENABLED').
    """
    entry = hub_entry_builder()
    entry.add_to_hass(hass)
    coordinator = _FakeCoordinator()
    coordinator.discovery_enabled = False
    _setup_hass_with_coordinator(hass, entry, coordinator)
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert "discovery_enabled" in diag
    assert diag["discovery_enabled"] is False


async def test_diag_availability_timeout_key_exact_name_and_value(
    hass: HomeAssistant, hub_entry_builder
) -> None:
    """diagnostics['availability_timeout'] must equal coordinator.availability_timeout.

    Kills mutmut_62 (value=None), mutmut_63 ('XXavailability_timeoutXX'),
    mutmut_64 ('AVAILABILITY_TIMEOUT').
    """
    entry = hub_entry_builder()
    entry.add_to_hass(hass)
    coordinator = _FakeCoordinator()
    coordinator.availability_timeout = 300
    _setup_hass_with_coordinator(hass, entry, coordinator)
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert "availability_timeout" in diag
    assert diag["availability_timeout"] == 300


async def test_diag_seen_field_keys_exact_name_sorted(
    hass: HomeAssistant, hub_entry_builder
) -> None:
    """diagnostics['seen_field_keys'] must be sorted(coordinator.seen_fields).

    Kills mutmut_86 (value=None), mutmut_87 ('XXseen_field_keysXX'),
    mutmut_88 ('SEEN_FIELD_KEYS').
    """
    entry = hub_entry_builder()
    entry.add_to_hass(hass)
    coordinator = _FakeCoordinator()
    coordinator.seen_fields = {"z_raw", "a_raw", "m_raw"}
    _setup_hass_with_coordinator(hass, entry, coordinator)
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert "seen_field_keys" in diag
    assert diag["seen_field_keys"] == ["a_raw", "m_raw", "z_raw"]


# ---------------------------------------------------------------------------
# async_get_config_entry_diagnostics: connection block keys
# ---------------------------------------------------------------------------


async def test_diag_connection_secure_key_exact(
    hass: HomeAssistant, hub_entry_builder
) -> None:
    """connection block must use key 'secure', not 'XXsecureXX' or 'SECURE'.

    Kills mutmut_49, mutmut_50.
    """
    entry = hub_entry_builder()
    entry.add_to_hass(hass)
    coordinator = _FakeCoordinator()
    coordinator.secure = True
    _setup_hass_with_coordinator(hass, entry, coordinator)
    diag = await async_get_config_entry_diagnostics(hass, entry)
    conn = diag["connection"]
    assert "secure" in conn
    assert conn["secure"] is True


async def test_diag_connection_port_present_and_not_redacted(
    hass: HomeAssistant, hub_entry_builder
) -> None:
    """connection block must include port (not redacted)."""
    entry = hub_entry_builder(port=9999)
    entry.add_to_hass(hass)
    coordinator = _FakeCoordinator()
    coordinator.port = 9999
    _setup_hass_with_coordinator(hass, entry, coordinator)
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert CONF_PORT in diag["connection"]
    assert diag["connection"][CONF_PORT] == 9999


async def test_diag_connection_path_present_and_not_redacted(
    hass: HomeAssistant, hub_entry_builder
) -> None:
    """connection block must include path (not redacted)."""
    entry = hub_entry_builder(path="/custom/ws")
    entry.add_to_hass(hass)
    coordinator = _FakeCoordinator()
    coordinator.path = "/custom/ws"
    _setup_hass_with_coordinator(hass, entry, coordinator)
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert CONF_PATH in diag["connection"]
    assert diag["connection"][CONF_PATH] == "/custom/ws"


async def test_diag_connection_host_is_redacted(
    hass: HomeAssistant, hub_entry_builder
) -> None:
    """Host must be redacted in the connection block."""
    entry = hub_entry_builder()
    entry.add_to_hass(hass)
    coordinator = _FakeCoordinator()
    coordinator.host = "private-host.internal"
    _setup_hass_with_coordinator(hass, entry, coordinator)
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["connection"][CONF_HOST] != "private-host.internal"


async def test_diag_connection_ws_url_is_redacted(
    hass: HomeAssistant, hub_entry_builder
) -> None:
    """ws_url must be redacted (it embeds the host)."""
    entry = hub_entry_builder()
    entry.add_to_hass(hass)
    coordinator = _FakeCoordinator()
    coordinator.host = "private-host.internal"
    _setup_hass_with_coordinator(hass, entry, coordinator)
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["connection"].get("ws_url") != coordinator.ws_url


# ---------------------------------------------------------------------------
# async_get_config_entry_diagnostics: devices block
# ---------------------------------------------------------------------------


async def test_diag_devices_key_is_dict(hass: HomeAssistant, hub_entry_builder) -> None:
    """diagnostics['devices'] must be a dict (not None).

    Kills mutmut_65 (devices=None), mutmut_66 ('XXdevicesXX'),
    mutmut_67 ('DEVICES').
    """
    entry = hub_entry_builder()
    entry.add_to_hass(hass)
    coordinator = _FakeCoordinator()
    coordinator.devices = {}
    _setup_hass_with_coordinator(hass, entry, coordinator)
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert "devices" in diag
    assert isinstance(diag["devices"], dict)


async def test_diag_devices_model_key_exact(
    hass: HomeAssistant, hub_entry_builder
) -> None:
    """Device entry must use key 'model', not 'XXmodelXX' or 'MODEL'.

    Kills mutmut_68, mutmut_69.
    """
    entry = hub_entry_builder()
    entry.add_to_hass(hass)
    coordinator = _FakeCoordinator()
    normalized = _FakeNormalized(model="Acurite-606TX", identity={"id": 42})
    coordinator.devices = {"Acurite-606TX-42": normalized}
    coordinator.device_fields = {"Acurite-606TX-42": {"temperature_C"}}
    coordinator.available = {"Acurite-606TX-42": True}
    coordinator.last_seen = {}
    _setup_hass_with_coordinator(hass, entry, coordinator)
    diag = await async_get_config_entry_diagnostics(hass, entry)
    dev = diag["devices"]["Acurite-606TX-42"]
    assert "model" in dev
    assert dev["model"] == "Acurite-606TX"


async def test_diag_devices_identity_key_exact(
    hass: HomeAssistant, hub_entry_builder
) -> None:
    """Device entry must use key 'identity', not 'XXidentityXX' or 'IDENTITY'.

    Kills mutmut_70, mutmut_71.
    """
    entry = hub_entry_builder()
    entry.add_to_hass(hass)
    coordinator = _FakeCoordinator()
    identity = {"id": 7, "channel": 2}
    normalized = _FakeNormalized(model="TestModel", identity=identity)
    coordinator.devices = {"TestModel-7-ch2": normalized}
    coordinator.device_fields = {}
    coordinator.available = {}
    coordinator.last_seen = {}
    _setup_hass_with_coordinator(hass, entry, coordinator)
    diag = await async_get_config_entry_diagnostics(hass, entry)
    dev = diag["devices"]["TestModel-7-ch2"]
    assert "identity" in dev
    assert dev["identity"] == identity


async def test_diag_devices_fields_key_exact_and_sorted(
    hass: HomeAssistant, hub_entry_builder
) -> None:
    """Device entry must use key 'fields' with sorted device_fields value.

    Kills mutmut_72 ('XXfieldsXX'), mutmut_73 ('FIELDS'),
    mutmut_74 (sorted(None)), mutmut_75 (get(None,set())),
    mutmut_76 (get(device_key,None)), mutmut_77 (get(set())),
    mutmut_78 (get(device_key,)).
    """
    entry = hub_entry_builder()
    entry.add_to_hass(hass)
    coordinator = _FakeCoordinator()
    normalized = _FakeNormalized(model="FooBar", identity={})
    device_key = "FooBar"
    coordinator.devices = {device_key: normalized}
    coordinator.device_fields = {device_key: {"z_field", "a_field", "m_field"}}
    coordinator.available = {device_key: True}
    coordinator.last_seen = {}
    _setup_hass_with_coordinator(hass, entry, coordinator)
    diag = await async_get_config_entry_diagnostics(hass, entry)
    dev = diag["devices"][device_key]
    assert "fields" in dev
    # Must be sorted list of the device's fields
    assert dev["fields"] == ["a_field", "m_field", "z_field"]


async def test_diag_devices_fields_empty_when_not_present(
    hass: HomeAssistant, hub_entry_builder
) -> None:
    """fields defaults to [] (from empty set) when device has no device_fields entry.

    Kills mutmut_76 (default=None instead of set()) and mutmut_78 (no default).
    """
    entry = hub_entry_builder()
    entry.add_to_hass(hass)
    coordinator = _FakeCoordinator()
    device_key = "DeviceWithNoFields"
    normalized = _FakeNormalized(model="DeviceWithNoFields", identity={})
    coordinator.devices = {device_key: normalized}
    coordinator.device_fields = {}  # device_key not present
    coordinator.available = {device_key: True}
    coordinator.last_seen = {}
    _setup_hass_with_coordinator(hass, entry, coordinator)
    diag = await async_get_config_entry_diagnostics(hass, entry)
    dev = diag["devices"][device_key]
    assert dev["fields"] == []


async def test_diag_devices_available_key_exact_and_value(
    hass: HomeAssistant, hub_entry_builder
) -> None:
    """Device entry must use key 'available' from coordinator.available[device_key].

    Kills mutmut_79 ('XXavailableXX'), mutmut_80 ('AVAILABLE'),
    mutmut_81 (get(None) instead of get(device_key)).
    """
    entry = hub_entry_builder()
    entry.add_to_hass(hass)
    coordinator = _FakeCoordinator()
    device_key = "SomeDevice"
    normalized = _FakeNormalized(model="SomeDevice", identity={})
    coordinator.devices = {device_key: normalized}
    coordinator.device_fields = {}
    coordinator.available = {device_key: False}
    coordinator.last_seen = {}
    _setup_hass_with_coordinator(hass, entry, coordinator)
    diag = await async_get_config_entry_diagnostics(hass, entry)
    dev = diag["devices"][device_key]
    assert "available" in dev
    assert dev["available"] is False


async def test_diag_devices_last_seen_key_exact_and_iso_format(
    hass: HomeAssistant, hub_entry_builder
) -> None:
    """Device entry must use key 'last_seen' with ISO 8601 datetime string.

    Kills mutmut_82 ('XXlast_seenXX'), mutmut_83 ('LAST_SEEN'),
    mutmut_84 (get(None) instead of get(device_key)),
    mutmut_85 (is None instead of is not None).
    """
    entry = hub_entry_builder()
    entry.add_to_hass(hass)
    coordinator = _FakeCoordinator()
    device_key = "TimedDevice"
    normalized = _FakeNormalized(model="TimedDevice", identity={})
    ts = datetime(2025, 6, 1, 12, 30, 45, tzinfo=UTC)
    coordinator.devices = {device_key: normalized}
    coordinator.device_fields = {}
    coordinator.available = {device_key: True}
    coordinator.last_seen = {device_key: ts}
    _setup_hass_with_coordinator(hass, entry, coordinator)
    diag = await async_get_config_entry_diagnostics(hass, entry)
    dev = diag["devices"][device_key]
    assert "last_seen" in dev
    # Must be the ISO string of the timestamp, not None
    assert dev["last_seen"] == ts.isoformat()


async def test_diag_devices_last_seen_none_when_never_seen(
    hass: HomeAssistant, hub_entry_builder
) -> None:
    """Device 'last_seen' is None when device_key not in coordinator.last_seen.

    mutmut_85 flips 'is not None' to 'is None', causing last_seen to be set
    to the isoformat string when the key is ABSENT (which would crash), and
    None when the key IS present. This test verifies the None case.
    """
    entry = hub_entry_builder()
    entry.add_to_hass(hass)
    coordinator = _FakeCoordinator()
    device_key = "NeverSeenDevice"
    normalized = _FakeNormalized(model="NeverSeenDevice", identity={})
    coordinator.devices = {device_key: normalized}
    coordinator.device_fields = {}
    coordinator.available = {}
    coordinator.last_seen = {}  # device_key not present
    _setup_hass_with_coordinator(hass, entry, coordinator)
    diag = await async_get_config_entry_diagnostics(hass, entry)
    dev = diag["devices"][device_key]
    assert dev["last_seen"] is None


async def test_diag_devices_last_seen_uses_device_key_not_none(
    hass: HomeAssistant, hub_entry_builder
) -> None:
    """last_seen lookup must use device_key, not None as the key.

    mutmut_84 replaces get(device_key) with get(None). With that mutation,
    looking up None in last_seen returns None even when device_key is present,
    so last_seen would be None instead of the ISO string.
    """
    entry = hub_entry_builder()
    entry.add_to_hass(hass)
    coordinator = _FakeCoordinator()
    device_key = "DeviceA"
    normalized = _FakeNormalized(model="DeviceA", identity={})
    ts = datetime(2024, 3, 15, 8, 0, 0, tzinfo=UTC)
    coordinator.devices = {device_key: normalized}
    coordinator.device_fields = {}
    coordinator.available = {device_key: True}
    # Only device_key in last_seen; None is not
    coordinator.last_seen = {device_key: ts}
    _setup_hass_with_coordinator(hass, entry, coordinator)
    diag = await async_get_config_entry_diagnostics(hass, entry)
    dev = diag["devices"][device_key]
    assert dev["last_seen"] == ts.isoformat()


async def test_diag_devices_available_uses_device_key_not_none(
    hass: HomeAssistant, hub_entry_builder
) -> None:
    """available lookup must use device_key, not None.

    mutmut_81 replaces get(device_key) with get(None). With that mutation,
    available would always be None even when device_key is present.
    """
    entry = hub_entry_builder()
    entry.add_to_hass(hass)
    coordinator = _FakeCoordinator()
    device_key = "DeviceB"
    normalized = _FakeNormalized(model="DeviceB", identity={})
    coordinator.devices = {device_key: normalized}
    coordinator.device_fields = {}
    coordinator.available = {device_key: True}  # device_key -> True; None -> absent
    coordinator.last_seen = {}
    _setup_hass_with_coordinator(hass, entry, coordinator)
    diag = await async_get_config_entry_diagnostics(hass, entry)
    dev = diag["devices"][device_key]
    # Must be True (from device_key lookup), not None (from None lookup)
    assert dev["available"] is True


async def test_diag_devices_fields_uses_device_key_not_none(
    hass: HomeAssistant, hub_entry_builder
) -> None:
    """device_fields.get must use device_key, not None.

    mutmut_75 replaces get(device_key, set()) with get(None, set()),
    returning empty list even when device_key has fields.
    """
    entry = hub_entry_builder()
    entry.add_to_hass(hass)
    coordinator = _FakeCoordinator()
    device_key = "dev01"
    normalized = _FakeNormalized(model="dev01", identity={})
    coordinator.devices = {device_key: normalized}
    coordinator.device_fields = {device_key: {"temp_field"}}
    coordinator.available = {}
    coordinator.last_seen = {}
    _setup_hass_with_coordinator(hass, entry, coordinator)
    diag = await async_get_config_entry_diagnostics(hass, entry)
    dev = diag["devices"][device_key]
    # Must contain "temp_field", not be empty
    assert dev["fields"] == ["temp_field"]


# ---------------------------------------------------------------------------
# async_get_config_entry_diagnostics: unmatched_field_keys uses registry param
# ---------------------------------------------------------------------------


async def test_diag_unmatched_field_keys_uses_registry_not_none(
    hass: HomeAssistant, hub_entry_builder
) -> None:
    """unmatched_field_keys must be called with registry, not None.

    mutmut_94 replaces 'registry' with None in the _unmatched_field_keys call.
    When registry is None, lookup falls back to the shipped library. We put a
    field in a custom registry that is NOT in the shipped library so the
    mutation changes the result.
    """
    entry = hub_entry_builder()
    entry.add_to_hass(hass)
    coordinator = _FakeCoordinator()
    coordinator.seen_fields = {"synthetic_test_known_field_xyz"}
    coordinator.devices = {}
    descriptor = FieldDescriptor(
        field_key="synthetic_test_known_field_xyz",
        platform="sensor",
        name="Synthetic",
        object_suffix="synthetic",
    )
    registry = Registry(flat={"synthetic_test_known_field_xyz": descriptor}, models={})
    _setup_hass_with_coordinator(hass, entry, coordinator, registry=registry)
    diag = await async_get_config_entry_diagnostics(hass, entry)
    # With real registry: field is matched, not in unmatched
    assert "synthetic_test_known_field_xyz" not in diag["unmatched_field_keys"]


# ---------------------------------------------------------------------------
# Full structural test: both paths
# ---------------------------------------------------------------------------


async def test_diag_full_structure_coordinator_present(
    hass: HomeAssistant, hub_entry_builder
) -> None:
    """Full structure test for coordinator-present path with a device."""
    entry = hub_entry_builder(host="myhost.local", port=8433, path="/ws")
    entry.add_to_hass(hass)
    coordinator = _FakeCoordinator()
    coordinator.host = "myhost.local"
    coordinator.port = 8433
    coordinator.path = "/ws"
    coordinator.secure = False
    coordinator.connected = True
    coordinator.discovery_enabled = True
    coordinator.availability_timeout = 600
    coordinator.seen_fields = {"temperature_C", "humidity"}
    device_key = "Acurite-606TX-99"
    normalized = _FakeNormalized(model="Acurite-606TX", identity={"id": 99})
    ts = datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC)
    coordinator.devices = {device_key: normalized}
    coordinator.device_fields = {device_key: {"temperature_C", "humidity"}}
    coordinator.available = {device_key: True}
    coordinator.last_seen = {device_key: ts}

    registry, skip_keys = load_library()
    _setup_hass_with_coordinator(
        hass, entry, coordinator, registry=registry, skip_keys=skip_keys
    )
    diag = await async_get_config_entry_diagnostics(hass, entry)

    # Top-level keys present
    assert diag["coordinator_loaded"] is True
    assert "entry" in diag
    assert "connection" in diag
    assert "connected" in diag
    assert "discovery_enabled" in diag
    assert "availability_timeout" in diag
    assert "devices" in diag
    assert "seen_field_keys" in diag
    assert "unmatched_field_keys" in diag

    # entry block
    assert diag["entry"]["entry_id"] == entry.entry_id
    assert diag["entry"]["title"] == entry.title
    assert "options" in diag["entry"]

    # connection block: host redacted, port/path visible
    assert diag["connection"][CONF_PORT] == 8433
    assert diag["connection"][CONF_PATH] == "/ws"
    assert diag["connection"].get(CONF_HOST) != "myhost.local"
    assert "secure" in diag["connection"]
    assert diag["connection"]["secure"] is False

    # runtime fields
    assert diag["connected"] is True
    assert diag["discovery_enabled"] is True
    assert diag["availability_timeout"] == 600

    # devices block
    dev = diag["devices"][device_key]
    assert dev["model"] == "Acurite-606TX"
    assert dev["identity"] == {"id": 99}
    assert dev["fields"] == ["humidity", "temperature_C"]
    assert dev["available"] is True
    assert dev["last_seen"] == ts.isoformat()

    # seen_field_keys is sorted
    assert diag["seen_field_keys"] == ["humidity", "temperature_C"]

    # temperature_C and humidity are mapped in the real library -> not unmatched
    assert "temperature_C" not in diag["unmatched_field_keys"]
    assert "humidity" not in diag["unmatched_field_keys"]


async def test_diag_full_structure_coordinator_absent(
    hass: HomeAssistant, hub_entry_builder
) -> None:
    """Full structure test for coordinator-absent path."""
    entry = hub_entry_builder(options={"opt_key": "opt_val"})
    entry.add_to_hass(hass)
    diag = await async_get_config_entry_diagnostics(hass, entry)

    assert diag["coordinator_loaded"] is False
    assert "entry" in diag
    assert diag["entry"]["entry_id"] == entry.entry_id
    assert diag["entry"]["title"] == entry.title
    assert "options" in diag["entry"]
    assert diag["entry"]["options"] == {"opt_key": "opt_val"}
    # No runtime blocks
    assert "connection" not in diag
    assert "connected" not in diag
    assert "devices" not in diag
    assert "seen_field_keys" not in diag
    assert "unmatched_field_keys" not in diag
