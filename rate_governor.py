"""Rate-limit governor for the fleet dispatcher.

Loads the top-level ``rate_limit`` block from ``fleet.json`` (defaults when
missing), detects 429 / rate-limit signals in captured worker output, and
computes exponential backoff for retrying tasks.

The dispatcher uses this to
  * cap concurrent ``running`` tasks (``max_concurrent``);
  * stagger task spawns by ``spawn_stagger_seconds`` so we don't burst-spawn
    N workers against the shared rate limit;
  * mark a task ``retrying`` (not ``failed``) when 429-style text appears in
    its terminal, sleep for an exponentially growing backoff, then re-queue.

Stdlib only.
"""

from __future__ import annotations

import json
from pathlib import Path


DEFAULTS = {
    "max_concurrent": 3,
    "spawn_stagger_seconds": 5,
    # How long without a fresh heartbeat (or terminal-output activity) before
    # the dispatcher flips ``attention: true`` on the agent record with reason
    # ``no heartbeat for Xm`` — see fleet.agent_stuck_reason. Kept in the
    # rate_limit block (rather than a sibling field) so a single fleet.json
    # section configures everything the dispatcher's safety governor needs.
    "stuck_after_seconds": 600,
    # T-06 failure & retry policy. ``max_retries`` is the number of retries
    # allowed before the dispatcher escalates a failed task (via notify_pm
    # or the attention bar). ``2`` means 1 initial attempt + 2 retries = 3
    # total attempts. The wait between retries reuses ``backoff`` below.
    "max_retries": 2,
    "backoff": {
        "initial_seconds": 30,
        "max_seconds": 600,
        "factor": 2.0,
    },
}

# Substrings whose presence (case-insensitive) in the worker terminal counts
# as a rate-limit hit. "429" alone is occasionally a false positive but is the
# canonical HTTP status workers report; tolerating an occasional spurious
# retry beats failing a task on a real rate-limit.
RATE_LIMIT_PATTERNS = (
    "429",
    "rate limit",
    "rate-limit",
    "ratelimit",
    "too many requests",
)


def _coerce(cfg: dict) -> dict:
    return {
        "max_concurrent": max(1, int(cfg["max_concurrent"])),
        "spawn_stagger_seconds": max(0.0, float(cfg["spawn_stagger_seconds"])),
        "stuck_after_seconds": max(1.0, float(cfg["stuck_after_seconds"])),
        "max_retries": max(0, int(cfg["max_retries"])),
        "backoff": {
            "initial_seconds": max(1.0, float(cfg["backoff"]["initial_seconds"])),
            "max_seconds": max(1.0, float(cfg["backoff"]["max_seconds"])),
            "factor": max(1.0, float(cfg["backoff"]["factor"])),
        },
    }


def _merge(raw: dict | None) -> dict:
    raw = raw or {}
    backoff = {**DEFAULTS["backoff"], **(raw.get("backoff") or {})}
    merged = {
        "max_concurrent": raw.get("max_concurrent", DEFAULTS["max_concurrent"]),
        "spawn_stagger_seconds": raw.get(
            "spawn_stagger_seconds", DEFAULTS["spawn_stagger_seconds"]
        ),
        "stuck_after_seconds": raw.get(
            "stuck_after_seconds", DEFAULTS["stuck_after_seconds"]
        ),
        "max_retries": raw.get("max_retries", DEFAULTS["max_retries"]),
        "backoff": backoff,
    }
    return _coerce(merged)


def load_rate_limit(config_path) -> dict:
    """Read top-level ``rate_limit`` from ``config_path``; missing / malformed yields defaults."""
    p = Path(config_path)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _merge(None)
    return _merge(data.get("rate_limit") if isinstance(data, dict) else None)


def detect_rate_limit(text: str) -> bool:
    """True iff ``text`` contains a 429 / rate-limit marker (case-insensitive)."""
    if not text:
        return False
    low = text.lower()
    return any(p in low for p in RATE_LIMIT_PATTERNS)


def compute_backoff_seconds(attempt: int, cfg: dict) -> float:
    """Backoff for the N-th retry. ``attempt`` is 1-indexed: 1 -> initial, 2 -> initial*factor, ..., capped at max."""
    if attempt < 1:
        attempt = 1
    b = cfg["backoff"]
    seconds = b["initial_seconds"] * (b["factor"] ** (attempt - 1))
    return float(min(seconds, b["max_seconds"]))
