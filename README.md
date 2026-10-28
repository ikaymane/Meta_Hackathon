# Focus Freeze

A Windows agent that understands what you're working on, freezes the apps you don't need (suspends them and pages their RAM to disk), and brings them back, with their state intact, when you say "I'm done".

Built for the Meta Global AI Developer Hackathon (Oct 26 – Nov 4, 2026). Problem statement 1: **Build for Your World**.

**Meta models:** `muse-spark-1.3` (planning, tool calling, vision), `muse-voice-transcribe-1.0` (voice commands).

> Status: planning. No product code yet.

## Repo map
| Path | What |
|---|---|
| [plans/ram-freeze-agent.md](plans/ram-freeze-agent.md) | Full plan: feasibility test, evaluation, architecture, steps, budget, security |
| [plans/roles/](plans/roles/README.md) | Step-by-step guide for each of the 4 team roles |
| [freezer/contracts.py](freezer/contracts.py) | Shared types every role codes against |
| [tools/freeze_test.ps1](tools/freeze_test.ps1) | Feasibility prototype: 800 MB process → 0 MB in 90 ms |
