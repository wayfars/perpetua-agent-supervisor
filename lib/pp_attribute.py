"""Attribution on every artifact (#7) — closing the confinement gap honestly.

The README already concedes the sandbox story: pi's cwd is the goal's
workspace, and the harness's worktree-guard has a documented escape — "cwd
plus an audit, not a sandbox." If the sandbox half cannot be fixed cheaply,
the audit half has to be airtight, and it was not: `git log` in a goal
workspace could not tell you which run, or which persona, produced a commit.

Two mechanisms, because they solve different problems and only one of them
survives the agent's own tools:

* `author_env()` / `supervisor_env()` export `GIT_AUTHOR_NAME` /
  `GIT_COMMITTER_NAME` (and matching emails) into the environment a commit is
  made in. Verified empirically: these env vars take precedence over a
  workspace's configured `user.name`/`user.email`, so `git log`'s author
  column is readable identity even before anyone looks at a trailer.
* `install()` puts a `prepare-commit-msg` hook in the workspace's `.git/hooks/`
  that appends `Perpetua-Goal` / `Perpetua-Run` / `Perpetua-Persona` trailers
  to every commit message. This is the one that actually matters, and it was
  chosen over `commit.template` after checking, not assuming: with only
  `commit.template` set, `git commit -m "x"` produces a message with NO
  trailer at all — the template only ever pre-fills an editor, and `-m`
  skips the editor entirely. `prepare-commit-msg` runs on every commit
  regardless of how the message was supplied, `-m` included, which is the
  only form an agent's raw `git commit -m` actually goes through. A trailer
  an agent can bypass by using `-m` — which is exactly what commit.template
  is — is worth nothing.

Nothing here is installed per worktree. Git worktrees share ONE `.git/hooks/`
(verified: a hook installed in the main workspace fires for a commit made in
a linked worktree too), so installing once in the goal's main workspace
already covers every swarm probe — a probe's own `PERPETUA_RUN` is already
`N.K` for exactly the run it is, so its commits are traceable to the probe
that authored them with no extra wiring.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

HOOK_NAME = "prepare-commit-msg"
#: Present in every hook this module writes; used both to detect "is this
#: OUR hook" (so a re-install is idempotent) and "is this a FOREIGN hook"
#: (so an existing human-authored one is never clobbered).
MARKER = "# perpetua-attribution-hook: do not edit by hand (see lib/pp_attribute.py)"

TRAILER_GOAL = "Perpetua-Goal"
TRAILER_RUN = "Perpetua-Run"
TRAILER_PERSONA = "Perpetua-Persona"

HOOK_BODY = f"""#!/usr/bin/env bash
{MARKER}
# Appends Perpetua-Goal/Run/Persona trailers to every commit made in this
# workspace, including a plain `git commit -m` (see pp_attribute.py's module
# docstring for why this is a hook and not commit.template). Values are read
# from the environment a perpetua session already runs with — never baked
# into this file — so one install covers every future session and every
# worktree that shares this repo's .git/hooks/.
msg_file="$1"
grep -q "^{TRAILER_GOAL}:" "$msg_file" 2>/dev/null && exit 0   # already attributed (e.g. --amend)
goal_id="$(basename "${{PERPETUA_GOAL_DIR:-}}")"
{{
  echo
  echo "{TRAILER_GOAL}: ${{goal_id:-none}}"
  echo "{TRAILER_RUN}: ${{PERPETUA_RUN:-none}}"
  echo "{TRAILER_PERSONA}: ${{PERPETUA_PERSONA:-none}}"
}} >> "$msg_file"
"""


def hook_path(workspace: Path) -> Path:
    return workspace / ".git" / "hooks" / HOOK_NAME


def is_installed(workspace: Path) -> bool:
    p = hook_path(workspace)
    try:
        return p.is_file() and MARKER in p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


def install(workspace: Path) -> bool:
    """Install the attribution hook. Returns whether it is installed after
    this call (True even if it was already there) — the caller (perpetuad,
    at the top of every run) does not need to know which.

    A `workspace` whose `.git` is not a real directory is either not a repo
    yet or is itself a linked worktree — a worktree's `.git` is a FILE
    pointing at the common gitdir, and hooks live in the common repo's
    `.git/hooks/`, already shared. Either way there is nothing to install
    here, and trying to `mkdir` through a file would raise.
    """
    git_dir = workspace / ".git"
    if not git_dir.is_dir():
        return False
    p = hook_path(workspace)
    if p.exists() and not is_installed(workspace):
        # A human (or another tool) put a real hook here first. Overwriting
        # it silently would be exactly the kind of "the harness owns your
        # workspace" behaviour the confinement story already disclaims.
        return False
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(HOOK_BODY, encoding="utf-8")
    p.chmod(0o755)
    return True


def author_env(*, run: str | int, persona: str | None) -> dict:
    """GIT_AUTHOR_*/GIT_COMMITTER_* for a SESSION's own commits — belt and
    braces alongside the trailer hook: readable directly in `git log`
    without `--format`, and it survives a plain `-m` too (env vars beat a
    workspace's configured user.name/user.email, verified)."""
    ident = persona or "generalist"
    return {
        "GIT_AUTHOR_NAME": f"perpetua/{ident}",
        "GIT_AUTHOR_EMAIL": f"{ident}@example.invalid",
        "GIT_COMMITTER_NAME": f"perpetua-run-{run}",
        "GIT_COMMITTER_EMAIL": "supervisor@example.invalid",
    }


def supervisor_env(*, run: str | int) -> dict:
    """The same, for a commit the SUPERVISOR itself makes (#5's merge-queue
    lands) — a distinct identity from any persona, and PERPETUA_PERSONA is
    explicitly cleared so the trailer reads "none" rather than defaulting to
    whichever persona happened to be standing, which would misattribute a
    mechanical merge as that persona's own work.
    """
    return {
        "PERPETUA_RUN": str(run),
        "PERPETUA_PERSONA": "",
        "GIT_AUTHOR_NAME": "perpetua-supervisor",
        "GIT_AUTHOR_EMAIL": "supervisor@example.invalid",
        "GIT_COMMITTER_NAME": "perpetua-supervisor",
        "GIT_COMMITTER_EMAIL": "supervisor@example.invalid",
    }


def commits_for_run(workspace: Path, run: str) -> list[str]:
    """Every commit (searched across every ref, so a probe's still-live
    branch counts) whose Perpetua-Run trailer names exactly this run —
    what `perpetua runs` shows, and the one thing `git log` alone cannot
    answer: which run produced this commit.
    """
    pattern = f"^{TRAILER_RUN}: {run}".replace(".", r"\.")
    try:
        proc = subprocess.run(
            ["git", "-C", str(workspace), "log", "--all", "-E",
             f"--grep={pattern}$", "--format=%H"],
            capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]
