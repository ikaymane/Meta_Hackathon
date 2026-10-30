import json

import pytest

from freezer import cost, planner
from tests.test_planner import FakeClient, all_decisions, call, reply, scenario


def test_cached_tokens_are_cheaper():
    full = cost.call_cost("muse-spark-1.3", 3000, 500)
    cached = cost.call_cost("muse-spark-1.3", 3000, 500, cached_tokens=2500)
    assert cached < full
    assert cached == pytest.approx((500 * 1.25 + 2500 * 0.15 + 500 * 4.25) / 1e6)


def test_every_call_is_logged(isolated_home):
    task, snaps = scenario("login_bug")
    planner.plan(task, snaps, client=FakeClient(reply(call("freeze_apps", all_decisions(snaps)))))
    lines = (isolated_home / "usage.jsonl").read_text().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["prompt_tokens"] == 3000 and entry["cost_usd"] > 0
    assert cost.total_spent() == pytest.approx(entry["cost_usd"])


def test_over_cap_uses_heuristic_without_calling_api(monkeypatch):
    monkeypatch.setenv("FF_COST_CAP_USD", "0.001")
    cost.record("muse-spark-1.3", 3000, 500, 0, 1.0)  # ~$0.006, over the cap
    task, snaps = scenario("login_bug")
    client = FakeClient()  # would raise IndexError if called
    reasons = []
    p = planner.plan(task, snaps, client=client, on_fallback=reasons.append)
    assert p.model == "heuristic" and client.requests == []
    assert "spend cap" in reasons[0]


def test_torn_log_line_is_ignored(isolated_home):
    cost.record("muse-spark-1.3", 1000, 100, 0, 1.0)
    with (isolated_home / "usage.jsonl").open("a") as f:
        f.write('{"cost_usd": 0.5')  # crash mid-write
    assert cost.total_spent() == pytest.approx(cost.call_cost("muse-spark-1.3", 1000, 100))
