"""Probe worktree lifecycle: one throwaway git worktree per parallel session.

Phase D's swarm launches K agent sessions against one goal at a time. The
thing that makes that possible is not spawning — it is that a goal's workspace
is ONE git repo, and K sessions committing to it concurrently is the failure
the whole design exists to avoid. So each probe k of run n gets its own
`git worktree` at `goals/<id>/probes/<n>-<k>` on a throwaway branch
`probe/<n>-<k>` off the workspace's current HEAD; a probe commits only inside
its own worktree, and after the swarm `harvest` returns, per probe, the commits
it made beyond the base — everything a synthesis session can cherry-pick.

Two rules shape every function.

* **The branch is the memory; the directory is throwaway.** The branches live
  in the workspace repo and survive a worktree a human already deleted by hand;
  the directories live under the goal dir, which is gitignored live state.
  So `destroy` asks git (`worktree remove`, then `worktree prune`) rather than
  trusting its own bookkeeping, and `harvest` reads commits from the branches,
  never from the directories.
* **No goal state.** This module owns directories and branches, period. Who
  owns a run is the supervisor's business and the wiring is a later task's —
  that is why there is no `pp_state` import anywhere here.
"""

from __future__ import annotations

import re
import shutil
import stat
import subprocess
from pathlib import Path
from typing import NamedTuple

from pp_common import run_id

# Worktree add/remove and branch ops are repo-wide and can be slow on a big
# goal, but a supervisor poll loop that calls this module must not hang forever
# on a wedged repo, so every call is bounded.
GIT_TIMEOUT_S = 60

# A probe branch has no reason to grow beyond a session's worth of commits.
# Capping the harvested log keeps a runaway probe from returning megabytes.
MAX_HARVEST_COMMITS = 1000


class ProbeError(ValueError):
    """A probe that cannot be created, or an argument that is not a probe id."""


class _GitResult(NamedTuple):
    """A git call's outcome as a value, never an exception.

    Richer than pp_progress._git's `""` on failure because the read paths here
    need "succeeded with empty output" told apart from "failed" — an empty
    harvest is an answer, not an error. The poll loop that calls `list_probes`
    and `harvest` must survive a wedged repo.
    """

    rc: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.rc == 0


def _git(ws: Path, *args: str) -> _GitResult:
    try:
        proc = subprocess.run(
            ["git", "-C", str(ws), *args],
            capture_output=True,
            check=False,
            text=True,
            timeout=GIT_TIMEOUT_S,
        )
        return _GitResult(proc.returncode, proc.stdout, proc.stderr)
    except (OSError, subprocess.SubprocessError) as exc:
        return _GitResult(-1, "", f"{type(exc).__name__}: {exc}")


def _run_no(n: str | int) -> str:
    """Canonical run id for a probe's run, refusing ids that are not runs.

    A probe id is a *valid* run id ("0042.3") but naming a probe after a probe
    would nest the swarm — the directory and branch names only make sense for a
    plain run, so it is refused loudly instead of normalised away.
    """
    nid = run_id(n)
    if "." in nid:
        raise ProbeError(f"run id {nid!r} is a probe id; probes never nest")
    return nid


def _probe_refs(goal: Path, n: str | int, k: str | int) -> tuple[str, Path, Path, int]:
    """Canonical branch name, worktree path, workspace repo and parsed k.

    Everything below only ever sees the shapes this function produced, so a raw
    goal id, run id or probe k can reach a git argv at most as one of them — the
    arg-array equivalent of `run_id`'s strict parse, and the reason no git
    invocation here builds a shell string (failure class 5's general shape, a
    gate leaking through an unvalidated string). k is parsed as strictly as n:
    a bool, a float or "007" are bugs, and a k of 0 never names a probe.
    """
    nid = _run_no(n)
    if (
        isinstance(k, bool)
        or not isinstance(k, (int, str))
        or (isinstance(k, str) and not k.strip().isdigit())
    ):
        raise ProbeError(f"probe k {k!r} must be a positive int")
    kk = int(k)
    if isinstance(k, str) and str(kk) != k.strip():
        raise ProbeError(f"probe k {k!r} must be a plain int, not a padded one")
    if kk < 1:
        raise ProbeError(f"probe k {kk!r} must be >= 1")
    name = f"{nid}-{kk}"
    return f"probe/{name}", goal / "probes" / name, goal / "workspace", kk


def create(goal: Path, n: str | int, k: str | int) -> dict:
    """Add probe k of run n: worktree at goal/probes/<n>-<k> on a new branch.

    The branch is created off the workspace's current HEAD, so `base` (the
    returned commit) is the honest start point `harvest` later subtracts.
    `git worktree add` creates the branch and the worktree in one command, so a
    failure cannot leave a branch with no worktree or a worktree with no branch
    — but it can leave a bare branch ref and a bare directory behind, so the
    create path cleans exactly what it asked git to make before raising.

    A probe that already exists — as a directory, a registered worktree or a
    branch — is refused rather than reused: a reused worktree is not fresh (it
    can hold a dead probe's dirty files), so a swarm retry destroys first and
    creates again. That choice is deliberate and pinned in the tests.
    """
    branch, path, ws, kk = _probe_refs(goal, n, k)
    if path.exists():
        raise ProbeError(
            f"probe {path.name} already exists at {path}; destroy() it and retry"
        )
    got = _git(ws, "rev-parse", "HEAD")
    if not got.ok:
        raise ProbeError(
            f"workspace {ws} has no HEAD commit to branch from: {got.stderr.strip()}"
        )
    base = got.stdout.strip()
    if _git(ws, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}").rc == 0:
        raise ProbeError(
            f"branch {branch!r} already exists; destroy() the probe and retry"
        )
    got = _git(ws, "worktree", "add", "-b", branch, str(path), "HEAD")
    if not got.ok:
        # git usually fails before creating anything, but when it fails part-way
        # it leaves the branch ref (and occasionally a bare dir). Both are
        # throwaway — remove them so the failure leaves no trace (plan §11:3).
        shutil.rmtree(path, ignore_errors=True)
        _git(ws, "worktree", "prune")
        _git(ws, "branch", "-D", branch)
        raise ProbeError(
            f"could not add worktree for probe {path.name}: {got.stderr.strip()}"
        )
    return {"k": kk, "worktree": str(path), "branch": branch, "base": base}


def _probe_k(nid: str, name: str) -> int | None:
    """k if `name` is the canonical `<n>-<k>` spelling of this run's probe, else None.

    The canonical check (`name == f"{nid}-{k}"`) matters: `0042-007` and `42-1`
    are not the worktrees this module creates, so they must not read as probe 7
    or 1. Failure class 3 applied to a directory name — an unexpected shape must
    be skipped, never coerced and never fatal.
    """
    m = re.fullmatch(r"(\d+)-(\d+)", name)
    if not m:
        return None
    k = int(m.group(2))
    if k < 1 or name != f"{nid}-{k}":
        return None
    return k


def _descriptor(nid: str, k: int, probes_dir: Path) -> dict:
    return {
        "k": k,
        "worktree": str(probes_dir / f"{nid}-{k}"),
        "branch": f"probe/{nid}-{k}",
        "on_disk": False,
        "registered": False,
        "branch_exists": False,
    }


def _registered_worktrees(ws: Path, probes_dir: Path, nid: str) -> list[Path]:
    """Worktree paths git believes exist, inside this run's probes dir."""
    got = _git(ws, "worktree", "list", "--porcelain")
    if not got.ok:
        return []
    parent = probes_dir.resolve()
    paths = []
    for line in got.stdout.splitlines():
        if not line.startswith("worktree "):
            continue
        path = Path(line[len("worktree ") :]).resolve()
        if path.parent == parent and _probe_k(nid, path.name) is not None:
            paths.append(path)
    return paths


def _probe_branches(ws: Path, nid: str) -> list[str]:
    got = _git(ws, "branch", "--list", f"probe/{nid}-*")
    if not got.ok:
        return []
    # git prefixes the main worktree's branch with `*` and a linked worktree's
    # with `+ `; no branch name can start with either, so stripping is safe.
    return [b.lstrip("*+ ").strip() for b in got.stdout.splitlines() if b.strip()]


def list_probes(goal: Path, n: str | int) -> list[dict]:
    """Probes of run n that still exist, k ascending — from disk, git, or both.

    A probe's *branch* is the durable form and the directory is throwaway, so
    each of the three sources adds a probe rather than defining one: a worktree
    whose dir a human deleted by hand still shows up (registered), and a stray
    directory that was never a worktree shows up as a probe with nothing behind
    it (neither registered nor a branch — destroy() is what such a slot is
    for). Both scans are value-only: the directory scan is wrapped whole
    (failure class 6) and a failing git call contributes nothing (invariant 7).
    """
    nid = _run_no(n)
    ws = goal / "workspace"
    probes_dir = goal / "probes"
    found: dict[int, dict] = {}
    try:
        names = [e.name for e in probes_dir.iterdir()] if probes_dir.is_dir() else []
    except OSError:
        names = []  # removed between is_dir and scan
    for name in names:
        path = probes_dir / name
        try:
            st = path.lstat()
        except OSError:
            continue
        if not stat.S_ISDIR(st.st_mode):
            continue  # a file/fifo/symlink is junk
        k = _probe_k(nid, name)
        if k is None:
            continue
        found.setdefault(k, _descriptor(nid, k, probes_dir))["on_disk"] = True
    for path in _registered_worktrees(ws, probes_dir, nid):
        k = _probe_k(nid, path.name)
        if k is not None:
            found.setdefault(k, _descriptor(nid, k, probes_dir))["registered"] = True
    for branch in _probe_branches(ws, nid):
        k = _probe_k(nid, branch.partition("/")[2])
        if k is not None:
            found.setdefault(k, _descriptor(nid, k, probes_dir))["branch_exists"] = True
    return [found[k] for k in sorted(found)]


def destroy(goal: Path, n: str | int, k: str | int) -> None:
    """Remove probe k: its worktree, its branch, its directory — then prune.

    Idempotent on purpose, and built to survive a human: a worktree whose dir a
    person already rm -rf'd makes `worktree remove --force` still succeed, and
    the prune that follows is git's own sweep of anything its bookkeeping still
    believes exists — the whole reason prune exists, and the reason destroy
    never trusts its own accounting. `--force` / `-D` are deliberate: a dead
    probe's worktree is dirty and its commits are throwaway by construction,
    and refusing either would turn destroy into a cleanup a human has to finish
    by hand. Every step is a value — each may legitimately fail ("already
    gone") and all three run regardless, so calling it twice is a no-op.
    """
    branch, path, ws, _kk = _probe_refs(goal, n, k)
    _git(ws, "worktree", "remove", "--force", str(path))
    _git(ws, "worktree", "prune")
    _git(ws, "branch", "-D", branch)
    # Ran last so a directory cannot survive a destroy that git accepted; only
    # ever removes throwaway state under the probes tree, and never through a
    # symlink (a link here is junk, and unlink-of-a-link must not become
    # rm-of-a-tree — failure class 2's shape). A non-directory at the slot
    # (a stray file or fifo) is unlinked too: it would otherwise make the slot
    # permanently uncreatable, since create refuses anything that exists.
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, ignore_errors=True)
    elif path.exists() and not path.is_symlink():
        path.unlink(missing_ok=True)


def _probe_commits(ws: Path, branch: str) -> tuple[str | None, list[dict]]:
    """(base, commits): everything on `branch` the workspace does not have.

    merge-base is the honest fork point even if the workspace branch moved while
    the probes ran, and `--no-merges` keeps a pull-merge a probe made to stay
    current from reading as a commit it authored. Oldest first: that is the
    order a picker would apply them. A missing branch or a failed git call is a
    value — a record with no commits, never an exception.
    """
    got = _git(ws, "merge-base", "HEAD", branch)
    if not got.ok:
        return None, []
    base = got.stdout.strip()
    got = _git(
        ws,
        "log",
        "--no-merges",
        "--reverse",
        "--format=%H%x00%s",
        "--max-count",
        str(MAX_HARVEST_COMMITS),
        f"{base}..{branch}",
    )
    if not got.ok:
        return base, []
    commits = []
    for line in got.stdout.splitlines():
        sha, sep, subject = line.partition("\0")
        if not sep:
            continue  # a weird line is a value
        commits.append({"sha": sha, "subject": subject})
    return base, commits


def harvest(goal: Path, n: str | int) -> list[dict]:
    """Per probe of run n, the commits it made beyond the base — oldest first.

    This is the synthesis session's input: sha and subject per commit, plus the
    branch they live on and the base commit they diverge from. Probes are found
    by branch rather than by directory — the branch is the durable memory, so a
    probe whose worktree a human deleted still harvests. A probe with no branch
    (a stray directory) yields an empty record; a git failure yields a value.
    """
    ws = goal / "workspace"
    out = []
    for p in list_probes(goal, n):
        base, commits = _probe_commits(ws, p["branch"])
        out.append(
            {
                "k": p["k"],
                "worktree": p["worktree"],
                "branch": p["branch"],
                "base": base,
                "commits": commits,
            }
        )
    return out
