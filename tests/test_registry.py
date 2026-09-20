from __future__ import annotations

from sense import registry
from sense.client import DiscoveredDevice
from sense.registry import Create, Delete, IndigoView, Rename, Retire, Revive, UpdatePower


def D(id, name, revoked=False, merged=()):
    return DiscoveredDevice(id, name, revoked, list(merged))


def V(dev_id, sense_id, name, enabled=True, power=0.0, is_on=None):
    if is_on is None:
        is_on = power > 0
    return IndigoView(dev_id, sense_id, name, enabled, power, is_on)


class TestPlan:
    def test_new_appliance_is_created(self):
        assert registry.plan([D("a", "Kettle")], {}, {}) == [Create("a", "Kettle")]

    def test_power_only_when_changed(self):
        existing = {"a": V(1, "a", "Kettle", power=0)}
        assert registry.plan([D("a", "Kettle")], {"a": ("Kettle", 900.0)}, existing) == [
            UpdatePower(1, 900.0)
        ]
        existing = {"a": V(1, "a", "Kettle", power=900)}
        assert registry.plan([D("a", "Kettle")], {"a": ("Kettle", 900.0)}, existing) == []
        assert registry.plan([D("a", "Kettle")], {"a": ("Kettle", 900.4)}, existing) == []
        assert registry.plan([D("a", "Kettle")], {}, existing) == [UpdatePower(1, 0.0)]

    def test_inconsistent_or_missing_is_on_forces_a_write(self):
        stale = {"a": IndigoView(1, "a", "Fridge", True, 143.0, None)}
        assert registry.plan([D("a", "Fridge")], {"a": ("Fridge", 143.0)}, stale) == [
            UpdatePower(1, 143.0)
        ]
        wrong = {"a": IndigoView(1, "a", "Fridge", True, 143.0, False)}
        assert registry.plan([D("a", "Fridge")], {"a": ("Fridge", 143.0)}, wrong) == [
            UpdatePower(1, 143.0)
        ]

    def test_rename_follows_sense(self):
        existing = {"a": V(1, "a", "Kettle")}
        assert registry.plan([D("a", "Tea Kettle")], {}, existing) == [Rename(1, "Tea Kettle")]

    def test_relearned_appliance_retires_old_id_before_creating_new(self):
        discovered = [D("new", "Downstairs AC"), D("old", "Downstairs AC", revoked=True)]
        existing = {"old": V(1, "old", "Downstairs AC", enabled=False)}
        assert registry.plan(discovered, {}, existing) == [
            Retire(1, "Downstairs AC (revoked old)"),
            Create("new", "Downstairs AC"),
        ]

    def test_retire_is_idempotent(self):
        existing = {"old": V(1, "old", "Downstairs AC (revoked old)", enabled=False)}
        assert registry.plan([D("old", "Downstairs AC", revoked=True)], {}, existing) == []

    def test_revive_after_unrevoke(self):
        existing = {"a": V(1, "a", "Kettle (revoked a)", enabled=False)}
        assert registry.plan([D("a", "Kettle")], {}, existing) == [Revive(1, "Kettle")]

    def test_user_disabled_device_is_left_alone(self):
        existing = {"a": V(1, "a", "Kettle", enabled=False)}
        assert registry.plan([D("a", "Kettle")], {"a": ("Kettle", 5.0)}, existing) == []

    def test_vanished_disabled_device_is_marked_stale_but_enabled_one_untouched(self):
        existing = {
            "gone": V(1, "gone", "Old Thing", enabled=False),
            "live": V(2, "live", "Fridge", enabled=True, power=80),
        }
        assert registry.plan([], {}, existing) == [Retire(1, "Old Thing (stale gone)")]

    def test_merged_device_is_deleted(self):
        existing = {"m": V(1, "m", "Dryer 2")}
        assert registry.plan([D("a", "Dryer", merged=["m"])], {}, existing) == [
            Delete(1),
            Create("a", "Dryer"),
        ]

    def test_solar_and_core_are_skipped(self):
        existing = {"core": V(9, "core", "Active Total", power=1)}
        assert registry.plan([D("solar", "Solar"), D("core", "x")], {}, existing) == []
        assert registry.plan([D("solar", "Solar")], {}, {}, include_solar=True) == [
            Create("solar", "Solar")
        ]
