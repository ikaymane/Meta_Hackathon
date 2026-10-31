"""ff: the team CLI for freeze/thaw/list.

    python -m freezer.cli freeze notepad.exe [--session thesis]
    python -m freezer.cli freeze --pids 1234,5678 --app myapp.exe
    python -m freezer.cli thaw notepad.exe
    python -m freezer.cli thaw --all
    python -m freezer.cli list

Process discovery here is CLI plumbing (find PIDs by exe name), not a
replacement for B's sensor, which owns app grouping and idle/audio state.
`ff freeze` bypasses the consent card and (unless passed a policy) B's policy:
it is a team testing tool, and the agent loop remains the enforced path.
"""
from __future__ import annotations

import argparse
import datetime
import logging
import os
import sys
from typing import Callable, Sequence

from freezer.actuator import FreezeError, RealActuator, startup_recovery
from freezer.contracts import AppSnapshot, PolicyResult, ProcRef
from freezer.ledger import LedgerTampered

Policy = Callable[[list[str], list[AppSnapshot]], PolicyResult] | None

log = logging.getLogger(__name__)


def build_actuator() -> RealActuator:
    return RealActuator()


def _windows_os():
    from freezer.actuator import WindowsOs

    return WindowsOs()


def find_pids_by_exe(exe_name: str) -> list[ProcRef]:
    """All live PIDs whose exe basename matches, with create times."""
    import ctypes
    from ctypes import wintypes

    if os.name != "nt":
        raise OSError("process discovery needs Windows")

    class PROCESSENTRY32(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                    ("szExeFile", wintypes.WCHAR * 260)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32)]
    k32.Process32FirstW.restype = wintypes.BOOL
    k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32)]
    k32.Process32NextW.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.CloseHandle.restype = wintypes.BOOL

    want = exe_name.lower()
    pids: list[int] = []
    snap = k32.CreateToolhelp32Snapshot(0x00000002, 0)
    if snap == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        entry = PROCESSENTRY32()
        entry.dwSize = ctypes.sizeof(entry)
        ok = k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            if str(entry.szExeFile).lower() == want:
                pids.append(entry.th32ProcessID)
            ok = k32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snap)
    os_layer = _windows_os()
    refs = []
    for pid in pids:
        try:
            info = os_layer.resolve(pid)
        except OSError:
            info = None
        if info is not None:
            refs.append(ProcRef(pid, info.create_time))
    return refs


def _snapshot_for(app_id: str, refs: list[ProcRef]) -> AppSnapshot:
    total_mb = 0.0
    if os.name == "nt":
        os_layer = _windows_os()
        for r in refs:
            try:
                total_mb += os_layer.working_set_mb(r.pid)
            except OSError:
                pass
    return AppSnapshot(app_id, app_id, refs, [], total_mb, 0.0, False, False, False, False)


def cmd_freeze(args: argparse.Namespace, actuator: RealActuator, policy: Policy = None) -> int:
    if args.pids:
        if not args.app:
            print("ff freeze --pids needs --app <app_id>", file=sys.stderr)
            return 2
        refs = [ProcRef(pid, 0.0) for pid in args.pids]
        app_id = args.app.lower()
    else:
        if not args.exe:
            print("ff freeze needs an <exe> or --pids", file=sys.stderr)
            return 2
        app_id = args.exe.lower()
        try:
            refs = find_pids_by_exe(app_id)
        except OSError as e:
            print(f"ff: discovery failed: {e}", file=sys.stderr)
            return 1
        if not refs:
            print(f"ff: no live {app_id} processes", file=sys.stderr)
            return 1
    if args.pids and os.name == "nt":  # fill in real create times for --pids
        os_layer = _windows_os()
        fixed = []
        for r in refs:
            try:
                info = os_layer.resolve(r.pid)
            except OSError:
                info = None
            fixed.append(ProcRef(r.pid, info.create_time if info else 0.0))
        refs = fixed
    snap = _snapshot_for(app_id, refs)
    if policy is not None:
        verdict = policy([app_id], [snap])
        if app_id not in verdict.allowed:
            reason = next((b.rule for b in verdict.blocked if b.app_id == app_id), "blocked")
            print(f"ff: policy refused {app_id}: {reason}", file=sys.stderr)
            return 1
    try:
        entry = actuator.freeze(snap, args.session or "")
    except LedgerTampered as e:
        print(f"ff: refused: {e}", file=sys.stderr)
        return 1
    except FreezeError as e:
        print(f"ff: {e}", file=sys.stderr)
        for pid, reason in e.failures.items():
            print(f"ff:   pid {pid}: {reason}", file=sys.stderr)
        return 1
    print(f"froze {entry.app_id}: {len(entry.procs)} procs, {entry.freed_mb:.0f} MB freed")
    return 0


def cmd_thaw(args: argparse.Namespace, actuator: RealActuator) -> int:
    if args.all:
        ids = actuator.thaw_all()  # thaw_all is allowed even in tamper mode
        report = getattr(actuator, "last_report", None)
        if report is not None:
            print(f"restored {report.restored_gb:.1f} GB in {report.seconds:.1f} s "
                  f"({len(ids)} apps)")
        else:
            print(f"thawed {len(ids)} apps")
        for app_id in ids:
            print(f"  {app_id}")
        return 0
    if args.app_id not in {e.app_id for e in actuator.frozen()}:
        print(f"{args.app_id} is not frozen (nothing to do)")
        return 0
    try:
        actuator.thaw(args.app_id)
    except LedgerTampered as e:
        print(f"ff: {e}", file=sys.stderr)
        return 1
    print(f"thawed {args.app_id}")
    return 0


def cmd_recover(args: argparse.Namespace, actuator: RealActuator) -> int:
    """Resume apps a previous run left frozen (crash recovery)."""
    del args
    ids = startup_recovery(actuator)
    print(f"recovered {len(ids)} app(s) left frozen by a previous run")
    for app_id in ids:
        print(f"  {app_id}")
    return 0


def cmd_list(args: argparse.Namespace, actuator: RealActuator) -> int:
    del args
    entries = actuator.frozen()
    if not entries:
        print("nothing frozen")
        return 0
    print(f"{'app':28} {'procs':>5} {'freed':>9}  frozen at            session")
    for e in sorted(entries, key=lambda x: x.frozen_at):
        ts = datetime.datetime.fromtimestamp(e.frozen_at).strftime("%m-%d %H:%M:%S")
        print(f"{e.app_id:28} {len(e.procs):>5} {e.freed_mb:>7.0f}MB  {ts}  {e.session}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="ff", description="Focus Freeze team CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)
    fr = sub.add_parser("freeze", help="freeze an app by exe name or PIDs")
    fr.add_argument("exe", nargs="?", help="exe basename, e.g. notepad.exe")
    fr.add_argument("--pids", help="comma-separated PIDs (needs --app)")
    fr.add_argument("--app", help="app_id for --pids")
    fr.add_argument("--session", default="", help="focus session name")
    th = sub.add_parser("thaw", help="thaw one app, or --all")
    th.add_argument("app_id", nargs="?", help="app_id to thaw")
    th.add_argument("--all", action="store_true", help="thaw everything in the ledger")
    sub.add_parser("list", help="show frozen apps")
    sub.add_parser("recover", help="resume apps left frozen by a previous run")
    return ap


def main(argv: Sequence[str] | None = None, *, policy: Policy = None) -> int:
    logging.basicConfig(level=logging.WARNING)
    args = build_parser().parse_args(argv)
    if args.cmd == "freeze" and args.pids:
        try:
            args.pids = [int(p) for p in args.pids.split(",") if p.strip()]
        except ValueError:
            print("ff: --pids must be comma-separated integers", file=sys.stderr)
            return 2
        if not args.pids:
            print("ff: --pids needs at least one PID", file=sys.stderr)
            return 2
    if args.cmd == "thaw" and not args.all and not args.app_id:
        print("ff thaw needs an <app_id> or --all", file=sys.stderr)
        return 2
    actuator = build_actuator()
    try:
        if args.cmd == "freeze":
            return cmd_freeze(args, actuator, policy)
        if args.cmd == "thaw":
            return cmd_thaw(args, actuator)
        if args.cmd == "recover":
            return cmd_recover(args, actuator)
        return cmd_list(args, actuator)
    except TimeoutError as e:
        print(f"ff: ledger busy, try again: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
