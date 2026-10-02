"""Hash-and-detect enforcement of the files a session must not be able to change.

R1/R3's problem is that the agent's cwd is the goal's `workspace/`, whose parent
**is** the goal directory: `cd ..` reaches BOUNDARIES.md and check.sh with plain
bash, so a write-refusal in pp-tool is decoration. What makes the fence real is
that the supervisor records a hash at goal creation, keeps a restoration copy,
and pauses the goal on any unexplained drift. A goal whose fence has moved does
not get to keep running unattended.

Two deliberate choices:

* **Restore-then-pause, never repair-then-continue.** The restoration makes the
  fence whole for whenever a human resumes the goal; the pause is what forces
  the human to look. Restoring without pausing would teach the loop that
  tampering is free.
* **check.sh is protected exactly like BOUNDARIES.md.** The one legitimate way
  it changes is an approved amendment (see pp_amend), which re-anchors the hash
  and the backup copy in the same step; anything else is a violation even if it
  makes the goal pass — the pass would be a lie.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pp_state
from pp_common import now_iso

#: name -> goal-relative filename. Everything in here is hashed at `perpetua new`
#: time, backed up into `.integrity/`, verified before every run, and restored
#: from the backup on an unexplained change. Adding a name here extends the
#: fence to another file without touching the supervisor's loop.
PROTECTED = {
    "boundaries": "BOUNDARIES.md",
    "check": "check.sh",
}


class VerifyOutcome:
    """What one pre-run verification found. Named fields, not a tuple."""

    __slots__ = ("explained", "restored", "violations")

    def __init__(self):
        self.violations: list[dict] = []
        self.restored: list[str] = []
        self.explained: list[str] = []


def sha256(path: Path) -> str | None:
    """The recorded identity of a file. None when it cannot be read, which is
    itself a mismatch against any recorded hash — deletion is a violation."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def backup_dir(goal: Path) -> Path:
    return goal / ".integrity"


def _refresh_backup(goal: Path, name: str) -> None:
    """Copy the current content into `.integrity/` with mode 0400.

    0400 is decoration, not enforcement — the supervisor runs as the same user
    as the agent, so either can chmod or rename over the copy. The hash in
    state.json is the enforcement; the 0400 mode only raises the bar for an
    accident and keeps the copy from being a tempting write target.
    """
    src = goal / PROTECTED[name]
    if not src.exists():
        return
    dst = backup_dir(goal) / PROTECTED[name]
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        # An existing 0400 copy is owner-read-only, and this process IS the
        # owner — regain write for the refresh (approving an amendment rewrites
        # check.sh, which re-anchors this copy to the new predicate).
        dst.chmod(0o600)
    dst.write_bytes(src.read_bytes())
    dst.chmod(0o400)


def record(goal: Path, state: dict) -> None:
    """Stamp state["integrity"] and back up the protected files. Call at creation.

    Runs in `perpetua new` after BOUNDARIES.md, check.sh and goal.json exist but
    before state.json is first written, so a goal has either a full integrity
    record or none — never a partial one where the supervisor enforces one file
    but not the other.
    """
    state["integrity"] = {}
    for name, filename in PROTECTED.items():
        path = goal / filename
        state["integrity"][name] = {"hash": sha256(path), "at": now_iso()}
        _refresh_backup(goal, name)


def verify(goal: Path) -> VerifyOutcome:
    """Compare every protected file against its recorded hash. Runs pre-run.

    Outcomes, per file:
      - matches the recorded hash: nothing.
      - check.sh differs but is *exactly* an approved amendment's applied
        content (crash window between apply and re-anchor): the difference is
        explained, the hash and backup are re-anchored to it, and the goal
        keeps running.
      - anything else: restore from `.integrity/` where a copy exists, and
        report a violation so the supervisor can pause, post and notify.

    A goal with no integrity record predates the feature and is skipped —
    schema fields stay optional on read, so old goals keep working.
    """
    out = VerifyOutcome()
    try:
        state = pp_state.load(goal)
    except FileNotFoundError:
        return out
    integrity = state.get("integrity")
    if not integrity:
        return out
    for name, filename in PROTECTED.items():
        rec = integrity.get(name)
        if not rec or not rec.get("hash"):
            continue
        path = goal / filename
        # The fence is a regular file, checked BEFORE the hash, not after:
        # hashing through a symlink reads the attacker's content, and hashing a
        # fifo BLOCKS. A symlink or fifo at the protected path is itself a
        # violation, even when the bytes behind it match.
        try:
            regular = path.is_file() and not path.is_symlink()
        except OSError:
            regular = False
        current = sha256(path) if regular else None
        if current == rec.get("hash"):
            continue
        if name == "check":
            # Imported lazily: pp_amend imports this module at top level, so a
            # top-level import here would be a cycle. The ledger is its owner;
            # this is the one read of it that integrity needs.
            from pp_amend import approved_matches

            match = approved_matches(goal, current)
            if match is not None:
                out.explained.append(name)
                amendment_n = match.get("n")

                def anchor(s, digest=current, amendment_n=amendment_n):
                    check = s.setdefault("integrity", {}).setdefault("check", {})
                    check["hash"] = digest
                    check["anchored_at"] = now_iso()
                    check["amendment"] = amendment_n

                pp_state.patch(goal, anchor)
                _refresh_backup(goal, name)
                continue
        # Violation: restore where we can, report regardless. The pause is the
        # point; the restore is only so the fence is standing when the human
        # looks.
        copy = backup_dir(goal) / filename
        if copy.exists():
            try:
                # Unlink the NAME, not the target: if the tamper was a symlink,
                # write_bytes follows the link and overwrites the attacker's
                # file, leaving the fence permanently aliased. Remove only the
                # name, then copy a real file onto it.
                path.unlink(missing_ok=True)
                path.write_bytes(copy.read_bytes())
                path.chmod(0o755 if name == "check" else 0o644)
                out.restored.append(name)
            except OSError:
                pass
        out.violations.append(
            {"name": name, "detail": "tampered or deleted outside the amendment path"}
        )
    return out
