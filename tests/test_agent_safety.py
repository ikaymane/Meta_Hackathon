"""Agent loop safety: per-app failures, frozen re-check, freeze rate limit."""
from freezer.agent import Decision, FocusAgent, Proposal
from freezer.contracts import Plan, PolicyResult
from freezer.mocks import MockActuator, MockSensor, mock_policy
from tests.test_agent import freezing
from tests.test_planner import FakeClient


class FlakyActuator(MockActuator):
    def __init__(self, fail_ids):
        super().__init__()
        self.fail_ids = set(fail_ids)

    def freeze(self, app, session):
        if app.app_id in self.fail_ids:
            raise RuntimeError("suspend denied")
        return super().freeze(app, session)


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def approve_all(p):
    return [d.app_id for d in p.to_freeze]


def test_one_bad_app_does_not_stop_the_rest():
    sensor = MockSensor("login_bug")
    agent = FocusAgent(sensor, mock_policy, FlakyActuator({"discord.exe"}), approve_all,
                       client=FakeClient(freezing(sensor, {"discord.exe", "steam.exe"})))
    out = agent.focus("fixing the login bug")
    assert [e.app_id for e in out.frozen] == ["steam.exe"]
    assert [(f.app_id, f.error) for f in out.failed] == [
        ("discord.exe", "RuntimeError: suspend denied")]


def test_rate_limit_stops_a_freeze_storm():
    sensor = MockSensor("login_bug")
    allow_all = lambda ids, snaps: PolicyResult(allowed=list(ids))  # noqa: E731
    agent = FocusAgent(sensor, allow_all, MockActuator(), approve_all,
                       client=FakeClient(freezing(sensor, {"discord.exe", "steam.exe", "spotify.exe"})),
                       max_freezes_per_min=2, clock=Clock())
    out = agent.focus("fixing the login bug")
    assert len(out.frozen) == 2 and len(out.failed) == 1
    assert "rate limited" in out.failed[0].error


def test_rate_window_rolls_off_after_a_minute():
    sensor = MockSensor("login_bug")
    clock = Clock()
    client = FakeClient(freezing(sensor, {"steam.exe"}), freezing(sensor, {"steam.exe"}, 1))
    agent = FocusAgent(sensor, mock_policy, MockActuator(), approve_all,
                       client=client, max_freezes_per_min=1, clock=clock)
    assert [e.app_id for e in agent.focus("t").frozen] == ["steam.exe"]
    agent.done()
    clock.now += 61.0
    out2 = agent.focus("t")
    assert [e.app_id for e in out2.frozen] == ["steam.exe"] and out2.failed == []


def test_already_frozen_apps_are_skipped_not_refrozen():
    sensor = MockSensor("login_bug")
    actuator = MockActuator()
    by_id = {s.app_id: s for s in sensor.snapshot()}
    actuator.freeze(by_id["discord.exe"], "")
    agent = FocusAgent(sensor, mock_policy, actuator, approve_all,
                       client=FakeClient(freezing(sensor, {"discord.exe", "steam.exe"})))
    out = agent.focus("fixing the login bug")
    assert [e.app_id for e in out.frozen] == ["steam.exe"]
    assert [(f.app_id, f.error) for f in out.failed] == [("discord.exe", "already frozen")]
    assert actuator.log.count(("freeze", "discord.exe")) == 1  # never asked twice


def test_app_missing_from_snapshot_fails_cleanly():
    sensor = MockSensor("login_bug")
    agent = FocusAgent(sensor, mock_policy, MockActuator(), approve_all, client=FakeClient())
    proposal = Proposal(plan=Plan("t", [], "m"), to_freeze=[Decision("ghost.exe", "freeze", "x")],
                        blocked=[], keep=[], free_mb=0, disconnects=[])
    out = agent._execute(proposal, sensor.snapshot(), ["ghost.exe"])
    assert out.frozen == []
    assert [(f.app_id, f.error) for f in out.failed] == [("ghost.exe", "not in snapshot")]
