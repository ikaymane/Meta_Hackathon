"""The agent loop: sensor -> planner -> policy -> consent card -> actuator.

Runs today on freezer.mocks; on integration day pass B's sensor/policy and A's
actuator instead, and D's consent card as the `consent` callback.

    python -m freezer.agent "fixing the login bug" --scenario login_bug
    python -m freezer.agent "writing my thesis" --scenario thesis --session thesis

Invariants:
- Policy runs after the planner, and the UI can only approve apps policy allowed.
- Nothing is frozen without consent, unless the user turned on auto_freeze
  for the active session and the trigger is the pressure guardian. Even then,
  at most MAX_AUTO_FREEZE apps per event; the rest still go to the card.
- Session rules are written here only from what the user did on the card.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Protocol

from freezer import sessions
from freezer.contracts import (Actuator, AppSnapshot, Blocked, Decision, FocusSession,
                               LedgerEntry, Plan, PolicyResult, PressureEvent)
from freezer.planner import DEFAULT_EFFORT, DEFAULT_MODEL, plan

log = logging.getLogger(__name__)

GUARDIAN_TASK = ("Free up memory. The user's current work is unknown: keep anything that "
                 "looks like active work and prefer freezing large idle unrelated apps.")
# auto_freeze has no card, so a manipulated plan can't freeze the whole desktop at once.
MAX_AUTO_FREEZE = 3


class Sensor(Protocol):
    def snapshot(self) -> list[AppSnapshot]: ...


Policy = Callable[[list[str], list[AppSnapshot]], PolicyResult]


@dataclass
class Proposal:
    """Everything the consent card shows."""
    plan: Plan
    to_freeze: list[Decision]       # planner said freeze and policy allowed it
    blocked: list[Blocked]          # planner said freeze, policy refused
    keep: list[Decision]
    free_mb: float
    disconnects: list[str]          # app_ids with network that will drop
    source: Literal["user", "guardian"] = "user"
    session: str | None = None

    @property
    def fallback_reason(self) -> str | None:
        """Show this on the card: the plan came from the simple rule, not the AI."""
        return self.plan.fallback_reason


@dataclass
class Outcome:
    proposal: Proposal | None = None
    frozen: list[LedgerEntry] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)  # approved, but the actuator raised
    question: str | None = None
    declined: bool = False


# Returns the app_ids the user approved (untick = leave out), or None to cancel.
Consent = Callable[[Proposal], "list[str] | None"]


class FocusAgent:
    def __init__(self, sensor: Sensor, policy: Policy, actuator: Actuator, consent: Consent, *,
                 model: str = DEFAULT_MODEL, effort: str | None = DEFAULT_EFFORT, client: Any = None,
                 on_fallback: Callable[[str], None] | None = None):
        self.sensor, self.policy, self.actuator, self.consent = sensor, policy, actuator, consent
        self.model, self.effort, self.client, self.on_fallback = model, effort, client, on_fallback
        self.session_name: str | None = None

    # ---- proposal ----------------------------------------------------------

    def propose(self, task: str, snaps: list[AppSnapshot], session: FocusSession | None,
                source: Literal["user", "guardian"] = "user",
                candidates: list[str] | None = None) -> Proposal:
        p = plan(task, snaps, session, model=self.model, reasoning_effort=self.effort, client=self.client,
                 on_fallback=self.on_fallback)
        wanted = [d for d in p.decisions if d.action == "freeze"
                  and (candidates is None or d.app_id in candidates)]
        verdict = self.policy([d.app_id for d in wanted], snaps)
        by_id = {s.app_id: s for s in snaps}
        allowed = {a for a in verdict.allowed if a in by_id}
        blocked = {b.app_id for b in verdict.blocked}
        to_freeze = [d for d in wanted if d.app_id in allowed]
        keep = []
        for d in p.decisions:
            if d.app_id in allowed or d.app_id in blocked:
                continue
            if d.action == "freeze":  # outside the guardian's candidates, or policy was silent
                outside = candidates is not None and d.app_id not in candidates
                d = Decision(d.app_id, "keep", "Not on the memory guardian's list" if outside
                             else "Policy did not clear it")
            keep.append(d)
        return Proposal(
            plan=p,
            to_freeze=to_freeze,
            blocked=verdict.blocked,
            keep=keep,
            free_mb=sum(by_id[d.app_id].working_set_mb for d in to_freeze),
            disconnects=[d.app_id for d in to_freeze if by_id[d.app_id].has_network],
            source=source,
            session=session.name if session else None,
        )

    def _execute(self, proposal: Proposal, snaps: list[AppSnapshot], approved: list[str]) -> Outcome:
        allowed = [d.app_id for d in proposal.to_freeze]
        approved_set = set(approved) & set(allowed)  # the UI cannot add apps policy refused
        if proposal.session and proposal.source == "user":
            for app_id in allowed:
                if app_id not in approved_set:  # user unticked it: remember for next time
                    sessions.add_rule(proposal.session, app_id, "keep")
        by_id = {s.app_id: s for s in snaps}
        frozen, failed = [], []
        for a in allowed:
            if a not in approved_set:
                continue
            try:  # one app failing must not leave the rest half-done and unreported
                frozen.append(self.actuator.freeze(by_id[a], proposal.session or ""))
            except Exception:
                log.exception("freeze failed for %s", a)
                failed.append(a)
        return Outcome(proposal=proposal, frozen=frozen, failed=failed)

    def _live(self, snaps: list[AppSnapshot]) -> list[AppSnapshot]:
        """Snapshot minus apps already in the ledger: never re-freeze a frozen app."""
        frozen_ids = {e.app_id for e in self.actuator.frozen()}
        return [s for s in snaps if s.app_id not in frozen_ids]

    # ---- entry points ------------------------------------------------------

    def focus(self, task: str, session_name: str | None = None) -> Outcome:
        """User: "I'm fixing the login bug" or "start thesis session"."""
        session = sessions.start(session_name, task) if session_name else None
        self.session_name = session.name if session else None
        snaps = self._live(self.sensor.snapshot())
        proposal = self.propose(session.task if session else task, snaps, session)
        if proposal.plan.question:
            return Outcome(proposal=proposal, question=proposal.plan.question)
        if not proposal.to_freeze:
            return Outcome(proposal=proposal)
        approved = self.consent(proposal)
        if approved is None:
            return Outcome(proposal=proposal, declined=True)
        return self._execute(proposal, snaps, approved)

    def on_pressure(self, event: PressureEvent) -> Outcome:
        """Guardian (A): RAM stayed high. Planner ranks the guardian's candidates."""
        session = sessions.get(self.session_name) if self.session_name else None
        live = self._live(self.sensor.snapshot())
        task = session.task if session else GUARDIAN_TASK
        proposal = self.propose(task, live, session, source="guardian", candidates=event.candidates)
        if not proposal.to_freeze or proposal.plan.question:
            return Outcome(proposal=proposal)
        if session and session.auto_freeze and len(proposal.to_freeze) <= MAX_AUTO_FREEZE:
            approved = [d.app_id for d in proposal.to_freeze]
        else:  # no auto_freeze, or it wants more than the no-card limit: ask
            approved = self.consent(proposal)
            if approved is None:
                return Outcome(proposal=proposal, declined=True)
        return self._execute(proposal, live, approved)

    def done(self) -> list[str]:
        """User: "I'm done". Thaw everything; works even if the planner is down."""
        self.session_name = None
        return self.actuator.thaw_all()


# ---- CLI (mock parts) -------------------------------------------------------

def _print_card(p: Proposal) -> None:
    head = f"Freeze {len(p.to_freeze)} apps, free ~{p.free_mb / 1024:.1f} GB?"
    print(f"\n{head}  [{p.plan.model}, ${p.plan.cost_usd:.5f}]")
    if p.fallback_reason:
        print(f"  ! AI planner unavailable ({p.fallback_reason}); this is the simple idle rule")
    for d in p.to_freeze:
        warn = "  (will disconnect)" if d.app_id in p.disconnects else ""
        print(f"  [x] {d.app_id:24} {d.reason}{warn}")
    for b in p.blocked:
        print(f"  [blocked] {b.app_id:18} policy: {b.rule}")
    for d in p.keep:
        print(f"  keep {d.app_id:23} {d.reason}")
    for r in p.plan.proposed_rules:
        print(f"  suggested rule: {r.rule} {r.app_id} ({r.reason})")


def _terminal_consent(auto_yes: bool) -> Consent:
    def consent(p: Proposal) -> list[str] | None:
        _print_card(p)
        ids = [d.app_id for d in p.to_freeze]
        if auto_yes:
            return ids
        ans = input("Enter = freeze all, n = cancel, or app_ids to untick (comma-separated): ").strip()
        if ans.lower() == "n":
            return None
        untick = {a.strip() for a in ans.split(",") if a.strip()}
        return [a for a in ids if a not in untick]
    return consent


def _main() -> None:
    import argparse

    from freezer.mocks import MockActuator, MockSensor, mock_policy

    ap = argparse.ArgumentParser(description="Run the agent loop on mock parts.")
    ap.add_argument("task")
    ap.add_argument("--scenario", required=True, help="scenario name from eval/scenarios.json")
    ap.add_argument("--session", help="focus session name")
    ap.add_argument("--model", default=DEFAULT_MODEL,
                    help="mock data is synthetic, so muse-spark-1.3-contributor is fine here")
    ap.add_argument("--yes", action="store_true", help="approve the card without asking")
    args = ap.parse_args()

    actuator = MockActuator()
    agent = FocusAgent(MockSensor(args.scenario), mock_policy, actuator, _terminal_consent(args.yes),
                       model=args.model, on_fallback=lambda w: print(f"[fallback] {w}"))
    out = agent.focus(args.task, args.session)
    for _ in range(3):  # vague task: the model asks, the user's answer becomes the task
        if not out.question or args.yes:
            break
        answer = input(f"\nAgent asks: {out.question}\n> ").strip()
        if not answer:
            break
        out = agent.focus(answer, args.session)
    if out.question:
        print(f"\nAgent asks: {out.question}")
    elif out.declined:
        print("\nCancelled, nothing frozen.")
    elif out.proposal and not out.proposal.to_freeze:
        print("\nNothing worth freezing.")
    else:
        print(f"\nFrozen: {[e.app_id for e in out.frozen]} "
              f"({sum(e.freed_mb for e in out.frozen) / 1024:.1f} GB)")
        print(f"\"I'm done\" -> thawed: {agent.done()}")


if __name__ == "__main__":
    _main()
