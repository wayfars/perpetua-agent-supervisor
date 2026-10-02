"""Revision-bound check receipts. A pass is evidence, never a criterion tick.

These are same-user operational records, not cryptographic attestations against
malicious workers. Runs whose workspace changed during checking are unverified.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import uuid
from pathlib import Path

from pp_common import flock, now_iso, read_json, write_json


def revision(workspace: Path) -> str | None:
    """Hash tracked and untracked nonignored content, without the activity cap."""
    try:
        p = subprocess.run(["git", "-C", str(workspace), "ls-files", "-z",
                            "--cached", "--others", "--exclude-standard"],
                           capture_output=True, timeout=30)
        if p.returncode:
            return None
        h = hashlib.sha256()
        for name in sorted(set(p.stdout.split(b"\0")) - {b""}):
            h.update(name + b"\0")
            path = workspace / name.decode("utf-8", "surrogateescape")
            if path.is_symlink():
                h.update(b"symlink:" + str(path.readlink()).encode())
            elif path.is_file():
                h.update(str(path.stat().st_mode & 0o777).encode())
                with path.open("rb") as fh:
                    for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                        h.update(chunk)
            elif not path.exists():
                h.update(b"deleted")
            else:
                return None
            h.update(b"\0")
        return h.hexdigest()
    except (OSError, subprocess.SubprocessError):
        return None


def check_hash(goal: Path) -> str | None:
    p = goal / "check.sh"
    try:
        return hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() and not p.is_symlink() else None
    except OSError:
        return None


def record(goal: Path, result, *, before: str | None, predicate: str | None,
           workspace: Path, started_at: str, elapsed_s: float) -> dict:
    after = revision(workspace)
    stable = bool(before and before == after and predicate and predicate == check_hash(goal))
    verdict = ("pass" if result.passed else "fail") if result.ran and stable else "unknown"
    rec = {"schema_version": 1, "id": uuid.uuid4().hex, "at": now_iso(),
           "started_at": started_at, "elapsed_s": round(elapsed_s, 3),
           "workspace": str(workspace), "revision": after, "check_hash": predicate,
           "stable": stable, "verdict": verdict, "ran": result.ran,
           "returncode": result.returncode, "detail": result.detail,
           "run": (read_json(goal / "state.json", {}) or {}).get("run")}
    with flock(goal / ".evidence.lock"):
        with open(goal / "evidence.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
        if workspace == goal / "workspace":
            write_json(goal / "evidence-latest.json", rec)
    return rec


def latest(goal: Path, *, verify: bool = False) -> dict:
    rec = read_json(goal / "evidence-latest.json", {}) or {}
    if not rec:
        return {"verdict": "unknown", "detail": "No revision-bound check recorded."}
    if verify and (rec.get("revision") != revision(goal / "workspace")
                   or rec.get("check_hash") != check_hash(goal)):
        return {**rec, "verdict": "stale"}
    return rec
