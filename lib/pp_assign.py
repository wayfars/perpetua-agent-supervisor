"""Structured delegation on the board — the ledger half of "build a way to
delegate, not own everything" (INCIDENT-LESSONS.md #6).

The incident's collective hit milestones through division of labour, but its
delegation ran on ad-hoc message conventions alone: those worked while everyone
was polite, alive, and reading, and they left the bookkeeping to the agents.
This is the bookkeeping, with the supervision deliberately left out.

* The LOG, board/assignments.jsonl, is append-only and carries every event —
  open, claim, resolve, comment. It is the single source of truth.
* The INDEX, board/assignments.json, is a derived fold over the log. It is a
  cache: it can always be deleted and rebuilt, which is exactly what makes a
  torn write survivable — the last log line may be half-written, and a reader
  treats it as the end of history rather than as an error.
* The BOARD is the notification. Every event also posts to the board so it
  flows through the normal unread path. The ledger is where truth lives; the
  board is where it is seen.
* ENFORCEMENT is none, deliberately. The supervisor guarantees the log is
  append-only and consistent and nothing more. Claiming a claimed assignment
  is refused by the fold — that is a consistency check, not an ownership rule.
  There is no claiming lock beyond the append lock, no "your claim, your
  problem" rule, and no referee for who may resolve what: the incident's norms
  (owner, HOLD, VETO) are conventions, and this ledger only makes the
  bookkeeping survive them.
"""
from __future__ import annotations

import json
from pathlib import Path

import pp_board
from pp_common import flock, log, now_iso, read_json, write_json

CHANNEL = "assignments"
OUTCOMES = ("done", "abandoned", "rejected")

#: The ledger gets its OWN lock file. board/.lock belongs to pp_board's index
#: and .state.lock belongs to state.json; nesting either of those inside this
#: lock (or this inside them) is the one-deep rule this codebase lives by, and
#: a separate file keeps the two ordering classes disjoint.
LOCK_NAME = ".assign.lock"


def _lock(goal: Path) -> Path:
    return pp_board.board_dir(goal) / LOCK_NAME


def log_path(goal: Path) -> Path:
    return pp_board.board_dir(goal) / "assignments.jsonl"


def index_path(goal: Path) -> Path:
    return pp_board.board_dir(goal) / "assignments.json"


def _iter_events(goal: Path):
    """Every event in append order, tolerating torn lines.

    Same rule as the board's reader: the append and the supervisor are different
    processes, and if one dies mid-write the last line can be half a JSON
    object. That line is the end of history, not corruption — the fold skips it
    and the next writer continues from what is whole.
    """
    p = log_path(goal)
    if not p.exists():
        return
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        # One unreadable ledger must never take out a session. Same rule as
        # pp_board._iter_messages one level up: report and step over.
        log(f"assignments: cannot read {p.name}: {type(exc).__name__}: {exc}")
        return
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue              # a torn line must never break the whole ledger


def fold(events) -> dict:
    """The assignment ledger, folded from the log: id -> record.

    Every event is read defensively. The log is append-only but never validated,
    so an event written against a newer schema has to read as a slightly empty
    version of itself rather than raise, and the fold has to ignore events it
    cannot attach (a claim for an assignment it never saw).
    """
    assignments: dict[str, dict] = {}
    for ev in events:
        evtype = ev.get("type")
        aid = ev.get("id")
        if evtype == "open":
            assignments[aid] = {
                "id": aid,
                "status": "open",
                "owner": ev.get("owner", ""),
                "title": ev.get("title", ""),
                "criteria": [str(c) for c in (ev.get("criteria") or [])],
                "bounty": ev.get("bounty") or None,
                "to": ev.get("to") or None,
                "claimed_by": None,
                "claimed_run": None,
                "outcome": None,
                "note": None,
                "resolved_by": None,
                "resolved_run": None,
                "comments": [],
                "opened_at": ev.get("ts", ""),
                "updated_at": ev.get("ts", ""),
            }
        elif evtype == "claim":
            a = assignments.get(aid)
            if a is None:
                continue
            a["status"] = "claimed"
            a["claimed_by"] = ev.get("claimer", "")
            a["claimed_run"] = ev.get("run")
            a["updated_at"] = ev.get("ts", "")
        elif evtype == "reassign":
            a = assignments.get(aid)
            if a is None:
                continue
            # Succession re-points the work as well as the history (#8). An
            # unclaimed assignment changes who it is addressed to; a claimed one
            # changes who is holding it, because the point of a succession is
            # that the claim did not die with the identity that made it.
            a["to"] = ev.get("to") or None
            if a["status"] == "claimed":
                a["claimed_by"] = ev.get("to") or a.get("claimed_by")
            a["updated_at"] = ev.get("ts", "")
        elif evtype == "resolve":
            a = assignments.get(aid)
            if a is None:
                continue
            out = ev.get("outcome", "")
            if out in OUTCOMES:
                a["status"] = out
            a["outcome"] = out or None
            a["note"] = ev.get("note", "") or None
            a["resolved_by"] = ev.get("by", "") or None
            a["resolved_run"] = ev.get("run")
            a["updated_at"] = ev.get("ts", "")
        elif evtype == "comment":
            a = assignments.get(aid)
            if a is None:
                continue
            a["comments"].append({"author": ev.get("author", ""),
                                  "body": ev.get("body", ""),
                                  "run": ev.get("run"),
                                  "ts": ev.get("ts", "")})
            a["updated_at"] = ev.get("ts", "")
    return assignments


def index(goal: Path) -> dict:
    """The derived index: id -> record. Reads the cache, or folds the log.

    A missing or stale index is a rebuild, not an error — but this read path
    never writes, because writing without the lock races a concurrent writer and
    the cache is cheap to reconstruct either way. The next mutation under the
    lock restores the file.
    """
    cached = read_json(index_path(goal))
    if cached is not None:
        return cached
    return fold(_iter_events(goal))


def rebuild(goal: Path) -> dict:
    """Fold the log and rewrite the index. The recovery path for a lost cache."""
    with flock(_lock(goal)):
        idx = fold(_iter_events(goal))
        write_json(index_path(goal), idx)
    return idx


def _notify(goal: Path, *, op: str, assignment_id: str, subject: str,
            body: str, author: str, run: int | None = None,
            to: str | None = None) -> dict | None:
    """Post the event to the board. Best effort, like every side channel here.

    The board post happens AFTER the ledger write and the lock release: a failed
    notification must not roll back an event that already landed, and holding
    the ledger lock while taking board/.lock would nest the two lock classes.
    """
    try:
        return pp_board.post(goal, channel=CHANNEL, author=author, run=run,
                             subject=subject, body=body, to=to,
                             meta={"assignment_id": assignment_id, "op": op})
    except Exception as exc:                            # noqa: BLE001
        log(f"assignments: {op} notification not posted: "
            f"{type(exc).__name__}: {exc}")
        return None


def _append(goal: Path, events: list, event: dict) -> dict:
    """Serialize one event and rebuild the index. Caller holds the lock."""
    with log_path(goal).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False) + "\n")
    idx = fold(events + [event])
    write_json(index_path(goal), idx)
    return idx


def open(goal: Path, *, owner: str, title: str, criteria=None,
         bounty: str | None = None, to: str | None = None,
         author: str | None = None, run: int | None = None) -> dict:
    """Open a new assignment. Returns its record plus the board notification."""
    if not str(title or "").strip():
        raise ValueError("open requires a non-empty title")
    author = author or owner
    ts = now_iso()
    with flock(_lock(goal)):
        events = list(_iter_events(goal))
        assignments = fold(events)
        # The fold keeps every opened assignment, resolved or not, so the number
        # of records IS the number of open events — the next id is that plus one.
        aid = f"a{len(assignments) + 1}"
        event = {"type": "open", "id": aid, "owner": owner, "title": str(title).strip(),
                 "criteria": [str(c) for c in (criteria or []) if str(c).strip()],
                 "bounty": bounty, "to": to, "author": author, "run": run, "ts": ts}
        idx = _append(goal, events, event)
    body = [f"**{aid}** opened by {owner}: {title.strip()}"]
    if event["criteria"]:
        body += ["criteria:"] + [f"- {c}" for c in event["criteria"]]
    if to:
        body.append(f"offered to: {to}")
    if bounty:
        body.append(f"bounty: {bounty}")
    posted = _notify(goal, op="open", assignment_id=aid,
                     subject=f"open {aid}: {title.strip()[:80]}",
                     body="\n".join(body), author=author, run=run, to=to)
    return {"assignment": idx[aid], "posted": posted}


def claim(goal: Path, *, id: str, claimer: str,
          author: str | None = None, run: int | None = None) -> dict:
    """Claim an open assignment. Exactly one concurrent claim wins.

    The refusal here is a consistency check on the fold, nothing more: status is
    derived from the log, the check runs under the same lock as the append, and
    there is no ownership beyond it — this is "the ledger says claimed", not
    "the supervisor decided".
    """
    author = author or claimer
    ts = now_iso()
    with flock(_lock(goal)):
        events = list(_iter_events(goal))
        assignments = fold(events)
        a = assignments.get(id)
        if a is None:
            return {"error": f"no such assignment: {id}"}
        if a["status"] == "claimed":
            return {"error": f"already claimed by {a['claimed_by']}",
                    "assignment": a}
        if a["status"] != "open":
            return {"error": f"assignment {id} is already {a['status']}",
                    "assignment": a}
        event = {"type": "claim", "id": id, "claimer": claimer,
                 "author": author, "run": run, "ts": ts}
        idx = _append(goal, events, event)
    posted = _notify(goal, op="claim", assignment_id=id,
                     subject=f"claim {id}",
                     body=f"{claimer} claimed **{id}** — {a['title']}",
                     author=author, run=run)
    return {"assignment": idx[id], "posted": posted}


def resolve(goal: Path, *, id: str, outcome: str, note: str,
            by: str | None = None, author: str | None = None,
            run: int | None = None) -> dict:
    """Close an assignment with an outcome: done, abandoned, or rejected."""
    outcome = (outcome or "").strip().lower()
    if outcome not in OUTCOMES:
        raise ValueError(f"resolve outcome must be one of {', '.join(OUTCOMES)}")
    if not str(note or "").strip():
        raise ValueError("resolve requires a note saying what happened")
    by = by or author or ""
    author = author or by
    ts = now_iso()
    with flock(_lock(goal)):
        events = list(_iter_events(goal))
        assignments = fold(events)
        a = assignments.get(id)
        if a is None:
            return {"error": f"no such assignment: {id}"}
        if a["status"] not in ("open", "claimed"):
            return {"error": f"assignment {id} is already {a['status']}",
                    "assignment": a}
        event = {"type": "resolve", "id": id, "outcome": outcome,
                 "note": str(note).strip(), "by": by, "author": author,
                 "run": run, "ts": ts}
        idx = _append(goal, events, event)
    posted = _notify(goal, op="resolve", assignment_id=id,
                     subject=f"{outcome} {id}",
                     body=f"{by} resolved **{id}** as {outcome} — {note.strip()}",
                     author=author, run=run)
    return {"assignment": idx[id], "posted": posted}


def comment(goal: Path, *, id: str, body: str, author: str,
            run: int | None = None) -> dict:
    """Attach a note to an assignment — open or not, on purpose."""
    if not str(body or "").strip():
        raise ValueError("comment requires a body")
    body = str(body).strip()
    ts = now_iso()
    with flock(_lock(goal)):
        events = list(_iter_events(goal))
        assignments = fold(events)
        a = assignments.get(id)
        if a is None:
            return {"error": f"no such assignment: {id}"}
        event = {"type": "comment", "id": id, "author": author,
                 "body": body, "run": run, "ts": ts}
        idx = _append(goal, events, event)
    posted = _notify(goal, op="comment", assignment_id=id,
                     subject=f"comment {id}",
                     body=f"{author} on **{id}** ({a['title']}):\n{body}",
                     author=author, run=run)
    return {"assignment": idx[id], "posted": posted}


def reassign(goal: Path, *, id: str, to: str, by: str,
             author: str | None = None, run: int | None = None) -> dict:
    """Re-point a live assignment at another persona — the succession primitive.

    Only open and claimed assignments move: a resolved one is history, and
    history does not change owner. Like every other mutation here this is an
    appended event, not an edit, so the ledger still reads as what actually
    happened rather than as who holds it now.
    """
    to = str(to or "").strip()
    if not to:
        raise ValueError("reassign requires a persona to hand the work to")
    author = author or by
    ts = now_iso()
    with flock(_lock(goal)):
        events = list(_iter_events(goal))
        assignments = fold(events)
        a = assignments.get(id)
        if a is None:
            return {"error": f"no such assignment: {id}"}
        if a["status"] not in ("open", "claimed"):
            return {"error": f"assignment {id} is already {a['status']}",
                    "assignment": a}
        was = a["status"]
        event = {"type": "reassign", "id": id, "to": to, "by": by,
                 "author": author, "run": run, "ts": ts}
        idx = _append(goal, events, event)
    posted = _notify(goal, op="reassign", assignment_id=id,
                     subject=f"reassign {id} → {to}",
                     body=f"{by} re-pointed **{id}** ({a['title']}) at {to} "
                          f"— it was {was}.",
                     author=author, run=run, to=to)
    return {"assignment": idx[id], "posted": posted}


def open_for(goal: Path, persona: str | None) -> list[dict]:
    """What a session's briefing should show: open assignments it could act on.

    Addressed to the running persona, or unassigned. Claimed and resolved
    assignments are someone else's story and the board's unread path already
    tells a session about activity it missed — this section is about what the
    session can still DO, so it reads the derived index directly and consumes
    no cursor (not the briefing's, not any author's).
    """
    out = [a for a in index(goal).values()
           if a.get("status") == "open"
           and (not a.get("to") or a.get("to") == persona)]
    out.sort(key=lambda a: a.get("updated_at") or "", reverse=True)
    return out


def render_open(assignments: list[dict]) -> str:
    """The briefing's compact view: one line per assignment we can act on."""
    lines = ["## Open assignments", ""]
    for a in assignments:
        meta = [f"by {a.get('owner') or '?'}"]
        if a.get("bounty"):
            meta.append(f"bounty: {a['bounty']}")
        if a.get("to"):
            meta.append(f"to: {a['to']}")
        ncrit = len(a.get("criteria") or [])
        if ncrit:
            meta.append(f"{ncrit} criter{'ion' if ncrit == 1 else 'ia'}")
        lines.append(f"- **{a['id']}** {a.get('title', '') or ''} "
                     f"({'; '.join(meta)})")
    lines += ["",
              ("Claim an open one with `perpetua_assign` (action \"claim\") — "
               "list all first; open new ones with action \"open\"; comment via "
               "action \"comment\"; resolve your own when done.")]
    return "\n".join(lines)


def render_list(assignments: list[dict]) -> str:
    """One line per assignment, newest first — the agent-facing list."""
    if not assignments:
        return "_(no assignments)_"
    lines = []
    for a in assignments:
        if a["status"] == "claimed":
            who = f"claimed by {a['claimed_by']}"
        elif a["status"] == "open":
            who = f"→ {a['to']}" if a.get("to") else "open to anyone"
        else:
            who = f"resolved {a.get('outcome') or a['status']} by " \
                  f"{a.get('resolved_by') or '?'}"
        title = a.get("title", "") or ""
        note = f" — {a['note']}" if a.get("note") else ""
        lines.append(f"- **{a['id']}** [{a['status']}] {title} ({who}){note}")
    return "\n".join(lines)
