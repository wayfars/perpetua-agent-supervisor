"""Goal state: the one file the supervisor and every session agree on.

state.json is mutated from two processes (the supervisor between runs, the pi
extension during a run), so every mutation goes through patch() under a lock.
"""
from __future__ import annotations

from pathlib import Path

import pp_criteria
from pp_common import (UnsafeRunId, flock, now_iso, read_json, run_id,
                       write_json)

STATUS_IDLE = "idle"
STATUS_RUNNING = "running"
STATUS_ACCOMPLISHED = "accomplished"
STATUS_PAUSED = "paused"


def default_state(goal_id: str) -> dict:
    return {
        "goal_id": goal_id,
        "status": STATUS_IDLE,
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "run": 0,
        "phase": "bootstrap",
        # The STANDING persona. It persists across sessions until a session
        # reassigns it or the no-progress rule rotates it — a persona that
        # evaporated after one session would barely be used, and the sessions
        # that write them do not know they have to re-assign every time.
        "persona": None,
        # What the NEXT run should use. A session that wants to change the
        # terms of its successor writes here and then ends itself.
        "next": {
            "backend": None,          # workload class; None = goal default
            "session_id": None,       # set only to RESUME an existing session
            "resume_reason": None,    # why we are resuming rather than starting fresh
            "nudge": None,            # extra text prepended to the kickoff prompt
        },
        "current_runs": [],
        "criteria": [],
        "breaker": {
            "consecutive_failures": 0,
            "consecutive_no_progress": 0,
            "consecutive_wedges": 0,
            # Times in a row the supervisor stopped a run for a runaway turn (one
            # generation heading for pi's output ceiling). Reset by any run that
            # reaches a handoff; at 3 the goal pauses for a human.
            "consecutive_runaway": 0,
            # Runs in a row that claimed the probe/refusal exemption. Capped by
            # goal.json's probe_limit, so the exemption cannot become permanent.
            "consecutive_exempt": 0,
            "paused_reason": None,
        },
        # Whether check.sh passed last time it ran, so the machine channel can
        # report a TRANSITION rather than the same red result every session.
        "last_check_passed": None,
        "board_cursor": {},
        "stats": {"runs_completed": 0, "sessions_ended_cleanly": 0, "reaped": 0,
                  "wedges_healed": 0, "backend_swaps": 0},
    }


def path_for(goal: Path) -> Path:
    return goal / "state.json"


def load(goal: Path) -> dict:
    st = read_json(path_for(goal))
    if st is None:
        raise FileNotFoundError(f"no state.json in {goal} — run `perpetua new` first")
    return st


def save(goal: Path, state: dict) -> None:
    state["updated_at"] = now_iso()
    write_json(path_for(goal), state)


def patch(goal: Path, fn) -> dict:
    """Read-modify-write state.json under the goal lock. `fn` mutates in place."""
    with flock(goal / ".state.lock"):
        state = load(goal)
        fn(state)
        state["updated_at"] = now_iso()
        write_json(path_for(goal), state)
        return state


def is_paused(state: dict) -> bool:
    """A pause is durable and survives restarts; only `perpetua resume` clears it.

    Both the status and the reason are checked because the two can disagree: a
    supervisor killed between the two writes of an older build could leave a
    reason with a non-paused status, and running such a goal is the failure this
    guards against.
    """
    return (state.get("status") == STATUS_PAUSED
            or bool((state.get("breaker") or {}).get("paused_reason")))


def reset_next(state: dict) -> None:
    state["next"] = {
        "backend": None, "session_id": None, "resume_reason": None, "nudge": None,
        # #7: whether the NEXT run is a swarm. Listed explicitly so that
        # resetting `next` clears a swarm request too — a request that outlived
        # the run it was made for would swarm again unasked.
        "swarm": False,
    }


def current_runs(state: dict) -> list:
    """All in-flight run records: the new list, the legacy single dict, or [].

    `pp_state.load` has no migration step, and goals that have been running
    since before the rename carry `state["current_run"]` as one dict. A record
    that has lived for weeks must survive the rename, so both shapes are read
    forever — and anything that is neither reads as "nothing in flight".
    """
    runs = state.get("current_runs")
    single = state.get("current_run")
    if isinstance(runs, list):
        # Both shapes at once (D6 W5). set_current_runs pops the legacy key on
        # every write, so a state carrying both used to lose the legacy record
        # unmerged — silently, even when it still named a live process. Fold it
        # in on READ instead: the first write then persists it into the list and
        # the legacy key goes away having been kept, not dropped. A record whose
        # `n` will not parse has no key, so it can match nothing and is carried
        # through rather than judged a duplicate.
        if isinstance(single, dict):
            key = record_key(single)
            # A legacy record whose `n` will not parse can never be matched, so
            # it can never be dropped either — folding it in would make it a
            # permanent phantom that every start logs and the TUI shows forever.
            # W5 was about not losing a record that still names a live process;
            # a record that names nothing identifiable is not that.
            if key is None:
                return runs
            keys = {record_key(r) for r in runs if isinstance(r, dict)}
            if key not in keys:
                # FIRST, not last: callers that want "the current run" take
                # in_flight[-1], and the legacy record is by definition the
                # older shape — appending it would hand `perpetua tail` a stale
                # run in preference to the live one.
                return [single, *runs]
        return runs
    return [single] if isinstance(single, dict) else []


def set_current_runs(state: dict, runs: list) -> None:
    """The only writer of the in-flight list. The legacy key never lingers:
    a goal carrying `current_run` beside `current_runs` would hold two
    contradictory ownership claims, so the old key is dropped with every
    write — but never required to be absent on read.
    """
    state["current_runs"] = runs
    state.pop("current_run", None)


def record_key(rec: dict) -> str | None:
    """The canonical run id a record names, tolerantly.

    Records carry `n` as an int for ordinary runs and as a probe-shaped string
    ("0042.3") for swarm members, so the only identity spanning both is the
    canonical form. A record whose `n` will not parse is corrupt — None puts it
    out of every keyed match instead of raising out of a patch closure, which
    is failure class 3 applied to a state record.
    """
    raw = rec.get("n")
    if raw is None:
        return None
    try:
        return run_id(raw)
    except UnsafeRunId:
        return None


def upsert_current_run(state: dict, run_key: str, rec: dict) -> None:
    """Merge `rec` into the in-flight record named by run_key, or append it.

    "Merge" is the point: the record is minted without a pid and upgraded in
    place when the process identity arrives, so recording twice never leaves
    two records for one run. set_current_runs converts a legacy goal to the
    new shape by its first write — the migration is the write, not a step.
    """
    runs = current_runs(state)
    for i, cur in enumerate(runs):
        if record_key(cur) == run_key:
            runs[i] = {**cur, **rec}
            break
    else:
        runs.append(rec)
    set_current_runs(state, runs)


def drop_current_run(state: dict, run_key: str) -> None:
    """Remove exactly one in-flight record, keeping any others."""
    set_current_runs(state,
                     [cur for cur in current_runs(state)
                      if record_key(cur) != run_key])


def criteria_summary(state: dict) -> str:
    """The tree-aware rollup view (#2) — `perpetua status`, the dashboard's
    Overview tab, and the briefing's checklist all render exactly this, so a
    parent's roll-up shows up everywhere a criterion count already did. A
    goal with no `parent`/`weight` fields at all (every goal before #2) is a
    tree of roots with no children, which renders identically to the old flat
    list — no migration, no behaviour change for an unmodified goal.
    """
    return pp_criteria.render_tree(state.get("criteria") or [])
