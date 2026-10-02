"""Persona identity: the file layout, the dossier, and role succession.

A persona began as one markdown file — `personas/<name>.md` — that becomes a
session's system prompt. That is enough to give a session a *perspective* and not
enough to give it an *identity*: the sceptic of run 12 knows nothing about what
the sceptic of runs 3-8 already tried, already refused, or left half-open, so
every session re-derives the same character from scratch and re-walks the same
blind alleys under its name.

The dossier is the missing half. `personas/<name>/` holds `persona.md` (who you
are — the prompt) beside `dossier.md` (what you have done — append-only history:
wins, failures, open threads, standing refusals). Both layouts are read forever:
every goal created before this existed has a flat file, and a layout change that
silently broke those would be a worse bug than the one it fixes. The flat file is
promoted to the directory form lazily, the first time something actually needs to
write a dossier.

Two rules the rest of the module exists to keep:

* The dossier is APPEND-ONLY prose. Nothing here rewrites an earlier entry, the
  same way nothing rewrites a board message — a history that can be edited by the
  identity it describes is not evidence of anything.
* Reading is defensive. `personas/` is inside the agent's reach with plain bash,
  so every read is lstat-guarded to a regular file and size-capped: a FIFO left
  at `dossier.md` would HANG the supervisor's briefing build, and a hang is not
  an exception you can catch.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pp_assign
from pp_common import UnsafeRunId, log, now_iso, read_json, run_dir, run_id

PERSONA_FILE = "persona.md"
DOSSIER_FILE = "dossier.md"

# The whole dossier is read into the briefing budget, so it is capped at the
# read, not at the render: an unbounded file would be paid for in RAM before
# anyone decided to truncate it.
DOSSIER_MAX_BYTES = 256 * 1024
PERSONA_MAX_BYTES = 256 * 1024

# What the briefing shows: the tail, because a dossier is a chronology and the
# recent entries are the ones a successor acts on. The rest stays on disk and is
# named in the stub.
DOSSIER_BRIEFING_CHARS = 2_400

#: Dossier entry kinds. Free text is allowed — these are the ones other modules
#: write, kept together so the vocabulary is discoverable rather than grepped.
KIND_SUCCESSION = "succession"
KIND_REFUSAL = "refusal"
KIND_HANDOFF = "handoff"


def _safe_name(name: str) -> str:
    """The same sanitisation pp-tool applies to a persona name.

    Duplicated deliberately rather than imported: this module is called from the
    supervisor as well as from pp-tool, and a name that reached here unsanitised
    would resolve a path outside `personas/`.
    """
    out = "".join(ch for ch in str(name or "") if ch.isalnum() or ch in "-_")[:48]
    return out


def personas_dir(goal: Path) -> Path:
    return goal / "personas"


def dir_for(goal: Path, name: str) -> Path:
    return personas_dir(goal) / _safe_name(name)


def flat_path(goal: Path, name: str) -> Path:
    return personas_dir(goal) / f"{_safe_name(name)}.md"


def nested_path(goal: Path, name: str) -> Path:
    return dir_for(goal, name) / PERSONA_FILE


def dossier_path(goal: Path, name: str) -> Path:
    return dir_for(goal, name) / DOSSIER_FILE


def _is_regular(path: Path) -> bool:
    """A regular file, not a symlink, fifo, socket or device.

    `lstat` and not `stat`: the question is what is AT the name, and following a
    link to a fifo answers a different one. Everything that reads a persona path
    goes through here — see the module docstring.
    """
    try:
        return path.lstat().st_mode & 0o170000 == 0o100000
    except OSError:
        return False


def _read(path: Path, max_bytes: int) -> str:
    if not _is_regular(path):
        return ""
    try:
        with open(path, "rb") as fh:
            return fh.read(max_bytes).decode("utf-8", "replace")
    except OSError:
        return ""


def persona_path(goal: Path, name: str) -> Path:
    """Where this persona's prompt lives — nested if it has been promoted.

    Returns the nested path when it exists and the flat path otherwise, so a
    caller that only wants to *show* the path (an error message, a tool result)
    gets the one a human should open. It does not promise the file exists.
    """
    nested = nested_path(goal, name)
    return nested if _is_regular(nested) else flat_path(goal, name)


def exists(goal: Path, name: str) -> bool:
    return _is_regular(nested_path(goal, name)) or _is_regular(flat_path(goal, name))


def names(goal: Path) -> list[str]:
    """Every persona this goal has, in either layout, sorted and deduplicated."""
    d = personas_dir(goal)
    found: set[str] = set()
    try:
        entries = list(d.iterdir())
    except OSError:
        # The scan is wrapped, not the per-item work: a session removing
        # personas/ between the check and the walk used to raise into the
        # briefing build.
        return []
    for entry in entries:
        try:
            if entry.is_dir() and _is_regular(entry / PERSONA_FILE):
                found.add(entry.name)
            elif entry.suffix == ".md" and _is_regular(entry):
                found.add(entry.stem)
        except OSError:
            continue
    return sorted(found)


def read_persona(goal: Path, name: str) -> str:
    return _read(persona_path(goal, name), PERSONA_MAX_BYTES)


def read_dossier(goal: Path, name: str) -> str:
    return _read(dossier_path(goal, name), DOSSIER_MAX_BYTES)


def has_dossier(goal: Path, name: str) -> bool:
    return _is_regular(dossier_path(goal, name))


def description(goal: Path, name: str) -> str:
    """The `description:` line from the persona's frontmatter, if it has one."""
    head = _read(persona_path(goal, name), 2048)
    for line in head.splitlines():
        if line.startswith("description:"):
            return line.split(":", 1)[1].strip()
    return ""


def promote(goal: Path, name: str) -> Path:
    """Move a flat `personas/<name>.md` to `personas/<name>/persona.md`.

    Called lazily by the first dossier write rather than eagerly at startup: a
    goal whose personas never accumulate history never needs the directory, and
    a migration that runs over every goal at once is a migration that can fail
    over every goal at once.

    Returns the nested persona path. A persona that is already nested, or that
    does not exist at all, is left exactly as it is.
    """
    safe = _safe_name(name)
    if not safe:
        raise ValueError("persona name must contain alphanumerics")
    nested = nested_path(goal, safe)
    if _is_regular(nested):
        return nested
    flat = flat_path(goal, safe)
    d = dir_for(goal, safe)
    # Refuse rather than write through whatever is sitting at the directory
    # name. A symlink here would put persona.md and dossier.md wherever it
    # points, which is the same class of bug as restoring through a link.
    if d.exists() and not d.is_dir() or d.is_symlink():
        raise ValueError(f"{d} exists and is not a directory")
    d.mkdir(parents=True, exist_ok=True)
    if _is_regular(flat):
        body = _read(flat, PERSONA_MAX_BYTES)
        nested.write_text(body, encoding="utf-8")
        flat.unlink(missing_ok=True)
        log(f"persona {safe}: promoted to {nested.parent}/ (dossier layout)",
            stream=sys.stderr)
    elif not nested.exists():
        nested.write_text(f"---\nname: {safe}\ndescription:\n---\n\n"
                          f"_(no persona body was ever written for `{safe}`.)_\n",
                          encoding="utf-8")
    return nested


def append_dossier(goal: Path, name: str, *, kind: str, body: str,
                   run: int | None = None, by: str = "") -> Path:
    """Append one dated entry to a persona's dossier. Promotes on first write.

    Append-only, like the board and the ledgers: an identity's history is only
    worth reading if the identity could not have rewritten it.
    """
    safe = _safe_name(name)
    if not safe:
        raise ValueError("persona name must contain alphanumerics")
    promote(goal, safe)
    path = dossier_path(goal, safe)
    if not path.exists():
        path.write_text(
            f"# Dossier — `{safe}`\n\n"
            f"_Append-only history of this identity: what it did, what it found "
            f"out, what it refused, and what it left open. Written by the harness "
            f"and by the sessions that ran as `{safe}`._\n", encoding="utf-8")
    head = f"\n## {kind} — run {run if run is not None else '?'}"
    if by:
        head += f" (by {by})"
    head += f" · {now_iso()}\n\n"
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(head + str(body).strip() + "\n")
    return path


def render_for_briefing(goal: Path, name: str) -> str:
    """The dossier tail, for the persona section. Empty when there is none."""
    text = read_dossier(goal, name).strip()
    if not text:
        return ""
    if len(text) > DOSSIER_BRIEFING_CHARS:
        text = ("_(older dossier entries omitted — the whole file is at "
                f"`{dossier_path(goal, name)}`.)_\n\n"
                + text[-DOSSIER_BRIEFING_CHARS:])
    return text


# ── succession ────────────────────────────────────────────────────────────
def for_persona(goal: Path, name: str) -> list[dict]:
    """Live assignments a persona holds: addressed to it, or claimed by it."""
    out = []
    for a in pp_assign.index(goal).values():
        if a.get("status") not in ("open", "claimed"):
            continue
        if a.get("to") == name or a.get("claimed_by") == name:
            out.append(a)
    out.sort(key=lambda a: a.get("id") or "")
    return out


def succeed(goal: Path, *, predecessor: str, successor: str, note: str = "",
            run: int | None = None, by: str = "") -> dict:
    """Transfer an identity: the history, and the work that is still open.

    Succession is the answer to a persona being lost between sessions. Without
    it, "the sceptic" is only the name of a prompt file, and the third sceptic
    starts as ignorant as the first. With it, the successor's dossier carries the
    predecessor's record, and everything the predecessor still had open is
    re-pointed so it is somebody's again rather than nobody's.

    Both dossiers get a record: the successor needs the inheritance, and the
    predecessor's own history must show where its open work went.
    """
    pred = _safe_name(predecessor)
    succ = _safe_name(successor)
    if not pred or not succ:
        raise ValueError("both persona names must contain alphanumerics")
    if pred == succ:
        raise ValueError("a persona cannot succeed itself — name a different successor")
    if not exists(goal, pred):
        raise FileNotFoundError(f"no persona {pred!r} to succeed")
    if not exists(goal, succ):
        raise FileNotFoundError(f"no persona {succ!r} — write it before handing over")

    moved: list[dict] = []
    for a in for_persona(goal, pred):
        # Best effort per assignment: a ledger that refuses one re-point (it was
        # resolved between the read and the write) must not abort a succession
        # that has already written half the dossier.
        try:
            res = pp_assign.reassign(goal, id=a["id"], to=succ, by=by or succ, run=run)
        except Exception as exc:                            # noqa: BLE001
            log(f"succession: {a['id']} not re-pointed: "
                f"{type(exc).__name__}: {exc}", stream=sys.stderr)
            continue
        if res.get("error"):
            log(f"succession: {a['id']} not re-pointed: {res['error']}",
                stream=sys.stderr)
            continue
        moved.append({"id": a["id"], "title": a.get("title", ""),
                      "was": a.get("status")})

    carried = read_dossier(goal, pred).strip()
    lines = [f"Succeeded **{pred}**"
             + (f" — {str(note).strip()}" if str(note).strip() else "") + "."]
    if moved:
        lines += ["", "Inherited open work:"]
        lines += [f"- **{m['id']}** ({m['was']}) {m['title']}" for m in moved]
    else:
        lines += ["", "`" + pred + "` held no open assignments at handover."]
    if carried:
        lines += ["", f"The predecessor's dossier is at `{dossier_path(goal, pred)}` "
                      f"— read it before re-deciding anything it already decided."]
    append_dossier(goal, succ, kind=KIND_SUCCESSION, body="\n".join(lines),
                   run=run, by=by or pred)

    back = [f"Handed this identity's open work to **{succ}**"
            + (f" — {str(note).strip()}" if str(note).strip() else "") + "."]
    if moved:
        back += ["", "Transferred:"] + [f"- **{m['id']}** {m['title']}" for m in moved]
    append_dossier(goal, pred, kind=KIND_SUCCESSION, body="\n".join(back),
                   run=run, by=by or pred)

    return {"predecessor": pred, "successor": succ,
            "reassigned": moved,
            "dossier": str(dossier_path(goal, succ))}


# ── #4: scorecards — rotate to what works, not to what's next ──────────────
# `no_progress_limit` used to rotate blindly to the next name in the list. The
# data to choose well already existed (run.json per run, the handoff each one
# wrote, `criteria[*].met_by`) and was unused. A scorecard is folded from
# exactly those — never from a persona's own dossier prose, which is the one
# thing it could pad. A persona that HELD a run which was later reaped did not
# complete it, so "runs held" and "reaped" are counted separately rather than
# folding a reap into a success just because the name matches.

#: A persona with zero history is "unproven", not "zero" — treated as this
#: score for ranking. Below it sits any persona whose ACTUAL rate has been
#: demonstrated to be worse (a real failure is worse evidence than no
#: evidence); above it sits any persona actually shown to do better. This is
#: what keeps exploration possible without favouring an untested name over a
#: demonstrated one.
UNPROVEN_SCORE = 0.5


def _run_records(goal: Path, name: str) -> list[tuple[str, dict]]:
    """(run_id, run.json) for every run this exact persona name held.

    Reads the harness's OWN record of who held a run, not anything a persona
    could have written about itself. A run.json that fails to parse as a
    canonical run id is corrupt and skipped — failure class 3, applied to a
    run record.
    """
    out: list[tuple[str, dict]] = []
    rdir = goal / "runs"
    if not rdir.exists():
        return out
    for p in sorted(rdir.glob("*")):
        rec = read_json(p / "run.json")
        if not isinstance(rec, dict) or (rec.get("persona") or None) != name:
            continue
        try:
            rid = run_id(rec.get("run", p.name))
        except UnsafeRunId:
            continue
        out.append((rid, rec))
    return out


def scorecard(goal: Path, name: str) -> dict:
    """This persona's queryable work history on THIS goal.

    Folded from run.json (who held it, whether it wedged, whether the
    supervisor caught a runaway turn), the handoff each run actually wrote
    (was it reaped, did the harness measure progress), and the criteria list's
    own `met_by` — three independent, harness-written sources, none of them
    the persona's own account of itself.
    """
    runs = _run_records(goal, name)
    held = len(runs)
    progressed = clean = reaped = wedges = runaway = 0
    last_run: str | None = None
    for rid, rec in runs:
        handoff = read_json(run_dir(goal, rid) / "handoff.json", {}) or {}
        att = handoff.get("attestation") or {}
        if att.get("progressed"):
            progressed += 1
        # A run this persona held that ended without its own handoff did not
        # complete anything — it was reconstructed by the reaper. Counting it
        # as a clean run would credit the persona for work a summariser
        # guessed at, which is the one thing a scorecard exists to refuse.
        if (handoff.get("source") or "agent") == "agent":
            clean += 1
        else:
            reaped += 1
        if rec.get("wedged"):
            wedges += 1
        if rec.get("watchdog") == "runaway-turn":
            runaway += 1
        last_run = rid
    state = read_json(goal / "state.json", {}) or {}
    ticked = sum(1 for c in (state.get("criteria") or [])
                if c.get("met_by") == name)
    return {
        "name": name,
        "runs_held": held,
        "progressed": progressed,
        "progress_rate": (progressed / held) if held else None,
        "clean_exits": clean,
        "clean_exit_rate": (clean / held) if held else None,
        "reaped": reaped,
        "reaped_rate": (reaped / held) if held else None,
        "criteria_ticked": ticked,
        "wedges": wedges,
        "runaway_turns": runaway,
        "last_run": last_run,
    }


def scoreboard(goal: Path) -> dict[str, dict]:
    """Every persona's scorecard, keyed by name."""
    return {n: scorecard(goal, n) for n in names(goal)}


def _rotation_score(card: dict) -> float:
    rate = card.get("progress_rate")
    return rate if rate is not None else UNPROVEN_SCORE


def _run_recency(card: dict) -> int:
    """The integer part of a persona's most recent run id, or -1 if it has
    never held one — which sorts as the LEAST recently used of all, on
    purpose: a persona that has never run is the safest possible exploration
    pick on a recency tiebreak."""
    last = card.get("last_run")
    if last is None:
        return -1
    try:
        return int(str(last).partition(".")[0])
    except ValueError:
        return -1


def choose_rotation(goal: Path, *, exclude: str | None) -> str | None:
    """Who the no-progress breaker should rotate TO, or None if nobody else
    is eligible (a goal with zero or one persona degrades to the old
    behaviour: no rotation target).

    Ranked by `_rotation_score` (best progress rate first, an unproven
    persona treated as a neutral prior rather than as a zero), tied toward
    the LEAST recently used — so a persona that happened to run first, and so
    accumulated an early lead, cannot monopolise the goal forever against
    names nobody has tried in a while.
    """
    board = scoreboard(goal)
    candidates = [n for n in board if n != exclude]
    if not candidates:
        return None
    candidates.sort(
        key=lambda n: (_rotation_score(board[n]), -_run_recency(board[n])),
        reverse=True)
    return candidates[0]


def render_scorecard(card: dict) -> str:
    held = card.get("runs_held", 0)
    if not held:
        return "no run history on this goal yet"
    rate = card.get("progress_rate") or 0.0
    bits = [f"{held} run(s) held", f"{card['progressed']} moved the goal "
            f"({rate * 100:.0f}%)", f"{card['criteria_ticked']} criteria ticked",
            f"{card['clean_exits']} clean exit(s)", f"{card['reaped']} reaped"]
    if card.get("wedges"):
        bits.append(f"{card['wedges']} wedge(s)")
    if card.get("runaway_turns"):
        bits.append(f"{card['runaway_turns']} runaway turn(s)")
    return " · ".join(bits)


def render_scoreboard(board: dict[str, dict]) -> str:
    if not board:
        return "_(no personas yet — sessions write their own with persona_write)_"
    ordered = sorted(board.items(),
                     key=lambda kv: kv[1].get("progress_rate") if kv[1].get("progress_rate") is not None else -1,
                     reverse=True)
    return "\n".join(f"- **{name}** — {render_scorecard(card)}"
                     for name, card in ordered)
