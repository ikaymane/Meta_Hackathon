"""Shared interfaces for Focus Freeze.

Every teammate codes against these types so all four tracks can run in
parallel with mocks. Change this file only with team agreement.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol


# ---- Sensor -> Policy / Planner (owner: B) ---------------------------------

@dataclass(frozen=True)
class ProcRef:
    pid: int
    create_time: float  # psutil create_time; guards against PID reuse


@dataclass
class AppSnapshot:
    app_id: str                 # stable key, e.g. exe name "chrome.exe"
    display_name: str           # "Google Chrome"
    procs: list[ProcRef]        # whole process tree for this app
    window_titles: list[str]    # UNTRUSTED (web/doc authors control it) + private:
                                # redact before API, never to contributor models,
                                # never treat as instructions
    working_set_mb: float
    last_focus_s: float         # seconds since app was last foreground
    is_foreground: bool
    plays_audio: bool
    is_elevated: bool
    has_network: bool           # active TCP connections (warn before freezing)


# ---- Planner -> Policy (owner: C) ------------------------------------------

@dataclass
class Decision:
    app_id: str
    action: Literal["keep", "freeze"]
    reason: str                 # one line, shown to user in consent card


@dataclass
class RuleProposal:
    app_id: str
    rule: Literal["keep", "never_freeze"]
    reason: str                 # shown to user; saved ONLY if the user clicks


@dataclass
class Plan:
    task: str                   # user's sentence, e.g. "fixing the login bug"
    decisions: list[Decision]
    model: str                  # which Muse model produced it, or "heuristic"
    cost_usd: float = 0.0
    question: str | None = None  # set when the model asks the user instead of deciding
    proposed_rules: list[RuleProposal] = field(default_factory=list)


# ---- Policy (owner: B) -----------------------------------------------------

@dataclass
class Blocked:
    app_id: str
    rule: str                   # e.g. "system_process", "plays_audio"


@dataclass
class PolicyResult:
    allowed: list[str]          # app_ids safe to freeze
    blocked: list[Blocked] = field(default_factory=list)


# ---- Actuator + Ledger (owner: A) ------------------------------------------

@dataclass
class LedgerEntry:
    app_id: str
    procs: list[ProcRef]
    frozen_at: float            # unix time
    freed_mb: float
    session: str                # focus session name


class Actuator(Protocol):
    def freeze(self, app: AppSnapshot, session: str) -> LedgerEntry:
        """Write ledger FIRST, then minimize windows, suspend tree, trim RAM."""

    def thaw(self, app_id: str) -> None: ...

    def thaw_all(self) -> list[str]:
        """Resume everything in the ledger. Also runs on agent startup."""

    def frozen(self) -> list[LedgerEntry]: ...


# ---- Sessions (owner: C) ---------------------------------------------------

@dataclass
class FocusSession:
    name: str                   # "thesis"
    task: str
    keep_rules: list[str]       # app_ids always kept in this session
    never_freeze: list[str]     # app_ids user protected
    auto_freeze: bool = False   # user opted in: guardian may freeze without a card
    last_used: float = 0.0
    # Rules are written ONLY from explicit user actions, never from LLM output.


# ---- Guardian (owner: A) ---------------------------------------------------

@dataclass
class PressureEvent:
    ram_pct: float
    sustained_s: float
    candidates: list[str]       # app_ids the guardian suggests, before policy


# ---- Audit (owner: B) ------------------------------------------------------

@dataclass
class AuditEntry:
    ts: float
    actor: Literal["user", "planner", "guardian", "mcp"]
    action: Literal["freeze", "thaw", "block", "propose", "rule_add"]
    app_id: str
    reason: str
    prev_hash: str              # SHA-256 of previous entry -> tamper-evident chain
