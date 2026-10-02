"""The swarm: K probes on one problem, then one session to synthesise them.

#7 in the incident plan. A swarm is *one* run of the goal that happens to be
carried out by K parallel sessions instead of one, and everything here follows
from that sentence.

* **One run, one number.** The swarm is run N. Its probes are `N.1 … N.K`,
  which is why a run id had to become a string (D1). `state["run"]` advances
  once, to N — probes never advance the counter, or K probes would burn K run
  numbers and the journal would read as K sessions that each did a fraction of
  a job.
* **One breaker unit.** All K probes together count as a single exempt run, not
  K exempt runs. Exploring is not evidence of being stuck and it is not evidence
  of progress, so the swarm leaves the no-progress breaker where it was — and it
  reuses #5's exemption, including its consecutive cap, rather than inventing a
  second accounting rule that could be spent without limit.
* **One repo, K worktrees.** `pp_probe` gives each probe its own worktree on a
  throwaway branch, because K sessions committing to one checkout is the failure
  the design exists to prevent. Nothing a probe commits reaches the goal until a
  later session cherry-picks it.

What lives here is the arithmetic and the prose — how many probes to run, and
what the synthesis session is told. The lifecycle (create, launch, wait,
finalise, harvest) is the supervisor's, in `bin/perpetuad`, because only it owns
`launch_pi` and the run records.
"""
from __future__ import annotations

#: Probes per swarm when nothing says otherwise. Three is the useful minimum for
#: comparing approaches, and one below the four llama.cpp slots the local server
#: has — the supervisor's own health probes need one.
DEFAULT_K = 3

#: Never more than this, whatever the config says. K parallel sessions share one
#: model server; past its slot count they do not run in parallel at all, they
#: queue, and the swarm's wall time becomes K sequential sessions with none of
#: the benefit.
MAX_K = 8


def backend_slots(spec: dict) -> int | None:
    """How many concurrent sessions this backend can really serve, if it says.

    A local llama.cpp server has a fixed slot count and going past it silently
    serialises. A hosted backend has no meaningful local bound, so it returns
    None and only the configured ceiling applies.
    """
    for key in ("slots", "parallel", "n_parallel"):
        raw = spec.get(key)
        try:
            val = int(raw)
        except (TypeError, ValueError):
            continue
        if val > 0:
            return val
    return 1 if spec.get("unit") or spec.get("base_url") else None


def clamp_k(cfg: dict, spec: dict) -> tuple[int, str | None]:
    """The number of probes to actually launch, and why it is not what was asked.

    Bounded three ways: the configured `swarm_k`, this module's MAX_K, and what
    the backend can serve at once minus one — the supervisor still has to be able
    to health-probe the server while the swarm runs, and a swarm that occupies
    every slot makes the box look wedged to everything else.

    Returns (k, note). `note` is None when nothing clamped, and otherwise says
    what did, so the reason ends up in the log and the machine channel rather
    than being silently absorbed.
    """
    try:
        want = int(cfg.get("swarm_k", DEFAULT_K))
    except (TypeError, ValueError):
        want = DEFAULT_K
    if want < 1:
        return 0, f"swarm_k={want} is not a swarm"
    k, note = want, None
    if k > MAX_K:
        k, note = MAX_K, f"clamped {want} to MAX_K={MAX_K}"
    slots = backend_slots(spec)
    if slots is not None:
        room = max(1, slots - 1)
        if k > room:
            k = room
            note = (f"clamped {want} to {room}: `{spec.get('class', '?')}` serves "
                    f"{slots} concurrent slot(s) and one is kept for health probes")
    return k, note


#: Appended to a probe's briefing. Everything here is something a probe would
#: otherwise get wrong by behaving like an ordinary session: it would try to tick
#: criteria (refused, confusingly), assume its work lands in the workspace (it
#: does not), or hedge toward the safe approach that the other K-1 probes are
#: also taking, which wastes the entire point of running K of them.
PROBE_BRIEFING_EXTRA = """

---

## You are probe {k} of {total} in swarm run {n}

This run is being explored **{total} ways at once**. You are one of them. The
other probes have the same briefing and the same goal, and they are running
right now, in parallel with you.

**Your workspace is your own.** You are in a private git worktree on branch
`{branch}`. Commit freely — nothing you do here touches the goal's workspace or
any other probe, and nothing is lost if you experiment. A later synthesis
session reads every probe's branch and decides what survives.

**Take the approach you actually think is best, not the safe one.** If all
{total} probes hedge toward the same cautious plan, the swarm has cost {total}
sessions and bought one result. Divergence is the point.

**A negative result is a real result.** If your approach turns out not to work,
that is worth more than a half-finished version of it — say so plainly in your
handoff, with what you tried and where it broke. The synthesis session reads
handoffs, not only diffs, and "this does not work" stops the next swarm
repeating you.

**What you may not do.** You cannot tick criteria, propose amendments, edit
personas, take assignments, register watchers, or switch the backend — those
change what every other probe and the goal itself inherit, and most probes are
discarded. The tools will refuse you; that is not a fault. Report what you found
on the board and in your handoff, and let the synthesis session make it durable.
"""


def summarise(harvest: list[dict]) -> dict:
    """Counts a human or a machine event can read without walking the harvest."""
    with_commits = [h for h in harvest if h.get("commits")]
    return {
        "probes": len(harvest),
        "productive": len(with_commits),
        "commits": sum(len(h.get("commits") or []) for h in harvest),
        "branches": [h.get("branch") for h in harvest if h.get("branch")],
    }


def synthesis_nudge(n, harvest: list[dict]) -> str:
    """What the session after a swarm is told.

    Deliberately not exempt and deliberately not a persona swap: synthesis is
    ordinary work — deciding what survives *is* advancing the goal. The nudge
    names every branch, because the probes' commits are reachable only by name
    and a session that has to go looking for them will not find them.

    The instruction to keep a *finding* from a probe that produced no commits
    matters more than it looks: the most useful probe result is often "this
    approach does not work", and that has no diff to cherry-pick. A synthesis
    that only reads branches throws that away and the swarm repeats it later.
    """
    stat = summarise(harvest)
    if not stat["probes"]:
        return (f"**Run {n} was a swarm that produced no probes.** Nothing was "
                f"explored, so treat this as an ordinary session and carry on "
                f"with the goal — but say in your handoff that the swarm came "
                f"back empty, because that is a fault worth someone noticing.")

    lines = [
        f"**This session synthesises swarm run {n}.** {stat['probes']} probe(s) "
        f"explored the same problem in parallel, each in its own git worktree on "
        f"its own branch; {stat['productive']} left commits "
        f"({stat['commits']} in total). Nothing they did is in the workspace yet.",
        "",
        "What each probe left:",
    ]
    for h in sorted(harvest, key=lambda x: x.get("k") or 0):
        commits = h.get("commits") or []
        branch = h.get("branch") or "(no branch)"
        if commits:
            lines.append(f"- probe {h.get('k')} — `{branch}`, {len(commits)} commit(s):")
            for c in commits[:10]:
                subject = (c.get("subject") or "").strip() or "(no subject)"
                lines.append(f"    - `{(c.get('sha') or '')[:10]}` {subject}")
            if len(commits) > 10:
                lines.append(f"    - …and {len(commits) - 10} more")
        else:
            lines.append(f"- probe {h.get('k')} — `{branch}`, no commits")
    lines += [
        "",
        "Your job is to decide what survives. Read each probe's journal entry "
        f"(`journal/{n}.*.md`) and its branch, then cherry-pick what is worth "
        "keeping into the workspace and commit it. You are not obliged to keep "
        "anything: a swarm where every approach failed is a real result.",
        "",
        "Two things the branches alone will not tell you. A probe that produced "
        "no commits may still have produced the most valuable finding — that an "
        "approach does not work — so read its handoff before you dismiss it. And "
        "when you have finished, say in your own handoff which probe you took "
        "from and why the others were not taken; the next session inherits that "
        "reasoning, not the branches, which are deleted once the swarm is closed.",
    ]
    return "\n".join(lines)
