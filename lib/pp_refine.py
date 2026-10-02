"""The merge queue for a swarm's probes (#5) — the step that used to be nobody's.

`pp_swarm`'s own docstring said it: "Nothing a probe commits reaches the goal
until a later session cherry-picks it." K probes burn K sessions' worth of
GPU and then land only if a LATER session happens to do the boring part —
read every branch, decide what survives, commit it — which is the one
unowned step in an otherwise mechanically-owned lifecycle. The gate to land
against already exists and is already shared by the CLI, the dashboard and
the supervisor: `check.sh` via `pp_check`. This module is what makes landing
mechanical instead of optional.

Owned by the SUPERVISOR, like the rest of the swarm lifecycle (`bin/perpetuad`
creates probes, launches them and harvests their commits) — for the same
reason: only the supervisor is trusted to touch the shared workspace between
sessions, and a synthesis session inheriting an already-cleaner state is
strictly better than one that has to do this by hand.

Three rules shape every function here:

* **Best-scoring first, ties by probe number.** A probe's score is the goal's
  OWN check.sh, run once against that probe's own worktree before anything is
  merged — the same predicate the goal is judged by, not a separate opinion.
* **Never worse.** A probe merges only if the check's result afterward is not
  worse than it was before that one merge — PASS beats "ran but failed" beats
  "could not run". Landing in best-score order and reverting on regression is
  what makes a combined failure resolve itself: if A (better-ranked) has
  already landed and B regresses the check when merged on top of it, B is
  skipped and A stays — which is exactly "land the better one, record the
  other as conflicting" without a separate bisection pass, because the order
  probes are tried in already IS the ranking by which one is better.
* **Never leave the workspace dirty or mid-merge.** The commit the workspace
  was on before the first attempt is recorded before that attempt runs, and
  every path out of a probe's turn — clean land, git conflict, check
  regression, timeout, exception — resets back to the last good checkpoint.
  Recovery never has to infer what "before" meant.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import NamedTuple

import pp_attribute
import pp_check
import pp_probe
from pp_common import log, now_iso, run_id, write_json

GIT_TIMEOUT_S = 120


class _GitResult(NamedTuple):
    rc: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.rc == 0


def _git(ws: Path, *args: str, env: dict | None = None) -> _GitResult:
    try:
        run_env = {**os.environ, **env} if env else None
        proc = subprocess.run(["git", "-C", str(ws), *args], capture_output=True,
                              text=True, timeout=GIT_TIMEOUT_S, env=run_env)
        return _GitResult(proc.returncode, proc.stdout, proc.stderr)
    except (OSError, subprocess.SubprocessError) as exc:
        return _GitResult(-1, "", f"{type(exc).__name__}: {exc}")


def _head(ws: Path) -> str | None:
    got = _git(ws, "rev-parse", "HEAD")
    return got.stdout.strip() if got.ok and got.stdout.strip() else None


def _ensure_clean(ws: Path, checkpoint: str) -> None:
    """Restore the workspace to exactly `checkpoint`, mid-merge or not.

    Called after EVERY probe's turn, win or lose — a merge that "failed" at
    the git level can still leave index state behind, and `git merge --abort`
    alone does not clean untracked files a probe's own commit introduced. This
    is the one place invariant 3 (never dirty, never mid-merge) is enforced,
    so every caller in this module gets it by construction rather than by
    remembering to call it.
    """
    if (ws / ".git" / "MERGE_HEAD").exists():
        _git(ws, "merge", "--abort")
    _git(ws, "reset", "--hard", checkpoint)
    _git(ws, "clean", "-fd")


def _check_rank(result: pp_check.Result | None) -> int:
    """PASS > "ran but not met" > "could not run" — the tri-state check.sh
    already returns, ordered so "not worse" is a plain integer comparison."""
    if result is None or not result.ran:
        return 0
    return 2 if result.passed else 1


def _score_probe(goal: Path, probe: dict) -> tuple[int, int, int]:
    """(check_rank, commit_count, -k) — sorted descending, this IS "best-
    scoring first, ties by probe number (ascending)". A probe whose worktree
    is gone (a human deleted it, or it was destroyed by a previous cleanup)
    scores as check_rank 0 rather than raising — it still harvests by
    commits alone, just last among equals.
    """
    commits = len(probe.get("commits") or [])
    worktree = Path(probe["worktree"]) if probe.get("worktree") else None
    rank = 0
    if worktree is not None and worktree.is_dir():
        try:
            rank = _check_rank(pp_check.run(goal, cwd=worktree))
        except Exception as exc:                            # noqa: BLE001
            log(f"refine: probe {probe.get('k')} could not be scored: "
                f"{type(exc).__name__}: {exc}")
    return (rank, commits, -int(probe.get("k") or 0))


def _rank(goal: Path, probes: list[dict]) -> list[dict]:
    return sorted(probes, key=lambda p: _score_probe(goal, p), reverse=True)


def harvest(goal: Path, n: str | int, *, probes: list[dict] | None = None,
           harvest_path: Path | None = None) -> dict:
    """Land what a swarm's probes actually improved. Returns the record also
    written to `runs/N/harvest.json` (or `harvest_path`, for a caller that
    wants to fold it into an existing file of that name).

    Never raises: every failure mode a probe merge can hit — a git conflict,
    a regressed check, a timeout, an exception mid-merge — is recorded as a
    skip and the loop moves on to the next-ranked probe. The one thing this
    function refuses outright is landing anything at all when the goal's own
    check.sh "could not run" even before harvest touched the workspace:
    landing code against a gate that cannot currently answer is worse than
    landing nothing, because nothing after this can tell whether it helped.
    """
    ws = goal / "workspace"
    nid = run_id(n)
    pp_attribute.install(ws)          # #7: idempotent; covers a goal older than it
    record: dict = {
        "run": nid, "at": now_iso(), "pre_commit": None,
        "baseline": None, "landed": [], "skipped": [],
    }
    pre_commit = _head(ws)
    record["pre_commit"] = pre_commit
    if harvest_path is not None:
        # Recorded to disk BEFORE the first merge is attempted — the whole
        # point is that a supervisor crash mid-harvest leaves this file
        # naming the exact commit recovery should reset to, rather than
        # something that has to be inferred after the fact.
        write_json(harvest_path, record)
    if not pre_commit:
        record["skipped"].append({"reason": "workspace has no HEAD commit"})
        return record

    try:
        baseline = pp_check.run(goal, cwd=ws)
    except Exception as exc:                            # noqa: BLE001
        log(f"refine: baseline check.sh raised: {type(exc).__name__}: {exc}")
        record["skipped"].append({"reason": f"baseline check raised: {exc}"})
        return record
    record["baseline"] = {"ran": baseline.ran, "passed": bool(baseline.passed)}
    if probes is None:
        probes = pp_probe.harvest(goal, nid)
    if not baseline.ran:
        record["skipped"] = [
            {"k": p["k"], "branch": p.get("branch"),
             "reason": "check.sh could not run before harvest started — "
                       "landing nothing against a gate with no answer"}
            for p in probes]
        return record

    checkpoint = pre_commit
    current_rank = _check_rank(baseline)
    for probe in _rank(goal, probes):
        k, branch = probe.get("k"), probe.get("branch")
        try:
            if not probe.get("commits"):
                record["skipped"].append({"k": k, "branch": branch,
                                          "reason": "no commits"})
                continue
            # #7: this commit is the SUPERVISOR landing a probe branch, not
            # any persona's own work — attributed as such (Perpetua-Run: the
            # swarm's own run number, Perpetua-Persona: none) via the shared
            # prepare-commit-msg hook, which reads these env vars fresh.
            merge_env = {"PERPETUA_GOAL_DIR": str(goal),
                        **pp_attribute.supervisor_env(run=nid)}
            merged = _git(ws, "merge", "--no-ff", "--no-edit", branch,
                         env=merge_env)
            if not merged.ok:
                detail = (merged.stderr or merged.stdout or "merge failed").strip()
                lines = detail.splitlines()
                _ensure_clean(ws, checkpoint)
                record["skipped"].append({
                    "k": k, "branch": branch,
                    "reason": f"conflict: {lines[-1] if lines else 'merge failed'}"})
                continue
            result = pp_check.run(goal, cwd=ws)
            rank = _check_rank(result)
            if rank >= current_rank:
                checkpoint = _head(ws) or checkpoint
                current_rank = rank
                record["landed"].append({
                    "k": k, "branch": branch,
                    "commits": len(probe.get("commits") or []),
                    "check": {"ran": result.ran, "passed": bool(result.passed)}})
            else:
                _ensure_clean(ws, checkpoint)
                record["skipped"].append({
                    "k": k, "branch": branch,
                    "reason": f"check regressed after merge "
                              f"(was rank {current_rank}, now {rank})"})
        except Exception as exc:                            # noqa: BLE001
            log(f"refine: probe {k} raised mid-harvest: "
                f"{type(exc).__name__}: {exc}")
            try:
                _ensure_clean(ws, checkpoint)
            except Exception:                                # noqa: BLE001
                pass
            record["skipped"].append({
                "k": k, "branch": branch,
                "reason": f"exception: {type(exc).__name__}: {exc}"})

    # Belt and braces: whatever the loop did, the workspace must be sitting
    # exactly at the last checkpoint when this returns, never mid-merge.
    try:
        _ensure_clean(ws, checkpoint)
    except Exception as exc:                            # noqa: BLE001
        log(f"refine: final cleanup failed: {type(exc).__name__}: {exc}")
    if harvest_path is not None:
        write_json(harvest_path, record)
    return record


def summarise(record: dict) -> dict:
    """Counts a human or a machine event can read without walking the record."""
    return {"landed": len(record.get("landed") or []),
            "skipped": len(record.get("skipped") or []),
            "landed_branches": [x.get("branch") for x in record.get("landed") or []]}
