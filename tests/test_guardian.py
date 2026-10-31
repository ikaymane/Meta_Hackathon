"""Guardian: one PressureEvent per episode, dismiss cooldown, ranked candidates."""
import os

import pytest

from freezer.contracts import AppSnapshot
from freezer.guardian import Guardian, windows_ram_pct


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class Ram:
    def __init__(self, v=50.0):
        self.v = v

    def __call__(self):
        return self.v


def app(app_id, idle_s, mb):
    return AppSnapshot(app_id, app_id, [], [], mb, idle_s, False, False, False, False)


SNAPS = [app("a.exe", 100.0, 100.0), app("b.exe", 10.0, 5000.0), app("d.exe", 5.0, 100.0)]


def drive(g, clock, ram, steps):
    """steps: list of (t, ram_pct) -> list of poll() results."""
    out = []
    for t, pct in steps:
        clock.now, ram.v = t, pct
        out.append(g.poll(SNAPS))
    return out


def test_fires_after_30s_above_85():
    clock, ram = Clock(), Ram()
    g = Guardian(ram_pct=ram, clock=clock)
    events = drive(g, clock, ram, [(0, 91), (29, 91), (30, 91)])
    assert events[0] is None and events[1] is None
    assert events[2] is not None
    assert events[2].ram_pct == 91 and events[2].sustained_s == 30.0
    assert events[2].candidates == ["b.exe", "a.exe", "d.exe"]  # idle x MB, desc


def test_dip_below_85_resets_the_clock():
    clock, ram = Clock(), Ram()
    g = Guardian(ram_pct=ram, clock=clock)
    events = drive(g, clock, ram, [(0, 91), (20, 91), (25, 80), (50, 91), (79, 91), (80, 91)])
    assert [e is not None for e in events] == [False, False, False, False, False, True]


def test_below_threshold_never_fires():
    clock, ram = Clock(), Ram()
    g = Guardian(ram_pct=ram, clock=clock)
    assert drive(g, clock, ram, [(0, 84), (100, 84), (200, 84)]) == [None, None, None]


def test_hysteresis_needs_dip_below_75_before_refire():
    clock, ram = Clock(), Ram()
    g = Guardian(ram_pct=ram, clock=clock)
    events = drive(g, clock, ram, [
        (0, 91), (30, 91),      # fires
        (60, 90), (90, 76),     # still high / mid: silent (disarmed)
        (100, 74),              # dip below 75: re-arms
        (110, 90), (139, 90), (140, 90),  # must sustain a fresh 30 s
    ])
    assert [e is not None for e in events] == [False, True, False, False, False, False, False, True]


def test_dismissed_card_starts_10min_cooldown():
    clock, ram = Clock(), Ram()
    g = Guardian(ram_pct=ram, clock=clock)
    drive(g, clock, ram, [(0, 91), (30, 91)])
    clock.now = 30
    g.dismissed()
    assert g.in_cooldown
    events = drive(g, clock, ram, [
        (40, 50),               # dip re-arms even mid-cooldown
        (50, 91), (80, 91),     # fresh 30 s sustain...
        (500, 91),              # ...still silent: inside the 10 min
        (631, 91), (661, 91),   # cooldown over at 630, sustain again -> fires
    ])
    assert [e is not None for e in events] == [False, False, False, False, False, True]
    assert not g.in_cooldown


def test_frozen_apps_are_not_candidates():
    clock, ram = Clock(), Ram()
    g = Guardian(ram_pct=ram, clock=clock)
    clock.now, ram.v = 0, 91
    assert g.poll(SNAPS, frozen_ids={"b.exe"}) is None
    clock.now = 30
    event = g.poll(SNAPS, frozen_ids={"b.exe"})
    assert event is not None and event.candidates == ["a.exe", "d.exe"]


@pytest.mark.skipif(os.name != "nt", reason="Windows-only RAM reading")
def test_windows_ram_reading_is_sane():
    assert 0.0 <= windows_ram_pct() <= 100.0


def test_ram_failure_is_not_pressure():
    def boom():
        raise OSError("sensor down")

    g = Guardian(ram_pct=boom, clock=Clock())
    assert g.poll(SNAPS) is None


def test_run_delivers_event_and_stops():
    from freezer.guardian import run

    clock = Clock()
    g = Guardian(ram_pct=Ram(91.0), clock=clock)
    seen = []
    ticks = iter([0, 30])

    def snapshots():
        clock.now = next(ticks)
        return SNAPS

    run(g, snapshots, seen.append, interval_s=0.01,
        stop=lambda: len(seen) >= 1 or clock.now >= 30)
    assert len(seen) == 1 and seen[0].candidates[0] == "b.exe"
