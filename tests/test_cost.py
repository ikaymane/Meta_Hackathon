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


def test_default_cap_and_bad_cap_value(monkeypatch):
    assert cost.cap_usd() == cost.DEFAULT_CAP_USD == 20.0
    monkeypatch.setenv("FF_COST_CAP_USD", "twenty")
    assert cost.cap_usd() == 20.0  # warns instead of crashing the planner


def test_cap_is_inclusive(monkeypatch):
    spent = cost.record("muse-spark-1.3", 1000, 0, 0, 0.1)
    monkeypatch.setenv("FF_COST_CAP_USD", str(spent))
    assert cost.over_cap()


def test_unknown_model_is_priced_high():
    assert cost.call_cost("muse-spark-9", 1000, 1000) == cost.call_cost("muse-spark-1.3", 1000, 1000)


def test_unreadable_log_counts_as_over_cap(isolated_home):
    isolated_home.mkdir(parents=True)
    (isolated_home / "usage.jsonl").mkdir()  # exists but can't be read as a file
    assert cost.over_cap()


def test_unwritable_log_fails_closed(monkeypatch, isolated_home):
    isolated_home.mkdir(parents=True)
    (isolated_home / "blocker").write_text("")  # a file where the log's folder should be
    monkeypatch.setattr(cost, "usage_path", lambda: isolated_home / "blocker" / "usage.jsonl")
    task, snaps = scenario("login_bug")
    client = FakeClient(reply(call("freeze_apps", all_decisions(snaps))), reply(call("freeze_apps", all_decisions(snaps), 1)))
    planner.plan(task, snaps, client=client)          # this call can't be logged...
    assert not cost.usage_log_ok()
    p = planner.plan(task, snaps, client=client)      # ...so the next one never reaches the API
    assert p.model == "heuristic" and "usage log" in p.fallback_reason and len(client.requests) == 1


def test_log_recovers_after_a_passing_lock(monkeypatch, isolated_home):
    isolated_home.mkdir(parents=True)
    blocker = isolated_home / "blocker"
    blocker.write_text("")  # a file where the log's folder should be: writes fail
    monkeypatch.setattr(cost, "usage_path", lambda: blocker / "usage.jsonl")
    spent = cost.record("muse-spark-1.3", 1000, 100, 0, 1.0)
    assert not cost.usage_log_ok() and cost.total_spent() == pytest.approx(spent)  # still counted
    blocker.unlink()  # the lock goes away
    assert cost.usage_log_ok()
    assert len((blocker / "usage.jsonl").read_text().splitlines()) == 1 and cost._pending == []
