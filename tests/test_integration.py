"""A x C integration: C's loop drives A's real actuator, ledger, and guardian.

The OS layer is in-memory (no real processes), but the ledger is the real
signed file on disk and the actuator is the real freeze/thaw logic, so this
proves the A/C seam instead of re-testing the mocks.
"""
from freezer.agent import FocusAgent
from freezer.actuator import RealActuator
from freezer.contracts import PressureEvent
from freezer.guardian import Guardian
from freezer.ledger import Ledger
from freezer.mocks import MockSensor, mock_policy
from tests.test_actuator import FakeOs, FakeProc
from tests.test_agent import freezing
from tests.test_planner import FakeClient


def real_actuator_for(sensor):
    """Real actuator + real ledger, fake OS mirroring the sensor's PID/ctimes."""
    procs = [FakeProc(p.pid, ctime=p.create_time)
             for s in sensor.snapshot() for p in s.procs]
    fake = FakeOs(procs)
    return RealActuator(os_layer=fake, ledger=Ledger()), fake


def approve_all(p):
    return [d.app_id for d in p.to_freeze]


def test_focus_freezes_through_real_actuator_and_done_thaws():
    sensor = MockSensor("login_bug")
    act, fake = real_actuator_for(sensor)
    agent = FocusAgent(sensor, mock_policy, act, approve_all,
                       client=FakeClient(freezing(sensor, {"discord.exe", "steam.exe"})))
    out = agent.focus("fixing the login bug")
    assert {e.app_id for e in out.frozen} == {"discord.exe", "steam.exe"}
    assert set(Ledger().load()) == {"discord.exe", "steam.exe"}  # really on disk
    assert all(fake.procs[pid].suspended for pid in fake.suspended)

    thawed = agent.done()
    assert sorted(thawed) == ["discord.exe", "steam.exe"]
    assert Ledger().load() == {}
    assert act.last_report is not None and act.last_report.restored_gb > 0


def test_guardian_event_flows_into_agent_and_freezes():
    sensor = MockSensor("login_bug")
    act, fake = real_actuator_for(sensor)
    agent = FocusAgent(sensor, mock_policy, act, approve_all,
                       client=FakeClient(freezing(sensor, {"steam.exe"})))
    now, ram = [0.0], [91.0]
    g = Guardian(ram_pct=lambda: ram[0], clock=lambda: now[0])
    snaps = sensor.snapshot()
    assert g.poll(snaps) is None
    now[0] = 30.0
    event = g.poll(snaps)
    assert isinstance(event, PressureEvent) and "steam.exe" in event.candidates

    out = agent.on_pressure(event)
    assert [e.app_id for e in out.frozen] == ["steam.exe"]
    assert set(Ledger().load()) == {"steam.exe"}
    agent.done()
    assert Ledger().load() == {}
