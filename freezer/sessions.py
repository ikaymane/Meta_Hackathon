"""Named focus sessions and their user-set rules, in ~/.focusfreeze/sessions.json.

Security (T7, memory poisoning): every writer here must be called from an
explicit user action, a click on the consent card or a voice command. The
planner never imports this module; its remember_rule calls only come back as
RuleProposal, which the UI shows and confirm_proposal() saves on a click.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import asdict, fields
from pathlib import Path
from typing import Literal

from freezer.contracts import FocusSession, RuleProposal
from freezer.cost import home

log = logging.getLogger(__name__)

MAX_ID_LEN = 260
_FIELDS = {f.name for f in fields(FocusSession)}
# UI thread and guardian thread both write; the save is atomic, the read-modify-write isn't.
_lock = threading.RLock()


def path() -> Path:
    return home() / "sessions.json"


def _norm(name: str) -> str:
    name = " ".join(name.split()).lower()
    if not name:
        raise ValueError("session name is empty")
    return name


def _check_app_id(app_id: str) -> str:
    if not app_id or len(app_id) > MAX_ID_LEN or any(c in app_id for c in "\r\n\0"):
        raise ValueError(f"bad app_id: {app_id!r}")
    return app_id


def _parse(s: object) -> FocusSession:
    if not isinstance(s, dict):
        raise TypeError("session is not an object")
    # Ignore unknown keys so an older or newer file still loads.
    session = FocusSession(**{k: v for k, v in s.items() if k in _FIELDS})
    if not isinstance(session.name, str) or not isinstance(session.task, str):
        raise TypeError("name and task must be strings")
    # The file is user-writable, and rule app_ids go into the prompt: validate them.
    for rules in (session.keep_rules, session.never_freeze):
        if not isinstance(rules, list):
            raise TypeError("rules must be lists")
        for app_id in rules:
            if not isinstance(app_id, str):
                raise TypeError(f"rule app_id is not a string: {app_id!r}")
            _check_app_id(app_id)
    session.auto_freeze = session.auto_freeze is True
    return session


def _backup(p: Path) -> None:
    bad = p.with_name(f"sessions.bad-{int(time.time())}.json")
    try:
        p.replace(bad)
        log.error("sessions file was unreadable; moved it to %s", bad)
    except OSError:
        log.exception("sessions file was unreadable and could not be moved aside")


def load() -> list[FocusSession]:
    """Never raises: a broken file must not stop the agent. Bad entries are dropped
    one by one and logged, so one typo doesn't wipe every session's rules."""
    p = path()
    if not p.exists():
        return []
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise ValueError("top level is not a list")
    except (OSError, ValueError):
        _backup(p)
        return []
    out = []
    for i, s in enumerate(raw):
        try:
            out.append(_parse(s))
        except (TypeError, ValueError) as e:
            log.error("dropped session #%d from %s: %s", i, p, e)
    if len(out) < len(raw):
        # Keep a copy of the original before the next save overwrites it.
        try:
            p.with_name(f"sessions.bad-{int(time.time())}.json").write_text(
                json.dumps(raw, indent=1), encoding="utf-8")
        except OSError:
            log.exception("could not back up the sessions file")
    return out


def _save(sessions: list[FocusSession]) -> None:
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps([asdict(s) for s in sessions], indent=1), encoding="utf-8")
    os.replace(tmp, p)  # atomic: a crash leaves the old file or the new one, never half


def get(name: str) -> FocusSession | None:
    key = _norm(name)
    return next((s for s in load() if s.name == key), None)


def _update(name: str, change) -> FocusSession:
    key = _norm(name)
    with _lock:
        sessions = load()
        s = next((s for s in sessions if s.name == key), None)
        if s is None:
            raise KeyError(f"no session {key!r}")
        change(s)
        _save(sessions)
    return s


def start(name: str, task: str | None = None) -> FocusSession:
    """User said "start <name> session". Loads it, or creates it with the task."""
    key = _norm(name)
    with _lock:
        sessions = load()
        s = next((s for s in sessions if s.name == key), None)
        if s is None:
            s = FocusSession(name=key, task=task or key, keep_rules=[], never_freeze=[])
            sessions.append(s)
        elif task:
            s.task = task
        s.last_used = time.time()
        _save(sessions)
    return s


def add_rule(name: str, app_id: str, rule: Literal["keep", "never_freeze"]) -> FocusSession:
    """User unticked an app on the card, or said "never freeze X"."""
    _check_app_id(app_id)

    def change(s: FocusSession) -> None:
        target = s.keep_rules if rule == "keep" else s.never_freeze if rule == "never_freeze" else None
        if target is None:
            raise ValueError(f"unknown rule {rule!r}")
        if app_id not in target:
            target.append(app_id)
    return _update(name, change)


def remove_rule(name: str, app_id: str) -> FocusSession:
    def change(s: FocusSession) -> None:
        s.keep_rules = [a for a in s.keep_rules if a != app_id]
        s.never_freeze = [a for a in s.never_freeze if a != app_id]
    return _update(name, change)


def confirm_proposal(name: str, proposal: RuleProposal) -> FocusSession:
    """User clicked "yes" on a rule the planner proposed."""
    return add_rule(name, proposal.app_id, proposal.rule)


def set_auto_freeze(name: str, on: bool) -> FocusSession:
    def change(s: FocusSession) -> None:
        s.auto_freeze = on
    return _update(name, change)


def delete(name: str) -> None:
    key = _norm(name)
    with _lock:
        _save([s for s in load() if s.name != key])
