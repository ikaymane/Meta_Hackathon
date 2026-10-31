"""Agent loop safety on the merged loop: per-app failures, re-check, rate limit.

C's merged loop reports per-app problems as Outcome.failed (ids), Outcome.skipped
(Blocked, incl. rate_limited), and Outcome.unticked (for the UI's rule offer);
session rules are written by the UI, not here.
"""
import os
import time
from collections import deque

from freezer import watchdog
from freezer.agent import Decision, FocusAgent, Proposal
from freezer.contracts import Blocked, Plan, PolicyResult
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


def approve_all(p):
    return [d.app_id for d in p.to_freeze]


def allow_all(ids, snaps):
    return PolicyResult(allowed=list(ids))


def test_one_bad_app_does_not_stop_the_rest():
    sensor = MockSensor("login_bug")
    agent = FocusAgent(sensor, mock_policy, FlakyActuator({"discord.exe"}), approve_all,
                       client=FakeClient(freezing(sensor, {"discord.exe", "steam.exe"})))
    out = agent.focus("fixing the login bug")
    assert [e.app_id for e in out.frozen] == ["steam.exe"]
    assert out.failed == ["discord.exe"]


def test_rate_limit_skips_over_limit_as_blocked():
    sensor = MockSensor("login_bug")
    agent = FocusAgent(sensor, allow_all, MockActuator(), approve_all,
                       client=FakeClient(freezing(sensor, {"discord.exe", "steam.exe", "spotify.exe"})))
    agent._freeze_times = deque([time.time()] * 10)
    out = agent.focus("fixing the login bug")
    assert out.frozen == [] and out.failed == []
    assert {b.app_id for b in out.skipped} == {"discord.exe", "steam.exe", "spotify.exe"}
    assert {b.rule for b in out.skipped} == {"rate_limited"}


def test_rate_window_rolls_off_after_a_minute():
    sensor = MockSensor("login_bug")
    agent = FocusAgent(sensor, mock_policy, MockActuator(), approve_all,
                       client=FakeClient(freezing(sensor, {"steam.exe"})))
    agent._freeze_times = deque([time.time() - 61] * 10)
    out = agent.focus("t")
    assert [e.app_id for e in out.frozen] == ["steam.exe"]


def test_frozen_apps_are_never_refrozen():
    sensor = MockSensor("login_bug")
    actuator = MockActuator()
    by_id = {s.app_id: s for s in sensor.snapshot()}
    actuator.freeze(by_id["discord.exe"], "")
    agent = FocusAgent(sensor, mock_policy, actuator, approve_all,
                       client=FakeClient(freezing(sensor, {"discord.exe", "steam.exe"})))
    out = agent.focus("fixing the login bug")
    assert [e.app_id for e in out.frozen] == ["steam.exe"]
    assert out.failed == []
    assert actuator.log.count(("freeze", "discord.exe")) == 1  # never asked twice


def test_unknown_app_is_reported_skipped_not_frozen():
    sensor = MockSensor("login_bug")
    agent = FocusAgent(sensor, mock_policy, MockActuator(), approve_all, client=FakeClient())
    proposal = Proposal(plan=Plan("t", [], "m"), to_freeze=[Decision("ghost.exe", "freeze", "x")],
                        blocked=[], keep=[], free_mb=0, disconnects=[])
    out = agent._execute(proposal, ["ghost.exe"])
    assert out.frozen == [] and out.failed == []
    assert out.skipped == [Blocked("ghost.exe", "unknown_app")]


def test_unticked_apps_are_reported_for_ui_rule():
    sensor = MockSensor("login_bug")
    agent = FocusAgent(sensor, mock_policy, MockActuator(), lambda p: ["steam.exe"],
                       client=FakeClient(freezing(sensor, {"steam.exe", "discord.exe"})))
    out = agent.focus("fixing the login bug")
    assert [e.app_id for e in out.frozen] == ["steam.exe"]
    assert sorted(out.unticked) == ["discord.exe"]


def test_loop_writes_heartbeat():
    sensor = MockSensor("login_bug")
    agent = FocusAgent(sensor, mock_policy, MockActuator(), approve_all,
                       client=FakeClient(freezing(sensor, {"steam.exe"})))
    agent.focus("t")
    pid, _ts = watchdog.read_beat()
    assert pid == os.getpid()
