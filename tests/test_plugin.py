from __future__ import annotations

import json

import pytest

import plugin as plugin_module
from sense.poller import LOGIN, TREND_INTERVAL, Poller

from .fake_sense import FakeSenseable, timeout

PREFS = {"username": "u", "password": "p", "rateLimit": "60", "apiTimeout": "30", "folderID": "42"}


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


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
        ok, _values, errors = p.validatePrefsConfigUi(dict(PREFS, folderID="999"))
        assert ok is False
        assert list(errors) == ["folderID"]

    def test_valid_prefs_are_accepted(self, fake_indigo):
        p = _plugin(fake_indigo)
        assert p.validatePrefsConfigUi(dict(PREFS)) is True

    @pytest.mark.parametrize(
        "field,value",
        [("rateLimit", "5"), ("rateLimit", "x"), ("apiTimeout", "2"), ("apiTimeout", "500")],
    )
    def test_out_of_range_numbers_are_rejected(self, fake_indigo, field, value):
        p = _plugin(fake_indigo)
        ok, _values, errors = p.validatePrefsConfigUi(dict(PREFS, **{field: value}))
        assert ok is False and list(errors) == [field]

    def test_top_level_folder_is_accepted(self, fake_indigo):
        p = _plugin(fake_indigo)
        assert p.validatePrefsConfigUi(dict(PREFS, folderID="0")) is True

    def test_folder_list_offers_top_level_and_every_folder(self, fake_indigo):
        p = _plugin(fake_indigo)
        fake_indigo.devices.folders[7] = "Energy"
        assert p.deviceFolderList() == [
            ("0", "(top level - no folder)"),
            ("42", "Sense"),
            ("7", "Energy"),
        ]

    def test_test_login_button_reports_outcome(self, fake_indigo):
        p = _plugin(fake_indigo)
        out = p.testLoginPressed(dict(PREFS))
        assert out["testLoginResult"] == "OK - monitor monitor-1, live data via realtime_update"
        FakeSenseable.password_ok = False
        try:
            out = p.testLoginPressed(dict(PREFS))
        finally:
            FakeSenseable.password_ok = True
        assert out["testLoginResult"].startswith("Login failed:")
        FakeSenseable.mfa_required = True
        try:
            out = p.testLoginPressed(dict(PREFS))
        finally:
            FakeSenseable.mfa_required = False
        assert "authenticator" in out["testLoginResult"]

    def test_prefs_feed_the_client_and_poller(self, fake_indigo):
        p = _plugin(fake_indigo, dict(PREFS, rateLimit="45", apiTimeout="12"))
        p.startup()
        assert _api().api_timeout == 12
        assert p.poller._interval["realtime"] == 45


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
    def _start(self, fake_indigo):
        p = _plugin(fake_indigo)
        clock = Clock()
        p.poller = Poller(realtime_interval=60, clock=clock)
        p.startup()
        p.createCore()
        p.getDevices()  # first pass: trends + device list + realtime all due
        return p, clock

    @staticmethod
    def _poll(p, clock, seconds=60):
        clock.t += seconds
        p.getDevices()

    def test_first_poll_creates_devices_and_sets_power(self, fake_indigo):
        p, clock = self._start(fake_indigo)
        names = sorted(d.name for d in fake_indigo.devices.values())
        # solar is skipped (solarEnabled off); the revoked id bd19692f has no device yet so
        # nothing is retired; "Downstairs AC" is created under its live id a660a997.
        assert names == ["Active Total", "Downstairs AC", "Dryer", "Espresso Maker"]
        espresso = _by_name(fake_indigo, "Espresso Maker")
        assert espresso.address == "9acfa0cd"
        assert espresso.pluginProps["senseId"] == "9acfa0cd"
        assert espresso.states["id"] == "9acfa0cd"
        assert espresso.states["power"] == 0  # created this poll; power lands next poll
        self._poll(p, clock)
        assert espresso.states["power"] == 1180
        assert espresso.states["isOn"] is True
        assert _by_name(fake_indigo, "Downstairs AC").states["power"] == 0
        core = _by_name(fake_indigo, "Active Total")
        assert core.states["power"] == 2175
        assert core.states["dailyKwh"] == 38.24
        assert core.states["voltage"] == "121.1/120.9"
        assert core.states["hz"] == 60.01

    def test_cadence_realtime_every_interval_trends_every_five_minutes(self, fake_indigo):
        p, clock = self._start(fake_indigo)
        api = _api()
        api.calls.clear()
        self._poll(p, clock, 30)
        assert api.calls == []
        self._poll(p, clock, 30)
        assert api.calls == ["update_realtime"]
        api.calls.clear()
        self._poll(p, clock, TREND_INTERVAL)
        assert api.calls == ["get_trend_data:DAY", "get_discovered_device_data", "update_realtime"]

    def test_unchanged_power_is_not_rewritten(self, fake_indigo):
        p, clock = self._start(fake_indigo)
        self._poll(p, clock)
        espresso = _by_name(fake_indigo, "Espresso Maker")
        core = _by_name(fake_indigo, "Active Total")
        n_esp, n_core = len(espresso.state_updates), len(core.state_updates)
        self._poll(p, clock)
        assert len(espresso.state_updates) == n_esp
        assert len(core.state_updates) == n_core

    def test_relearned_appliance_gets_a_live_device(self, fake_indigo):
        """The 7-stuck-devices scenario: a disabled old-id device holds the name."""
        p = _plugin(fake_indigo)
        p.startup()
        p.createCore()
        old = fake_indigo.Device(name="Downstairs AC", folder=42)
        old.states["id"] = "bd19692f"  # legacy: id only in the state, no address
        old.enabled = False
        fake_indigo.devices[old.id] = old
        p.deviceStartComm(old)
        assert old.address == "bd19692f"
        p.getDevices()
        assert old.name == "Downstairs AC (revoked bd19692f)"
        assert old.enabled is False
        new = _by_name(fake_indigo, "Downstairs AC")
        assert new.address == "a660a997" and new.enabled

    def test_revoked_live_device_is_retired_and_revived(self, fake_indigo):
        p, clock = self._start(fake_indigo)
        espresso = _by_name(fake_indigo, "Espresso Maker")
        api = _api()
        api.raw_devices[0]["tags"] = {"Revoked": "true"}
        self._poll(p, clock, TREND_INTERVAL)
        assert espresso.enabled is False
        assert espresso.name == "Espresso Maker (revoked 9acfa0cd)"
        api.raw_devices[0]["tags"] = {}
        self._poll(p, clock, TREND_INTERVAL)
        assert espresso.enabled is True
        assert espresso.name == "Espresso Maker"

    def test_migrated_device_gets_is_on_without_a_power_change(self, fake_indigo):
        p = _plugin(fake_indigo)
        p.startup()
        p.createCore()
        legacy = fake_indigo.Device(name="Espresso Maker")
        legacy.states = {"id": "9acfa0cd", "power": "1180"}  # written by the old plugin
        fake_indigo.devices[legacy.id] = legacy
        p.deviceStartComm(legacy)
        p.getDevices()
        assert legacy.states["isOn"] is True
        assert legacy.states["power"] == 1180

    def test_duplicate_sense_id_uses_first_enabled_and_warns_once(self, fake_indigo, caplog):
        p, clock = self._start(fake_indigo)
        first = fake_indigo.Device(name="Disposal", pluginProps={"senseId": "1a547f5c"})
        second = fake_indigo.Device(name="Disposal (1a547f5c)", pluginProps={"senseId": "1a547f5c"})
        fake_indigo.devices[first.id] = first
        fake_indigo.devices[second.id] = second
        _api().raw_devices.append({"id": "1a547f5c", "name": "Disposal", "tags": {}})
        before = len(fake_indigo.devices)
        self._poll(p, clock, TREND_INTERVAL)
        self._poll(p, clock, TREND_INTERVAL)
        assert first.address == second.address == "1a547f5c"
        assert len(fake_indigo.devices) == before  # nothing created, nothing deleted
        warnings = [r for r in caplog.records if "More than one Indigo device" in r.message]
        assert len(warnings) == 1 and "Disposal (1a547f5c)" in warnings[0].message

    def test_half_created_device_is_adopted_by_its_senseid_prop(self, fake_indigo):
        """A create interrupted after indigo.device.create left only pluginProps["senseId"]."""
        p, clock = self._start(fake_indigo)
        orphan = fake_indigo.Device(name="Disposal (1a547f5c)", pluginProps={"senseId": "1a547f5c"})
        fake_indigo.devices[orphan.id] = orphan
        _api().raw_devices.append({"id": "1a547f5c", "name": "Disposal", "tags": {}})
        self._poll(p, clock, TREND_INTERVAL)
        assert orphan.address == "1a547f5c"
        assert orphan.states["id"] == "1a547f5c"
        assert [
            d for d in fake_indigo.devices.values() if d.pluginProps.get("senseId") == "1a547f5c"
        ] == [orphan]
        assert orphan.name == "Disposal"  # renamed to Sense's name now that it is recognised

    def test_name_clash_with_foreign_device_falls_back_to_suffixed_name(self, fake_indigo):
        p = _plugin(fake_indigo)
        p.startup()
        other = fake_indigo.Device(name="Dryer", deviceTypeId="zwaveRelay")
        fake_indigo.devices[other.id] = other
        p.getDevices()
        assert _by_name(fake_indigo, "Dryer (400b819d)").address == "400b819d"

    def test_sense_rename_is_mirrored(self, fake_indigo):
        p, clock = self._start(fake_indigo)
        _api().raw_devices[0]["name"] = "Coffee Machine"
        self._poll(p, clock, TREND_INTERVAL)
        assert _by_name(fake_indigo, "Coffee Machine").address == "9acfa0cd"

    def test_merged_device_is_deleted(self, fake_indigo):
        p, clock = self._start(fake_indigo)
        merged = fake_indigo.Device(name="Old Dryer", pluginProps={"address": "1111aaaa"})
        fake_indigo.devices[merged.id] = merged
        self._poll(p, clock)
        assert merged.id not in fake_indigo.devices

    def test_transient_error_backs_off_and_keeps_state_and_client(self, fake_indigo):
        p, clock = self._start(fake_indigo)
        api = _api()
        api.fail_with = timeout()
        self._poll(p, clock)
        assert p.client is not None
        assert _by_name(fake_indigo, "Active Total").states["power"] == 2175
        self._poll(p, clock, 60)  # second failure: the wait doubles to 120 s
        api.calls.clear()
        self._poll(p, clock, 60)  # inside the doubled wait: no request
        assert api.calls == []
        self._poll(p, clock, 60)
        assert api.calls == ["update_realtime"]

    def test_lost_session_is_reestablished(self, fake_indigo):
        p, clock = self._start(fake_indigo)
        api = _api()
        api.fail_with = __import__("sense_energy").SenseAuthenticationException("401")
        self._poll(p, clock)
        assert p.client is None
        self._poll(p, clock, 60)
        assert p.client is not None and len(FakeSenseable.instances) == 2

    def test_missing_folder_falls_back_to_top_level(self, fake_indigo):
        p = _plugin(fake_indigo)
        p.startup()
        fake_indigo.devices.folders.clear()
        p.getDevices()
        assert _by_name(fake_indigo, "Espresso Maker").folderId == 0

    def test_poll_without_session_retries_login_later(self, fake_indigo):
        p = _plugin(fake_indigo)
        FakeSenseable.password_ok = False
        try:
            p.startup()
            p.getDevices()  # inside the retry window: no second login attempt
            assert [c for c in _api().calls if c == "authenticate"] == ["authenticate"]
            p.poller.reset(LOGIN)
            p.getDevices()
            assert len(FakeSenseable.instances) == 2
        finally:
            FakeSenseable.password_ok = True

    def test_run_loop_survives_unexpected_errors(self, fake_indigo, monkeypatch):
        p = _plugin(fake_indigo)
        p.startup()
        monkeypatch.setattr(p, "getDevices", lambda: 1 / 0)
        sleeps = []

        def fake_sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) > 2:
                raise p.StopThread()

        monkeypatch.setattr(p, "sleep", fake_sleep)
        p.runConcurrentThread()
        assert sleeps == [5, 1, 1]
