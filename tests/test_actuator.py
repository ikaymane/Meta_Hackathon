"""Actuator: ledger-first freeze, skips, mismatch keeps, smart thaw."""
import json
import os

import pytest

from freezer import ledger as ledger_mod
from freezer.actuator import FreezeError, ProcInfo, RealActuator, WindowsOs
from freezer.contracts import AppSnapshot, ProcRef
from freezer.ledger import Ledger, LedgerTampered


class FakeProc:
    def __init__(self, pid, exe="app.exe", ctime=100.0, ws_mb=50.0, session=1):
        self.pid, self.exe, self.ctime = pid, exe, ctime
        self.ws_mb, self.session = ws_mb, session
        self.alive, self.suspended = True, False


class FakeOs:
    """In-memory process table. Touches nothing real. Lists record successes."""

    def __init__(self, procs):
        self.procs = {p.pid: p for p in procs}
        self.suspended, self.resumed, self.trimmed = [], [], []
        self.minimized, self.restored = [], []
        self.denied = set()  # live but unreachable, like protected processes
        self.fail_suspend, self.fail_resume, self.fail_trim = set(), set(), set()

    def resolve(self, pid):
        p = self.procs.get(pid)
        if p is None or not p.alive or pid in self.denied:
            return None
        return ProcInfo(pid, p.exe, p.ctime, p.session)

    def confirmed_dead(self, pid):
        if pid in self.denied:
            return False
        p = self.procs.get(pid)
        return p is None or not p.alive

    def suspend_verified(self, pid, expected_ctime):
        p = self.procs.get(pid)
        if p is None or not p.alive:
            return False
        if pid in self.denied:
            raise OSError("access denied")
        if abs(p.ctime - expected_ctime) > 2.0:
            return False
        if pid in self.fail_suspend:
            raise OSError("access denied")
        p.suspended = True
        self.suspended.append(pid)
        return True

    def resume(self, pid):
        if pid in self.fail_resume:
            raise OSError("access denied")
        self.resumed.append(pid)
        self.procs[pid].suspended = False

    def trim(self, pid):
        if pid in self.fail_trim:
            raise OSError("access denied")
        self.trimmed.append(pid)
        self.procs[pid].ws_mb = 0.0

    def working_set_mb(self, pid):
        p = self.procs.get(pid)
        return p.ws_mb if p and p.alive else 0.0

    def minimize(self, pids):
        self.minimized.append(set(pids))
        return 0

    def restore(self, pids):
        self.restored.append(set(pids))
        return 0


class FakeClock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


def snap(app_id, *procs, **kw):
    return AppSnapshot(app_id, app_id, list(procs), [], 100.0, 60.0, False, False,
                       kw.get("elevated", False), False)


def test_freeze_writes_ledger_first_even_when_suspend_fails():
    fake = FakeOs([FakeProc(11)])
    fake.fail_suspend.add(11)
    act = RealActuator(os_layer=fake, ledger=Ledger())
    with pytest.raises(FreezeError):
        act.freeze(snap("a.exe", ProcRef(11, 100.0)), "s")
    assert set(Ledger().load()) == {"a.exe"}  # entry survived the failure


def test_freeze_suspends_trims_and_measures_freed():
    fake = FakeOs([FakeProc(11, ws_mb=100.0), FakeProc(12, ws_mb=50.0)])
    act = RealActuator(os_layer=fake, ledger=Ledger())
    e = act.freeze(snap("a.exe", ProcRef(11, 100.0), ProcRef(12, 100.0)), "thesis")
    assert fake.suspended == [11, 12] and fake.trimmed == [11, 12]
    assert fake.minimized == [{11, 12}]  # windows minimized before suspending
    assert e.freed_mb == 150.0 and e.session == "thesis"


def test_freeze_skips_conhost_system_and_session0():
    fake = FakeOs([FakeProc(11), FakeProc(12, exe="conhost.exe"),
                   FakeProc(13, exe="svchost.exe"), FakeProc(14, session=0)])
    act = RealActuator(os_layer=fake, ledger=Ledger())
    e = act.freeze(snap("a.exe", *[ProcRef(pid, 100.0) for pid in (11, 12, 13, 14)]), "s")
    assert fake.suspended == [11]  # only the real app process
    assert [p.pid for p in e.procs] == [11, 12, 13, 14]  # entry keeps everything


def test_freeze_never_suspends_itself():
    me = os.getpid()
    fake = FakeOs([FakeProc(me), FakeProc(11)])
    act = RealActuator(os_layer=fake, ledger=Ledger())
    act.freeze(snap("a.exe", ProcRef(me, 100.0), ProcRef(11, 100.0)), "s")
    assert fake.suspended == [11]


def test_freeze_never_drops_entry_on_pid_reuse():
    fake = FakeOs([FakeProc(11, ctime=999.0)])  # PID reused since the snapshot
    act = RealActuator(os_layer=fake, ledger=Ledger())
    act.freeze(snap("a.exe", ProcRef(11, 100.0)), "s")
    assert fake.suspended == []  # stranger process untouched
    assert [p.pid for p in Ledger().load()["a.exe"].procs] == [11]


def test_freeze_twice_raises_and_suspends_once():
    fake = FakeOs([FakeProc(11)])
    act = RealActuator(os_layer=fake, ledger=Ledger())
    act.freeze(snap("a.exe", ProcRef(11, 100.0)), "s")
    with pytest.raises(FreezeError, match="already frozen"):
        act.freeze(snap("a.exe", ProcRef(11, 100.0)), "s")
    assert fake.suspended == [11]  # no double suspend (would need double resume)


def test_freeze_reports_each_failure_but_freezes_the_rest():
    fake = FakeOs([FakeProc(11), FakeProc(12)])
    fake.fail_suspend.add(11)
    act = RealActuator(os_layer=fake, ledger=Ledger())
    with pytest.raises(FreezeError) as ei:
        act.freeze(snap("a.exe", ProcRef(11, 100.0), ProcRef(12, 100.0)), "s")
    assert list(ei.value.failures) == [11]
    assert fake.suspended == [12]  # proc 12 still frozen; only successes recorded
    assert set(Ledger().load()) == {"a.exe"}


def test_thaw_resumes_and_clears():
    fake = FakeOs([FakeProc(11)])
    act = RealActuator(os_layer=fake, ledger=Ledger())
    act.freeze(snap("a.exe", ProcRef(11, 100.0)), "s")
    act.thaw("a.exe")
    assert fake.resumed == [11] and fake.restored == [{11}]
    assert Ledger().load() == {}


def test_thaw_keeps_entry_on_mismatch():
    fake = FakeOs([FakeProc(11)])
    act = RealActuator(os_layer=fake, ledger=Ledger())
    act.freeze(snap("a.exe", ProcRef(11, 100.0)), "s")
    fake.procs[11].ctime = 999.0  # PID reused while frozen
    act.thaw("a.exe")
    assert fake.resumed == []  # stranger process untouched
    assert [p.pid for p in Ledger().load()["a.exe"].procs] == [11]


def test_thaw_prunes_dead_pids_but_keeps_mismatched_live_ones():
    fake = FakeOs([FakeProc(11), FakeProc(12)])
    act = RealActuator(os_layer=fake, ledger=Ledger())
    act.freeze(snap("a.exe", ProcRef(11, 100.0), ProcRef(12, 100.0)), "s")
    fake.procs[11].alive = False   # exited: nothing to resume
    fake.procs[12].ctime = 999.0   # reused: must stay listed
    act.thaw("a.exe")
    assert [p.pid for p in Ledger().load()["a.exe"].procs] == [12]


def test_thaw_unknown_app_is_noop():
    RealActuator(os_layer=FakeOs([]), ledger=Ledger()).thaw("nope.exe")


def test_thaw_all_restores_oldest_first_and_reports():
    fake = FakeOs([FakeProc(11, ws_mb=1024.0), FakeProc(12, ws_mb=1024.0), FakeProc(13, ws_mb=1024.0)])
    clock = FakeClock()
    act = RealActuator(os_layer=fake, ledger=Ledger(), clock=clock)
    clock.now = 30.0
    act.freeze(snap("a.exe", ProcRef(11, 100.0)), "s")
    clock.now = 10.0
    act.freeze(snap("b.exe", ProcRef(12, 100.0)), "s")
    clock.now = 20.0
    act.freeze(snap("c.exe", ProcRef(13, 100.0)), "s")
    clock.now = 100.0
    assert act.thaw_all() == ["b.exe", "c.exe", "a.exe"]
    assert fake.resumed == [12, 13, 11]
    assert act.last_report is not None
    assert act.last_report.app_ids == ["b.exe", "c.exe", "a.exe"]
    assert act.last_report.restored_gb == pytest.approx(3.0)
    assert Ledger().load() == {}


def _tamper() -> None:
    p = ledger_mod.ledger_path()
    doc = json.loads(p.read_text(encoding="utf-8"))
    doc["entries"][0]["freed_mb"] += 1
    p.write_text(json.dumps(doc), encoding="utf-8")


def test_freeze_refused_when_ledger_tampered():
    fake = FakeOs([FakeProc(11), FakeProc(12)])
    act = RealActuator(os_layer=fake, ledger=Ledger())
    act.freeze(snap("a.exe", ProcRef(11, 100.0)), "s")
    _tamper()
    with pytest.raises(LedgerTampered):
        act.freeze(snap("b.exe", ProcRef(12, 100.0)), "s")
    assert 12 not in fake.suspended


def test_thaw_all_still_works_when_tampered():
    fake = FakeOs([FakeProc(11)])
    act = RealActuator(os_layer=fake, ledger=Ledger())
    act.freeze(snap("a.exe", ProcRef(11, 100.0)), "s")
    _tamper()
    assert act.thaw_all() == ["a.exe"]
    assert fake.resumed == [11]


@pytest.mark.skipif(os.name != "nt", reason="Windows-only ctypes smoke test")
def test_windows_os_can_resolve_self():
    info = WindowsOs().resolve(os.getpid())
    assert info is not None and info.pid == os.getpid()
    assert info.exe.endswith(".exe") and info.create_time > 0
    assert WindowsOs().working_set_mb(os.getpid()) > 0
    assert WindowsOs().confirmed_dead(os.getpid()) is False
    assert WindowsOs().confirmed_dead(999999) is True


def test_concurrent_freeze_of_same_app_suspends_once():
    import threading

    fake = FakeOs([FakeProc(11)])
    act = RealActuator(os_layer=fake, ledger=Ledger())
    snapshot = snap("a.exe", ProcRef(11, 100.0))
    barrier = threading.Barrier(2)
    outcomes = []

    def worker():
        barrier.wait(timeout=30)
        try:
            act.freeze(snapshot, "s")
            outcomes.append("ok")
        except FreezeError as e:
            outcomes.append("already" if "already frozen" in str(e) else "other")

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
        assert not t.is_alive()
    assert sorted(outcomes) == ["already", "ok"]
    assert fake.suspended == [11]  # no double suspend (would need double resume)
    assert set(Ledger().load()) == {"a.exe"}


def test_create_time_within_tolerance_still_matches():
    fake = FakeOs([FakeProc(11, ctime=100.4)])  # psutil-style float wobble
    act = RealActuator(os_layer=fake, ledger=Ledger())
    act.freeze(snap("a.exe", ProcRef(11, 100.0)), "s")
    assert fake.suspended == [11]
    act.thaw("a.exe")
    assert fake.resumed == [11] and Ledger().load() == {}


def test_freeze_reports_unreachable_live_process_as_failure():
    fake = FakeOs([FakeProc(11), FakeProc(12)])
    fake.denied.add(12)  # live but protected
    act = RealActuator(os_layer=fake, ledger=Ledger())
    with pytest.raises(FreezeError) as ei:
        act.freeze(snap("a.exe", ProcRef(11, 100.0), ProcRef(12, 100.0)), "s")
    assert list(ei.value.failures) == [12]
    assert fake.suspended == [11]


def test_thaw_keeps_unreachable_live_process_listed():
    fake = FakeOs([FakeProc(11)])
    act = RealActuator(os_layer=fake, ledger=Ledger())
    act.freeze(snap("a.exe", ProcRef(11, 100.0)), "s")
    fake.denied.add(11)  # protected after the fact, still live
    act.thaw("a.exe")
    assert fake.resumed == []
    assert [p.pid for p in Ledger().load()["a.exe"].procs] == [11]


def test_freeze_skips_unnameable_exe():
    fake = FakeOs([FakeProc(11, exe="")])  # fail closed: never suspend the unknown
    act = RealActuator(os_layer=fake, ledger=Ledger())
    act.freeze(snap("a.exe", ProcRef(11, 100.0)), "s")
    assert fake.suspended == []


def test_thaw_all_prefers_mru_when_snapshots_given():
    fake = FakeOs([FakeProc(11, ws_mb=100.0), FakeProc(12, ws_mb=100.0)])
    clock = FakeClock()
    act = RealActuator(os_layer=fake, ledger=Ledger(), clock=clock)
    clock.now = 30.0
    act.freeze(snap("old.exe", ProcRef(11, 100.0)), "s")  # frozen first...
    clock.now = 10.0
    act.freeze(snap("new.exe", ProcRef(12, 100.0)), "s")  # ...but used most recently
    snaps = [AppSnapshot("old.exe", "old", [], [], 0.0, 100.0, False, False, False, False),
             AppSnapshot("new.exe", "new", [], [], 0.0, 1.0, False, False, False, False)]
    assert act.thaw_all(snaps) == ["new.exe", "old.exe"]
    assert act.last_report is not None and act.last_report.app_ids == ["new.exe", "old.exe"]
