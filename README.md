# Focus Freeze

A Windows agent that understands what you're working on, freezes the apps you don't need (suspends them and pages their RAM to disk), and brings them back, with their state intact, when you say "I'm done".

Built for the Meta Global AI Developer Hackathon (Oct 26 – Nov 4, 2026). Problem statement 1: **Build for Your World**.

**Meta models:** `muse-spark-1.3` (planning, tool calling, vision), `muse-voice-transcribe-1.0` (voice commands).

> Status (Oct 30): the AI layer (role C) runs end to end on mock parts. A, B and D
> plug in on integration day (Nov 1). Planner accuracy 150/150 on 32 scenarios vs 51%
> for an idle timer; red-team 1 breach in 264 attack runs; about $0.002 per plan.

## Quick start
```
pip install -r requirements.txt
python -m pytest -q                                      # no API key needed
python -m freezer.agent "fixing the login bug" --scenario login_bug   # needs MODEL_API_KEY
python -m eval.run --offline                             # timer baseline, no API
python -m eval.redteam --offline                         # harness plumbing, no API
```

## Repo map
| Path | What |
|---|---|
| [plans/ram-freeze-agent.md](plans/ram-freeze-agent.md) | Full plan: feasibility test, evaluation, architecture, steps, budget, security |
| [plans/roles/](plans/roles/README.md) | Step-by-step guide for each of the 4 team roles |
| [freezer/contracts.py](freezer/contracts.py) | Shared types every role codes against |
| [freezer/planner.py](freezer/planner.py) | Muse Spark planner: injection-safe prompt, validation, heuristic fallback |
| [freezer/agent.py](freezer/agent.py) | Agent loop: sensor → planner → policy → consent card → actuator |
| [freezer/sessions.py](freezer/sessions.py), [freezer/cost.py](freezer/cost.py) | Focus sessions (user rules) and the spend cap |
| [freezer/mocks.py](freezer/mocks.py) | Stand-ins for A's and B's parts until integration day |
| [eval/](eval/) | Accuracy eval (`run.py`, `scenarios.json`) and red-team (`redteam.py`, `attacks.json`) |
| [tools/freeze_test.ps1](tools/freeze_test.ps1) | Feasibility prototype: 800 MB process → 0 MB in 90 ms |
