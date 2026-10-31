"""Watchdog: stale heartbeat thaws; panic hotkey lifecycle + dispatch."""
import json
import os
import threading
import time

import pytest

from freezer import watchdog
from freezer.mocks import MockActuator
from freezer.watchdog import PanicKey


class CountingActuator(MockActuator):
    def __init__(self):
        super().__init__()
        self.thaw_all_calls = 0

    def thaw_all(self):
        self.thaw_all_calls += 1
        return super().thaw_all()


def write_beat(pid, ts):
    p = watchdog.heartbeat_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"pid": pid, "ts": ts}), encoding="utf-8")


def test_fresh_heartbeat_thaws_nothing():
    watchdog.beat()
    act = CountingActuator()
    assert watchdog.check(act, clock=lambda: time.time()) is False
    assert act.thaw_all_calls == 0


def test_stale_heartbeat_thaws_everything():
    write_beat(1234, time.time() - 100)
    act = CountingActuator()
    assert watchdog.check(act) is True
    assert act.thaw_all_calls == 1


def test_missing_heartbeat_does_nothing():
    assert watchdog.check(CountingActuator()) is False


def test_run_watchdog_checks_once_then_stops():
    write_beat(1234, time.time() - 100)
    act = CountingActuator()
    watchdog.run_watchdog(act, interval_s=0.01, stop=lambda: True)
    assert act.thaw_all_calls == 1


@pytest.mark.skipif(os.name != "nt", reason="Windows-only hotkey")
def test_panic_key_lifecycle():
    key = PanicKey(lambda: None)
    key.start()
    assert key.running and key.thread_id
    with pytest.raises(RuntimeError):
        key.start()
    key.stop()
    assert not key.running
    key.start()  # restart works, hotkey was released
    key.stop()
    assert not key.running


@pytest.mark.skipif(os.name != "nt", reason="Windows-only hotkey")
def test_panic_key_dispatches_to_callback():
    import ctypes

    fired = threading.Event()
    key = PanicKey(fired.set)
    key.start()
    try:
        u32 = ctypes.WinDLL("user32")
        assert key.thread_id is not None
        assert u32.PostThreadMessageW(key.thread_id, PanicKey.WM_HOTKEY, 0, 0)
        assert fired.wait(5)
    finally:
        key.stop()
