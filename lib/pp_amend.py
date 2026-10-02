"""The charter-amendment ledger — the honest path for a mis-scoped goal.

The incident lesson R3: 30-40% of ExploitGym's targets were objectively
impossible, and impossible tasks were the engine that pushed agents into
cheating the harness. Perpetua's answer is to make re-scoping a first-class,
logged act: a session files a structured proposal, the cheap kinds apply
immediately and visibly, and the dangerous kinds — anything that would move the
completion predicate or the limits nobody agreed to — stop in a `pending/`
queue until a human approves them by name.

Why a ledger and not a tool flag: an amendment is the audit trail of the goal's
own definition. It has to survive the session that filed it (append-only
`NNNN.json` records in their own directory) and it has to be visible to the
human (`pending/` symlinks, `perpetua amend --list`, a count in
`perpetua status`). The check.sh integrity hash (pp_integrity) is re-anchored
ONLY through this module, which is what closes the "`cd ..` && edit check.sh"
hole: an approved amendment is the one way that hash legitimately moves.

The classification is by target, not by payload: adding a criterion is cheap,
editing or removing one is not; clarifying prose is cheap, rewriting the
objective is not. `change` is validated against the target's shape before the
record is written, so a malformed proposal fails at filing, not at approval.

Locking: the ledger is numbered and written under `amendments/.lock`.
Application mutates state.json or GOAL.md, which take their own locks, so the
ledger lock is always released before apply() runs — locks nest one deep.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pp_criteria
import pp_integrity
import pp_machine
import pp_notify
import pp_state
from pp_common import flock, now_iso, read_json, write_json

#: The goal-relative directory holding one NNNN.json per proposal, plus pending/
#: of symlinks to proposals that need a human. Both are outside KNOWLEDGE_GLOBS,
#: so filing or approving an amendment is bookkeeping, never progress.
LEDGER = "amendments"
PENDING = "amendments/pending"

#: #2: decomposing a criterion into children is a SOFT change (it only ever
#: adds nodes, so nothing already relied on disappears), exactly like adding a
#: plain criterion. Re-parenting or removing one is HARD — it can change what
#: an existing rollup means for whoever already depends on it — so it stays
#: folded into criteria-edit/criteria-remove below rather than getting its own
#: target.
SOFT_TARGETS = {"notes", "goal-prose", "criteria-add", "criteria-reorder",
                "criteria-decompose"}
HARD_TARGETS = {
    "criteria-edit",
    "criteria-remove",
    "check.sh",
    "objective",
    "goal.json",
}

#: goal.json keys a session may propose to change, and the value type allowed.
#: Anything outside this allowlist is refused at filing — the amendment path is
#: for the goal's operating limits, not for arbitrary configuration.
GOAL_JSON_LIMITS = {
    key: val
    for key, val in (
        ("session_timeout_s", int),
        ("stall_timeout_s", int),
        # Every other per-goal operating limit is amendable; leaving this one
        # out meant a session that watched its own check.sh outgrow its budget
        # could ask for more session time but not for more check time, and only
        # a human editing goal.json could fix it.
        ("check_timeout_s", int),
        ("max_runs", int),
        ("no_progress_limit", int),
        ("consolidate_every", int),
        ("probe_limit", int),
        ("allow_hosted", bool),
        ("notify", bool),
        ("budget", dict),
        ("routing", dict),
        ("backend_timeouts", dict),
    )
}

CRITERION_ID = r"^c\d+$"


def classify(target: str) -> str:
    if target in SOFT_TARGETS:
        return "soft"
    if target in HARD_TARGETS:
        return "hard"
    raise ValueError(
        f"unknown amendment target {target!r} — one of "
        f"{', '.join(sorted(SOFT_TARGETS | HARD_TARGETS))}"
    )


def path_for(goal: Path, n: int) -> Path:
    return goal / LEDGER / f"{n:04d}.json"


def pending_path_for(goal: Path, n: int) -> Path:
    return goal / PENDING / f"{n:04d}.json"


def _next_n(goal: Path) -> int:
    d = goal / LEDGER
    biggest = 0
    if d.exists():
        for f in d.glob("*.json"):
            try:
                biggest = max(biggest, int(f.stem))
            except ValueError:
                continue
    return biggest + 1


def load_record(goal: Path, n: int) -> dict | None:
    return read_json(path_for(goal, n))


def list_records(goal: Path) -> list[dict]:
    """All amendments, newest first. A torn record must not break the read."""
    d = goal / LEDGER
    if not d.exists():
        return []
    out = []
    for f in sorted(d.glob("*.json"), reverse=True):
        try:
            rec = json.loads(f.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError):
            continue
        rec["pending"] = (goal / PENDING / f.name).exists()
        out.append(rec)
    return out


def pending_count(goal: Path) -> int:
    """Symlinks are the ledger's own index of what needs a human."""
    d = goal / PENDING
    return len(list(d.glob("*.json"))) if d.exists() else 0


def _validate_change(target: str, change) -> None:
    """A proposal must be mechanically applicable; reject it at the door."""
    if target in ("notes", "goal-prose", "objective", "check.sh"):
        if not isinstance(change, str) or not change.strip():
            raise ValueError(
                f"amendment target {target!r} needs a non-empty text change"
            )
    elif target == "criteria-add":
        if not isinstance(change, list) or not change:
            raise ValueError(
                "amendment target 'criteria-add' needs a non-empty list of "
                "criterion texts, or {'text': ..., 'parent': 'cN'} items"
            )
        for item in change:
            if isinstance(item, str):
                if not item.strip():
                    raise ValueError("criteria-add: an item's text is empty")
            elif isinstance(item, dict):
                if not str(item.get("text", "")).strip():
                    raise ValueError("criteria-add: an item needs 'text'")
            else:
                raise ValueError(
                    f"criteria-add: item must be a string or a dict, got "
                    f"{type(item).__name__}"
                )
    elif target == "criteria-reorder":
        if (
            not isinstance(change, list)
            or not change
            or not all(isinstance(x, str) and x.strip() for x in change)
        ):
            raise ValueError(
                "amendment target 'criteria-reorder' needs a non-empty list "
                "of criterion ids"
            )
    elif target == "criteria-decompose":
        if not isinstance(change, dict) or not str(change.get("id", "")).strip():
            raise ValueError(
                "amendment target 'criteria-decompose' needs "
                "{'id': 'cN', 'into': [text, ...]}"
            )
        into = change.get("into")
        if (
            not isinstance(into, list)
            or not into
            or not all(isinstance(x, str) and x.strip() for x in into)
        ):
            raise ValueError(
                "amendment target 'criteria-decompose' needs a non-empty "
                "'into' list of new criterion texts"
            )
    elif target == "criteria-edit":
        if not isinstance(change, dict) or not str(change.get("id", "")).strip():
            raise ValueError("amendment target 'criteria-edit' needs {'id': 'cN', ...}")
        if "text" not in change and "parent" not in change:
            raise ValueError(
                "amendment target 'criteria-edit' needs 'text' and/or "
                "'parent' to change — otherwise there is nothing to edit"
            )
    elif target == "criteria-remove":
        if not isinstance(change, dict) or not str(change.get("id", "")).strip():
            raise ValueError(f"amendment target {target!r} needs {{'id': 'cN', ...}}")
    elif target == "goal.json":
        if not isinstance(change, dict) or not change:
            raise ValueError("amendment target 'goal.json' needs a dict of limits")
        for key, value in change.items():
            if key not in GOAL_JSON_LIMITS:
                raise ValueError(
                    f"goal.json amendment refuses key {key!r} — allowed: "
                    f"{', '.join(sorted(GOAL_JSON_LIMITS))}"
                )
            expected = GOAL_JSON_LIMITS[key]
            if key in {"budget", "routing", "backend_timeouts"}:
                from pp_control import validate_policy
                validate_policy({key: value})
            if value is not None and not isinstance(value, expected):
                raise ValueError(
                    f"goal.json amendment key {key!r} must be {expected.__name__}"
                )


def _append_to_goald(goal: Path, rec: dict) -> None:
    """Append a signed amendment block. The one mechanical way prose clarifies:
    spliced mid-document edits need a model, and the ledger is required to be
    reproducible by a human — so the change is attached whole and attributed."""
    who = f"{rec.get('author') or 'unknown'}" + (
        f" (run {rec['run']})" if rec.get("run") else ""
    )
    block = (
        f"\n## Amendment A{rec['n']:04d} ({rec['target']}) — filed by {who} "
        f"on {rec.get('at')}\n\n{rec['change']}\n"
    )
    if rec.get("rationale"):
        block += f"\n_Rationale: {rec['rationale']}_\n"
    with open(goal / "GOAL.md", "a", encoding="utf-8") as fh:
        fh.write(block)


def _rewrite_objective(goal: Path, new_text: str) -> None:
    """Replace the paragraph under GOAL.md's `## Objective` heading.

    Mechanical on purpose: the template's objective is the first paragraph after
    that heading, and re-splicing arbitrary prose is a model's job, not the
    ledger's. A goal whose GOAL.md lost the heading gets the amendment appended
    like a note instead of failing."
    """
    path = goal / "GOAL.md"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    head, sep, tail = text.partition("\n## Objective")
    if not sep:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"\n## Objective amendment\n\n{new_text.strip()}\n")
        return
    # The objective is the paragraph between its heading and the next one; a
    # goal whose objective is the LAST section has no next heading, so this is
    # a partition, not a split — an unpack that assumed two parts would crash
    # exactly on the well-formed case.
    _, sep2, more = tail.partition("\n## ")
    new_tail = f"\n\n{new_text.strip()}\n\n"
    if sep2:
        new_tail += sep2 + more
    path.write_text(head + sep + new_tail, encoding="utf-8")


def _write_check(goal: Path, change: str) -> None:
    body = change if change.startswith("#!") else "#!/usr/bin/env bash\n" + change
    check = goal / "check.sh"
    check.write_text(body.rstrip() + "\n", encoding="utf-8")
    check.chmod(0o755)


def _patch_goal_json(goal: Path, change: dict) -> None:
    cfg = read_json(goal / "goal.json", {}) or {}
    cfg.update({k: v for k, v in change.items() if v is not None})
    write_json(goal / "goal.json", cfg)


def _new_criterion_id(crit: list) -> str:
    """The next `cN` id, past the highest one already used.

    `len(crit) + 1` (the old scheme) collides the instant a removal and an
    addition have both happened: remove c2 from {c1,c2,c3} and the next add
    reuses "c3" for a criterion that is not the one already holding that id.
    Scanning the existing ids is the only way to keep them unique once
    `criteria-remove` exists.
    """
    biggest = 0
    for c in crit:
        m = re.match(r"^c(\d+)$", str(c.get("id", "")))
        if m:
            biggest = max(biggest, int(m.group(1)))
    return f"c{biggest + 1}"


def _mutate_criteria(state: dict, rec: dict):
    """The in-state part of every criteria amendment, in one place.

    #2: `parent` and `weight` are the only new fields a record can carry, and
    both stay absent unless a caller actually asked for a hierarchy — a flat
    `state.json` from before this existed must round-trip through here
    unchanged. Every parent reference is checked against `pp_criteria` before
    it is written: a cycle refused here can never surface later as an
    infinite rollup.
    """
    target, change = rec["target"], rec["change"]
    crit = state.setdefault("criteria", [])
    ids = {c.get("id") for c in crit}
    if target == "criteria-add":
        for item in change:
            if isinstance(item, str):
                text, parent, weight = item.strip(), None, None
            else:
                text = str(item.get("text", "")).strip()
                parent = item.get("parent") or None
                weight = item.get("weight")
            new_id = _new_criterion_id(crit)
            if parent and parent not in ids:
                raise ValueError(
                    f"criteria-add: no such parent criterion {parent!r}"
                )
            new_rec = {
                "id": new_id,
                "text": text,
                "done": False,
                "added_by": rec.get("author"),
                "added_at": rec.get("at"),
            }
            if parent:
                new_rec["parent"] = parent
            if weight not in (None, 1, 1.0):
                new_rec["weight"] = weight
            crit.append(new_rec)
            ids.add(new_id)
    elif target == "criteria-decompose":
        parent_id = change["id"]
        if parent_id not in ids:
            raise ValueError(f"criteria-decompose: no such criterion {parent_id!r}")
        for text in change["into"]:
            new_id = _new_criterion_id(crit)
            crit.append({
                "id": new_id, "text": text.strip(), "done": False,
                "parent": parent_id,
                "added_by": rec.get("author"), "added_at": rec.get("at"),
            })
            ids.add(new_id)
    elif target == "criteria-reorder":
        wanted = list(change)
        by_id = {c.get("id"): c for c in crit}
        if sorted(by_id) != sorted(wanted) or len(wanted) != len(by_id):
            raise ValueError(
                "criteria-reorder must list every criterion id exactly once"
            )
        state["criteria"] = [by_id[cid] for cid in wanted]
    elif target == "criteria-edit":
        cid = change["id"]
        for c in crit:
            if c.get("id") == cid:
                if "parent" in change:
                    new_parent = change.get("parent") or None
                    if new_parent and new_parent not in ids:
                        raise ValueError(
                            f"criteria-edit: no such parent criterion "
                            f"{new_parent!r}"
                        )
                    if pp_criteria.would_cycle(crit, cid, new_parent):
                        raise ValueError(
                            f"criteria-edit: parent {new_parent!r} would make "
                            f"{cid} its own ancestor — refusing the write"
                        )
                    if new_parent:
                        c["parent"] = new_parent
                    else:
                        c.pop("parent", None)
                if "text" in change:
                    c["text"] = str(change.get("text", "")).strip()
                break
        else:
            raise ValueError(f"no criterion with id {cid!r}")
    elif target == "criteria-remove":
        cid = change["id"]
        # A removed parent's children are not deleted with it: pp_criteria's
        # roots() already treats a dangling `parent` as "no parent", so they
        # become roots rather than vanishing along with the node that named
        # them.
        state["criteria"] = [c for c in crit if c.get("id") != cid]


def apply(goal: Path, n: int) -> dict:
    """Mechanically apply amendment n. Returns what changed.

    Called by propose() for soft amendments and by approve() for hard ones —
    always with the ledger lock already released, because the mutations here
    take their own locks (state.json) or are plain appends (GOAL.md).
    """
    rec = load_record(goal, n)
    if rec is None:
        raise FileNotFoundError(f"no amendment A{n:04d} in {goal / LEDGER}")
    target = rec["target"]
    applied = []
    if target in ("notes", "goal-prose"):
        _append_to_goald(goal, rec)
        applied.append("GOAL.md")
    elif target == "objective":
        _rewrite_objective(goal, str(rec["change"]))
        applied.append("GOAL.md")
    elif target in (
        "criteria-add",
        "criteria-decompose",
        "criteria-edit",
        "criteria-remove",
        "criteria-reorder",
    ):

        def mut(s, _rec=rec):
            _mutate_criteria(s, _rec)

        pp_state.patch(goal, mut)
        applied.append("state.criteria")
    elif target == "check.sh":
        _write_check(goal, str(rec["change"]))
        applied.append("check.sh")
    elif target == "goal.json":
        _patch_goal_json(goal, rec["change"])
        applied.append("goal.json")
    return {"applied": applied, "integrity_changed": (target == "check.sh")}


def _post(goal: Path, kind: str, rec: dict, extra: str = "") -> None:
    try:
        pp_machine.event(
            goal,
            kind,
            f"Amendment A{rec['n']:04d} [{rec['target']}/{rec['class']}] "
            f"filed by {rec.get('author') or 'unknown'}: "
            f"{str(rec.get('rationale') or rec.get('change'))[:200]}{extra}",
            run=rec.get("run"),
            n=rec["n"],
            target=rec["target"],
        )
    except Exception:  # noqa: BLE001
        # Best-effort side channel, same rule as the supervisor's own events:
        # a failed board post must never fail the amendment.
        return


def _notify_human(goal: Path, text: str) -> None:
    cfg = read_json(goal / "goal.json", {}) or {}
    pp_notify.notify(text, enabled=bool(cfg.get("notify", False)))


def propose(
    goal: Path,
    *,
    target: str,
    change,
    rationale: str = "",
    evidence: str = "",
    author: str = "",
    run: int | None = None,
) -> dict:
    """File an amendment. Soft ones are applied here and returned as such.

    The record is written under the ledger lock, then the lock is released and
    apply() runs — a state.json patch must never happen under the ledger lock.
    """
    cls = classify(target)
    _validate_change(target, change)
    rec = {
        "n": 0,
        "at": now_iso(),
        "author": author or "unknown",
        "run": run,
        "target": target,
        "change": change,
        "rationale": (rationale or "").strip(),
        "evidence": (evidence or "").strip(),
        "class": cls,
        "status": "pending",
        "pending": bool(cls == "hard"),
    }
    with flock(goal / LEDGER / ".lock"):
        n = _next_n(goal)
        rec["n"] = n
        write_json(path_for(goal, n), rec)
    if cls == "soft":
        result = apply(goal, n)
        with flock(goal / LEDGER / ".lock"):
            rec = load_record(goal, n)
            rec["status"] = "applied"
            rec["applied_at"] = now_iso()
            rec["applied"] = result["applied"]
            write_json(path_for(goal, n), rec)
        _post(goal, "amendment-applied", rec)
    else:
        pending = pending_path_for(goal, n)
        pending.parent.mkdir(parents=True, exist_ok=True)
        try:
            pending.symlink_to(f"../{pending.name}")
        except FileExistsError:
            pass
        _post(goal, "amendment-filed", rec)
        _notify_human(
            goal,
            f"⏳ perpetua '{goal.name}': amendment A{n:04d} "
            f"[{target}] needs your decision.\n"
            f"Rationale: {(rationale or change)[:400]}\n"
            f"Review: perpetua amend {goal.name} --list",
        )
    return rec


def approve(goal: Path, n: int) -> dict:
    """The human's yes. Applies the amendment and re-anchors integrity in one
    breath, so a goal whose predicate was just legitimately changed does not
    trip the very firewall that protects it."""
    rec = load_record(goal, n)
    if rec is None:
        raise FileNotFoundError(f"no amendment A{n:04d} in {goal / LEDGER}")
    if rec.get("class") != "hard":
        raise ValueError(
            f"A{n:04d} is {rec.get('class')} and was applied at filing — "
            f"nothing to approve"
        )
    if rec.get("status") != "pending":
        raise ValueError(f"A{n:04d} is already {rec.get('status')}")

    result = apply(goal, n)
    with flock(goal / LEDGER / ".lock"):
        rec = load_record(goal, n)
        rec["status"] = "approved"
        rec["approved_at"] = now_iso()
        if result["integrity_changed"]:
            rec["applied_hash"] = pp_integrity.sha256(goal / "check.sh")
        write_json(path_for(goal, n), rec)
    pending = pending_path_for(goal, n)
    with flock(goal / LEDGER / ".lock"):
        pending.unlink(missing_ok=True)

    if result["integrity_changed"]:
        # Re-anchor the supervisor's hash and restoration copy to the new
        # predicate. If this state patch is what crashes, pp_integrity.verify
        # finds the approved applied_hash and re-anchors on the next run.
        check_hash = pp_integrity.sha256(goal / "check.sh")

        def anchor(s, digest=check_hash, amendment_n=rec["n"]):
            check = s.setdefault("integrity", {}).setdefault("check", {})
            check["hash"] = digest
            check["anchored_at"] = now_iso()
            check["amendment"] = amendment_n

        pp_state.patch(goal, anchor)
        pp_integrity._refresh_backup(goal, "check")
    _post(
        goal,
        "amendment-approved",
        {**rec, "applied": result["applied"]},
        extra=f" — applied: {', '.join(result['applied'])}",
    )
    return {**rec, "applied": result["applied"]}


def reject(goal: Path, n: int, reason: str) -> dict:
    """The human's no. The proposal stays in the ledger, marked, so the goal's
    audit trail shows the re-scope was asked for and refused — a later session
    must not re-litigate it from scratch."""
    rec = load_record(goal, n)
    if rec is None:
        raise FileNotFoundError(f"no amendment A{n:04d} in {goal / LEDGER}")
    if rec.get("status") != "pending":
        raise ValueError(f"A{n:04d} is already {rec.get('status')}")
    with flock(goal / LEDGER / ".lock"):
        rec = load_record(goal, n)
        rec["status"] = "rejected"
        rec["rejected_at"] = now_iso()
        rec["reject_reason"] = reason
        write_json(path_for(goal, n), rec)
        pending_path_for(goal, n).unlink(missing_ok=True)
    _post(
        goal,
        "amendment-rejected",
        rec,
        extra=f" — rejected by the human: {reason[:200]}",
    )
    return rec


def approved_matches(goal: Path, digest: str) -> dict | None:
    """Which approved check.sh amendment applied exactly this content, if any.

    This is what makes the crash window between apply and re-anchor harmless:
    content that provably came from an approved amendment reads as explained,
    not as tampering. Content that matches nothing stays a violation — which is
    the whole point of hashing check.sh.
    """
    for rec in list_records(goal):
        if (
            rec.get("target") == "check.sh"
            and rec.get("status") == "approved"
            and rec.get("applied_hash") == digest
        ):
            return rec
    return None
