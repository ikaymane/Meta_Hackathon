# Focus Freeze: team roles

Each file is a step-by-step guide for one teammate. Read yours first, then skim the others' "Hands to others" and dates.

| Role | File | One line |
|---|---|---|
| **A: Systems** | [A-systems.md](A-systems.md) | Freeze/restore engine, crash recovery, signed ledger, pressure guardian |
| **B: OS data + security lead** | [B-os-data-security.md](B-os-data-security.md) | App scanner, "never freeze" rules, threat model, red-team, audit log |
| **C: AI** | [C-ai.md](C-ai.md) | Muse Spark planner, injection-safe prompts, eval, sessions + memory, cost cap |
| **D: UI, voice, demo + Representative** | [D-ui-voice-demo.md](D-ui-voice-demo.md) | Tray, gauge, consent card, voice, demo VM, video, submission |

**Shared:** [`freezer/contracts.py`](../../freezer/contracts.py) holds the types everyone codes against. Full plan: [../ram-freeze-agent.md](../ram-freeze-agent.md).

## Key dates (ET)
| Date | Milestone |
|---|---|
| Wed Oct 28 | Team call: agree on contracts, public repo, everyone claims $150 credits |
| Thu Oct 29 noon | B ships fixtures to C |
| Sat Oct 31 | MVP feature freeze, demo VM + script draft ready |
| **Sun Nov 1** | **Integration day**: mocks → real parts |
| Tue Nov 3 | Record video |
| **Wed Nov 4 noon** | **Submit** (hard deadline 9 PM) |

## Team rules
- `main` always runs. Merge via PR with 1 review.
- A reviews any freeze or security PR.
- Contributor models only see synthetic data.
- 15-min daily sync. Blockers go in the chat immediately.
- Behind on Nov 1 → cut features, never crash recovery or the video.
