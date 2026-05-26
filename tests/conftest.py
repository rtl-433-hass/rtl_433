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
