"""Pressure guardian: watches RAM and proposes a freeze card when it stays high.

- Fires one PressureEvent when RAM stays above hi_pct (default 85%) for
  sustain_s (default 30 s) straight. D's UI polls poll() on a timer
  (every 5 s) and shows the card.
- Hysteresis: after firing, the guardian re-arms only when RAM drops below
  lo_pct (default 75%), so one pressure episode means one card, not a storm.
- Cooldown: Guardian.dismissed() — called by D's UI when the user dismisses
  the card — suppresses new events for cooldown_s (default 600 s).
- Candidates: non-frozen apps sorted by last_focus_s * working_set_mb.
  The planner (C) refines them, policy (B) filters them.
- The guardian never freezes anything itself. Freezing without a card
  happens only through the agent loop when the session opted into
  auto_freeze.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Collection
from dataclasses import dataclass
from typing import Callable

from freezer.contracts import AppSnapshot, PressureEvent

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class GuardianConfig:
    hi_pct: float = 85.0
    lo_pct: float = 75.0
    sustain_s: float = 30.0
    cooldown_s: float = 600.0


def windows_ram_pct() -> float:
    """Current RAM usage percent via GlobalMemoryStatusEx."""
    import ctypes
    from ctypes import wintypes

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(MEMORYSTATUSEX)]
    k32.GlobalMemoryStatusEx.restype = wintypes.BOOL
    st = MEMORYSTATUSEX()
    st.dwLength = ctypes.sizeof(st)
    if not k32.GlobalMemoryStatusEx(ctypes.byref(st)):
        raise ctypes.WinError(ctypes.get_last_error())
    return float(st.dwMemoryLoad)


class Guardian:
    def __init__(self, *, ram_pct: Callable[[], float] | None = None,
                 clock: Callable[[], float] | None = None,
                 config: GuardianConfig = GuardianConfig()) -> None:
        self._ram = ram_pct or windows_ram_pct
        self._clock = clock or time.monotonic
        self._config = config
        self._armed = True
        self._above_since: float | None = None
        self._cooldown_until = 0.0

    @property
    def in_cooldown(self) -> bool:
        return self._clock() < self._cooldown_until

    def dismissed(self) -> None:
        """D's UI calls this when the user dismisses the guardian card.

        Starts the cooldown: no new card for cooldown_s. Tracking resets,
        so after the cooldown pressure must sustain again from zero. Note the
        guardian also stays disarmed until RAM dips below lo_pct: refiring
        needs the dip plus a fresh sustain, not sustain alone.
        """
        self._cooldown_until = self._clock() + self._config.cooldown_s
        self._above_since = None

    def poll(self, snapshots: list[AppSnapshot],
             frozen_ids: Collection[str] = ()) -> PressureEvent | None:
        """Check RAM once. Returns a PressureEvent exactly when a card is due."""
        now = self._clock()
        try:
            ram = float(self._ram())
        except Exception:  # noqa: BLE001 - a failed reading is not pressure
            log.debug("RAM reading failed", exc_info=True)
            return None
        cfg = self._config
        if ram < cfg.lo_pct:
            self._armed = True  # re-arm even mid-cooldown: pressure is gone
            self._above_since = None
            return None
        if now < self._cooldown_until:
            return None
        if ram < cfg.hi_pct:
            self._above_since = None  # "straight" means continuous
            return None
        if not self._armed:
            return None
        if self._above_since is None:
            self._above_since = now
            return None
        sustained = now - self._above_since
        if sustained < cfg.sustain_s:
            return None
        self._armed = False
        self._above_since = None
        frozen = set(frozen_ids)
        ranked = sorted((s for s in snapshots if s.app_id not in frozen),
                        key=lambda s: s.last_focus_s * s.working_set_mb,
                        reverse=True)
        return PressureEvent(ram_pct=ram, sustained_s=sustained,
                             candidates=[s.app_id for s in ranked])


def run(guardian: Guardian, snapshots_fn: Callable[[], list[AppSnapshot]],
        on_event: Callable[[PressureEvent], None], *, interval_s: float = 5.0,
        frozen_fn: Callable[[], Collection[str]] | None = None,
        stop: Callable[[], bool] | None = None) -> None:
    """Poll loop for D's UI thread or a guardian process. stop() ends it (tests)."""
    while True:
        try:
            event = guardian.poll(snapshots_fn(), frozen_fn() if frozen_fn else ())
            if event is not None:
                on_event(event)
        except Exception:  # noqa: BLE001 - the guardian must never die
            log.exception("guardian poll failed")
        if stop is not None and stop():
            return
        time.sleep(interval_s)
