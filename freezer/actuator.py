"""Real Windows actuator: suspend/trim process trees, thaw them back.

Implements the contracts.Actuator protocol. Freeze order per app:
ledger FIRST -> minimize windows -> suspend every live verified process ->
trim (EmptyWorkingSet) every suspended process.

Safety rules:
- A process is acted on only if its PID is live, its exe is not a system
  binary (conhost.exe included), it is not in session 0, it is not the
  agent itself, and its create time still matches the snapshot (PID reuse
  guard, re-checked immediately before suspending).
- Skipped processes (system/conhost/self/dead/PID-reuse) never fail the
  freeze; OS errors do, via FreezeError, one per app, so the agent loop
  can report them per app without stopping the rest.
- A ledger entry is NEVER dropped because of a PID/create-time mismatch:
  on freeze the full entry is written before anything is touched, and on
  thaw a mismatched live process is left in the entry. Only confirmed-dead
  PIDs are pruned, and only on the thaw path.
- Freezing an already-frozen app raises instead of suspending twice: a
  double suspend needs a double resume, which would strand the app frozen.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Callable, Protocol

from freezer.contracts import AppSnapshot, LedgerEntry, ProcRef
from freezer.ledger import Ledger, LedgerTampered

log = logging.getLogger(__name__)

# Last-line guard: policy (B) decides what may freeze, but the actuator never
# suspends these even if asked. conhost.exe is a console window host: freezing
# it would hang every console app's window, including our own.
SYSTEM_EXES = frozenset({
    "conhost.exe",
    "csrss.exe", "winlogon.exe", "wininit.exe", "smss.exe", "services.exe",
    "lsass.exe", "svchost.exe", "dwm.exe", "explorer.exe", "ctfmon.exe",
    "fontdrvhost.exe", "msmpeng.exe", "securityhealthservice.exe",
    "narrator.exe", "magnify.exe", "spoolsv.exe", "searchindexer.exe",
    "searchhost.exe", "sihost.exe", "taskhostw.exe", "registry", "system",
    "secure system", "memory compression",
})


class FreezeError(Exception):
    """One app could not be (fully) frozen. The ledger entry is kept."""

    def __init__(self, app_id: str, message: str, *,
                 failures: dict[int, str] | None = None,
                 skipped: dict[int, str] | None = None,
                 entry: LedgerEntry | None = None) -> None:
        super().__init__(f"{app_id}: {message}")
        self.app_id = app_id
        self.failures = failures or {}  # pid -> OS error, needs attention
        self.skipped = skipped or {}    # pid -> reason, deliberately untouched
        self.entry = entry


class ThawError(Exception):
    """thaw_all tried but at least one live process could not be resumed."""


@dataclass
class ProcInfo:
    pid: int
    exe: str            # lowercased basename, "" if unknown
    create_time: float  # unix time, guards against PID reuse
    session_id: int = 1


class OsLayer(Protocol):
    def resolve(self, pid: int) -> ProcInfo | None:
        """Live process info, or None if the PID is dead or unreachable."""
    def suspend(self, pid: int) -> None: ...
    def resume(self, pid: int) -> None: ...
    def trim(self, pid: int) -> None: ...
    def working_set_mb(self, pid: int) -> float: ...
    def minimize(self, pids: set[int]) -> int: ...
    def restore(self, pids: set[int]) -> int: ...


# ---- real Windows backend ------------------------------------------------------

class WindowsOs:
    """OsLayer over ntdll/psapi/user32 via ctypes. No admin, no drivers."""

    PROCESS_SUSPEND_RESUME = 0x0800
    PROCESS_SET_QUOTA = 0x0100
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError("WindowsOs needs Windows")
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        self._w = wintypes
        ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        u32 = ctypes.WinDLL("user32", use_last_error=True)

        ntdll.NtSuspendProcess.argtypes = [wintypes.HANDLE]
        ntdll.NtSuspendProcess.restype = ctypes.c_long
        ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
        ntdll.NtResumeProcess.restype = ctypes.c_long
        psapi.EmptyWorkingSet.argtypes = [wintypes.HANDLE]
        psapi.EmptyWorkingSet.restype = wintypes.BOOL
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.CloseHandle.restype = wintypes.BOOL
        k32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD)]
        k32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        k32.ProcessIdToSessionId.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        k32.ProcessIdToSessionId.restype = wintypes.BOOL

        class _FILETIME(ctypes.Structure):
            _fields_ = [("dwLowDateTime", wintypes.DWORD),
                        ("dwHighDateTime", wintypes.DWORD)]
        self._FILETIME = _FILETIME
        k32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(_FILETIME)] * 4
        k32.GetProcessTimes.restype = wintypes.BOOL

        class _PMC(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
                (n, ctypes.c_size_t) for n in (
                    "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                    "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
                    "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]
        self._PMC = _PMC
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PMC), wintypes.DWORD]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL

        u32.EnumWindows.argtypes = [ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM),
                                    wintypes.LPARAM]
        u32.EnumWindows.restype = wintypes.BOOL
        u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        u32.GetWindowThreadProcessId.restype = wintypes.DWORD
        for name in ("IsWindowVisible", "IsIconic"):
            getattr(u32, name).argtypes = [wintypes.HWND]
            getattr(u32, name).restype = wintypes.BOOL
        u32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        u32.ShowWindow.restype = wintypes.BOOL

        self._ntdll, self._psapi, self._k32, self._u32 = ntdll, psapi, k32, u32

    def _open(self, pid: int, access: int):
        h = self._k32.OpenProcess(access, False, pid)
        if not h:
            raise self._ctypes.WinError(self._ctypes.get_last_error())
        return h

    def _close(self, h) -> None:
        self._k32.CloseHandle(h)

    def resolve(self, pid: int) -> ProcInfo | None:
        ctypes, wintypes = self._ctypes, self._w
        try:
            h = self._open(pid, self.PROCESS_QUERY_LIMITED_INFORMATION)
        except OSError:
            return None
        try:
            size = wintypes.DWORD(32768)
            buf = ctypes.create_unicode_buffer(size.value)
            if self._k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                exe = (buf.value.rsplit("\\", 1)[-1] or "").lower()
            else:
                exe = ""
            created, exited, kern, user = (self._FILETIME() for _ in range(4))
            if not self._k32.GetProcessTimes(
                    h, ctypes.byref(created), ctypes.byref(exited),
                    ctypes.byref(kern), ctypes.byref(user)):
                return None
            ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
            ctime = ticks / 10_000_000 - 11_644_473_600
            session = wintypes.DWORD(1)
            self._k32.ProcessIdToSessionId(pid, ctypes.byref(session))
            return ProcInfo(pid, exe, ctime, session.value)
        finally:
            self._close(h)

    def suspend(self, pid: int) -> None:
        h = self._open(pid, self.PROCESS_SUSPEND_RESUME)
        try:
            status = self._ntdll.NtSuspendProcess(h)
            if status != 0:
                raise OSError(f"NtSuspendProcess failed: status 0x{status & 0xFFFFFFFF:08X}")
        finally:
            self._close(h)

    def resume(self, pid: int) -> None:
        h = self._open(pid, self.PROCESS_SUSPEND_RESUME)
        try:
            status = self._ntdll.NtResumeProcess(h)
            if status != 0:
                raise OSError(f"NtResumeProcess failed: status 0x{status & 0xFFFFFFFF:08X}")
        finally:
            self._close(h)

    def trim(self, pid: int) -> None:
        # EmptyWorkingSet needs QUERY rights next to SET_QUOTA (else error 5).
        h = self._open(pid, self.PROCESS_SET_QUOTA | self.PROCESS_QUERY_LIMITED_INFORMATION)
        try:
            if not self._psapi.EmptyWorkingSet(h):
                raise self._ctypes.WinError(self._ctypes.get_last_error())
        finally:
            self._close(h)

    def working_set_mb(self, pid: int) -> float:
        try:
            h = self._open(pid, self.PROCESS_QUERY_LIMITED_INFORMATION)
        except OSError:
            return 0.0
        try:
            pmc = self._PMC()
            pmc.cb = self._ctypes.sizeof(pmc)
            if not self._psapi.GetProcessMemoryInfo(h, self._ctypes.byref(pmc), pmc.cb):
                return 0.0
            return pmc.WorkingSetSize / 1024 / 1024
        finally:
            self._close(h)

    def _for_each_window(self, pids: set[int], want_iconic: bool, cmd: int) -> int:
        ctypes, wintypes = self._ctypes, self._w
        done = 0

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def cb(hwnd, _lparam):
            nonlocal done
            pid = wintypes.DWORD()
            self._u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            try:
                if (pid.value in pids and self._u32.IsWindowVisible(hwnd)
                        and bool(self._u32.IsIconic(hwnd)) == want_iconic):
                    self._u32.ShowWindow(hwnd, cmd)
                    done += 1
            except OSError:
                pass  # window closed mid-enumeration
            return True

        self._cb = cb  # keep the callback alive during the call
        self._u32.EnumWindows(cb, 0)
        return done

    def minimize(self, pids: set[int]) -> int:
        return self._for_each_window(pids, want_iconic=False, cmd=6)  # SW_MINIMIZE

    def restore(self, pids: set[int]) -> int:
        return self._for_each_window(pids, want_iconic=True, cmd=9)  # SW_RESTORE


# ---- the actuator ----------------------------------------------------------------

@dataclass
class RestoreReport:
    app_ids: list[str]
    restored_gb: float
    seconds: float


class RealActuator:
    """contracts.Actuator over real processes and the signed ledger."""

    def __init__(self, os_layer: OsLayer | None = None, ledger: Ledger | None = None,
                 clock: Callable[[], float] | None = None) -> None:
        self._os = os_layer or WindowsOs()
        self._ledger = ledger or Ledger()
        self._clock = clock or time.time
        self.last_report: RestoreReport | None = None

    def _skip_reason(self, info: ProcInfo) -> str | None:
        if info.pid == os.getpid():
            return "self"
        if info.exe in SYSTEM_EXES:
            return "system_exe"
        if info.session_id == 0:
            return "system_session"
        return None

    def _safe_resolve(self, pid: int) -> ProcInfo | None:
        try:
            return self._os.resolve(pid)
        except OSError:
            return None

    def _ws_total(self, procs: list[ProcRef]) -> float:
        total = 0.0
        for p in procs:
            try:
                total += self._os.working_set_mb(p.pid)
            except OSError:
                pass
        return total

    # -- contracts.Actuator --------------------------------------------------------

    def freeze(self, app: AppSnapshot, session: str) -> LedgerEntry:
        live = self._ledger.load()
        if app.app_id in live:
            raise FreezeError(app.app_id, "already frozen", entry=live[app.app_id])
        entry = LedgerEntry(app.app_id, list(app.procs), self._clock(), 0.0, session)
        self._ledger.add(entry)  # FIRST: before any process is touched
        before_mb = self._ws_total(app.procs)
        try:
            self._os.minimize({p.pid for p in app.procs})
        except Exception:  # noqa: BLE001 - cosmetic only, never fails a freeze
            log.debug("minimize failed for %s", app.app_id, exc_info=True)
        failures: dict[int, str] = {}
        skipped: dict[int, str] = {}
        for pref in app.procs:
            info = self._safe_resolve(pref.pid)
            if info is None:
                skipped[pref.pid] = "exited_or_denied"
                continue
            reason = self._skip_reason(info)
            if reason is not None:
                skipped[pref.pid] = reason
                continue
            if info.create_time != pref.create_time:  # re-checked just before freezing
                log.warning("freeze: PID reuse on %s pid=%d, keeping ledger entry",
                            app.app_id, pref.pid)
                skipped[pref.pid] = "pid_reuse"
                continue
            try:
                self._os.suspend(pref.pid)
                self._os.trim(pref.pid)
            except OSError as e:
                failures[pref.pid] = str(e)
        after_mb = self._ws_total(app.procs)
        entry.freed_mb = max(before_mb - after_mb, 0.0)
        self._ledger.update(entry)
        if failures:
            raise FreezeError(app.app_id, f"{len(failures)} process(es) failed",
                              failures=failures, skipped=skipped, entry=entry)
        log.info("froze %s: %.0f MB, %d skipped (%s)",
                 app.app_id, entry.freed_mb, len(skipped),
                 ",".join(sorted(set(skipped.values()))) or "none")
        return entry

    def _resume_entry(self, entry: LedgerEntry) -> list[ProcRef]:
        """Resume every verifiable process; return the procs that must stay listed."""
        remaining: list[ProcRef] = []
        for pref in entry.procs:
            info = self._safe_resolve(pref.pid)
            if info is None:
                continue  # dead: nothing to resume, prune from the entry
            if info.create_time != pref.create_time:
                log.warning("thaw: PID reuse on %s pid=%d, keeping ledger entry",
                            entry.app_id, pref.pid)
                remaining.append(pref)  # NEVER drop an entry on a mismatch
                continue
            try:
                self._os.resume(pref.pid)
            except OSError as e:
                log.warning("thaw: resume failed on %s pid=%d (%s), keeping entry",
                            entry.app_id, pref.pid, e)
                remaining.append(pref)
        try:
            self._os.restore({p.pid for p in entry.procs})
        except Exception:  # noqa: BLE001 - cosmetic only
            log.debug("restore failed for %s", entry.app_id, exc_info=True)
        return remaining

    def thaw(self, app_id: str) -> None:
        entry = self._ledger.load().get(app_id)
        if entry is None:
            return
        if self._ledger.tampered:
            raise LedgerTampered("single thaw refused: ledger tampered, use thaw_all")
        remaining = self._resume_entry(entry)
        if remaining:
            entry.procs = remaining
            self._ledger.update(entry)
        else:
            self._ledger.remove(app_id)

    def thaw_all(self) -> list[str]:
        t0 = self._clock()
        ordered = sorted(self._ledger.load().values(), key=lambda e: e.frozen_at)
        leftovers: dict[str, LedgerEntry] = {}
        thawed: list[str] = []
        freed_mb = 0.0
        for entry in ordered:
            remaining = self._resume_entry(entry)
            if remaining:
                entry.procs = remaining
                leftovers[entry.app_id] = entry
            else:
                thawed.append(entry.app_id)
                freed_mb += entry.freed_mb
        self._ledger.save_thawed(leftovers)
        seconds = max(self._clock() - t0, 0.0)
        self.last_report = RestoreReport(thawed, freed_mb / 1024, seconds)
        log.info("restored %.1f GB in %.1f s (%d apps)", freed_mb / 1024, seconds, len(thawed))
        return thawed

    def frozen(self) -> list[LedgerEntry]:
        return list(self._ledger.load().values())


def startup_recovery(actuator: RealActuator | None = None) -> list[str]:
    """Resume everything left frozen by a previous run. Runs on agent startup."""
    thawed = (actuator or RealActuator()).thaw_all()
    if thawed:
        log.warning("startup_recovery: resumed %d app(s) left frozen: %s", len(thawed), thawed)
    return thawed
