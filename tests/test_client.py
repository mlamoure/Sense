from __future__ import annotations

import pytest
from sense_energy import SenseAPIException

from sense.client import SenseAuthError, SenseClient, SenseTransientError, parse_discovered

from .fake_sense import FakeSenseable, timeout

SAVED = {
    "access_token": "old",
    "user_id": "user-1",
    "device_id": "dev-1",
    "refresh_token": "refresh-1",
    "monitor_id": "monitor-1",
}


def _client(**kwargs):
    FakeSenseable.instances.clear()
    c = SenseClient("me@example.com", "pw", senseable_factory=FakeSenseable, **kwargs)
    return c, FakeSenseable.instances[-1]


class TestConnect:
    def test_password_login_returns_tokens(self):
        c, api = _client()
        auth = c.connect()
        assert api.calls == ["authenticate"]
        assert auth == {
            "access_token": "access-pw",
            "user_id": "user-1",
            "device_id": "fake-device-id",
            "refresh_token": "refresh-1",
            "monitor_id": "monitor-1",
        }

    def test_saved_session_is_resumed_without_password(self):
        c, api = _client(saved_auth=SAVED)
        auth = c.connect()
        assert api.calls == ["load_auth", "renew_auth"]
        assert auth["access_token"] == "access-renewed"
        assert auth["device_id"] == "dev-1"

    def test_dead_saved_session_falls_back_to_password(self):
        c, api = _client(saved_auth=SAVED)
        api.renew_ok = False
        c.connect()
        assert api.calls == ["load_auth", "renew_auth", "authenticate"]

    def test_incomplete_saved_session_is_ignored(self):
        c, api = _client(saved_auth={"access_token": "x"})
        c.connect()
        assert api.calls == ["authenticate"]

    def test_bad_password(self):
        c, api = _client()
        api.password_ok = False
        with pytest.raises(SenseAuthError) as err:
            c.connect()
        assert not err.value.mfa_required

    def test_mfa_without_code_asks_the_user(self):
        c, api = _client()
        api.mfa_required = True
        with pytest.raises(SenseAuthError) as err:
            c.connect()
        assert err.value.mfa_required
        assert "authenticator" in str(err.value)

    def test_mfa_with_code(self):
        c, api = _client(mfa_code="123456")
        api.mfa_required = True
        auth = c.connect()
        assert api.calls == ["authenticate", "validate_mfa"]
        assert auth["access_token"] == "access-mfa"

    def test_mfa_with_wrong_code(self):
        c, api = _client(mfa_code="000000")
        api.mfa_required = True
        with pytest.raises(SenseAuthError):
            c.connect()

    def test_network_trouble_during_login_is_transient(self):
        c, api = _client(saved_auth=SAVED)
        api.renew_auth = lambda: (_ for _ in ()).throw(timeout())
        with pytest.raises(SenseTransientError):
            c.connect()

    def test_library_rate_limit_is_disabled(self):
        _c, api = _client()
        assert api.rate_limit == 0


class TestData:
    def test_realtime_snapshot(self):
        c, api = _client()
        c.connect()
        snap = c.refresh_realtime()
        assert snap.total_w == pytest.approx(2175.18)
        assert snap.voltage == [121.1, 120.9]
        assert snap.hz == pytest.approx(60.01)
        assert snap.devices == {"9acfa0cd": ("Espresso Maker", pytest.approx(1180.4))}
        assert c.realtime_path == "realtime_update"

    def test_trends_only_fetch_the_daily_scale(self):
        c, api = _client()
        c.connect()
        snap = c.refresh_trends()
        assert api.calls[-1] == "get_trend_data:DAY"
        assert snap.daily_kwh == pytest.approx(38.24)

    def test_discovered_devices(self):
        c, api = _client()
        c.connect()
        devices = {d.id: d for d in c.discovered_devices()}
        assert devices["9acfa0cd"].name == "Espresso Maker"
        assert devices["bd19692f"].revoked is True
        assert devices["a660a997"].revoked is False
        assert devices["400b819d"].merged_ids == ["1111aaaa", "2222bbbb"]

    @pytest.mark.parametrize(
        "exc", [timeout(), SenseAPIException("API Return Code: 500"), ConnectionError("dns")]
    )
    def test_data_errors_are_transient(self, exc):
        c, api = _client()
        c.connect()
        api.fail_with = exc
        with pytest.raises(SenseTransientError):
            c.refresh_realtime()
        with pytest.raises(SenseTransientError):
            c.discovered_devices()


class TestParseDiscovered:
    def test_handles_missing_tags_and_boolean_flags(self):
        out = parse_discovered(
            [{"id": 1, "name": "A"}, {"id": "b", "name": "B", "tags": {"UserDeleted": True}}]
        )
        assert out[0].id == "1" and out[0].revoked is False and out[0].merged_ids == []
        assert out[1].revoked is True
