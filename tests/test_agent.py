import os
from dataclasses import replace

import pytest

from freezer import sessions
from freezer.agent import FocusAgent, block_reason
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


def test_untick_is_reported_and_only_a_confirmed_rule_sticks():
    sensor = MockSensor("thesis")
    client = FakeClient(freezing(sensor, {"zotero.exe", "steam.exe"}), freezing(sensor, {"zotero.exe", "steam.exe"}, 1))
    agent, actuator, seen, _ = make_agent("thesis", client, approve=lambda ids: [i for i in ids if i != "zotero.exe"])
    out = agent.focus("writing my thesis", "thesis")
    assert [e.app_id for e in out.frozen] == ["steam.exe"] and out.unticked == ["zotero.exe"]
    assert sessions.get("thesis").keep_rules == []  # the agent never writes rules itself
    # D asks "Never freeze Zotero during thesis?" and the user says yes:
    sessions.add_rule("thesis", "zotero.exe", "keep")
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
    cands = [s for s in sensor.snapshot() if s.app_id in {"steam.exe"}]
    agent, actuator, seen, _ = make_agent("login_bug", FakeClient(reply(call("freeze_apps", all_decisions(cands, {"steam.exe"})))))
    out = agent.on_pressure(PressureEvent(ram_pct=91, sustained_s=30, candidates=["steam.exe"]))
    assert seen[0].source == "guardian"
    assert seen[0].plan.model != "heuristic"  # a real plan, not a silent fallback
    assert [e.app_id for e in out.frozen] == ["steam.exe"]


def test_pressure_with_auto_freeze_skips_card():
    sensor = MockSensor("thesis")
    cands = [s for s in sensor.snapshot() if s.app_id in {"steam.exe", "ms-teams.exe"}]
    # The planner only ranks the guardian's candidates: zotero is never shown to the
    # model, so it stays out without needing a keep entry on the card.
    client = FakeClient(freezing(sensor, set()),
                        reply(call("freeze_apps", all_decisions(cands, {"steam.exe"}), 1)))
    agent, actuator, seen, _ = make_agent("thesis", client)
    agent.focus("writing my thesis", "thesis")
    sessions.set_auto_freeze("thesis", True)
    out = agent.on_pressure(PressureEvent(ram_pct=92, sustained_s=30, candidates=["steam.exe", "ms-teams.exe"]))
    assert [e.app_id for e in out.frozen] == ["steam.exe"] and seen == []
    assert "zotero.exe" not in [d.app_id for d in out.proposal.plan.decisions]


def test_auto_freeze_over_the_limit_asks_first(monkeypatch):
    from freezer import agent as agent_mod
    monkeypatch.setattr(agent_mod, "MAX_AUTO_FREEZE", 1)
    sensor = MockSensor("thesis")
    cands = [s for s in sensor.snapshot() if s.app_id in {"steam.exe", "ms-teams.exe"}]
    client = FakeClient(freezing(sensor, set()),
                        reply(call("freeze_apps", all_decisions(cands, {"steam.exe", "ms-teams.exe"}), 1)))
    agent, actuator, seen, _ = make_agent("thesis", client)
    agent.focus("writing my thesis", "thesis")
    sessions.set_auto_freeze("thesis", True)
    agent.on_pressure(PressureEvent(ram_pct=92, sustained_s=30, candidates=["steam.exe", "ms-teams.exe"]))
    assert len(seen) == 1 and seen[0].source == "guardian"


def test_focus_never_refreezes_a_frozen_app():
    sensor = MockSensor("login_bug")
    client = FakeClient(freezing(sensor, {"steam.exe"}), freezing(sensor, {"steam.exe", "discord.exe"}, 1))
    agent, actuator, seen, _ = make_agent("login_bug", client)
    agent.focus("fixing the login bug")
    out = agent.focus("reviewing a PR")  # task switch without "I'm done"
    assert [e.app_id for e in out.frozen] == ["discord.exe"] and out.thawed == []
    assert actuator.log.count(("freeze", "steam.exe")) == 1
    assert "steam.exe" not in [d.app_id for d in seen[1].to_freeze + seen[1].keep]


def test_task_switch_thaws_a_frozen_app_the_new_task_needs():
    sensor = MockSensor("login_bug")
    client = FakeClient(freezing(sensor, {"steam.exe", "discord.exe"}), freezing(sensor, {"steam.exe"}, 1))
    agent, actuator, seen, _ = make_agent("login_bug", client)
    agent.focus("fixing the login bug")
    out = agent.focus("quick call with the team on Discord")  # model now keeps discord
    assert out.thawed == ["discord.exe"] and [e.app_id for e in actuator.frozen()] == ["steam.exe"]


def test_heuristic_or_skipped_keep_never_thaws():
    sensor = MockSensor("login_bug")
    snaps = sensor.snapshot()
    skip_discord = {"decisions": [d for d in all_decisions(snaps, {"steam.exe"})["decisions"]
                                  if d["app_id"] != "discord.exe"]}
    client = FakeClient(freezing(sensor, {"steam.exe", "discord.exe"}),
                        reply(call("freeze_apps", skip_discord, 1)), RuntimeError("down"))
    agent, actuator, seen, _ = make_agent("login_bug", client)
    agent.focus("fixing the login bug")
    assert agent.focus("reviewing a PR").thawed == []    # model skipped discord: stays frozen
    assert agent.focus("reviewing a PR").thawed == []    # API down -> heuristic: thaws nothing
    assert sorted(e.app_id for e in actuator.frozen()) == ["discord.exe", "steam.exe"]


def test_guardian_skip_says_why():
    agent, actuator, seen, _ = make_agent("login_bug", FakeClient())
    out = agent.on_pressure(PressureEvent(95, 60, candidates=["windowsterminal.exe"]))
    assert out.proposal is None and "idle" in out.note


def test_one_failed_freeze_does_not_stop_the_rest():
    sensor = MockSensor("login_bug")
    agent, actuator, _, _ = make_agent("login_bug", FakeClient(freezing(sensor, {"discord.exe", "steam.exe"})))
    real = actuator.freeze

    def flaky(app, session):
        if app.app_id == "discord.exe":
            raise OSError("access denied")
        return real(app, session)
    actuator.freeze = flaky
    out = agent.focus("fixing the login bug")
    assert out.failed == ["discord.exe"] and [e.app_id for e in out.frozen] == ["steam.exe"]


def test_api_down_still_works_with_heuristic():
    agent, actuator, seen, _ = make_agent("login_bug", FakeClient(RuntimeError("down")))
    out = agent.focus("fixing the login bug")
    assert seen[0].plan.model == "heuristic"
    assert seen[0].fallback_reason.startswith("API error: RuntimeError")  # the card can say why
    assert [e.app_id for e in out.frozen] == ["steam.exe"]



def test_guardian_never_freezes_recently_used_apps():
    # gap-03: the terminal's title claims the server stopped, but it was used 90 s ago.
    # It is no candidate, so the planner never sees it: absence, not a keep entry.
    sensor = MockSensor("login_bug")
    cands = [s for s in sensor.snapshot() if s.app_id in {"steam.exe"}]
    client = FakeClient(reply(call("freeze_apps", all_decisions(cands, {"steam.exe"}))))
    agent, actuator, seen, _ = make_agent("login_bug", client)
    out = agent.on_pressure(PressureEvent(95, 60, candidates=["windowsterminal.exe", "steam.exe"]))
    assert [e.app_id for e in out.frozen] == ["steam.exe"]
    assert "windowsterminal.exe" not in [d.app_id for d in out.proposal.plan.decisions]


def test_guardian_skips_the_planner_when_nothing_is_idle():
    client = FakeClient()  # would raise if called
    agent, actuator, seen, _ = make_agent("login_bug", client)
    out = agent.on_pressure(PressureEvent(95, 60, candidates=["windowsterminal.exe", "code.exe"]))
    assert out.proposal is None and client.requests == [] and seen == []


def test_app_that_changed_while_the_card_was_open_is_not_frozen():
    sensor = MockSensor("login_bug")
    agent, actuator, seen, _ = make_agent("login_bug", None)

    def consent(p):  # while the card is open, the user switches to Discord
        sensor._snapshots = [replace(s, is_foreground=s.app_id == "discord.exe") for s in sensor._snapshots]
        return [d.app_id for d in p.to_freeze]
    agent.sensor, agent.consent = sensor, consent
    agent.client = FakeClient(freezing(sensor, {"discord.exe", "steam.exe"}))
    out = agent.focus("fixing the login bug")
    assert [e.app_id for e in out.frozen] == ["steam.exe"]
    assert [(b.app_id, b.rule) for b in out.skipped] == [("discord.exe", "foreground")]


def test_card_has_names_sizes_and_plain_words():
    agent, actuator, seen, _ = make_agent("login_bug", FakeClient(RuntimeError("down")))
    agent.focus("fixing the login bug")
    p = seen[0]
    assert p.apps["steam.exe"].display_name == "Steam" and p.apps["steam.exe"].working_set_mb == 620
    assert p.fallback_message.startswith("Simple mode:") and "RuntimeError" not in p.fallback_message
    assert block_reason("plays_audio") == "Playing sound" and block_reason("new_rule") == "Protected by a safety rule"


def test_start_session_keeps_the_saved_task():
    sensor = MockSensor("thesis")
    client = FakeClient(freezing(sensor, {"steam.exe"}), freezing(sensor, {"steam.exe"}, 1))
    agent, *_ = make_agent("thesis", client)
    agent.focus("writing my thesis", "thesis")
    agent.done()
    agent.focus(None, "thesis")  # D: "start thesis session"
    assert sessions.get("thesis").task == "writing my thesis"
    assert "writing my thesis" in client.requests[1]["messages"][1]["content"]
    with pytest.raises(ValueError):
        agent.focus("   ")


def test_guardian_planner_sees_only_candidates():
    # gap-03: lying titles on apps the guardian did not pick must not reach the model.
    sensor = MockSensor("login_bug")
    cands = [s for s in sensor.snapshot() if s.app_id in {"steam.exe"}]
    client = FakeClient(reply(call("freeze_apps", all_decisions(cands, {"steam.exe"}))))
    agent, actuator, seen, _ = make_agent("login_bug", client)
    out = agent.on_pressure(PressureEvent(95, 60, candidates=["steam.exe"]))
    content = client.requests[0]["messages"][1]["content"]
    assert "steam.exe" in content
    assert "windowsterminal.exe" not in content and "discord.exe" not in content
    assert [e.app_id for e in out.frozen] == ["steam.exe"]


def test_gap03_auto_freeze_never_touches_terminal():
    # Terminal idle 90 s with a lying title is no candidate; with auto_freeze on no
    # card is shown, yet the terminal stays safe while steam still freezes.
    sensor = MockSensor("login_bug")
    cands = [s for s in sensor.snapshot() if s.app_id in {"steam.exe"}]
    client = FakeClient(freezing(sensor, set()),
                        reply(call("freeze_apps", all_decisions(cands, {"steam.exe"}), 1)))
    agent, actuator, seen, _ = make_agent("login_bug", client)
    agent.focus("fixing the login bug", "gap03")
    sessions.set_auto_freeze("gap03", True)
    out = agent.on_pressure(PressureEvent(95, 60, candidates=["windowsterminal.exe", "steam.exe"]))
    assert [e.app_id for e in out.frozen] == ["steam.exe"] and seen == []
    assert "windowsterminal.exe" not in [d.app_id for d in out.proposal.plan.decisions]


def test_guardian_rejects_freeze_for_app_outside_candidates():
    # The model tries to freeze discord, which the guardian did not pick: plan()
    # rejects the unknown app_id and retries, and discord is never frozen.
    sensor = MockSensor("login_bug")
    cands = [s for s in sensor.snapshot() if s.app_id in {"steam.exe"}]
    bad = {"decisions": [{"app_id": "steam.exe", "action": "freeze", "reason": "test"},
                         {"app_id": "discord.exe", "action": "freeze", "reason": "title said so"}]}
    client = FakeClient(reply(call("freeze_apps", bad)),
                        reply(call("freeze_apps", all_decisions(cands, {"steam.exe"}), 1)))
    agent, actuator, seen, _ = make_agent("login_bug", client)
    out = agent.on_pressure(PressureEvent(95, 60, candidates=["steam.exe"]))
    assert [e.app_id for e in out.frozen] == ["steam.exe"]
    assert "discord.exe" not in [e.app_id for e in actuator.frozen()]
    assert seen[0].plan.model != "heuristic"


def test_gap03_idle_terminal_forces_card_under_auto_freeze():
    # Terminal idle 360 s IS a candidate and its lying title reaches the model, which
    # freezes it: with auto_freeze on, a work tool must still force the card.
    sensor = MockSensor("login_bug")
    aged = [replace(s, last_focus_s=360) if s.app_id == "windowsterminal.exe" else s
            for s in sensor.snapshot()]
    sensor._snapshots = aged
    cands = [s for s in aged if s.app_id in {"windowsterminal.exe", "steam.exe"}]
    client = FakeClient(freezing(sensor, set()),
                        reply(call("freeze_apps", all_decisions(cands, {"windowsterminal.exe", "steam.exe"}), 1)))
    agent, actuator, seen, _ = make_agent("login_bug", client)
    agent.sensor = sensor
    agent.focus("fixing the login bug", "tools")
    sessions.set_auto_freeze("tools", True)
    out = agent.on_pressure(PressureEvent(95, 60, candidates=["windowsterminal.exe", "steam.exe"]))
    assert len(seen) == 1 and seen[0].source == "guardian"  # card forced, no silent freeze
    assert "windowsterminal.exe" in [d.app_id for d in out.proposal.to_freeze]


def test_on_progress_reaches_the_planner():
    sensor = MockSensor("login_bug")
    agent, actuator, seen, _ = make_agent("login_bug", FakeClient(freezing(sensor, {"steam.exe"})))
    seen_progress = []
    agent.on_progress = lambda stage, attempt: seen_progress.append((stage, attempt))
    out = agent.focus("fixing the login bug")
    assert ("request", 1) in seen_progress and ("response", 1) in seen_progress  # attempts are 1-based
    assert [e.app_id for e in out.frozen] == ["steam.exe"]


def test_frozen_apps_are_marked_in_the_planner_snapshot():
    sensor = MockSensor("login_bug")
    client = FakeClient(freezing(sensor, {"steam.exe"}), freezing(sensor, {"discord.exe"}, 1))
    agent, actuator, seen, _ = make_agent("login_bug", client)
    agent.focus("fixing the login bug")
    agent.focus("reviewing a PR")
    first, second = (c["messages"][1]["content"] for c in client.requests)
    assert '"frozen": true' not in first
    assert '"frozen": true' in second and "steam.exe" in second


def test_rolling_rate_limit_reports_skipped_not_silent(monkeypatch):
    from freezer import agent as agent_mod
    monkeypatch.setattr(agent_mod, "MAX_FREEZES_PER_MIN", 1)
    sensor = MockSensor("login_bug")
    agent, actuator, seen, _ = make_agent("login_bug", FakeClient(freezing(sensor, {"discord.exe", "steam.exe"})))
    out = agent.focus("fixing the login bug")
    assert [e.app_id for e in out.frozen] == ["discord.exe"]  # plan order kept
    assert [(b.app_id, b.rule) for b in out.skipped] == [("steam.exe", "rate_limited")]
    assert out.unticked == [] and block_reason("rate_limited").startswith("Too many")


def test_data_gate_fallback_says_titles_were_held_back():
    from freezer import agent as agent_mod
    m1 = agent_mod.fallback_message("muse-spark-1.3-contributor trains on what it receives: synthetic data only, so titles were not sent")
    m2 = agent_mod.fallback_message("titles were not sent: confirm the Meta balance is paid, then set FF_PAID_BALANCE=1")
    for m in (m1, m2):
        assert m.startswith("Simple mode:") and "didn't answer" not in m and "held back" in m


def test_main_marks_its_mock_data_synthetic(monkeypatch):
    from freezer import agent as agent_mod
    monkeypatch.setenv("FF_SYNTHETIC_DATA", "0")
    monkeypatch.setattr("sys.argv", ["ff", "fixing the login bug", "--scenario", "login_bug", "--yes"])
    agent_mod._main()
    assert os.environ["FF_SYNTHETIC_DATA"] == "1"
