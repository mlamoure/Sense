from __future__ import annotations

from sense.poller import LOGIN, MAX_BACKOFF, REALTIME, TRENDS, Poller


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _poller(**kw):
    clock = Clock()
    return Poller(clock=clock, **kw), clock


class TestCadence:
    def test_everything_due_at_start(self):
        p, _ = _poller()
        assert p.due(REALTIME) and p.due(TRENDS) and p.due(LOGIN)

    def test_success_schedules_next_interval(self):
        p, clock = _poller(realtime_interval=60, trend_interval=300)
        p.succeeded(REALTIME)
        p.succeeded(TRENDS)
        clock.t += 59
        assert not p.due(REALTIME) and not p.due(TRENDS)
        clock.t += 1
        assert p.due(REALTIME) and not p.due(TRENDS)
        clock.t += 240
        assert p.due(TRENDS)

    def test_realtime_interval_has_a_floor(self):
        p, clock = _poller(realtime_interval=5)
        p.succeeded(REALTIME)
        clock.t += 29
        assert not p.due(REALTIME)
        clock.t += 1
        assert p.due(REALTIME)


class TestBackoff:
    def test_failures_double_up_to_the_cap_and_success_resets(self):
        p, clock = _poller(realtime_interval=60)
        assert p.failed(REALTIME) == 60
        assert p.failed(REALTIME) == 120
        assert p.failed(REALTIME) == 240
        assert p.failed(REALTIME) == MAX_BACKOFF
        assert p.failures(REALTIME) == 4
        clock.t += MAX_BACKOFF
        assert p.due(REALTIME)
        p.succeeded(REALTIME)
        assert p.failures(REALTIME) == 0
        assert p.failed(REALTIME) == 60

    def test_explicit_wait_and_reset(self):
        p, clock = _poller()
        assert p.failed(LOGIN, wait=900) == 900
        assert not p.due(LOGIN)
        assert p.seconds_until(LOGIN) == 900
        p.reset(LOGIN)
        assert p.due(LOGIN)

    def test_login_default_backoff_uses_transient_base(self):
        p, _ = _poller()
        assert p.failed(LOGIN) == 60
        assert p.failed(LOGIN) == 120
