"""Thin, Indigo-free wrapper around the community `sense_energy` client.

Responsibilities:
- authenticate once with email/password (optionally an MFA code) and hand back the tokens
  so the caller can persist them; on later starts resume from those tokens and renew them,
  falling back to the password only if the renewal fails;
- normalise the library's exceptions into two kinds the plugin cares about:
  `SenseAuthError` (needs the user) and `SenseTransientError` (retry later);
- expose plain snapshots (`RealtimeSnapshot`, `TrendSnapshot`, `DiscoveredDevice`) so the
  plugin and its tests never touch library internals.
"""

from __future__ import annotations

import logging
import socket
from dataclasses import dataclass, field

import requests
from sense_energy import (
    Scale,
    SenseAPIException,
    SenseAPITimeoutException,
    SenseAuthenticationException,
    Senseable,
    SenseMFARequiredException,
    SenseWebsocketException,
)

DEFAULT_TIMEOUT = 30  # seconds per API call (Home Assistant's default)
AUTH_KEYS = ("access_token", "user_id", "device_id", "refresh_token", "monitor_id")

log = logging.getLogger("Plugin")


class SenseAuthError(Exception):
    """Authentication needs the user: bad password, MFA code required/invalid, renewal dead."""

    def __init__(self, message: str, mfa_required: bool = False):
        super().__init__(message)
        self.mfa_required = mfa_required


class SenseTransientError(Exception):
    """Timeout, network or API hiccup: keep the last values and try again later."""


@dataclass
class RealtimeSnapshot:
    total_w: float
    solar_w: float
    voltage: list[float]
    hz: float
    # Sense id -> (name, watts) for every device Sense currently sees running.
    devices: dict[str, tuple[str, float]] = field(default_factory=dict)


@dataclass
class TrendSnapshot:
    daily_kwh: float
    daily_solar_kwh: float


@dataclass
class DiscoveredDevice:
    id: str
    name: str
    revoked: bool = False
    merged_ids: list[str] = field(default_factory=list)


def _is_true(value) -> bool:
    return value is True or str(value).lower() == "true"


def parse_discovered(raw: list[dict]) -> list[DiscoveredDevice]:
    """Map the raw `monitors/<id>/devices` list to DiscoveredDevice records."""
    out = []
    for entry in raw or []:
        tags = entry.get("tags") or {}
        revoked = _is_true(tags.get("Revoked")) or _is_true(tags.get("UserDeleted"))
        merged = [m for m in str(tags.get("MergedDevices", "")).split(",") if m]
        out.append(DiscoveredDevice(str(entry["id"]), entry.get("name", ""), revoked, merged))
    return out


class SenseClient:
    def __init__(
        self,
        username: str,
        password: str,
        timeout: int = DEFAULT_TIMEOUT,
        saved_auth: dict | None = None,
        mfa_code: str = "",
        senseable_factory=Senseable,
    ):
        self._username = username
        self._password = password
        self._mfa_code = (mfa_code or "").strip()
        self._saved_auth = dict(saved_auth) if saved_auth else None
        self._api = senseable_factory(api_timeout=timeout, wss_timeout=timeout)
        # The plugin owns the poll cadence; never let the library silently skip a fetch.
        self._api.rate_limit = 0
        self.realtime_path = "unknown"

    # ------------------------------------------------------------------ auth
    def connect(self) -> dict:
        """Authenticate (resuming saved tokens when possible) and return the tokens to persist."""
        if self._saved_auth and all(self._saved_auth.get(k) for k in AUTH_KEYS):
            try:
                self._api.load_auth(
                    self._saved_auth["access_token"],
                    self._saved_auth["user_id"],
                    self._saved_auth["device_id"],
                    self._saved_auth["refresh_token"],
                )
                self._api.set_monitor_id(self._saved_auth["monitor_id"])
                self._call(self._api.renew_auth)
                log.debug("Resumed Sense session from saved tokens")
                return self.auth_data()
            except SenseAuthError as err:
                log.info(f"Saved Sense session could not be renewed ({err}); logging in again")
            # transient errors propagate: the caller retries connect() later

        try:
            self._call(self._api.authenticate, self._username, self._password)
        except SenseAuthError as err:
            if not err.mfa_required:
                raise
            if not self._mfa_code:
                raise SenseAuthError(
                    "Sense asks for a multi-factor code: enter the current code from your "
                    "authenticator app in the plugin configuration and save.",
                    mfa_required=True,
                ) from err
            self._call(self._api.validate_mfa, self._mfa_code)
        log.debug("Logged in to Sense with email/password")
        return self.auth_data()

    def auth_data(self) -> dict:
        return {
            "access_token": self._api.sense_access_token,
            "user_id": self._api.sense_user_id,
            "device_id": self._api.device_id,
            "refresh_token": self._api.refresh_token,
            "monitor_id": self._api.sense_monitor_id,
        }

    # ------------------------------------------------------------------ data
    def refresh_realtime(self) -> RealtimeSnapshot:
        self._call(self._api.update_realtime)
        raw = self._api.get_realtime() or {}
        devices = {}
        for entry in raw.get("devices") or []:
            devices[str(entry["id"])] = (entry.get("name", ""), float(entry.get("w", 0) or 0))
        try:
            self.realtime_path = (
                "realtime_update" if self._api.supports_realtime_update_api() else "websocket"
            )
        except Exception:  # noqa: BLE001 - purely informational
            pass
        return RealtimeSnapshot(
            total_w=float(self._api.active_power or 0),
            solar_w=float(self._api.active_solar_power or 0),
            voltage=[float(v) for v in (self._api.active_voltage or [])],
            hz=float(self._api.active_frequency or 0),
            devices=devices,
        )

    def refresh_trends(self) -> TrendSnapshot:
        # Only the daily scale is shown; one call instead of the library's five.
        self._call(self._api.get_trend_data, Scale.DAY)
        return TrendSnapshot(
            daily_kwh=float(self._api.daily_usage or 0),
            daily_solar_kwh=float(self._api.daily_production or 0),
        )

    def discovered_devices(self) -> list[DiscoveredDevice]:
        return parse_discovered(self._call(self._api.get_discovered_device_data))

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _call(fn, *args):
        """Run one library call, translating its failure modes."""
        try:
            return fn(*args)
        except SenseMFARequiredException as err:
            raise SenseAuthError(str(err), mfa_required=True) from err
        except SenseAuthenticationException as err:
            raise SenseAuthError(str(err)) from err
        except (SenseAPITimeoutException, SenseWebsocketException, SenseAPIException) as err:
            raise SenseTransientError(f"{type(err).__name__}: {err}") from err
        except (requests.RequestException, socket.error, OSError, ValueError) as err:
            # ValueError covers JSON decode errors on a malformed / HTML error response.
            raise SenseTransientError(f"{type(err).__name__}: {err}") from err
