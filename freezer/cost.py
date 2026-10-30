"""Token prices, per-call usage log and a hard spend cap.

Every API call is appended to ~/.focusfreeze/usage.jsonl. Once the logged total
reaches the cap (default $20, env FF_COST_CAP_USD) the planner stops calling the
API and uses the heuristic instead.

Fails closed: if the log can't be written, usage_log_ok() turns False and the
planner stops calling the API, because spend it can't count it can't cap.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

# $ per million tokens: (input, cached input, output)
PRICES: dict[str, tuple[float, float, float]] = {
    "muse-spark-1.3": (1.25, 0.15, 4.25),
    "muse-spark-1.3-contributor": (0.10, 0.10, 0.20),
}
# Unknown model: charge the most expensive known price so the cap errs high.
_FALLBACK_PRICE = max(PRICES.values(), key=lambda p: p[2])
DEFAULT_CAP_USD = 20.0

_lock = threading.Lock()  # eval runs planner calls in a thread pool; keep log lines whole
_log_failed = False


def home() -> Path:
    return Path(os.environ.get("FF_HOME") or Path.home() / ".focusfreeze")


def usage_path() -> Path:
    return home() / "usage.jsonl"


def cap_usd() -> float:
    raw = os.environ.get("FF_COST_CAP_USD")
    if not raw:
        return DEFAULT_CAP_USD
    try:
        return float(raw)
    except ValueError:
        log.warning("FF_COST_CAP_USD=%r is not a number; using $%.2f", raw, DEFAULT_CAP_USD)
        return DEFAULT_CAP_USD


def call_cost(model: str, prompt_tokens: int, completion_tokens: int, cached_tokens: int = 0) -> float:
    if model not in PRICES:
        log.warning("no price for model %r; charging the highest known price", model)
    price_in, price_cached, price_out = PRICES.get(model, _FALLBACK_PRICE)
    fresh = max(prompt_tokens - cached_tokens, 0)
    return (fresh * price_in + cached_tokens * price_cached + completion_tokens * price_out) / 1_000_000


def record(model: str, prompt_tokens: int, completion_tokens: int, cached_tokens: int,
           latency_s: float) -> float:
    """Append one API call to the usage log and return its cost. Never raises."""
    global _log_failed
    cost = call_cost(model, prompt_tokens, completion_tokens, cached_tokens)
    entry = {"ts": time.time(), "model": model, "prompt_tokens": prompt_tokens,
             "cached_tokens": cached_tokens, "completion_tokens": completion_tokens,
             "cost_usd": round(cost, 8), "latency_s": round(latency_s, 3)}
    try:
        path = usage_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with _lock, path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        log.exception("can't write the usage log; API calls stop until restart")
        _log_failed = True
    return cost


def usage_log_ok() -> bool:
    return not _log_failed


def total_spent() -> float:
    path = usage_path()
    if not path.exists():
        return 0.0
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        log.exception("can't read the usage log; treating spend as over the cap")
        return float("inf")
    total, torn = 0.0, 0
    for line in text.splitlines():
        try:
            total += float(json.loads(line)["cost_usd"])
        except (ValueError, KeyError, TypeError):
            torn += 1  # a torn line must not crash the agent
    if torn:
        log.warning("%d unreadable lines in %s; spend may be undercounted", torn, path)
    return total


def over_cap() -> bool:
    return total_spent() >= cap_usd()
