"""Token prices, per-call usage log and a hard spend cap.

Every API call is appended to ~/.focusfreeze/usage.jsonl. Once the logged total
reaches the cap (default $20, env FF_COST_CAP_USD) the planner stops calling the
API and uses the heuristic instead.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

# $ per million tokens: (input, cached input, output)
PRICES: dict[str, tuple[float, float, float]] = {
    "muse-spark-1.3": (1.25, 0.15, 4.25),
    "muse-spark-1.3-contributor": (0.10, 0.10, 0.20),
}
DEFAULT_CAP_USD = 20.0


def home() -> Path:
    return Path(os.environ.get("FF_HOME") or Path.home() / ".focusfreeze")


def usage_path() -> Path:
    return home() / "usage.jsonl"


def cap_usd() -> float:
    return float(os.environ.get("FF_COST_CAP_USD", DEFAULT_CAP_USD))


def call_cost(model: str, prompt_tokens: int, completion_tokens: int, cached_tokens: int = 0) -> float:
    price_in, price_cached, price_out = PRICES.get(model, PRICES["muse-spark-1.3"])
    fresh = max(prompt_tokens - cached_tokens, 0)
    return (fresh * price_in + cached_tokens * price_cached + completion_tokens * price_out) / 1_000_000


def record(model: str, prompt_tokens: int, completion_tokens: int, cached_tokens: int,
           latency_s: float) -> float:
    """Append one API call to the usage log and return its cost."""
    cost = call_cost(model, prompt_tokens, completion_tokens, cached_tokens)
    path = usage_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {"ts": time.time(), "model": model, "prompt_tokens": prompt_tokens,
             "cached_tokens": cached_tokens, "completion_tokens": completion_tokens,
             "cost_usd": round(cost, 8), "latency_s": round(latency_s, 3)}
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
    return cost


def total_spent() -> float:
    path = usage_path()
    if not path.exists():
        return 0.0
    total = 0.0
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            total += float(json.loads(line)["cost_usd"])
        except (ValueError, KeyError, TypeError):
            continue  # a torn line must not crash the agent
    return total


def over_cap() -> bool:
    return total_spent() >= cap_usd()
