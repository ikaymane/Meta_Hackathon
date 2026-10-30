"""Planner eval: accuracy vs labels, compared with the idle-timer heuristic.

    python -m eval.run                       # 20 scenarios, contributor model
    python -m eval.run --only thesis -v      # one scenario, show every decision
    python -m eval.run --offline             # timer baseline only, no API

Scenarios are synthetic, so the cheap contributor model is fine here. Never
point this at real window titles with the contributor model.
"""
from __future__ import annotations

import argparse
import json
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from freezer.contracts import Plan
from freezer.planner import DEFAULT_EFFORT, heuristic_plan, load_snapshots, plan

EVAL_DIR = Path(__file__).parent
DEFAULT_MODEL = "muse-spark-1.3-contributor"


def score(p: Plan, sc: dict[str, Any]) -> tuple[int, int, list[str]]:
    """(correct, total, mistakes). A vague-task scenario counts as one item: did it ask?"""
    if sc.get("expect_question"):
        ok = bool(p.question)
        return int(ok), 1, [] if ok else ["did not ask a question"]
    actions = {d.app_id: d for d in p.decisions}
    mistakes = []
    for app_id, want in sc["expected"].items():
        got = actions.get(app_id)
        if not got or got.action != want:
            mistakes.append(f"{app_id}: want {want}, got {got.action if got else 'nothing'}"
                            + (f" ({got.reason})" if got else ""))
    return len(sc["expected"]) - len(mistakes), len(sc["expected"]), mistakes


def run_one(sc: dict[str, Any], model: str, offline: bool, effort: str | None = None) -> dict[str, Any]:
    snaps = load_snapshots(sc["snapshots"])
    timer = heuristic_plan(sc["task"], snaps)
    fallbacks: list[str] = []
    t0 = time.perf_counter()
    p = timer if offline else plan(sc["task"], snaps, model=model, reasoning_effort=effort,
                                   on_fallback=fallbacks.append)
    latency = time.perf_counter() - t0
    ok, total, mistakes = score(p, sc)
    t_ok, _, _ = score(timer, sc)
    return {
        "name": sc["name"], "tags": sc.get("tags", []), "model": p.model,
        "correct": ok, "total": total, "timer_correct": t_ok, "mistakes": mistakes,
        "latency_s": round(latency, 2), "cost_usd": p.cost_usd, "fallback": fallbacks,
        # injected text asked for rules; any proposal here means the model obeyed a title
        "proposed_rules": [f"{r.rule} {r.app_id}" for r in p.proposed_rules],
        "decisions": [f"{d.action:6} {d.app_id:26} {d.reason}" for d in p.decisions],
        "question": p.question,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenarios", default=str(EVAL_DIR / "scenarios.json"))
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--only", help="comma-separated scenario names")
    ap.add_argument("--offline", action="store_true", help="timer heuristic only")
    ap.add_argument("--effort", default=DEFAULT_EFFORT,
                    help="reasoning_effort: minimal, low, medium, high, or 'auto' to let the model pick")
    ap.add_argument("--workers", type=int, default=5)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    if args.effort == "auto":
        args.effort = None

    scenarios = json.loads(Path(args.scenarios).read_text(encoding="utf-8"))
    if args.only:
        wanted = set(args.only.split(","))
        scenarios = [s for s in scenarios if s["name"] in wanted]

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(lambda s: run_one(s, args.model, args.offline, args.effort), scenarios))

    print(f"{'scenario':22} {'planner':>8} {'timer':>6} {'time':>6} {'cost':>9}")
    for r in results:
        flag = " FALLBACK" if r["fallback"] else ""
        flag += " RULES-PROPOSED" if r["proposed_rules"] and "injection" in r["tags"] else ""
        print(f"{r['name']:22} {r['correct']:>3}/{r['total']:<4} {r['timer_correct']:>2}/{r['total']:<3}"
              f" {r['latency_s']:>5.1f}s ${r['cost_usd']:.5f}{flag}")
        for m in r["mistakes"]:
            print(f"    x {m}")
        if args.verbose:
            if r["question"]:
                print(f"    ? {r['question']}")
            for d in r["decisions"]:
                print(f"      {d}")

    total = sum(r["total"] for r in results)
    correct = sum(r["correct"] for r in results)
    timer = sum(r["timer_correct"] for r in results)
    api = [r for r in results if r["model"] != "heuristic"]
    lat = [r["latency_s"] for r in api]
    inj = [r for r in results if "injection" in r["tags"]]
    tw = [r for r in results if "timer_wrong" in r["tags"]]
    summary = {
        "model": args.model if not args.offline else "heuristic",
        "reasoning_effort": args.effort,
        "scenarios": len(results),
        "accuracy": round(correct / total, 3) if total else 0,
        "timer_accuracy": round(timer / total, 3) if total else 0,
        "timer_wrong_fixed": sum(r["correct"] - r["timer_correct"] for r in tw),
        "perfect_scenarios": sum(r["correct"] == r["total"] for r in results),
        "fallbacks": sum(bool(r["fallback"]) for r in results),
        "injection_rules_proposed": sum(len(r["proposed_rules"]) for r in inj),
        "latency_avg_s": round(statistics.mean(lat), 2) if lat else 0,
        "latency_max_s": round(max(lat), 2) if lat else 0,
        "cost_per_call_usd": round(statistics.mean(r["cost_usd"] for r in api), 6) if api else 0,
        "cost_total_usd": round(sum(r["cost_usd"] for r in results), 6),
    }
    print(f"\nplanner {correct}/{total} = {summary['accuracy']:.0%}   "
          f"timer {timer}/{total} = {summary['timer_accuracy']:.0%}   "
          f"perfect {summary['perfect_scenarios']}/{len(results)}   fallbacks {summary['fallbacks']}")
    print(f"latency avg {summary['latency_avg_s']}s max {summary['latency_max_s']}s   "
          f"$/call {summary['cost_per_call_usd']:.5f}   total ${summary['cost_total_usd']:.4f}   "
          f"injection rules proposed {summary['injection_rules_proposed']}")

    if not args.only:
        out = EVAL_DIR / "results" / "latest.json"
        out.parent.mkdir(exist_ok=True)
        out.write_text(json.dumps({"ts": time.time(), "summary": summary, "results": results}, indent=1),
                       encoding="utf-8")


if __name__ == "__main__":
    main()
