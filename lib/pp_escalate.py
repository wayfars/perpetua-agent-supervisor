"""Per-goal escalation ledger — the durable half of severity-routed pausing.

Failure handling used to be binary: pause, `pp_notify.notify()` once, sit. On
an unattended multi-day goal that is the whole ball game — a goal that pauses
at 2am with the phone on silent is dead until a human happens to look.

`escalations.jsonl` is house rule #2's shape: an append-only log that is the
truth, and `escalations.json` a rebuildable fold over it, tolerant of a torn
final line the same way the board and the assignment ledger are. Records are
never edited; a bump or an acknowledgement is a new event that references an
earlier one's `n`, and the fold decides what the record looks like now.

Nothing here decides WHEN to bump — that is `check_and_bump`, called from
`perpetuad`'s own startup path (a paused goal's supervisor exits before its
loop ever starts, so "the supervisor started" is the only event this can ride
on) and, as a fallback if that proves too rare in practice, from
`perpetua escalate-check`, which needs no supervisor lock at all because it
only ever reads and, best-effort, writes this ledger and calls `pp_notify`.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from pp_common import flock, log, now_iso, read_json, write_json
import pp_notify

LOG_NAME = "escalations.jsonl"
INDEX_NAME = "escalations.json"
LOCK_NAME = ".escalate.lock"

#: Two bumps, never more: WARN -> URGENT is the ceiling, so a goal ignored
#: long enough eventually goes silent rather than paging forever.
MAX_BUMPS = 2
LEVELS = pp_notify.LEVELS   # (INFO, WARN, URGENT) — one vocabulary, not two

#: goal.json's `escalation_stale_s` default: how long an unacknowledged
#: escalation sits before it bumps itself one severity.
DEFAULT_STALE_S = 4 * 3600


def _lock(goal: Path) -> Path:
    return goal / LOCK_NAME


def log_path(goal: Path) -> Path:
    return goal / LOG_NAME


def index_path(goal: Path) -> Path:
    return goal / INDEX_NAME


def _iter_events(goal: Path):
    """Every event in append order, tolerating a torn final line.

    Same rule as the board and the assignment ledger: the append and a
    reader are different processes, and a line half-written when one died is
    the end of history, not corruption.
    """
    p = log_path(goal)
    if not p.exists():
        return
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        log(f"escalate: cannot read {p.name}: {type(exc).__name__}: {exc}")
        return
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue              # a torn line must never break the ledger


def _n(ev: dict) -> int | None:
    try:
        return int(ev.get("n"))
    except (TypeError, ValueError):
        return None


def fold(events) -> list[dict]:
    """The escalation ledger, folded from the log: a list ordered by `n`.

    An event for an `n` never opened is ignored — the fold cannot attach a
    bump or an ack to a record it never saw, the same tolerance the
    assignment ledger applies to a claim with no matching open.
    """
    by_n: dict[int, dict] = {}
    for ev in events:
        n = _n(ev)
        if n is None:
            continue
        kind = ev.get("type")
        if kind == "open":
            by_n[n] = {"n": n, "at": ev.get("at"), "kind": ev.get("kind"),
                      "level": ev.get("level") or pp_notify.WARN,
                      "text": ev.get("text", ""),
                      "acknowledged_at": None, "bumps": 0,
                      # When this record last SAID something. The staleness
                      # clock runs from here, not from `at`: measuring from the
                      # open time means that once a goal is stale at all, every
                      # subsequent check bumps it again — the whole ladder fires
                      # within seconds and "4 hours later, louder" becomes
                      # "WARN and URGENT at once".
                      "last_at": ev.get("at")}
        elif kind == "bump":
            rec = by_n.get(n)
            if rec is None:
                continue
            rec["level"] = ev.get("level", rec["level"])
            rec["bumps"] = int(ev.get("bumps", rec.get("bumps", 0)) or 0)
            rec["last_at"] = ev.get("at") or rec.get("last_at")
        elif kind == "ack":
            rec = by_n.get(n)
            if rec is None:
                continue
            rec["acknowledged_at"] = ev.get("at")
    return [by_n[n] for n in sorted(by_n)]


def index(goal: Path) -> list[dict]:
    """The derived index — reads the cache, or folds the log if it is missing."""
    cached = read_json(index_path(goal))
    if isinstance(cached, list):
        return cached
    return fold(_iter_events(goal))


def rebuild(goal: Path) -> list[dict]:
    with flock(_lock(goal)):
        idx = fold(_iter_events(goal))
        write_json(index_path(goal), idx)
    return idx


def _append(goal: Path, events: list, event: dict) -> list[dict]:
    with log_path(goal).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False) + "\n")
    idx = fold(events + [event])
    write_json(index_path(goal), idx)
    return idx


def open_escalation(goal: Path, *, kind: str, level: str = pp_notify.WARN,
                    text: str) -> dict:
    """Open a new escalation. Pausing a goal always opens one at WARN."""
    if level not in LEVELS:
        level = pp_notify.WARN
    with flock(_lock(goal)):
        events = list(_iter_events(goal))
        n = sum(1 for e in events if e.get("type") == "open") + 1
        event = {"type": "open", "n": n, "at": now_iso(), "kind": kind,
                 "level": level, "text": text}
        idx = _append(goal, events, event)
    return next(r for r in idx if r["n"] == n)


def latest_unacknowledged(goal: Path) -> dict | None:
    unacked = [r for r in index(goal) if not r.get("acknowledged_at")]
    return unacked[-1] if unacked else None


def acknowledge(goal: Path, *, n: int | None = None) -> dict | None:
    """Mark an escalation seen: `perpetua resume` and `perpetua ack` both call
    this. Returns None when there is nothing unacknowledged to mark — a
    goal that was never escalated, or one already acknowledged."""
    with flock(_lock(goal)):
        events = list(_iter_events(goal))
        idx = fold(events)
        target_n = n
        if target_n is None:
            # The latest unacknowledged one, or — calling this twice must be
            # harmless, not a "nothing to do" that loses the record — the
            # latest one at all when everything is already acked.
            unacked = [r for r in idx if not r.get("acknowledged_at")]
            if unacked:
                target_n = unacked[-1]["n"]
            elif idx:
                target_n = idx[-1]["n"]
            else:
                return None
        rec = next((r for r in idx if r["n"] == target_n), None)
        if rec is None:
            return None
        if rec.get("acknowledged_at"):
            return rec
        event = {"type": "ack", "n": target_n, "at": now_iso()}
        idx = _append(goal, events, event)
    return next(r for r in idx if r["n"] == target_n)


def bump(goal: Path, *, n: int | None = None) -> dict | None:
    """Escalate one record one severity, capped at MAX_BUMPS. Returns the
    updated record, or None if there is nothing eligible: already
    acknowledged, already at the cap, or no such record."""
    with flock(_lock(goal)):
        events = list(_iter_events(goal))
        idx = fold(events)
        rec = (next((r for r in idx if r["n"] == n), None) if n is not None
               else next((r for r in reversed(idx)
                          if not r.get("acknowledged_at")), None))
        if rec is None or rec.get("acknowledged_at"):
            return None
        if int(rec.get("bumps", 0)) >= MAX_BUMPS:
            return None
        cur = LEVELS.index(rec["level"]) if rec["level"] in LEVELS else 1
        new_level = LEVELS[min(cur + 1, len(LEVELS) - 1)]
        event = {"type": "bump", "n": rec["n"], "at": now_iso(),
                 "level": new_level, "bumps": int(rec.get("bumps", 0)) + 1}
        idx = _append(goal, events, event)
    return next(r for r in idx if r["n"] == rec["n"])


def _parse_iso(text) -> datetime | None:
    if not text:
        return None
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None


def check_and_bump(goal: Path, *, stale_s: float = DEFAULT_STALE_S,
                   notify_enabled: bool = False) -> dict | None:
    """If the latest unacknowledged escalation has been silent for longer than
    `stale_s`, bump it one severity and re-notify. Returns the updated record,
    or None when there was nothing to do: no open escalation, not stale yet,
    already acknowledged, or the bump cap is already spent — the ceiling is one
    URGENT and then silence, on purpose, not an alarm that never stops.

    "Silent for" is measured from the record's LAST event (`last_at` — its open,
    or its most recent bump), not from when it was opened. Measuring from the
    open time makes the interval one-shot rather than recurring: the moment a
    goal is stale at all, every later check finds it stale again and bumps
    immediately, so both rungs of the ladder fire seconds apart and the
    escalation says nothing that the first notification did not.
    """
    rec = latest_unacknowledged(goal)
    if rec is None:
        return None
    spoke = _parse_iso(rec.get("last_at") or rec.get("at"))
    if spoke is None:
        return None
    age = (datetime.now(timezone.utc) - spoke).total_seconds()
    if age < stale_s:
        return None
    bumped = bump(goal, n=rec["n"])
    if bumped is None:
        return None
    pp_notify.notify(
        f"⏫ perpetua '{goal.name}' escalation bumped to {bumped['level']} "
        f"after {int(age // 60)} minute(s) unacknowledged: {bumped['text']}",
        level=bumped["level"], enabled=notify_enabled)
    return bumped
