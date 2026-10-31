"""Watchdog + panic hotkey: the last lines of defense against stranded apps.

- The agent calls beat() every few seconds. A separate watchdog process runs
  run_watchdog(): if the heartbeat is older than max_age_s the agent is
  assumed dead (or stuck) and everything in the ledger is thawed.
- PanicKey registers Ctrl+Alt+Shift+F system-wide; pressing it runs the
  callback (thaw_all) from anywhere. Thawing is always safe, so both paths
  work even when the ledger HMAC does not verify.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Callable, Protocol

from freezer.cost import home

log = logging.getLogger(__name__)


class ThawAll(Protocol):
    def thaw_all(self) -> list[str]: ...


def heartbeat_path() -> Path:
    return home() / "heartbeat.json"


def beat(clock: Callable[[], float] = time.time) -> None:
    """Record a heartbeat. The agent calls this every few seconds."""
    p = heartbeat_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"pid": os.getpid(), "ts": clock()}), encoding="utf-8")
    os.replace(tmp, p)


def read_beat() -> tuple[int, float] | None:
    try:
        doc = json.loads(heartbeat_path().read_text(encoding="utf-8"))
        if not isinstance(doc, dict):
            return None
        return int(doc["pid"]), float(doc["ts"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def check(actuator: ThawAll, *, max_age_s: float = 15.0,
          clock: Callable[[], float] = time.time) -> bool:
    """Thaw everything if the agent looks dead. Returns True if it thawed."""
    data = read_beat()
    if data is None:
        return False  # agent never started: nothing to rescue
    pid, ts = data
    if clock() - ts <= max_age_s:
        return False
    thawed = actuator.thaw_all()
    log.warning("watchdog: agent pid=%d silent for %.0fs, thawed %d app(s)",
                pid, clock() - ts, len(thawed))
    return True


def run_watchdog(actuator: ThawAll, *, interval_s: float = 5.0, max_age_s: float = 15.0,
                 stop: Callable[[], bool] | None = None,
                 clock: Callable[[], float] = time.time) -> None:
    """Watchdog-process main loop. stop() ends it (used by tests)."""
    while True:
        try:
            check(actuator, max_age_s=max_age_s, clock=clock)
        except Exception:  # noqa: BLE001 - the watchdog itself must never die
            log.exception("watchdog check failed")
        if stop is not None and stop():
            return
        time.sleep(interval_s)


class PanicKey:
    """System-wide Ctrl+Alt+Shift+F that runs a callback (thaw_all)."""

    MODIFIERS = 0x0001 | 0x0002 | 0x0004  # Alt | Ctrl | Shift
    VK_F = 0x46
    WM_HOTKEY = 0x0312
    WM_QUIT = 0x0012

    def __init__(self, callback: Callable[[], None]) -> None:
        self._callback = callback
        self._thread: threading.Thread | None = None
        self._thread_id: int | None = None
        self._ready = threading.Event()
        self._error: BaseException | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def thread_id(self) -> int | None:
        return self._thread_id

    def start(self, timeout_s: float = 10.0) -> None:
        if os.name != "nt":
            raise OSError("panic hotkey needs Windows")
        if self.running:
            raise RuntimeError("panic hotkey already started")
        self._ready.clear()
        self._error = None
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout_s):
            raise TimeoutError("panic hotkey thread did not start")
        if self._error is not None:
            raise self._error

    def stop(self, timeout_s: float = 10.0) -> None:
        t = self._thread
        if t is None or not t.is_alive():
            self._thread = None
            return
        import ctypes

        u32 = ctypes.WinDLL("user32")
        u32.PostThreadMessageW.argtypes = [ctypes.c_ulong, ctypes.c_uint,
                                           ctypes.c_size_t, ctypes.c_size_t]
        u32.PostThreadMessageW.restype = ctypes.c_int
        if self._thread_id is None:
            raise RuntimeError("panic hotkey thread never started")
        if not u32.PostThreadMessageW(self._thread_id, self.WM_QUIT, 0, 0):
            raise ctypes.WinError(ctypes.get_last_error())
        t.join(timeout_s)
        if t.is_alive():
            raise TimeoutError("panic hotkey thread did not stop")
        self._thread = None

    def _loop(self) -> None:
        import ctypes
        from ctypes import wintypes

        class _MSG(ctypes.Structure):
            _fields_ = [("hwnd", wintypes.HWND), ("message", wintypes.UINT),
                        ("wParam", wintypes.WPARAM), ("lParam", wintypes.LPARAM),
                        ("time", wintypes.DWORD), ("pt", wintypes.POINT)]

        try:
            u32 = ctypes.WinDLL("user32", use_last_error=True)
            u32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int,
                                           ctypes.c_uint, ctypes.c_uint]
            u32.RegisterHotKey.restype = wintypes.BOOL
            u32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
            u32.UnregisterHotKey.restype = wintypes.BOOL
            u32.GetMessageW.argtypes = [ctypes.POINTER(_MSG), wintypes.HWND,
                                        wintypes.UINT, wintypes.UINT]
            u32.GetMessageW.restype = ctypes.c_int
            u32.PeekMessageW.argtypes = [ctypes.POINTER(_MSG), wintypes.HWND,
                                         wintypes.UINT, wintypes.UINT, wintypes.UINT]
            u32.PeekMessageW.restype = wintypes.BOOL
            if not u32.RegisterHotKey(None, 1, self.MODIFIERS, self.VK_F):
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                msg = _MSG()
                u32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)  # create the queue
                self._thread_id = threading.get_native_id()
                self._ready.set()
                while True:
                    rc = u32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                    if rc == 0:
                        break  # WM_QUIT from stop()
                    if rc == -1:
                        log.error("panic hotkey message loop failed, hotkey dead")
                        break
                    if msg.message == self.WM_HOTKEY:
                        try:
                            self._callback()
                        except Exception:  # noqa: BLE001 - hotkey must keep working
                            log.exception("panic hotkey callback failed")
            finally:
                u32.UnregisterHotKey(None, 1)
        except BaseException as e:  # noqa: BLE001 - reported to start()
            self._error = e
            self._ready.set()
