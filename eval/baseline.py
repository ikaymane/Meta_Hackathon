"""Keyword baseline for the eval only, never used by the product.

A transparent rule table: if it scores close to the model, the model isn't earning its
place. The tables were written looking at scenarios.json, so its score there flatters
it; eval/holdout.json is the fairer comparison. Matches app_id only, because a hostile
app picks its own display name.
"""
from __future__ import annotations

import re

from freezer.contracts import AppSnapshot, Decision, Plan
from freezer.planner import IDLE_FREEZE_S, SIZE_FREEZE_MB

CATEGORY_WORDS: dict[str, set[str]] = {
    "write": {"thesis", "write", "writing", "paper", "novel", "essay", "notes", "docs"},
    "slides": {"slides", "slide", "presentation", "sprint", "review", "talk"},
    "code": {"bug", "login", "api", "code", "coding", "app", "payments", "service",
             "crash", "android", "react", "course", "unity", "dev"},
    "data": {"churn", "model", "jupyter", "training", "budget", "taxes", "tax", "excel"},
    "study": {"study", "studying", "spanish", "vocabulary", "anki", "calculus", "research"},
    "media": {"vlog", "edit", "editing", "mix", "mixing", "track", "stream", "movie", "ableton"},
    "call": {"call", "client", "meeting", "zoom", "standup"},
    "play": {"play", "playing", "game", "gaming", "break", "elden"},
}
CATEGORY_APPS: dict[str, set[str]] = {
    "write": {"winword.exe", "zotero.exe", "acrord32.exe", "scrivener.exe", "onenote.exe"},
    "slides": {"powerpnt.exe"},
    "code": {"code.exe", "studio64.exe", "windowsterminal.exe", "postman.exe",
             "docker desktop.exe", "qemu-system-x86_64.exe", "unity.exe", "aseprite.exe"},
    "data": {"excel.exe", "msedge.exe", "chrome.exe", "acrobat.exe"},
    "study": {"anki.exe", "msedge.exe", "chrome.exe", "geogebra.exe"},
    "media": {"adobe premiere pro.exe", "ableton live 12 suite.exe", "obs64.exe", "vlc.exe"},
    "call": {"zoom.exe", "ms-teams.exe", "onenote.exe"},
    "play": {"eldenring.exe", "steam.exe", "valorant.exe"},
}


def rules_plan(task: str, snapshots: list[AppSnapshot]) -> Plan:
    words = set(re.findall(r"[a-z]+", task.lower()))
    keep = {a for cat, keys in CATEGORY_WORDS.items() if words & keys for a in CATEGORY_APPS[cat]}
    decisions = []
    for s in snapshots:
        named = any(len(w) >= 4 and w in s.app_id.lower() for w in words)
        if s.is_foreground or s.plays_audio or s.app_id in keep or named:
            decisions.append(Decision(s.app_id, "keep", "Matches the task category"))
        elif s.last_focus_s > IDLE_FREEZE_S and s.working_set_mb > SIZE_FREEZE_MB:
            decisions.append(Decision(s.app_id, "freeze", "Large and idle"))
        else:
            decisions.append(Decision(s.app_id, "keep", "Recently used or small"))
    return Plan(task=task, decisions=decisions, model="rules")
