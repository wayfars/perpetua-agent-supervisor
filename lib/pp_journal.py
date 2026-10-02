"""The handoff journal — the one artefact a run is not allowed to skip.

Every run must leave a handoff. If the agent writes one itself (via
perpetua_end_session) it lands here; if the run dies without one, the supervisor's
reaper synthesises it from the session transcript. Either way the successor gets
something. A run that vanishes silently is the single failure that makes a
thousand-session goal worthless.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from pp_common import now_iso, read_json, run_dir, run_id, write_json

DIGEST_ENTRIES = 40          # entries kept in JOURNAL.md
DIGEST_CHARS = 2200          # per-entry cap inside the digest


def handoff_path(goal: Path, n: str | int) -> Path:
    return run_dir(goal, n) / "handoff.json"


def entry_path(goal: Path, n: str | int) -> Path:
    return goal / "journal" / f"{run_id(n)}.md"


def has_handoff(goal: Path, n: str | int) -> bool:
    return handoff_path(goal, n).exists()


def write_handoff(goal: Path, n: str | int, data: dict) -> dict:
    data.setdefault("run", n)
    data.setdefault("written_at", now_iso())
    data.setdefault("source", "agent")
    write_json(handoff_path(goal, n), data)
    return data


def attest(goal: Path, n: str | int, attestation: dict) -> dict | None:
    """Attach the supervisor's own measurement to a handoff, before it is rendered.

    The supervisor already computes whether the run moved the goal — that boolean
    was previously spent on a breaker counter and thrown away. Writing it back
    here is what makes a claim inheritable *with its doubt attached*: the
    successor reads the same journal entry, and sees both what the session said
    it did and what the harness could actually observe.

    Must be called BEFORE commit(), which renders handoff.json into journal/.
    """
    data = read_json(handoff_path(goal, n))
    if data is None:
        return None
    data["attestation"] = {**attestation, "at": now_iso()}
    write_json(handoff_path(goal, n), data)
    return data


def flags_for(handoff: dict, *, progressed: bool, workspace: Path) -> list[str]:
    """Deterministic doubts about a handoff. No model, no judgement calls.

    Each flag is a disagreement between what the session claimed and what the
    harness measured. None of them accuses the session of lying — a session can
    do real work that leaves no trace — but the successor is entitled to know
    which is which.
    """
    ev = handoff.get("evidence") or {}
    flags: list[str] = []
    done = [d for d in (handoff.get("done") or []) if str(d).strip()]

    if done and not progressed:
        flags.append("unverified-work")

    check = ev.get("check") or {}
    if handoff.get("reason") == "goal-complete" and not check.get("passed"):
        flags.append("unverified-completion")

    for sha in _claimed_commits(handoff, ev):
        if not _commit_exists(workspace, sha):
            flags.append("commit-missing")
            break

    if (not ev.get("commits_this_run") and not ev.get("dirty")
            and not ev.get("criteria_ticked")
            and not (ev.get("board_seq_range") or [None, None])[1]):
        flags.append("no-evidence")
    return flags


def _claimed_commits(handoff: dict, ev: dict) -> list[str]:
    shas = []
    head = ev.get("commit")
    if head:
        shas.append(str(head))
    return shas


def _commit_exists(workspace: Path, sha: str) -> bool:
    try:
        proc = subprocess.run(
            ["git", "-C", str(workspace), "cat-file", "-e", f"{sha}^{{commit}}"],
            capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return True          # cannot check is not the same as does not exist
    return proc.returncode == 0


def _evidence_block(handoff: dict) -> str:
    ev = handoff.get("evidence") or {}
    att = handoff.get("attestation") or {}
    if not ev and not att:
        return ""
    bits = []
    if ev.get("commit"):
        bits.append(f"commit `{str(ev['commit'])[:10]}`"
                    + (f" ({ev['commits_this_run']} this run)"
                       if ev.get("commits_this_run") is not None else ""))
    if ev.get("files_changed") is not None:
        bits.append(f"{ev['files_changed']} file(s) uncommitted"
                    if ev.get("dirty") else "working tree clean")
    if ev.get("criteria_ticked"):
        bits.append("criteria ticked: " + ", ".join(map(str, ev["criteria_ticked"])))
    rng = ev.get("board_seq_range")
    if rng and rng[1]:
        bits.append(f"board #{rng[0]}–#{rng[1]}")
    check = ev.get("check") or {}
    if check.get("ran"):
        bits.append("check.sh " + ("PASSED" if check.get("passed") else "failed"))
    if "progressed" in att:
        bits.append("harness measured progress: "
                    + ("yes" if att["progressed"] else "NO"))
    if not bits:
        return ""
    return "\n**Evidence**\n" + "\n".join(f"- {b}" for b in bits) + "\n"


def _lines(value) -> str:
    if not value:
        return "_none_"
    if isinstance(value, str):
        return value.strip()
    return "\n".join(f"- {str(v).strip()}" for v in value if str(v).strip()) or "_none_"


def render(handoff: dict) -> str:
    n = run_id(handoff.get("run", 0))
    src = handoff.get("source", "agent")
    tag = "" if src == "agent" else f" _(reconstructed by {src} — the session did not write its own)_"
    flags = (handoff.get("attestation") or {}).get("flags") or []
    warn = (f"> ⚠ unverified: {', '.join(flags)} — the harness could not confirm "
            f"the claims below. Treat them as leads, not as facts.\n\n") if flags else ""
    return f"""## Run {n} — {handoff.get('written_at', '?')}{tag}

{warn}
- **ended because:** {handoff.get('reason', 'unknown')}
- **backend:** {handoff.get('backend', '?')} · **persona:** {handoff.get('persona') or 'none'}
- **session:** `{handoff.get('session_id', '?')}`

**Done this run**
{_lines(handoff.get('done'))}

**Learned**
{_lines(handoff.get('learned'))}

**Next steps**
{_lines(handoff.get('next_steps'))}

**Blockers**
{_lines(handoff.get('blockers'))}

**Open questions**
{_lines(handoff.get('open_questions'))}
{_evidence_block(handoff)}"""


def commit(goal: Path, n: str | int) -> Path | None:
    """Render runs/NNNN/handoff.json into journal/NNNN.md and refresh JOURNAL.md."""
    data = read_json(handoff_path(goal, n))
    if data is None:
        return None
    path = entry_path(goal, n)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(data), encoding="utf-8")
    rebuild_digest(goal)
    return path


def _journal_entries(jdir: Path) -> list[Path]:
    """Journal entry files, newest run first, probes inside a run ascending.

    A filename that is not digits or digits.digits is not an entry — notes.md
    and 00xx.md get globbed, and letting one sort (or raise) would take the
    digest down on a single junk file. Failure class 3, applied to the journal.
    """
    found = []
    for p in jdir.glob("[0-9]*.md"):
        if not p.is_file():
            continue        # a fifo or a dir here would open() forever — skip
        left, _, right = p.stem.partition(".")
        if not left.isdigit():
            continue
        if right and not right.isdigit():
            continue
        k = int(right) if right else None
        # The negative int keeps newest runs first, as the old reverse sort
        # did; a run's own entry then sorts above its probes, probes ascending.
        found.append(((-int(left), 0 if k is None else 1, k if k is not None else -1), p))
    return [p for _, p in sorted(found)]


def rebuild_digest(goal: Path) -> Path:
    jdir = goal / "journal"
    jdir.mkdir(parents=True, exist_ok=True)
    entries = _journal_entries(jdir)[:DIGEST_ENTRIES]
    body = [f"# Journal — {goal.name}", "",
            f"_Newest first. {len(entries)} of "
            f"{len(_journal_entries(jdir))} entries shown; the rest are in "
            f"`journal/`._", ""]
    for e in entries:
        text = e.read_text(encoding="utf-8", errors="replace")
        if len(text) > DIGEST_CHARS:
            text = text[:DIGEST_CHARS] + f"\n\n_… truncated; full entry in `journal/{e.name}`_\n"
        body.append(text.rstrip())
        body.append("")
    out = goal / "JOURNAL.md"
    out.write_text("\n".join(body), encoding="utf-8")
    return out


#: What `render` stamps on an entry no session wrote. A run of these carries the
#: information of one of them.
RECONSTRUCTED = "_(reconstructed by"


def is_reconstructed(entry: str) -> bool:
    """Was this journal entry written by the reaper rather than by a session?"""
    return RECONSTRUCTED in (entry or "")


def recent(goal: Path, limit: int = 5) -> list[str]:
    jdir = goal / "journal"
    if not jdir.exists():
        return []
    return [p.read_text(encoding="utf-8", errors="replace")
            for p in _journal_entries(jdir)[:limit]]


def recent_meaningful(goal: Path, limit: int = 5, scan: int = 20
                      ) -> tuple[list[str], int]:
    """The most recent entries a SESSION wrote, and how many reaped ones were
    skipped to find them.

    Four consecutive reaper skeletons — which is what the first live goal
    produced on 2026-09-03 — say "the session ended without a handoff and could
    not be summarised" four times, and between them they pushed the operator's
    own board messages out of the briefing budget entirely. The lowest-value
    content in the system was displacing the highest. They are still on disk and
    still in JOURNAL.md; they simply stop being worth four slots in the one
    place that has to fit in a context window.
    """
    jdir = goal / "journal"
    if not jdir.exists():
        return [], 0
    kept: list[str] = []
    skipped = 0
    for path in _journal_entries(jdir)[:scan]:
        text = path.read_text(encoding="utf-8", errors="replace")
        if is_reconstructed(text):
            skipped += 1
            continue
        kept.append(text)
        if len(kept) >= limit:
            break
    return kept, skipped


def reconcile(goal: Path, upto_run: int) -> int:
    """Render any handoff whose journal entry is missing (supervisor died mid-commit)."""
    fixed = 0
    for n in range(1, upto_run + 1):
        if has_handoff(goal, n) and not entry_path(goal, n).exists():
            commit(goal, n)
            fixed += 1
    return fixed
