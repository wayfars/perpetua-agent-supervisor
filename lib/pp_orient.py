"""Cheap mid-session re-orientation (#8).

Gas Town's Propulsion Principle — "if you find something on your hook, YOU
RUN IT" — is backed by cheap orientation commands (`gt hook`, `gt prime`) so
an agent looks things up instead of trying to remember them. Perpetua's
briefing is the opposite shape: one large document, read once at spawn, and
everything after that is the session's own decaying memory of it. `orient`
is the cheap look-again — regenerated from the same sources as the briefing,
capped at roughly a screenful, and cheap enough to call every few turns
instead of only once.

Read-only by construction: nothing here writes a file, patches state, or
posts anywhere. That is what makes it safe to hand to a swarm probe (whose
forbidden list exists specifically to stop it changing anything the goal or
the other probes inherit — reading is not that) and to a watcher's run.sh
(which has no live session to attribute a write to, but can still legitimately
want to know what a check just found).

It reads `state.json` FRESH on every call, not a value captured at launch —
the entire point is that a session which has just ticked a criterion, or
whose predecessor's next_steps it is trying to recall, sees that reflected
immediately rather than the stale picture its own briefing was built from.
"""
from __future__ import annotations

import time
from pathlib import Path

import pp_criteria
import pp_state
from pp_common import read_json, run_dir

#: "Roughly a screenful" — an order of magnitude smaller than the briefing
#: budget (pp_briefing.BRIEFING_MAX_CHARS), on purpose: a session that is
#: already mid-work does not need the charter and the board again, only
#: enough to remember what it was doing.
MAX_CHARS = 2_000
NEXT_STEPS_LIMIT = 6
OPEN_CRITERIA_LIMIT = 6
#: How many runs back to look for the last SESSION-written handoff — a
#: string of reaper skeletons must not make orient look expensive or empty.
HANDOFF_SCAN = 20


def _budget_line(deadline_epoch, budget_s) -> str:
    """Wall-clock remaining, from the same env vars the convergence hook
    reads (PERPETUA_DEADLINE_EPOCH / PERPETUA_BUDGET_S) — orient does not
    invent a second clock, it reports the one the session is already
    running against."""
    try:
        deadline = float(deadline_epoch)
        budget = float(budget_s)
    except (TypeError, ValueError):
        return "no wall-clock budget is set for this session"
    if budget <= 0:
        return "no wall-clock budget is set for this session"
    left_s = deadline - time.time()
    frac = max(0.0, min(1.0, 1 - left_s / budget))
    return (f"{max(0.0, left_s / 60):.0f} minute(s) left "
            f"({frac * 100:.0f}% of this session's wall-clock budget spent)")


def _run_int(run) -> int:
    """The integer part of a run id — a plain run or a probe's "N.K" alike.
    `state["run"]` lags the CALLER's own run number until the session ends
    (it is bumped once at launch and not again until the run finishes), so
    scanning must start from what the caller says it is, not from state.
    """
    try:
        return int(str(run).partition(".")[0])
    except (TypeError, ValueError):
        return 0


def _latest_agent_handoff(goal: Path, this_run, *, scan: int = HANDOFF_SCAN
                          ) -> dict | None:
    """The most recent handoff a SESSION actually wrote, walking back from
    THIS session's own run number — a run reaped into a skeleton has no
    next_steps worth repeating, the same skip `pp_journal.recent_meaningful`
    already applies to the briefing."""
    run = _run_int(this_run)
    for n in range(run - 1, max(run - 1 - scan, 0), -1):
        data = read_json(run_dir(goal, n) / "handoff.json")
        if isinstance(data, dict) and (data.get("source") or "agent") == "agent":
            return data
    return None


def _open_criteria_lines(criteria: list[dict]) -> list[str]:
    if not criteria:
        return ["criteria: none tracked yet"]
    rolled = pp_criteria.rollup(criteria)
    parents = pp_criteria.parent_ids(criteria)
    met, total = pp_criteria.leaf_totals(criteria)
    open_leaves = [c for c in criteria
                  if c.get("id") not in parents
                  and not rolled.get(c.get("id"), {}).get("done")]
    lines = [f"criteria: {met}/{total} met"]
    for c in open_leaves[:OPEN_CRITERIA_LIMIT]:
        lines.append(f"  - [ ] {c.get('id')}: {c.get('text', '')}")
    extra = len(open_leaves) - OPEN_CRITERIA_LIMIT
    if extra > 0:
        lines.append(f"  … and {extra} more open (goal_state / `perpetua status`)")
    return lines


def compose(goal: Path, *, run, persona: str | None, backend: str | None,
           deadline_epoch=None, budget_s=None) -> str:
    """Assemble the orient text. Never raises: a goal.json/state.json this
    cannot read is a harness fault, not a reason to fail a read-only tool a
    probe or a watcher might be calling too — callers still get a short,
    honest message rather than a stack trace.
    """
    try:
        state = pp_state.load(goal)
    except FileNotFoundError:
        return "orient: no state.json for this goal — nothing to report yet."

    standing = state.get("persona")
    persona_line = f"persona this session: {persona or 'generalist'}"
    if (standing or None) != (persona or None):
        persona_line += f" (the STANDING persona is now {standing or 'generalist'})"

    lines = [
        f"# orient — `{state.get('goal_id', goal.name)}`, session {run}",
        f"status: {state.get('status', '?')} · phase: {state.get('phase', '?')}",
        persona_line,
        f"backend: {backend or '?'}",
        f"budget: {_budget_line(deadline_epoch, budget_s)}",
        "",
    ]
    lines += _open_criteria_lines(state.get("criteria") or [])
    lines.append("")

    handoff = _latest_agent_handoff(goal, run)
    steps = [s for s in (handoff or {}).get("next_steps") or [] if str(s).strip()]
    if steps:
        lines.append("your predecessor's next steps:")
        lines += [f"  - {s}" for s in steps[:NEXT_STEPS_LIMIT]]
        if len(steps) > NEXT_STEPS_LIMIT:
            lines.append(f"  … and {len(steps) - NEXT_STEPS_LIMIT} more "
                         f"(read the full handoff in `journal/`)")
    else:
        lines.append("no next_steps on record from a prior session")

    lines += ["", "for more: `recall` to search this goal's history, "
                  "`board_read`/`board_search` for traffic, `persona_list`/"
                  "`persona_dossier` for identity, `perpetua_assign` "
                  "action=\"list\" for open work."]

    text = "\n".join(lines)
    return text if len(text) <= MAX_CHARS else text[:MAX_CHARS] + "\n… (truncated)"
