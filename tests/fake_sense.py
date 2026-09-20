"""A stand-in for `sense_energy.Senseable` that records calls and replays canned data."""

from __future__ import annotations

from sense_energy import (
    Scale,
    SenseAPITimeoutException,
    SenseAuthenticationException,
    SenseMFARequiredException,
)

RAW_DEVICES = [
    {"id": "9acfa0cd", "name": "Espresso Maker", "tags": {}},
    {"id": "a660a997", "name": "Downstairs AC", "tags": {}},
    {"id": "bd19692f", "name": "Downstairs AC", "tags": {"Revoked": "true"}},
    {"id": "400b819d", "name": "Dryer", "tags": {"MergedDevices": "1111aaaa,2222bbbb"}},
    {"id": "solar", "name": "Solar", "tags": {}},
]

REALTIME = {
    "w": 2175.18,
    "solar_w": 0,
    "voltage": [121.1, 120.9],
    "hz": 60.01,
    "devices": [{"id": "9acfa0cd", "name": "Espresso Maker", "icon": "cup", "w": 1180.4}],
}


class FakeSenseable:
    """Only the surface `sense.client.SenseClient` touches."""

    instances: list[FakeSenseable] = []

    # Behaviour knobs. Class-level so a test can set them before the plugin builds the
    # instance (tests must restore them); instance-level overrides work too.
    password_ok = True
    mfa_required = False
    mfa_valid_code = "123456"
    renew_ok = True
    fail_with: Exception | None = None  # raised by every data call
    realtime_update_supported = True

    def __init__(self, api_timeout=5, wss_timeout=5, **_):
        FakeSenseable.instances.append(self)
        self.api_timeout = api_timeout
        self.rate_limit = 60
        self.calls: list[str] = []
        # data
        self.raw_devices = [dict(d) for d in RAW_DEVICES]
        self._realtime = {}
        self.device_id = "fake-device-id"
        self.sense_access_token = ""
        self.sense_user_id = ""
        self.refresh_token = ""
        self.sense_monitor_id = ""

    # auth ---------------------------------------------------------------
    def _grant(self, token):
        self.sense_access_token = token
        self.sense_user_id = "user-1"
        self.refresh_token = "refresh-1"
        self.sense_monitor_id = "monitor-1"

    def authenticate(self, username, password):
        self.calls.append("authenticate")
        if self.mfa_required:
            raise SenseMFARequiredException("mfa required")
        if not self.password_ok:
            raise SenseAuthenticationException("Please check username and password")
        self._grant("access-pw")

    def validate_mfa(self, code):
        self.calls.append("validate_mfa")
        if code != self.mfa_valid_code:
            raise SenseAuthenticationException("bad code")
        self._grant("access-mfa")

    def load_auth(self, access_token, user_id, device_id, refresh_token):
        self.calls.append("load_auth")
        self.sense_access_token = access_token
        self.sense_user_id = user_id
        self.device_id = device_id
        self.refresh_token = refresh_token

    def set_monitor_id(self, monitor_id):
        self.sense_monitor_id = monitor_id

    def renew_auth(self):
        self.calls.append("renew_auth")
        if not self.renew_ok:
            raise SenseAuthenticationException("API Return Code: 401")
        self.sense_access_token = "access-renewed"

    # data ---------------------------------------------------------------
    def _maybe_fail(self):
        if self.fail_with:
            raise self.fail_with

    def update_realtime(self):
        self.calls.append("update_realtime")
        self._maybe_fail()
        self._realtime = dict(REALTIME)

    def get_realtime(self):
        return self._realtime

    def supports_realtime_update_api(self):
        return self.realtime_update_supported

    @property
    def active_power(self):
        return self._realtime.get("w", 0)

    @property
    def active_solar_power(self):
        return self._realtime.get("solar_w", 0)

    @property
    def active_voltage(self):
        return self._realtime.get("voltage", [])

    @property
    def active_frequency(self):
        return self._realtime.get("hz", 0)

    def get_trend_data(self, scale, dt=None):
        self.calls.append(f"get_trend_data:{scale.name}")
        self._maybe_fail()
        assert scale is Scale.DAY

    @property
    def daily_usage(self):
        return 38.24

    @property
    def daily_production(self):
        return 0.0

    def get_discovered_device_data(self):
        self.calls.append("get_discovered_device_data")
        self._maybe_fail()
        return self.raw_devices


def timeout():
    return SenseAPITimeoutException("API call timed out")
