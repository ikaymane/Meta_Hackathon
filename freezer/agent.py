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
- Session rules are never written here. Outcome.unticked goes back to the UI, which
  asks "Never freeze this during <session>?" and calls sessions.add_rule on yes.
- A task switch can thaw a frozen app, but only when the model explicitly keeps it
  for the new task; a heuristic plan never thaws anything.
"""
from __future__ import annotations

import logging
import os
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Protocol

from freezer import sessions
from freezer.contracts import (Actuator, AppSnapshot, Blocked, Decision, FocusSession,
                               LedgerEntry, Plan, PolicyResult, PressureEvent)
from freezer.planner import DEFAULT_EFFORT, DEFAULT_MODEL, SKIPPED_REASON, plan

log = logging.getLogger(__name__)

GUARDIAN_TASK = ("Free up memory. The user's current work is unknown: keep anything that "
                 "looks like active work and prefer freezing large idle unrelated apps.")
# auto_freeze has no card, so a manipulated plan can't freeze the whole desktop at once.
MAX_AUTO_FREEZE = 3
# Rolling rate limit (handoff patch 01, re-applied): at most this many freezes per
# rolling minute across calls, so repeated triggers cannot strip the machine unattended.
MAX_FREEZES_PER_MIN = 10
# Work tools are never frozen without a card: killing a terminal or IDE can lose
# running work (dev server, debug session) that the sensor cannot see, and titles
# on these apps are attacker-writable (red-team gap-03). Matched by app_id only;
# titles and display names play no part. B's real policy owns the full list.
WORK_TOOLS_NO_AUTO = frozenset({"windowsterminal.exe", "code.exe", "studio64.exe"})
# The guardian never touches an app used in the last 5 minutes. Idle time comes from the
# sensor, so a title like "server stopped, terminal not in use" can't fake it (red-team gap-03).
GUARDIAN_MIN_IDLE_S = 5 * 60

# Plain words for the card (D renders these; policy rule ids are B's).
BLOCK_REASONS = {
    "system_process": "Part of Windows", "security": "Security software",
    "foreground": "You're using it right now", "plays_audio": "Playing sound",
    "elevated": "Runs as administrator", "self": "Focus Freeze itself",
    "unknown_app": "Closed or not found",
    "rate_limited": "Too many freezes in a minute",
}
POLICY_SILENT = "Kept for safety"


def block_reason(rule: str) -> str:
    return BLOCK_REASONS.get(rule, "Protected by a safety rule")


def fallback_message(reason: str | None) -> str | None:
    """Card banner when the plan came from the simple idle rule instead of the AI."""
    if not reason:
        return None
    if "spend cap" in reason:
        why = "the AI budget for this month is used up"
    elif "no API client" in reason:
        why = "the AI isn't set up on this PC"
    elif "usage log" in reason:
        why = "Focus Freeze can't record AI costs right now"
    elif "trains on what it receives" in reason:
        why = "the AI learns from what it sees, so app titles were held back for privacy"
    elif "FF_PAID_BALANCE" in reason:
        why = "the AI balance needs confirming, so app titles were held back"
    else:
        why = "the AI didn't answer in time"
    return f"Simple mode: {why}, so this list uses idle time only. Check it before you freeze."


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
    thaw: list[Decision] = field(default_factory=list)  # frozen apps the new task needs back
    apps: dict[str, AppSnapshot] = field(default_factory=dict)  # display_name, MB for the card

    @property
    def fallback_reason(self) -> str | None:
        """Technical reason the plan came from the simple rule (logs); see fallback_message."""
        return self.plan.fallback_reason

    @property
    def fallback_message(self) -> str | None:
        """The same, in words for the card banner."""
        return fallback_message(self.plan.fallback_reason)


@dataclass
class Outcome:
    proposal: Proposal | None = None
    frozen: list[LedgerEntry] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)  # approved, but the actuator raised
    unticked: list[str] = field(default_factory=list)  # on the card but left out: UI may offer a rule
    skipped: list[Blocked] = field(default_factory=list)  # approved, but policy said no at freeze time
    thawed: list[str] = field(default_factory=list)
    question: str | None = None
    declined: bool = False
    note: str | None = None          # why nothing happened, when it wasn't a failure


# Returns the app_ids the user approved (untick = leave out), or None to cancel.
Consent = Callable[[Proposal], "list[str] | None"]


class FocusAgent:
    def __init__(self, sensor: Sensor, policy: Policy, actuator: Actuator, consent: Consent, *,
                 model: str = DEFAULT_MODEL, effort: str | None = DEFAULT_EFFORT, client: Any = None,
                 on_fallback: Callable[[str], None] | None = None,
                 on_progress: Callable[[str, int], None] | None = None):
        self.sensor, self.policy, self.actuator, self.consent = sensor, policy, actuator, consent
        self.model, self.effort, self.client, self.on_fallback = model, effort, client, on_fallback
        self.on_progress = on_progress
        self.session_name: str | None = None
        # Rolling rate limit: freeze timestamps (unix seconds) spent by _execute.
        self._freeze_times: deque[float] = deque()

    # ---- proposal ----------------------------------------------------------

    def propose(self, task: str, snaps: list[AppSnapshot], session: FocusSession | None,
                source: Literal["user", "guardian"] = "user",
                candidates: list[str] | None = None, frozen: set[str] | frozenset = frozenset()) -> Proposal:
        """frozen = app_ids already in the ledger: shown to the planner, never frozen twice."""
        p = plan(task, snaps, session, model=self.model, reasoning_effort=self.effort, client=self.client,
                 on_fallback=self.on_fallback, frozen=frozen, on_progress=self.on_progress)
        wanted = [d for d in p.decisions if d.action == "freeze" and d.app_id not in frozen
                  and (candidates is None or d.app_id in candidates)]
        verdict = self.policy([d.app_id for d in wanted], snaps)
        by_id = {s.app_id: s for s in snaps}
        allowed = {a for a in verdict.allowed if a in by_id}
        blocked = {b.app_id for b in verdict.blocked}
        to_freeze = [d for d in wanted if d.app_id in allowed]
        thaw = [d for d in p.decisions if d.app_id in frozen and d.action == "keep"
                and p.model != "heuristic" and d.reason != SKIPPED_REASON]
        keep = []
        for d in p.decisions:
            if d.app_id in allowed or d.app_id in blocked or d.app_id in frozen:
                continue
            if d.action == "freeze":  # policy was silent: keep to be safe
                d = Decision(d.app_id, "keep", POLICY_SILENT)
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
            thaw=thaw,
            apps=by_id,
        )

    def _execute(self, proposal: Proposal, approved: list[str]) -> Outcome:
        allowed = [d.app_id for d in proposal.to_freeze]
        approved_set = set(approved) & set(allowed)  # the UI cannot add apps policy refused
        # The card may have been open for a while: re-check against a fresh snapshot, so an
        # app that became foreground, started playing sound or closed is not frozen.
        by_id = {s.app_id: s for s in self.sensor.snapshot()}
        ids = [a for a in allowed if a in approved_set]
        recheck = self.policy([a for a in ids if a in by_id], list(by_id.values()))
        ok = {a for a in recheck.allowed if a in by_id}
        skipped = list(recheck.blocked) + [Blocked(a, "unknown_app") for a in ids if a not in by_id]
        # Rolling rate limit, in plan order: over-limit approvals are reported as
        # skipped, never silently dropped.
        ready = [a for a in ids if a in ok]
        now = time.time()
        while self._freeze_times and now - self._freeze_times[0] >= 60:
            self._freeze_times.popleft()
        attempt = ready[:max(0, MAX_FREEZES_PER_MIN - len(self._freeze_times))]
        over = [a for a in ready if a not in attempt]
        skipped += [Blocked(a, "rate_limited") for a in over]
        if over:
            log.info("rate limit: freezing %d of %d approved apps", len(attempt), len(ready))
        frozen, failed = [], []
        for a in attempt:
            try:  # one app failing must not leave the rest half-done and unreported
                frozen.append(self.actuator.freeze(by_id[a], proposal.session or ""))
            except Exception:
                log.exception("freeze failed for %s", a)
                failed.append(a)
        for _ in frozen:
            self._freeze_times.append(time.time())
        return Outcome(proposal=proposal, frozen=frozen, failed=failed, skipped=skipped,
                       unticked=[a for a in allowed if a not in approved_set])

    def _thaw(self, decisions: list[Decision]) -> list[str]:
        thawed = []
        for d in decisions:
            try:
                self.actuator.thaw(d.app_id)
                thawed.append(d.app_id)
            except Exception:
                log.exception("thaw failed for %s", d.app_id)
        return thawed

    def _live(self, snaps: list[AppSnapshot]) -> list[AppSnapshot]:
        """Snapshot minus apps already in the ledger: never re-freeze a frozen app."""
        frozen_ids = {e.app_id for e in self.actuator.frozen()}
        return [s for s in snaps if s.app_id not in frozen_ids]

    # ---- entry points ------------------------------------------------------

    def focus(self, task: str | None, session_name: str | None = None) -> Outcome:
        """User: "I'm fixing the login bug", or "start thesis session" (task=None keeps the
        session's saved task; a task replaces it)."""
        if not session_name and not (task and task.strip()):
            raise ValueError("say what you're working on, or name a session")
        session = sessions.start(session_name, task) if session_name else None
        self.session_name = session.name if session else None
        snaps = self.sensor.snapshot()
        frozen = {e.app_id for e in self.actuator.frozen()}
        proposal = self.propose(session.task if session else task, snaps, session, frozen=frozen)
        if proposal.plan.question:
            return Outcome(proposal=proposal, question=proposal.plan.question)
        thawed = self._thaw(proposal.thaw)  # bringing an app back needs no consent
        if not proposal.to_freeze:
            return Outcome(proposal=proposal, thawed=thawed)
        approved = self.consent(proposal)
        if approved is None:
            return Outcome(proposal=proposal, declined=True, thawed=thawed)
        out = self._execute(proposal, approved)
        out.thawed = thawed
        return out

    def on_pressure(self, event: PressureEvent) -> Outcome:
        """Guardian (A): RAM stayed high. Planner ranks the guardian's candidates."""
        session = sessions.get(self.session_name) if self.session_name else None
        live = self._live(self.sensor.snapshot())
        idle = {s.app_id for s in live if s.last_focus_s >= GUARDIAN_MIN_IDLE_S and not s.is_foreground}
        candidates = [c for c in event.candidates if c in idle]
        if dropped := [c for c in event.candidates if c not in idle]:
            log.info("guardian skipped recently used or unknown apps: %s", dropped)
        if not candidates:
            # No planner call, no card. Demo tip: open the distractions 5+ min beforehand.
            return Outcome(note=f"guardian skipped: no candidate idle for {GUARDIAN_MIN_IDLE_S // 60}+ min")
        task = session.task if session else GUARDIAN_TASK
        # The planner ranks ONLY the guardian's candidates. Non-candidate titles
        # never reach the model, so a lying title on an app the guardian did not
        # pick (red-team gap-03) cannot steer the plan. Candidates themselves come
        # from sensor idle seconds, never from text. Policy + freeze-time re-check
        # still apply to the result.
        plan_snaps = [s for s in live if s.app_id in set(candidates)]
        proposal = self.propose(task, plan_snaps, session, source="guardian", candidates=candidates)
        if not proposal.to_freeze or proposal.plan.question:
            return Outcome(proposal=proposal)
        tools = [d.app_id for d in proposal.to_freeze if d.app_id.lower() in WORK_TOOLS_NO_AUTO]
        if session and session.auto_freeze and len(proposal.to_freeze) <= MAX_AUTO_FREEZE and not tools:
            approved = [d.app_id for d in proposal.to_freeze]
        else:  # no auto_freeze, over the no-card limit, or a work tool needs eyes: ask
            if tools:
                log.info("guardian shows card: auto_freeze skips work tools: %s", tools)
            approved = self.consent(proposal)
            if approved is None:
                return Outcome(proposal=proposal, declined=True)
        return self._execute(proposal, approved)

    def done(self) -> list[str]:
        """User: "I'm done". Thaw everything; works even if the planner is down."""
        self.session_name = None
        return self.actuator.thaw_all()


# ---- CLI (mock parts) -------------------------------------------------------

def _print_card(p: Proposal) -> None:
    n = len(p.to_freeze)
    head = f"Freeze {n} app{'' if n == 1 else 's'}, free ~{p.free_mb / 1024:.1f} GB?"
    print(f"\n{head}  [{p.plan.model}, ${p.plan.cost_usd:.5f}]")
    if p.fallback_message:
        print(f"  ! {p.fallback_message}")
    for d in p.to_freeze:
        warn = "  (will disconnect)" if d.app_id in p.disconnects else ""
        print(f"  [x] {d.app_id:24} {d.reason}{warn}")
    for b in p.blocked:
        print(f"  [blocked] {b.app_id:18} {block_reason(b.rule)}")
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
    os.environ["FF_SYNTHETIC_DATA"] = "1"  # this CLI only ever sees mock data
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
