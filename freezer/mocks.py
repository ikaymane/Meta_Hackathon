"""Stand-ins for A's and B's parts so the whole loop runs today.

Swap on integration day:
    MockSensor    -> B's freezer.sensor   (needs .snapshot() -> list[AppSnapshot])
    mock_policy   -> B's freezer.policy   (filter(freeze_ids, snapshots) -> PolicyResult)
    MockActuator  -> A's freezer.actuator (contracts.Actuator)
Nothing here touches real processes.
"""
from __future__ import annotations

import copy
import json
import os
import time
from pathlib import Path

from freezer.contracts import AppSnapshot, Blocked, LedgerEntry, PolicyResult

SCENARIOS = Path(__file__).parent.parent / "eval" / "scenarios.json"

# Minimal deny-list; B's real policy adds signer checks, IsProcessCritical, rate limits.
_SYSTEM = {
    "csrss.exe", "winlogon.exe", "wininit.exe", "smss.exe", "services.exe", "lsass.exe",
    "svchost.exe", "dwm.exe", "explorer.exe", "ctfmon.exe", "fontdrvhost.exe",
    "msmpeng.exe", "securityhealthservice.exe", "narrator.exe", "magnify.exe",
}


class MockSensor:
    """Serves the snapshot of one eval scenario, or a list you pass in."""

    def __init__(self, scenario: str | None = None, snapshots: list[AppSnapshot] | None = None):
        if snapshots is None:
            from freezer.planner import load_snapshots
            data = json.loads(SCENARIOS.read_text(encoding="utf-8"))
            sc = next((s for s in data if s["name"] == scenario), None)
            if sc is None:
                raise KeyError(f"no scenario {scenario!r}; pick one from {SCENARIOS.name}")
            snapshots = load_snapshots(sc["snapshots"])
        self._snapshots = snapshots

    def snapshot(self) -> list[AppSnapshot]:
        return copy.deepcopy(self._snapshots)


def mock_policy(freeze_ids: list[str], snapshots: list[AppSnapshot]) -> PolicyResult:
    by_id = {s.app_id: s for s in snapshots}
    me = os.getpid()
    result = PolicyResult(allowed=[])
    for app_id in freeze_ids:
        s = by_id.get(app_id)
        rule = (
            "unknown_app" if s is None else
            "system_process" if app_id.lower() in _SYSTEM else
            "self" if any(p.pid == me for p in s.procs) else
            "elevated" if s.is_elevated else
            "foreground" if s.is_foreground else
            "plays_audio" if s.plays_audio else
            None
        )
        if rule:
            result.blocked.append(Blocked(app_id, rule))
        else:
            result.allowed.append(app_id)
    return result


class MockActuator:
    """In-memory ledger. Records calls instead of suspending anything."""

    def __init__(self) -> None:
        self._ledger: dict[str, LedgerEntry] = {}
        self.log: list[tuple[str, str]] = []

    def freeze(self, app: AppSnapshot, session: str) -> LedgerEntry:
        entry = LedgerEntry(app.app_id, list(app.procs), time.time(), app.working_set_mb, session)
        self._ledger[app.app_id] = entry
        self.log.append(("freeze", app.app_id))
        return entry

    def thaw(self, app_id: str) -> None:
        if self._ledger.pop(app_id, None):
            self.log.append(("thaw", app_id))

    def thaw_all(self) -> list[str]:
        ids = list(self._ledger)
        for app_id in ids:
            self.thaw(app_id)
        return ids

    def frozen(self) -> list[LedgerEntry]:
        return list(self._ledger.values())
