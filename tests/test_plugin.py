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
    def _start(self, fake_indigo):
        p = _plugin(fake_indigo)
        p.startup()
        p.createCore()
        return p

    def test_first_poll_creates_devices_and_sets_power(self, fake_indigo):
        p = self._start(fake_indigo)
        p.getDevices()
        names = sorted(d.name for d in fake_indigo.devices.values())
        # solar is skipped (solarEnabled off); the revoked id bd19692f has no device yet so
        # nothing is retired; "Downstairs AC" is created under its live id a660a997.
        assert names == ["Active Total", "Downstairs AC", "Dryer", "Espresso Maker"]
        espresso = _by_name(fake_indigo, "Espresso Maker")
        assert espresso.address == "9acfa0cd"
        assert espresso.pluginProps["senseId"] == "9acfa0cd"
        assert espresso.states["id"] == "9acfa0cd"
        assert espresso.states["power"] == 0  # created this poll; power lands next poll
        p.getDevices()
        assert espresso.states["power"] == 1180
        assert espresso.states["isOn"] is True
        assert _by_name(fake_indigo, "Downstairs AC").states["power"] == 0
        core = _by_name(fake_indigo, "Active Total")
        assert core.states["power"] == 2175
        assert core.states["dailyKwh"] == 38.24
        assert core.states["voltage"] == "121.1/120.9"
        assert core.states["hz"] == 60.01

    def test_unchanged_power_is_not_rewritten(self, fake_indigo):
        p = self._start(fake_indigo)
        p.getDevices()
        p.getDevices()
        espresso = _by_name(fake_indigo, "Espresso Maker")
        core = _by_name(fake_indigo, "Active Total")
        n_esp, n_core = len(espresso.state_updates), len(core.state_updates)
        p.getDevices()
        assert len(espresso.state_updates) == n_esp
        assert len(core.state_updates) == n_core

    def test_relearned_appliance_gets_a_live_device(self, fake_indigo):
        """The 7-stuck-devices scenario: a disabled old-id device holds the name."""
        p = self._start(fake_indigo)
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
        p = self._start(fake_indigo)
        p.getDevices()
        espresso = _by_name(fake_indigo, "Espresso Maker")
        api = _api()
        api.raw_devices[0]["tags"] = {"Revoked": "true"}
        p.getDevices()
        assert espresso.enabled is False
        assert espresso.name == "Espresso Maker (revoked 9acfa0cd)"
        api.raw_devices[0]["tags"] = {}
        p.getDevices()
        assert espresso.enabled is True
        assert espresso.name == "Espresso Maker"

    def test_migrated_device_gets_is_on_without_a_power_change(self, fake_indigo):
        p = self._start(fake_indigo)
        legacy = fake_indigo.Device(name="Espresso Maker")
        legacy.states = {"id": "9acfa0cd", "power": "1180"}  # written by the old plugin
        fake_indigo.devices[legacy.id] = legacy
        p.deviceStartComm(legacy)
        p.getDevices()
        assert legacy.states["isOn"] is True
        assert legacy.states["power"] == 1180

    def test_duplicate_sense_id_uses_first_enabled_and_warns_once(self, fake_indigo, caplog):
        p = self._start(fake_indigo)
        p.getDevices()  # the regular devices exist
        first = fake_indigo.Device(name="Disposal", pluginProps={"senseId": "1a547f5c"})
        second = fake_indigo.Device(name="Disposal (1a547f5c)", pluginProps={"senseId": "1a547f5c"})
        fake_indigo.devices[first.id] = first
        fake_indigo.devices[second.id] = second
        _api().raw_devices.append({"id": "1a547f5c", "name": "Disposal", "tags": {}})
        before = len(fake_indigo.devices)
        p.getDevices()
        p.getDevices()
        assert first.address == second.address == "1a547f5c"
        assert len(fake_indigo.devices) == before  # nothing created, nothing deleted
        warnings = [r for r in caplog.records if "More than one Indigo device" in r.message]
        assert len(warnings) == 1 and "Disposal (1a547f5c)" in warnings[0].message

    def test_half_created_device_is_adopted_by_its_senseid_prop(self, fake_indigo):
        """A create interrupted after indigo.device.create left only pluginProps["senseId"]."""
        p = self._start(fake_indigo)
        orphan = fake_indigo.Device(name="Disposal (1a547f5c)", pluginProps={"senseId": "1a547f5c"})
        fake_indigo.devices[orphan.id] = orphan
        _api().raw_devices.append({"id": "1a547f5c", "name": "Disposal", "tags": {}})
        p.getDevices()
        assert orphan.address == "1a547f5c"
        assert orphan.states["id"] == "1a547f5c"
        assert [
            d for d in fake_indigo.devices.values() if d.pluginProps.get("senseId") == "1a547f5c"
        ] == [orphan]
        assert orphan.name == "Disposal"  # renamed to Sense's name now that it is recognised

    def test_name_clash_with_foreign_device_falls_back_to_suffixed_name(self, fake_indigo):
        p = self._start(fake_indigo)
        other = fake_indigo.Device(name="Dryer", deviceTypeId="zwaveRelay")
        fake_indigo.devices[other.id] = other
        p.getDevices()
        assert _by_name(fake_indigo, "Dryer (400b819d)").address == "400b819d"

    def test_sense_rename_is_mirrored(self, fake_indigo):
        p = self._start(fake_indigo)
        p.getDevices()
        _api().raw_devices[0]["name"] = "Coffee Machine"
        p.getDevices()
        assert _by_name(fake_indigo, "Coffee Machine").address == "9acfa0cd"

    def test_merged_device_is_deleted(self, fake_indigo):
        p = self._start(fake_indigo)
        p.getDevices()
        merged = fake_indigo.Device(name="Old Dryer", pluginProps={"address": "1111aaaa"})
        fake_indigo.devices[merged.id] = merged
        p.getDevices()
        assert merged.id not in fake_indigo.devices

    def test_transient_error_keeps_state_and_client(self, fake_indigo):
        p = self._start(fake_indigo)
        _api().fail_with = timeout()
        p.getDevices()
        assert p.client is not None
        assert _by_name(fake_indigo, "Active Total").states["power"] == 0

    def test_missing_folder_falls_back_to_top_level(self, fake_indigo):
        p = self._start(fake_indigo)
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
