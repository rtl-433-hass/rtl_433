"""Managed SDR-settings subsystem for the rtl_433 coordinator.

When a hub has ``manage_settings`` enabled, Home Assistant owns the receiver's
SDR settings: it adopts the server's current values on the first connect,
persists the desired state to a per-hub ``Store`` (so a value change never churns
the config entry), and replays that desired state on every reconnect. This
module holds that policy — the ``Store`` subclass, the desired/managed maps' load
and persist, the per-field command builders, the write path, adoption, and
reconnect enforcement.

:class:`_SdrSettingsMixin` is mixed into ``Rtl433Coordinator`` (see ``base.py``).
It delegates the actual ``/cmd`` I/O to the coordinator's transport
(``_send_cmd`` / ``_refresh_meta``, defined in ``base.py``) and relies on the
desired-state attributes declared in the coordinator's ``__init__``
(``_desired``, ``_managed``, ``_store``, ``_initial_freq_seeded``,
``initial_center_frequency``, ``meta``, ``connected``).
"""

from __future__ import annotations

from typing import Any

from homeassistant.helpers.storage import Store

from ..sdr_settings import (
    KEY_CENTER_FREQUENCY,
    KEY_GAIN_AUTO,
    KEY_GAIN_DB,
    SDR_SETTINGS,
    SDR_SETTINGS_BY_KEY,
    gain_command_arg,
)


class _SdrStore(Store[dict[str, Any]]):
    """Per-hub desired-state Store with a Hz->MHz center-frequency migration.

    Version 1 persisted ``values["center_frequency"]`` in Hz; version 2 stores it
    in MHz (matching the control entity and the setup field). ``async_load``
    invokes this migrator when the on-disk version is older than
    :data:`..const.SDR_STORE_VERSION`, so an existing managed hub's frequency
    converts transparently on the next load.
    """

    async def _async_migrate_func(
        self,
        old_major_version: int,
        old_minor_version: int,
        old_data: dict[str, Any],
    ) -> dict[str, Any]:
        """Convert a version-1 payload's center frequency from Hz to MHz."""
        if old_major_version < 2:
            values = old_data.get("values")
            if isinstance(values, dict):
                hz = values.get(KEY_CENTER_FREQUENCY)
                if isinstance(hz, (int, float)):
                    values[KEY_CENTER_FREQUENCY] = float(hz) / 1_000_000
        return old_data


class _SdrSettingsMixin:
    """Desired-state Store, write path, adoption, and reconnect enforcement."""

    async def async_load_desired_state(self) -> None:
        """Load the persisted desired state, or wipe it when management is off.

        Called from :meth:`async_start` before the connect loop is created. When
        ``manage_settings`` is ``False`` the Store is removed so a later re-enable
        re-adopts the server's then-current settings from scratch; otherwise the
        ``{"values": ..., "managed": [...]}`` payload is loaded into
        ``self._desired`` / ``self._managed``.
        """
        if not self.manage_settings:
            await self._store.async_remove()  # re-enable will re-adopt
            self._desired, self._managed = {}, set()
            self._initial_freq_seeded = False
            return
        data = await self._store.async_load() or {}
        self._desired = dict(data.get("values", {}))
        self._managed = set(data.get("managed", []))
        # Absent in stores written before this flag existed -> treat as not yet
        # seeded; the seed only applies when an ``initial_center_frequency`` is
        # configured, so a legacy store without one is unaffected.
        self._initial_freq_seeded = bool(data.get("initial_freq_seeded", False))

    async def _persist_desired(self) -> None:
        """Persist the desired-state map + managed-set to the per-hub Store."""
        await self._store.async_save(
            {
                "values": self._desired,
                "managed": sorted(self._managed),
                "initial_freq_seeded": self._initial_freq_seeded,
            }
        )

    def _gain_command_arg(self) -> str | None:
        """Compose the ``gain`` ``/cmd`` arg from the combined desired gain state.

        Gain is two desired keys (``gain`` dB + ``gain_auto`` bool) that resolve
        to one ``gain`` command; both the per-field build (:meth:`_command_args`)
        and connect-time enforcement (:meth:`_enforce_all`) compose it the same
        way, so the composition lives here.
        """
        return gain_command_arg(
            self._desired.get(KEY_GAIN_DB),
            bool(self._desired.get(KEY_GAIN_AUTO, False)),
        )

    def _command_args(self, key: str) -> tuple[str, int | None, str | None] | None:
        """Build the ``(command, val, arg)`` to send for one managed field.

        Returns ``None`` when the field has no desired value to send. Gain is two
        desired keys (``gain`` dB + ``gain_auto`` bool) but one ``gain`` command,
        so its ``arg`` is composed from the *combined* desired state via
        :func:`gain_command_arg`; both gain keys resolve to the same command and
        the caller is responsible for emitting it only once.
        """
        if key in (KEY_GAIN_DB, KEY_GAIN_AUTO):
            return ("gain", None, self._gain_command_arg())
        setting = SDR_SETTINGS_BY_KEY.get(key)
        if setting is None or key not in self._desired:
            return None
        sent = setting.to_command(self._desired[key])
        if setting.arg_kind == "val":
            return (setting.command, int(sent), None)
        return (setting.command, None, str(sent))

    async def set_sdr(self, field: str, value: Any) -> None:
        """Write a desired SDR value: persist it, then enforce it if connected.

        Updates ``self._desired[field]``, marks the field managed, and persists
        the Store first so the intent survives a restart even if the live send
        fails. If connected, the mapped setter command is issued (under the lock)
        and ``self.meta`` is reconciled via a best-effort ``_refresh_meta``; a
        ``/cmd`` failure is swallowed and **leaves the desired value intact**.
        """
        self._desired[field] = value
        self._managed.add(field)
        await self._persist_desired()
        if self.connected:
            await self._enforce_field(field)

    async def _enforce_field(self, field: str) -> None:
        """Send one managed field's command, then read it back (best effort)."""
        args = self._command_args(field)
        if args is None:
            return
        command, val, arg = args
        await self._send_cmd(command, val=val, arg=arg)
        # Reconcile self.meta from the server; swallow its own failures.
        await self._refresh_meta()

    async def _seed_desired_on_first_connect(self) -> None:
        """Establish the desired state on the first managed connect.

        Two independent one-time steps, both safe to call on every connect:

        - Adopt the server's current settings while ``_desired`` is empty (the
          very first connect). Once adopted+persisted, later connects skip this.
        - Apply the setup-time ``initial_center_frequency`` exactly once, gated on
          a persisted ``_initial_freq_seeded`` flag rather than on ``_desired``
          being empty. This makes the user's explicit choice win over the adopted
          (and hop-mode-skipped) value even when the Store already holds desired
          state, and never re-applies after the user later changes the frequency
          via the control.
        """
        if not self._desired:
            await self._adopt_from_server()
        if self.initial_center_frequency is not None and not self._initial_freq_seeded:
            self._desired[KEY_CENTER_FREQUENCY] = self.initial_center_frequency
            self._managed.add(KEY_CENTER_FREQUENCY)
            self._initial_freq_seeded = True
            await self._persist_desired()

    async def _adopt_from_server(self) -> None:
        """Seed the desired state from the server's current ``self.meta``.

        Runs once (when ``_desired`` is empty) on first connect. If the getters
        produced nothing (``self.meta`` empty / ``/cmd`` hidden behind a proxy)
        we adopt nothing and leave the Store empty — the reachability repair on
        the getter path already surfaces that; we never raise here. Guard: skip
        ``center_frequency`` while the server is in hop mode
        (``len(frequencies) > 1``) so HA does not pin a single frequency. The
        gain pair is seeded explicitly: ``gain_auto`` from ``gain == ""`` and the
        ``gain`` dB value parsed from the gain string when not auto.
        """
        meta = self.meta
        if not meta:  # /cmd unreachable -> adopt nothing
            return
        hopping = len(meta.get("frequencies", []) or []) > 1
        for setting in SDR_SETTINGS:
            if setting.key in (KEY_GAIN_DB, KEY_GAIN_AUTO):
                continue  # handled as a pair below
            if setting.key == "center_frequency" and hopping:
                continue
            current = setting.read(meta)
            if current is None:
                continue
            self._desired[setting.key] = current
            self._managed.add(setting.key)

        # Gain pair: only adopt when the server reported a gain string at all.
        if "gain" in meta:
            gain = meta["gain"]
            auto = gain == ""
            self._desired[KEY_GAIN_AUTO] = auto
            self._managed.add(KEY_GAIN_AUTO)
            if not auto:
                try:
                    self._desired[KEY_GAIN_DB] = float(gain)
                    self._managed.add(KEY_GAIN_DB)
                except TypeError, ValueError:
                    pass

        await self._persist_desired()

    async def _enforce_all(self) -> None:
        """Replay every managed field's setter command (gain emitted once).

        Best-effort: each send swallows its own ``/cmd`` errors and never raises,
        so a reconnect replay cannot disturb the connect loop or the event
        stream. The gain pair shares one ``gain`` command, so it is composed from
        the combined desired values and emitted exactly once.

        After the replay, reconcile ``self.meta`` from the server (mirroring the
        single-field :meth:`_enforce_field` read-back) so the hub's "actual" SDR
        sensors — and the controls that fall back to meta — reflect the values
        just applied instead of the pre-enforce snapshot taken on connect. The
        emit inside ``_refresh_meta`` also repaints the controls after the
        first-connect seed changed their desired value.
        """
        gain_managed = bool(self._managed & {KEY_GAIN_DB, KEY_GAIN_AUTO})
        for key in sorted(self._managed):
            if key in (KEY_GAIN_DB, KEY_GAIN_AUTO):
                continue  # emitted once below
            args = self._command_args(key)
            if args is None:
                continue
            command, val, arg = args
            await self._send_cmd(command, val=val, arg=arg)
        if gain_managed:
            await self._send_cmd("gain", arg=self._gain_command_arg())
        # Reconcile meta from the server so the actual-value sensors converge now
        # rather than waiting for the next refresh tick; swallows its own errors.
        # Skip when nothing is managed (the connect-time refresh already ran and
        # there were no setter sends to reconcile).
        if self._managed:
            await self._refresh_meta()

    # ----------------------------------------------------------------- #
    # Public read API for the control entities                          #
    # ----------------------------------------------------------------- #
    def get_desired(self, field: str) -> Any:
        """Return the current desired value for a field (``None`` if unset)."""
        return self._desired.get(field)

    def is_managed(self, field: str) -> bool:
        """Return whether Home Assistant is actively managing a field."""
        return field in self._managed

    async def clear_desired_state(self) -> None:
        """Drop all desired state and remove the Store (management turned off)."""
        self._desired, self._managed = {}, set()
        self._initial_freq_seeded = False
        await self._store.async_remove()
