"""Mapping-library loading for the rtl_433 integration's config-entry setup.

The Home-Assistant-aware layer over the pure ``mapping`` module: load the shipped
library once (cached on ``hass.data``) and merge a hub's stored user overrides
over it. ``async_setup_entry`` calls both during setup; kept here so ``__init__``
stays focused on the lifecycle wiring.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_USER_MAPPINGS, DATA_LIBRARY, DOMAIN, LOGGER
from .mapping import Registry, load_library, merge_overrides


async def _async_load_library(
    hass: HomeAssistant,
) -> tuple[Registry, set[str]]:
    """Load (and cache) the shipped library.

    The glob/parse touches the filesystem, so it runs in the executor. The
    shipped ``(registry, skip_keys)`` is cached on
    ``hass.data[DOMAIN][DATA_LIBRARY]`` so additional hubs reuse a single load.
    Per-hub user overrides are merged over this result in
    :func:`_merge_entry_library` and cached separately per entry.
    """
    domain_data = hass.data.setdefault(DOMAIN, {})
    cached = domain_data.get(DATA_LIBRARY)
    if cached is not None:
        return cached

    registry, skip_keys = await hass.async_add_executor_job(load_library)
    domain_data[DATA_LIBRARY] = (registry, skip_keys)
    return registry, skip_keys


def _merge_entry_library(
    hass: HomeAssistant,
    entry: ConfigEntry,
    shipped_registry: Registry,
    shipped_skip_keys: set[str],
) -> tuple[Registry, set[str]]:
    """Merge this hub's stored user overrides over the shipped library.

    Reads ``entry.data[CONF_USER_MAPPINGS]`` (the per-hub normalized override
    object) and layers it over the shipped ``(registry, skip_keys)`` via the pure
    :func:`merge_overrides` (no I/O, so no executor needed). Defensive: any
    unexpected error is logged and the shipped inputs (copied) are returned so a
    bad override never crashes setup.
    """
    overrides = entry.data.get(CONF_USER_MAPPINGS) or {}
    try:
        return merge_overrides(shipped_registry, shipped_skip_keys, overrides)
    except Exception:  # noqa: BLE001 - never let a bad override crash setup
        LOGGER.warning(
            "Failed to merge user mappings for hub %s; using shipped library",
            entry.title,
            exc_info=True,
        )
        return (
            Registry(
                flat=dict(shipped_registry.flat),
                models={
                    model: dict(entries)
                    for model, entries in shipped_registry.models.items()
                },
            ),
            set(shipped_skip_keys),
        )
