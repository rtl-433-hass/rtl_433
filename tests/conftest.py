"""Shared fixtures for the rtl_433 test suite.

Provides the ``enable_custom_integrations`` plumbing the
``pytest-homeassistant-custom-component`` plugin needs to discover the
``custom_components/rtl_433`` package, plus small builders for the two kinds of
config entry (hub and per-device) and a loader for the project-authored JSON
event fixtures.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.rtl_433.const import (
    CONF_AVAILABILITY_TIMEOUT,
    CONF_DEVICE_KEY,
    CONF_DISCOVERY_ENABLED,
    CONF_ENTRY_TYPE,
    CONF_HOST,
    CONF_HUB_ENTRY_ID,
    CONF_MODEL,
    CONF_PATH,
    CONF_PORT,
    DEFAULT_PATH,
    DEFAULT_PORT,
    DOMAIN,
    ENTRY_TYPE_DEVICE,
    ENTRY_TYPE_HUB,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


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
    options: dict[str, Any] | None = None,
    entry_id: str | None = None,
) -> MockConfigEntry:
    """Build a hub ``MockConfigEntry`` with sensible defaults for tests."""
    data: dict[str, Any] = {
        CONF_ENTRY_TYPE: ENTRY_TYPE_HUB,
        CONF_HOST: host,
        CONF_PORT: port,
        CONF_PATH: path,
        "secure": secure,
        CONF_DISCOVERY_ENABLED: discovery_enabled,
    }
    if availability_timeout is not None:
        data[CONF_AVAILABILITY_TIMEOUT] = availability_timeout

    kwargs: dict[str, Any] = {
        "domain": DOMAIN,
        "title": f"rtl_433 ({host})",
        "data": data,
        "options": options or {},
        "unique_id": f"hub:{host}:{port}",
    }
    if entry_id is not None:
        kwargs["entry_id"] = entry_id
    return MockConfigEntry(**kwargs)


def build_device_entry(
    *,
    hub_entry_id: str,
    device_key: str,
    model: str = "",
    options: dict[str, Any] | None = None,
    entry_id: str | None = None,
) -> MockConfigEntry:
    """Build a per-device ``MockConfigEntry`` linked to its hub."""
    kwargs: dict[str, Any] = {
        "domain": DOMAIN,
        "title": f"{model} ({device_key})" if model else device_key,
        "data": {
            CONF_ENTRY_TYPE: ENTRY_TYPE_DEVICE,
            CONF_HUB_ENTRY_ID: hub_entry_id,
            CONF_DEVICE_KEY: device_key,
            CONF_MODEL: model,
        },
        "options": options or {},
        "unique_id": f"{hub_entry_id}:{device_key}",
    }
    if entry_id is not None:
        kwargs["entry_id"] = entry_id
    return MockConfigEntry(**kwargs)


@pytest.fixture
def hub_entry_builder():
    """Expose :func:`build_hub_entry` as a fixture."""
    return build_hub_entry


@pytest.fixture
def device_entry_builder():
    """Expose :func:`build_device_entry` as a fixture."""
    return build_device_entry
