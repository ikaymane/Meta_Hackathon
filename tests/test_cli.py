"""ff CLI: freeze/thaw/list against a recording actuator; live discovery."""
import os

import pytest

from freezer import cli
from freezer.contracts import AppSnapshot
from freezer.mocks import MockActuator


class RecordingActuator(MockActuator):
    def __init__(self):
        super().__init__()
        self.freeze_calls: list[AppSnapshot] = []


@pytest.fixture()
def actuator(monkeypatch):
    act = RecordingActuator()
    monkeypatch.setattr(cli, "build_actuator", lambda: act)
    orig_freeze = act.freeze

    def freeze(app, session):
        act.freeze_calls.append(app)
        return orig_freeze(app, session)

    monkeypatch.setattr(act, "freeze", freeze)
    return act


def test_freeze_by_pids_records_snapshot(actuator):
    assert cli.main(["freeze", "--pids", "99999111,99999222", "--app", "T.EXE",
                     "--session", "thesis"]) == 0
    assert [a.app_id for a in actuator.freeze_calls] == ["t.exe"]
    assert [p.pid for p in actuator.freeze_calls[0].procs] == [99999111, 99999222]


def test_freeze_needs_exe_or_pids(actuator, capsys):
    assert cli.main(["freeze"]) == 2
    assert cli.main(["freeze", "--pids", "123"]) == 2  # missing --app


def test_freeze_reports_actuator_failure(actuator, capsys, monkeypatch):
    def boom(app, session):
        from freezer.actuator import FreezeError
        raise FreezeError(app.app_id, "denied", failures={1: "no"})
    monkeypatch.setattr(actuator, "freeze", boom)
    assert cli.main(["freeze", "--pids", "1", "--app", "x.exe"]) == 1
    assert "denied" in capsys.readouterr().err


def test_thaw_and_list_round_trip(actuator, capsys):
    cli.main(["freeze", "--pids", "99999111", "--app", "x.exe"])
    assert cli.main(["list"]) == 0
    out = capsys.readouterr().out
    assert "x.exe" in out and "procs" in out
    assert cli.main(["thaw", "x.exe"]) == 0
    assert cli.main(["list"]) == 0
    assert "nothing frozen" in capsys.readouterr().out


def test_thaw_all_lists_everything(actuator, capsys):
    cli.main(["freeze", "--pids", "1", "--app", "a.exe"])
    cli.main(["freeze", "--pids", "2", "--app", "b.exe"])
    assert cli.main(["thaw", "--all"]) == 0
    out = capsys.readouterr().out
    assert "a.exe" in out and "b.exe" in out


def test_thaw_needs_target(actuator):
    assert cli.main(["thaw"]) == 2


@pytest.mark.skipif(os.name != "nt", reason="Windows-only process discovery")
def test_find_pids_by_exe_finds_self():
    from freezer.actuator import WindowsOs

    info = WindowsOs().resolve(os.getpid())
    assert info is not None
    refs = cli.find_pids_by_exe(info.exe)
    assert os.getpid() in [r.pid for r in refs]


@pytest.mark.skipif(os.name != "nt", reason="Windows-only process discovery")
def test_freeze_unknown_exe_fails_cleanly(actuator, capsys):
    assert cli.main(["freeze", "definitely-not-a-real-app-xyz.exe"]) == 1
    assert "no live" in capsys.readouterr().err
