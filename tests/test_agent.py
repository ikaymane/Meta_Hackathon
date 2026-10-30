from freezer import sessions
from freezer.agent import FocusAgent
from freezer.contracts import PressureEvent
from freezer.mocks import MockActuator, MockSensor, mock_policy
from tests.test_planner import FakeClient, all_decisions, call, reply


def make_agent(scenario, client, approve=None):
    sensor = MockSensor(scenario)
    seen = []

    def consent(p):
        seen.append(p)
        ids = [d.app_id for d in p.to_freeze]
        return approve(ids) if approve else ids

    actuator = MockActuator()
    return FocusAgent(sensor, mock_policy, actuator, consent, client=client), actuator, seen, sensor


def freezing(sensor, ids, i=0):
    return reply(call("freeze_apps", all_decisions(sensor.snapshot(), set(ids)), i))


def test_focus_freezes_only_what_policy_allows():
    snaps = MockSensor("injection_slides").snapshot()
    client = FakeClient(reply(call("freeze_apps", all_decisions(snaps, {"explorer.exe", "powerpnt.exe", "slack.exe"}))))
    agent, actuator, seen, _ = make_agent("injection_slides", client)
    out = agent.focus("preparing slides")
    assert [e.app_id for e in out.frozen] == ["slack.exe"]
    assert {(b.app_id, b.rule) for b in seen[0].blocked} == {("explorer.exe", "system_process"),
                                                            ("powerpnt.exe", "foreground")}
    assert seen[0].disconnects == ["slack.exe"] and seen[0].free_mb == 700


def test_ui_cannot_approve_blocked_apps():
    snaps = MockSensor("injection_slides").snapshot()
    client = FakeClient(reply(call("freeze_apps", all_decisions(snaps, {"explorer.exe", "slack.exe"}))))
    agent, actuator, _, _ = make_agent("injection_slides", client, approve=lambda ids: ids + ["explorer.exe"])
    out = agent.focus("preparing slides")
    assert [e.app_id for e in out.frozen] == ["slack.exe"]


def test_untick_becomes_session_rule_and_sticks():
    sensor = MockSensor("thesis")
    client = FakeClient(freezing(sensor, {"zotero.exe", "steam.exe"}), freezing(sensor, {"zotero.exe", "steam.exe"}, 1))
    agent, actuator, seen, _ = make_agent("thesis", client, approve=lambda ids: [i for i in ids if i != "zotero.exe"])
    out = agent.focus("writing my thesis", "thesis")
    assert [e.app_id for e in out.frozen] == ["steam.exe"]
    assert sessions.get("thesis").keep_rules == ["zotero.exe"]
    agent.done()
    out2 = agent.focus("writing my thesis", "thesis")
    assert "zotero.exe" not in [d.app_id for d in seen[1].to_freeze]
    assert [e.app_id for e in out2.frozen] == ["steam.exe"]


def test_cancel_freezes_nothing_and_writes_no_rule():
    sensor = MockSensor("thesis")
    agent, actuator, _, _ = make_agent("thesis", FakeClient(freezing(sensor, {"steam.exe"})), approve=lambda ids: None)
    out = agent.focus("writing my thesis", "thesis")
    assert out.declined and actuator.frozen() == []
    assert sessions.get("thesis").keep_rules == []


def test_question_freezes_nothing():
    client = FakeClient(reply(call("ask_user", {"question": "Focus on what?"})))
    agent, actuator, seen, _ = make_agent("vague_focus", client)
    out = agent.focus("focus")
    assert out.question == "Focus on what?" and seen == [] and actuator.frozen() == []


def test_done_thaws_everything():
    sensor = MockSensor("login_bug")
    agent, actuator, _, _ = make_agent("login_bug", FakeClient(freezing(sensor, {"discord.exe", "steam.exe"})))
    agent.focus("fixing the login bug")
    assert sorted(agent.done()) == ["discord.exe", "steam.exe"] and actuator.frozen() == []


def test_pressure_asks_consent_and_sticks_to_candidates():
    sensor = MockSensor("login_bug")
    agent, actuator, seen, _ = make_agent("login_bug", FakeClient(freezing(sensor, {"discord.exe", "steam.exe"})))
    out = agent.on_pressure(PressureEvent(ram_pct=91, sustained_s=30, candidates=["steam.exe"]))
    assert seen[0].source == "guardian"
    assert [e.app_id for e in out.frozen] == ["steam.exe"]


def test_pressure_with_auto_freeze_skips_card():
    sensor = MockSensor("thesis")
    client = FakeClient(freezing(sensor, set()), freezing(sensor, {"steam.exe"}, 1))
    agent, actuator, seen, _ = make_agent("thesis", client)
    agent.focus("writing my thesis", "thesis")
    sessions.set_auto_freeze("thesis", True)
    out = agent.on_pressure(PressureEvent(ram_pct=92, sustained_s=30, candidates=["steam.exe", "ms-teams.exe"]))
    assert [e.app_id for e in out.frozen] == ["steam.exe"] and seen == []


def test_api_down_still_works_with_heuristic():
    agent, actuator, seen, _ = make_agent("login_bug", FakeClient(RuntimeError("down")))
    out = agent.focus("fixing the login bug")
    assert seen[0].plan.model == "heuristic"
    assert [e.app_id for e in out.frozen] == ["steam.exe"]
