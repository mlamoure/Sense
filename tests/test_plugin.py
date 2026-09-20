from __future__ import annotations

import json

import pytest

import plugin as plugin_module

from .fake_sense import FakeSenseable, timeout

PREFS = {"username": "u", "password": "p", "rateLimit": "60", "folderID": "42"}


@pytest.fixture(autouse=True)
def fake_library(monkeypatch):
    FakeSenseable.instances.clear()
    monkeypatch.setattr(plugin_module, "SENSEABLE_FACTORY", FakeSenseable)
    yield


def _plugin(fake_indigo, prefs=None):
    fake_indigo.devices.folders[42] = "Sense"
    return plugin_module.Plugin(
        "com.howartp.sense", "Sense Home Energy", "2026.9.0", prefs or dict(PREFS)
    )


def _api():
    return FakeSenseable.instances[-1]


def _by_name(fake_indigo, name):
    return next(d for d in fake_indigo.devices.values() if d.name == name)


class TestConfig:
    def test_unknown_folder_is_rejected(self, fake_indigo):
        p = _plugin(fake_indigo)
        ok, _values, errors = p.validatePrefsConfigUi({"folderID": "999"})
        assert ok is False
        assert "folderID" in errors

    def test_known_folder_is_accepted(self, fake_indigo):
        p = _plugin(fake_indigo)
        assert p.validatePrefsConfigUi({"folderID": "42"}) is True


class TestStartup:
    def test_startup_logs_in_and_persists_tokens(self, fake_indigo):
        p = _plugin(fake_indigo)
        p.startup()
        assert p.client is not None
        assert _api().calls == ["authenticate"]
        saved = json.loads(p.pluginPrefs["senseAuth"])
        assert saved["monitor_id"] == "monitor-1"

    def test_second_start_resumes_saved_session(self, fake_indigo):
        p = _plugin(fake_indigo)
        p.startup()
        prefs = dict(p.pluginPrefs)
        p2 = _plugin(fake_indigo, prefs)
        p2.startup()
        assert _api().calls == ["load_auth", "renew_auth"]

    def test_bad_password_does_not_crash(self, fake_indigo):
        p = _plugin(fake_indigo)
        FakeSenseable.password_ok = False
        try:
            p.startup()
        finally:
            FakeSenseable.password_ok = True
        assert p.client is None

    def test_mfa_code_is_used_once_then_cleared(self, fake_indigo):
        prefs = dict(PREFS, mfaCode="123456")
        p = _plugin(fake_indigo, prefs)
        FakeSenseable.mfa_required = True
        try:
            p.startup()
        finally:
            FakeSenseable.mfa_required = False
        assert p.client is not None
        assert p.pluginPrefs["mfaCode"] == ""

    def test_prefs_change_forgets_saved_session(self, fake_indigo):
        p = _plugin(fake_indigo)
        p.startup()
        p.closedPrefsConfigUi(dict(PREFS, password="new"), userCancelled=False)
        assert _api().calls == ["authenticate"]  # a fresh FakeSenseable, no load_auth


class TestPolling:
    def test_first_poll_creates_devices_and_sets_power(self, fake_indigo):
        p = _plugin(fake_indigo)
        p.startup()
        p.createCore()
        p.getDevices()
        names = sorted(d.name for d in fake_indigo.devices.values())
        # solar is skipped (solarEnabled off); the revoked duplicate "Downstairs AC" cannot be
        # created because its name is taken - the identity fix comes in the next PR.
        assert names == ["Active Total", "Downstairs AC", "Dryer", "Espresso Maker"]
        assert _by_name(fake_indigo, "Espresso Maker").states["power"] == "0"  # created this poll
        p.deviceStartComm(_by_name(fake_indigo, "Espresso Maker"))
        p.getDevices()
        assert _by_name(fake_indigo, "Espresso Maker").states["power"] == "1180"
        assert _by_name(fake_indigo, "Active Total").states["power"] == "2175"

    def test_transient_error_keeps_state_and_client(self, fake_indigo):
        p = _plugin(fake_indigo)
        p.startup()
        p.createCore()
        _api().fail_with = timeout()
        p.getDevices()
        assert p.client is not None
        assert _by_name(fake_indigo, "Active Total").states["power"] == "0"

    def test_poll_without_session_retries_login_later(self, fake_indigo):
        p = _plugin(fake_indigo)
        FakeSenseable.password_ok = False
        try:
            p.startup()
            p.getDevices()  # inside the retry window: no second login attempt
            assert [c for c in _api().calls if c == "authenticate"] == ["authenticate"]
            p._next_connect_attempt = 0
            p.getDevices()
            assert len(FakeSenseable.instances) == 2
        finally:
            FakeSenseable.password_ok = True

    def test_run_loop_survives_unexpected_errors(self, fake_indigo, monkeypatch):
        p = _plugin(fake_indigo)
        p.startup()
        p.dontStart = False
        monkeypatch.setattr(p, "getDevices", lambda: 1 / 0)
        p.runConcurrentThread()  # the stub's sleep() raises StopThread after one iteration
