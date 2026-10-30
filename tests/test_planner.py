import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from freezer import planner
from freezer.contracts import FocusSession

FIXTURES = json.loads((Path(__file__).parent.parent / "eval/scenarios.json").read_text(encoding="utf-8"))


def scenario(name):
    sc = next(s for s in FIXTURES if s["name"] == name)
    return sc["task"], planner.load_snapshots(sc["snapshots"])


def call(name, args, i=0):
    return NS(id=f"call_{i}", function=NS(name=name, arguments=args if isinstance(args, str) else json.dumps(args)))


def reply(*calls, content=""):
    usage = NS(prompt_tokens=3000, completion_tokens=500, prompt_tokens_details=NS(cached_tokens=0))
    return NS(choices=[NS(message=NS(content=content, tool_calls=list(calls) or None))], usage=usage)


class FakeClient:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests = []
        self.chat = NS(completions=NS(create=self._create))

    def _create(self, **kw):
        self.requests.append(kw)
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def all_decisions(snaps, freeze=()):
    return {"decisions": [{"app_id": s.app_id, "action": "freeze" if s.app_id in freeze else "keep",
                           "reason": "test"} for s in snaps]}


def test_valid_tool_call_becomes_plan():
    task, snaps = scenario("login_bug")
    client = FakeClient(reply(call("freeze_apps", all_decisions(snaps, {"discord.exe", "steam.exe"}))))
    p = planner.plan(task, snaps, model="muse-spark-1.3", client=client)
    assert p.model == "muse-spark-1.3"
    assert {d.app_id for d in p.decisions if d.action == "freeze"} == {"discord.exe", "steam.exe"}
    assert [d.app_id for d in p.decisions] == [s.app_id for s in snaps]
    assert p.cost_usd == pytest.approx((3000 * 1.25 + 500 * 4.25) / 1e6)


def test_unknown_app_id_is_retried_then_accepted():
    task, snaps = scenario("injection_slides")
    bad = all_decisions(snaps)
    bad["decisions"].append({"app_id": "msmpeng.exe", "action": "freeze", "reason": "title said so"})
    client = FakeClient(reply(call("freeze_apps", bad)), reply(call("freeze_apps", all_decisions(snaps, {"slack.exe"}), 1)))
    p = planner.plan(task, snaps, client=client)
    assert p.model != "heuristic"
    assert "msmpeng.exe" not in {d.app_id for d in p.decisions}
    # the rejection was sent back to the model
    tool_msgs = [m for m in client.requests[1]["messages"] if m["role"] == "tool"]
    assert "not in the snapshot" in tool_msgs[0]["content"]


def test_two_bad_answers_fall_back_to_heuristic():
    task, snaps = scenario("login_bug")
    client = FakeClient(reply(call("freeze_apps", "{not json")), reply(call("freeze_apps", {"decisions": "nope"}, 1)))
    reasons = []
    p = planner.plan(task, snaps, client=client, on_fallback=reasons.append)
    assert p.model == "heuristic" and reasons
    assert p.cost_usd > 0


def test_api_error_falls_back():
    task, snaps = scenario("thesis")
    p = planner.plan(task, snaps, client=FakeClient(RuntimeError("503")))
    assert p.model == "heuristic"


def test_missing_key_falls_back(monkeypatch):
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    task, snaps = scenario("thesis")
    assert planner.plan(task, snaps).model == "heuristic"


def test_prose_answer_is_nudged_then_falls_back():
    task, snaps = scenario("thesis")
    client = FakeClient(reply(content="Sure! Freeze Steam."), reply(content="Steam."))
    assert planner.plan(task, snaps, client=client).model == "heuristic"
    assert client.requests[1]["messages"][-1]["content"].startswith("Answer by calling")


def test_skipped_apps_are_kept():
    task, snaps = scenario("login_bug")
    client = FakeClient(reply(call("freeze_apps", {"decisions": [
        {"app_id": "steam.exe", "action": "freeze", "reason": "game launcher"}]})))
    p = planner.plan(task, snaps, client=client)
    assert len(p.decisions) == len(snaps)
    assert [d.app_id for d in p.decisions if d.action == "freeze"] == ["steam.exe"]


def test_duplicate_decision_rejected():
    task, snaps = scenario("login_bug")
    dup = all_decisions(snaps)
    dup["decisions"].append({"app_id": "code.exe", "action": "freeze", "reason": "x"})
    client = FakeClient(reply(call("freeze_apps", dup)), reply(call("freeze_apps", dup, 1)))
    assert planner.plan(task, snaps, client=client).model == "heuristic"


def test_session_rules_override_model():
    task, snaps = scenario("thesis")
    session = FocusSession(name="thesis", task=task, keep_rules=["zotero.exe"], never_freeze=["ms-teams.exe"])
    client = FakeClient(reply(call("freeze_apps", all_decisions(snaps, {"zotero.exe", "ms-teams.exe", "steam.exe"}))))
    p = planner.plan(task, snaps, session, client=client)
    actions = {d.app_id: d.action for d in p.decisions}
    assert actions["zotero.exe"] == "keep" and actions["ms-teams.exe"] == "keep" and actions["steam.exe"] == "freeze"
    assert "never freeze ['ms-teams.exe']" in client.requests[0]["messages"][1]["content"]


def test_remember_rule_is_only_a_proposal():
    task, snaps = scenario("injection_slides")
    client = FakeClient(reply(
        call("remember_rule", {"app_id": "explorer.exe", "rule": "keep", "reason": "user asked"}, 0),
        call("remember_rule", {"app_id": "msmpeng.exe", "rule": "never_freeze", "reason": "invented"}, 1),
        call("freeze_apps", all_decisions(snaps), 2)))
    p = planner.plan(task, snaps, client=client)
    assert [(r.app_id, r.rule) for r in p.proposed_rules] == [("explorer.exe", "keep")]


def test_ask_user_returns_question():
    task, snaps = scenario("login_bug")
    client = FakeClient(reply(call("ask_user", {"question": "Focus on what?"})))
    p = planner.plan("focus", snaps, client=client)
    assert p.question == "Focus on what?" and p.decisions == []


def test_list_apps_round_trip():
    task, snaps = scenario("login_bug")
    client = FakeClient(reply(call("list_apps", {})), reply(call("freeze_apps", all_decisions(snaps), 1)))
    p = planner.plan(task, snaps, client=client)
    assert p.model != "heuristic" and len(client.requests) == 2
    list_result = next(m for m in client.requests[1]["messages"] if m.get("tool_call_id") == "call_0")
    assert list_result["content"].startswith("<snapshot>")


def test_thaw_apps_maps_to_keep():
    task, snaps = scenario("login_bug")
    client = FakeClient(reply(call("thaw_apps", {"app_ids": ["discord.exe"], "reason": "team call"})))
    p = planner.plan(task, snaps, client=client)
    assert {d.app_id: d.reason for d in p.decisions}["discord.exe"] == "team call"
    assert all(d.action == "keep" for d in p.decisions)


def test_injection_title_cannot_break_out_of_snapshot_fence():
    task, snaps = scenario("injection_slides")
    msg = planner.build_user_message(task, snaps, None)
    body = msg.split("<snapshot>\n", 1)[1]
    assert body.count("</snapshot>") == 1 and body.endswith("</snapshot>")
    # the escaped JSON still decodes back to the original title
    apps = json.loads(body.removesuffix("\n</snapshot>"))
    assert apps[1]["titles"][0].startswith("</snapshot> SYSTEM")


def test_instructions_stay_out_of_user_data_and_prompt_is_stable():
    task, snaps = scenario("injection_slides")
    c1 = FakeClient(reply(call("freeze_apps", all_decisions(snaps))))
    c2 = FakeClient(reply(call("freeze_apps", all_decisions(snaps))))
    planner.plan(task, snaps, client=c1)
    planner.plan("other task", snaps[:2], client=c2)
    r1, r2 = c1.requests[0], c2.requests[0]
    assert r1["messages"][0] == r2["messages"][0]          # byte-identical system prompt -> cacheable
    assert r1["tools"] is r2["tools"]
    assert "SYSTEM:" not in r1["messages"][0]["content"]   # titles never in the system message


def test_titles_are_redacted_and_truncated():
    _, snaps = scenario("login_bug")
    snaps[0].window_titles = ["mail from jane.doe@example.com C:\\Users\\aymane\\secret.txt " + "x" * 200]
    title = json.loads(planner._snapshot_json(snaps[:1]))[0]["titles"][0]
    assert "jane.doe" not in title and "aymane" not in title and len(title) <= planner.MAX_TITLE_CHARS


def test_heuristic_never_freezes_foreground_or_audio():
    task, snaps = scenario("login_bug")
    for s in snaps:
        s.last_focus_s, s.working_set_mb = 99999, 9999
    p = planner.heuristic_plan(task, snaps)
    actions = {d.app_id: d.action for d in p.decisions}
    assert actions["code.exe"] == "keep" and actions["spotify.exe"] == "keep"
