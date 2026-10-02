"""Running `check.sh` — the one predicate that decides when a goal stops.

There were three copies of this: the supervisor's (with a timeout), the CLI's
(without one) and the dashboard's (without one, inside curses). They disagreed
about the timeout, about which streams are captured and about what a missing
check.sh means, so `perpetua check` could say "not met" where the supervisor
would have said "PASS". One implementation, three callers.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path

DEFAULT_TIMEOUT_S = 600
OUTPUT_TAIL = 800


class Result:
    """(passed, detail) with a third state: whether the check could run at all."""

    __slots__ = ("passed", "detail", "ran", "returncode")

    def __init__(self, passed: bool, detail: str, *, ran: bool = True,
                 returncode: int | None = None):
        self.passed = passed
        self.detail = detail
        self.ran = ran
        self.returncode = returncode

    def __iter__(self):
        # Every existing caller unpacks a 2-tuple; keep that working.
        return iter((self.passed, self.detail))

    def __repr__(self) -> str:
        return f"Result(passed={self.passed!r}, ran={self.ran!r}, detail={self.detail[:60]!r})"


def allowance(goal: Path, default: float = DEFAULT_TIMEOUT_S) -> float:
    """How long THIS goal's check.sh is allowed, from goal.json.

    A predicate that runs a real test suite takes minutes, and the number was
    hard-coded three different ways: 600s in the supervisor, 120s in the
    session's own `check` verb, 60s in the dashboard. Each had a good local
    reason — but the consequence was that a goal whose check takes 130s is
    PASSING for the supervisor and "could not run" for the agent that is trying
    to decide whether it is finished. One number per goal, read by all three.
    """
    try:
        cfg = json.loads((goal / "goal.json").read_text(encoding="utf-8"))
        value = float(cfg.get("check_timeout_s") or default)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return float(default)
    return value if value > 0 else float(default)


def run(goal: Path, *, timeout: float | None = None, cap: float | None = None,
        tail: int = OUTPUT_TAIL, cwd: Path | None = None, cancel=None) -> Result:
    import pp_evidence
    from pp_common import now_iso
    workspace = cwd if cwd is not None else goal / "workspace"
    before, predicate = pp_evidence.revision(workspace), pp_evidence.check_hash(goal)
    started_at, started = now_iso(), time.monotonic()
    result = _run(goal, timeout=timeout, cap=cap, tail=tail, cwd=cwd, cancel=cancel)
    pp_evidence.record(goal, result, before=before, predicate=predicate,
                       workspace=workspace, started_at=started_at,
                       elapsed_s=time.monotonic() - started)
    return result


def _run(goal: Path, *, timeout: float | None = None, cap: float | None = None,
        tail: int = OUTPUT_TAIL, cwd: Path | None = None, cancel=None) -> Result:
    """Run the goal's predicate. `cap` is a caller's own ceiling (the agent and
    the dashboard both block on this call, so they refuse to block forever);
    when a cap cuts the goal's allowance short, the timeout message says so,
    because "you gave me less time than the supervisor will" and "your check
    failed" are not the same news.

    `cwd` overrides where the script RUNS (default `goal / "workspace"`) —
    the one thing #5's merge-queue harvest needs that no other caller does: it
    scores a swarm probe by running the goal's own check.sh inside the
    probe's own worktree, which is a different directory but the same
    predicate. `check.sh` itself is always read from the goal dir; only its
    working directory moves.
    """
    limit = allowance(goal) if timeout is None else float(timeout)
    # What the SUPERVISOR will allow — which is what the capped message claims,
    # so it must come from the goal rather than from this caller's own timeout.
    allowed = allowance(goal)
    capped = cap is not None and float(cap) < limit
    if capped:
        limit = float(cap)
    check = goal / "check.sh"
    if not check.exists():
        return Result(False, "no check.sh — this goal can never self-declare completion",
                      ran=False)
    workspace = cwd if cwd is not None else goal / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.Popen(
            ["bash", str(check)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True,
            cwd=str(workspace),
            env={**os.environ, "PERPETUA_GOAL_DIR": str(goal)},
            start_new_session=True)
        deadline = time.monotonic() + limit
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or (cancel is not None and cancel.is_set()):
                raise subprocess.TimeoutExpired(proc.args, limit)
            try:
                stdout, stderr = proc.communicate(timeout=min(0.2, remaining) if cancel is not None else remaining)
                break
            except subprocess.TimeoutExpired:
                if cancel is None:
                    raise
    except subprocess.TimeoutExpired:
        # A hung check must never read as completion, and must never read as a
        # clean "not met" either. Kill its whole process group: check scripts
        # commonly spawn test runners or servers, and killing only bash would
        # leave those children mutating the workspace after the check timed out.
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            proc.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            proc.communicate()
        detail = f"check.sh timed out after {limit:.0f}s"
        if cancel is not None and cancel.is_set():
            detail = "check cancelled by operator"
        if capped:
            detail += (f" — this caller caps at {limit:.0f}s, but the supervisor "
                       f"allows this goal {allowed:.0f}s, so it may still pass "
                       f"there. Not a failing check.")
        return Result(False, detail, ran=False)
    except OSError as exc:
        return Result(False, f"check.sh could not be run: {exc}", ran=False)
    out = ((stdout or "") + (stderr or "")).strip()
    return Result(proc.returncode == 0, out[-tail:] if tail else out,
                  returncode=proc.returncode)
