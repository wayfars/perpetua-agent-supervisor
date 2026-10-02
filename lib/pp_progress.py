"""What counts as progress.

The no-progress breaker is the only thing standing between a stuck goal and a
thousand identical sessions, so what it measures matters more than how cheaply it
measures it. The previous signal was
`git HEAD | len(git status) | criteria met | board head`, which was wrong in both
directions:

  * `len(status --porcelain)` is a *length*. Rewriting a file's entire contents
    without changing its path leaves it identical, so a session that spent an hour
    productively editing uncommitted files read as "no progress".
  * The board head is a monotonic counter of *messages posted*. A session that
    posted "still thinking" and did nothing else reset the breaker — the failure
    mode the breaker exists to catch is precisely a session that talks instead of
    working.

So: progress is a change in the durable artefacts of the goal — committed and
uncommitted workspace content, the charter, the completion predicate, the tracked
criteria, the handoff journal, the personas, and the goal's wiki. Board traffic
and run bookkeeping (logs, launch.json, traces, state's own timestamps) are
deliberately excluded: they always change, so including them would mean the
breaker could never fire.
"""
from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

from pp_common import UnsafeRunId, run_id

# Reading every tracked file would make the snapshot O(repo) twice per run. Git's
# own object ids already hash committed content, so only *uncommitted* content has
# to be hashed by hand, and that is bounded by what one session can plausibly edit.
MAX_DIRTY_BYTES = 4 * 1024 * 1024
GIT_TIMEOUT_S = 60

# Durable, agent-authored knowledge. Everything not listed here is bookkeeping.
KNOWLEDGE_GLOBS = (
    "GOAL.md",
    "check.sh",
    "journal/*.md",
    # `**` matches zero directories too, so this covers BOTH persona layouts:
    # the flat `personas/<name>.md` and the promoted
    # `personas/<name>/{persona,dossier}.md`. A dossier entry is durable,
    # agent-authored knowledge — if it did not count here, writing one would
    # read as a session that changed nothing.
    "personas/**/*.md",
    ".pi/wiki/**/*.md",
    ".pi/wiki/**/*.json",
)


def _git(ws: Path, *args: str) -> str:
    try:
        proc = subprocess.run(["git", "-C", str(ws), *args], capture_output=True,
                              text=True, timeout=GIT_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout if proc.returncode == 0 else ""


def _hash_file(h: "hashlib._Hash", path: Path, label: str, budget: list[int]) -> None:
    h.update(label.encode("utf-8", "replace"))
    h.update(b"\0")
    try:
        with open(path, "rb") as fh:
            while budget[0] > 0:
                chunk = fh.read(min(65536, budget[0]))
                if not chunk:
                    break
                budget[0] -= len(chunk)
                h.update(chunk)
    except OSError:
        h.update(b"<unreadable>")
    h.update(b"\n")


def workspace_digest(ws: Path) -> str:
    """Committed history plus the exact bytes of everything not yet committed."""
    h = hashlib.sha256()
    h.update(b"head:")
    h.update(_git(ws, "rev-parse", "HEAD").strip().encode())
    h.update(b"\nstatus:\n")
    status = _git(ws, "status", "--porcelain", "-z", "--untracked-files=all")
    # -z keeps filenames with newlines from forging a status line, and sorting
    # makes the digest independent of git's traversal order.
    entries = sorted(e for e in status.split("\0") if e)
    budget = [MAX_DIRTY_BYTES]
    for entry in entries:
        h.update(entry.encode("utf-8", "replace"))
        h.update(b"\n")
        rel = entry[3:] if len(entry) > 3 else ""
        if not rel or entry.startswith("D "):
            continue
        candidate = ws / rel
        if candidate.is_file():
            _hash_file(h, candidate, rel, budget)
    if budget[0] <= 0:
        # Truncation is recorded so two different oversized trees do not collide
        # into the same digest just because both ran out of budget.
        h.update(b"<dirty-content-truncated>")
    return h.hexdigest()


def knowledge_digest(goal: Path, *, exclude_run: int | None = None) -> str:
    """The handoffs, personas, charter, predicate and wiki — the goal's memory.

    `exclude_run` drops the journal entry a specific run wrote. Every run writes
    one, including a run that achieved nothing and was reaped, so counting it
    would make the snapshot differ after every single session and the no-progress
    breaker could never fire. A handoff describing an hour of nothing is not
    progress; what the session did to the goal is.
    """
    h = hashlib.sha256()
    # Through run_id, not `:04d`: a probe's run id is a string ("0042.3") and
    # an int format spec raises on it. A caller that hands over an unparseable
    # id excludes nothing rather than taking the fingerprint down — measuring
    # slightly too much is a wrong answer, and raising here is no answer at all.
    try:
        skip = {f"journal/{run_id(exclude_run)}.md"} if exclude_run else set()
    except UnsafeRunId:
        skip = set()
    seen: set[Path] = set()
    for pattern in KNOWLEDGE_GLOBS:
        for path in sorted(goal.glob(pattern)):
            if not path.is_file() or path in seen:
                continue
            rel = str(path.relative_to(goal))
            if rel in skip:
                continue
            seen.add(path)
            _hash_file(h, path, rel, [1 << 30])
    return h.hexdigest()


def criteria_digest(state: dict) -> str:
    """Which criteria exist and which are met — not merely how many.

    #2: a LEAF's own `done` flag flipping changes this hash on its own — the
    record for that id is right here in the loop — so a run that ticks one
    counts as progress even when nothing else about the goal changed. A
    parent's rolled-up `done` is never stored on its record (see
    `pp_criteria`'s module docstring for why), so there is nothing here for it
    to double-count: the parent's hash contribution is exactly its own
    unchanged `done` field, whatever the rollup over its children says.
    """
    h = hashlib.sha256()
    for crit in state.get("criteria") or []:
        h.update(f"{crit.get('id')}={crit.get('text')}:{bool(crit.get('done'))}\n"
                 .encode("utf-8", "replace"))
    return h.hexdigest()


def snapshot(goal: Path, state: dict, *, exclude_run: int | None = None) -> str:
    """A stable content hash of everything that counts as progress.

    Equal snapshots across two runs means the goal is in the same place it was.
    Take the before- and after-snapshots of a run with the SAME `exclude_run`, or
    the run's own handoff will make every run look productive.
    """
    return "|".join((
        workspace_digest(goal / "workspace")[:16],
        knowledge_digest(goal, exclude_run=exclude_run)[:16],
        criteria_digest(state)[:16],
    ))


def describe() -> str:
    """The definition, in the words the briefing and the docs both use."""
    return ("Measurable progress means a change in the goal's durable artefacts: "
            "workspace content (committed or not), GOAL.md, check.sh, the tracked "
            "criteria, the personas, the goal wiki, or an earlier session's journal "
            "entry. Board messages, run logs and your OWN handoff are explicitly "
            "NOT progress — a session that only posts to the board, or only writes "
            "a handoff about having achieved nothing, has not moved the goal.")
