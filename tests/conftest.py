"""Shared fixtures for the rtl_433 test suite.

Provides the ``enable_custom_integrations`` plumbing the
``pytest-homeassistant-custom-component`` plugin needs to discover the
``custom_components/rtl_433`` package, plus a builder for the single hub config
entry (optionally pre-seeded with a per-device map at ``data["devices"]``) and a
loader for the project-authored JSON event fixtures.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

from pyrtl_433 import Rtl433Client
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.rtl_433.const import (
    CONF_AVAILABILITY_TIMEOUT,
    CONF_DEVICES,
    CONF_DISCOVERY_ENABLED,
    CONF_HOST,
    CONF_PATH,
    CONF_PORT,
    DEFAULT_PATH,
    DEFAULT_PORT,
    DOMAIN,
)
from custom_components.rtl_433.coordinator import Rtl433Coordinator

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def pytest_configure(config):
    """Register the suite's own markers."""
    config.addinivalue_line(
        "markers",
        "hub_disconnected: do not auto-connect coordinators (see "
        "hub_connected_by_default)",
    )


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Make Home Assistant load ``custom_components/rtl_433`` in every test."""
    yield


def load_events(name: str) -> list[dict[str, Any]]:
    """Load a project-authored fixture file as a list of event dicts."""
    path = FIXTURES_DIR / name
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        return [data]
    return list(data)


@pytest.fixture
def events():
    """Return the fixture loader so tests can pull event lists by file name."""
    return load_events


def build_hub_entry(
    *,
    host: str = "rtl433.local",
    port: int = DEFAULT_PORT,
    path: str = DEFAULT_PATH,
    secure: bool = False,
    discovery_enabled: bool = True,
    availability_timeout: int | None = None,
    devices: dict[str, Any] | None = None,
    options: dict[str, Any] | None = None,
    entry_id: str | None = None,
    version: int = 2,
) -> MockConfigEntry:
    """Build a hub ``MockConfigEntry`` with sensible defaults for tests.

    ``devices`` (when given) is placed at ``data["devices"]`` — the single source
    of truth for nested-device state, keyed by ``device_key`` with each value
    carrying ``model`` / ``fields`` / optional ``timeout_override``. The entry
    defaults to ``version=2`` so normal lifecycle setup does not trigger the
    1 -> 2 migration; the migration test builds its v1 entries directly.
    """
    data: dict[str, Any] = {
        CONF_HOST: host,
        CONF_PORT: port,
        CONF_PATH: path,
        "secure": secure,
        CONF_DISCOVERY_ENABLED: discovery_enabled,
    }
    if availability_timeout is not None:
        data[CONF_AVAILABILITY_TIMEOUT] = availability_timeout
    if devices is not None:
        data[CONF_DEVICES] = devices

    kwargs: dict[str, Any] = {
        "domain": DOMAIN,
        "title": f"rtl_433 ({host})",
        "data": data,
        "options": options or {},
        "unique_id": f"hub:{host}:{port}",
        "version": version,
    }
    if entry_id is not None:
        kwargs["entry_id"] = entry_id
    return MockConfigEntry(**kwargs)


@pytest.fixture
def hub_entry_builder():
    """Expose :func:`build_hub_entry` as a fixture."""
    return build_hub_entry


def mark_hub_connected(coordinator: Any) -> None:
    """Put a coordinator in the state a live hub connection leaves behind.

    Tests inject events straight into the client's frame handler instead of over
    a real socket, so the client's ``connected`` flag stays False and the
    coordinator's connection-backed availability gate reads the whole run as one
    long outage: every device behind the hub is unavailable whatever its own
    silence timeout says (see ``coordinator/_watchdog.py``). Any test that feeds
    events is implicitly assuming the hub is connected, so it has to say so —
    this is that statement.

    Sets the connect-edge state directly rather than firing the client callback:
    the callback path also triggers SDR adoption, which these tests do not want.
    It does dispatch the availability repaint, because entities added while the
    coordinator was still disconnected have already written ``unavailable`` and
    would otherwise keep it until their next event.
    """
    coordinator._client.connected = True
    coordinator._was_connected = True
    coordinator._ever_connected = True
    coordinator._disconnected_since = None
    coordinator._async_sync_hub_availability()


@pytest.fixture
def hub_connected():
    """Expose :func:`mark_hub_connected` as a fixture."""
    return mark_hub_connected


@pytest.fixture(autouse=True)
def hub_connected_by_default(request):
    """Leave every coordinator a test starts in the connected state.

    A connected hub is what almost every test means, so it is the default rather
    than an opt-in each setup site has to remember: forgetting it does not fail
    where the hub is set up, it fails much later as an unrelated-looking device
    timeout as soon as the test looks at an entity's state.

    Marking connected once at startup is not enough on its own: the real setup
    also starts the library client's reconnect loop against a host that does not
    resolve, and its failures flip ``connected`` back to False asynchronously,
    part-way through whatever the test is doing. Every device entity is gated on
    that flag, so the loop is stubbed out here too — otherwise "connected by
    default" silently stops holding as soon as a test lets the event loop run.

    Tests that exercise the outage side opt out with
    ``@pytest.mark.hub_disconnected`` and drive the edges themselves.
    """
    if "hub_disconnected" in request.keywords:
        yield
        return

    original = Rtl433Coordinator.async_start

    async def _async_start(self: Rtl433Coordinator) -> None:
        await original(self)
        mark_hub_connected(self)

    with (
        patch.object(Rtl433Client, "start", new=AsyncMock()),
        patch.object(Rtl433Client, "stop", new=AsyncMock()),
        patch.object(Rtl433Coordinator, "async_start", _async_start),
    ):
        yield


@pytest.fixture
def no_socket():
    """Stub the transport's connect loop so no real WebSocket is ever opened.

    Opt-in (not autouse): only the tests that drive a hub entry through the real
    ``async_setup_entry`` need it. ``Rtl433Client.start`` is the single place the
    socket is opened, so a no-op keeps ``coordinator.async_start`` intact while
    leaving setup — and any later ``async_reload`` — offline. ``test_lifecycle``
    keeps its own module-scoped copy; this one exists for the flow-level modules
    that reload an entry mid-test.

    ``hub_connected_by_default`` above already stubs the same method for every
    test that does not opt out, so requesting this fixture is now a statement of
    intent rather than the thing keeping the socket shut. It still matters for a
    ``@pytest.mark.hub_disconnected`` test, which gets no stub of its own.
    """

    async def _noop(self) -> None:
        return None

    with patch.object(Rtl433Client, "start", _noop):
        yield
