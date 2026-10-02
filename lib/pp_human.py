"""Questions a session raises FOR THE HUMAN, and the answers that come back.

The gap this closes was found the hard way. Run 16 of the first real goal hit a
decision it correctly refused to make alone — a test gate scoped to two literal
motion keys that blocked every future prone movement — and it did the only thing
the harness offered: posted to a topic channel, addressed to nobody, and hoped.
Nothing notified the human. Nothing surfaced it in `perpetua status`. The board
section was over budget in that run's briefing and in the seven after it, so no
later session saw it either, while the briefing's cursor marked it read anyway.
Five sessions then re-derived "I am blocked on a human" from scratch, and the
human found out by reading raw JSONL.

`perpetua_amend` was not the answer and the session was right to say so: an
amendment changes the CHARTER (the objective, the criteria, check.sh, goal.json).
A question about the code, the approach, or a gate that is not the fence has no
charter to amend. That is this module: a first-class "I need a human", separate
from "I propose changing what this goal is".

Four properties, each of them a thing that failed above:

* **A question is a durable record, not a message.** `questions.jsonl` is the
  append-only truth and `questions.json` a rebuildable fold, the shape house rule
  2 requires. The board still gets a copy because that is where traffic is
  *seen*; the ledger is where it is *tracked*. A message can be missed. A record
  cannot.
* **It reaches the human out of band, immediately.** Asking opens an escalation,
  so it rides the ladder built for pauses and lands in Discord when it is
  raised — not when someone next opens the dashboard.
* **The answer reaches the session even when the briefing is full.** An open
  question and an undelivered answer are priority-1 briefing content, alongside
  BOUNDARIES.md and above everything a session can be told by anything else.
  The whole failure was a load-bearing message losing a budget fight.
* **Waiting is bounded.** Sessions that keep running against an unanswered
  question burn a local model's hours re-deriving that they are stuck, which is
  the same failure the unresolved-amendment rule already pauses for.
"""
from __future__ import annotations

import json
from pathlib import Path

from pp_common import flock, log, now_iso, read_json, write_json

LOG_NAME = "questions.jsonl"
INDEX_NAME = "questions.json"
LOCK_NAME = ".questions.lock"

#: The board channel a question and its answer are copied to. Its own channel so
#: `board_read --channel human` is the whole human conversation, and so a topic
#: channel's traffic can never bury it.
CHANNEL = "human"

#: Option keys, in the order they are handed out. A question carries at most this
#: many recommended options; past that the human is being asked to read a menu,
#: not make a decision.
_OPTION_KEYS = "ABCDEFGH"


def normalise_options(raw) -> list[dict]:
    """Turn whatever a session passed as `options` into the canonical shape:
    a list of ``{"key", "label", "detail", "recommended"}``.

    Sessions think in labels and a recommendation; the ledger and every view
    need a stable key to answer against. Keys are handed out A, B, C… unless the
    caller set one explicitly. A caller that marks more than one option
    recommended keeps only the first — "pick any of these" is not a
    recommendation.
    """
    if not raw:
        return []
    if not isinstance(raw, list):
        raise ValueError("options must be a list of {label, detail?, recommended?}")
    if len(raw) > len(_OPTION_KEYS):
        raise ValueError(f"at most {len(_OPTION_KEYS)} options — more than that is "
                         f"a menu, not a decision")
    out: list[dict] = []
    seen_reco = False
    for i, opt in enumerate(raw):
        if isinstance(opt, str):
            opt = {"label": opt}
        if not isinstance(opt, dict):
            raise ValueError("each option is a string label or an object with "
                             "'label'")
        label = str(opt.get("label", "")).strip()
        if not label:
            raise ValueError("every option needs a non-empty 'label'")
        key = str(opt.get("key", "")).strip().upper() or _OPTION_KEYS[i]
        reco = bool(opt.get("recommended")) and not seen_reco
        seen_reco = seen_reco or reco
        out.append({"key": key, "label": label,
                    "detail": str(opt.get("detail", "")).strip(),
                    "recommended": reco})
    if len({o["key"] for o in out}) != len(out):
        raise ValueError("option keys must be distinct")
    return out


def recommended_option(rec: dict) -> dict | None:
    return next((o for o in rec.get("options") or [] if o.get("recommended")), None)


def find_option(rec: dict, key: str) -> dict | None:
    key = str(key).strip().upper()
    return next((o for o in rec.get("options") or [] if o["key"] == key), None)


def compose_answer(option: dict, note: str = "") -> str:
    """The text a session actually reads when the human answered by picking.

    Spells the choice out in full rather than leaving a bare letter the session
    would have to map back to its own list — by the time the answer lands the
    session that wrote the options is long gone.
    """
    parts = [f"Selected option {option['key']}: {option['label']}"]
    if option.get("detail"):
        parts.append(option["detail"])
    note = (note or "").strip()
    if note:
        parts.append(note)
    return "\n\n".join(parts)


def render_options(options: list[dict], *, chosen: str | None = None,
                   indent: str = "") -> str:
    """A menu a human reads at a terminal. ★ marks the session's recommendation;
    ✓ marks what was actually chosen once it has been."""
    if not options:
        return ""
    chosen = (chosen or "").strip().upper()
    lines = []
    for o in options:
        marks = "".join(m for m, on in (("★", o.get("recommended")),
                                        ("✓", o["key"] == chosen)) if on)
        head = f"{indent}[{o['key']}] {marks + ' ' if marks else ''}{o['label']}"
        lines.append(head)
        if o.get("detail"):
            lines.append(f"{indent}     {o['detail']}")
    return "\n".join(lines)


def log_path(goal: Path) -> Path:
    return goal / LOG_NAME


def index_path(goal: Path) -> Path:
    return goal / INDEX_NAME


def _lock(goal: Path) -> Path:
    return goal / LOCK_NAME


def _iter_events(goal: Path):
    """Every event in append order, tolerating a torn final line — the same rule
    the board and the assignment ledger live by: a line half-written when a
    process died is the end of history, not corruption."""
    p = log_path(goal)
    if not p.exists():
        return
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        log(f"human: cannot read {p.name}: {type(exc).__name__}: {exc}")
        return
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


def _n(ev: dict) -> int | None:
    try:
        return int(ev.get("n"))
    except (TypeError, ValueError):
        return None


def fold(events) -> list[dict]:
    """The ledger as it stands now, ordered by `n`.

    An event naming an `n` that was never asked is ignored rather than
    inventing a record for it — the tolerance `pp_assign` applies to a claim
    with no matching open.
    """
    by_n: dict[int, dict] = {}
    for ev in events:
        n = _n(ev)
        if n is None:
            continue
        kind = ev.get("type")
        if kind == "ask":
            by_n[n] = {
                "n": n, "at": ev.get("at"), "run": ev.get("run"),
                "persona": ev.get("persona"), "subject": ev.get("subject", ""),
                "body": ev.get("body", ""), "board_seq": ev.get("board_seq"),
                "options": ev.get("options") or [],
                "answered_at": None, "answer": None, "answered_by": None,
                "answer_option": None, "answer_board_seq": None,
                # Which run last had this record in its briefing. An answer is
                # only "delivered" once a session has actually been handed it,
                # the same distinction perpetuad already draws for the board
                # cursor: a briefing that is built and then thrown away by a
                # failed launch must not count as delivery.
                "seen_run": None, "answer_seen_run": None,
                "withdrawn_at": None, "withdrawn_reason": None,
            }
            continue
        rec = by_n.get(n)
        if rec is None:
            continue
        if kind == "answer":
            rec["answered_at"] = ev.get("at")
            rec["answer"] = ev.get("text", "")
            rec["answered_by"] = ev.get("by") or "human"
            rec["answer_option"] = ev.get("option")
            rec["answer_board_seq"] = ev.get("board_seq")
        elif kind == "seen":
            run = ev.get("run")
            if ev.get("of") == "answer":
                rec["answer_seen_run"] = run
            else:
                rec["seen_run"] = run
        elif kind == "withdraw":
            rec["withdrawn_at"] = ev.get("at")
            rec["withdrawn_reason"] = ev.get("reason", "")
    return [by_n[n] for n in sorted(by_n)]


def index(goal: Path) -> list[dict]:
    cached = read_json(index_path(goal))
    if isinstance(cached, list):
        return cached
    return fold(_iter_events(goal))


def rebuild(goal: Path) -> list[dict]:
    with flock(_lock(goal)):
        idx = fold(_iter_events(goal))
        write_json(index_path(goal), idx)
    return idx


def _append(goal: Path, events: list, event: dict) -> list[dict]:
    """Append one event and refresh the derived index. Callers hold the lock."""
    with log_path(goal).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False) + "\n")
    idx = fold(events + [event])
    write_json(index_path(goal), idx)
    return idx


def ask(goal: Path, *, subject: str, body: str, run=None,
        persona: str | None = None, board_seq: int | None = None,
        options=None) -> dict:
    """Record a question. The board copy and the escalation are the caller's
    job (`pp-tool ask-human`), because this module must stay usable — and
    testable — without a board, a notifier or a network.

    `options` is the questionnaire: the concrete choices the session wants the
    human to pick between, one of them marked recommended. The human can still
    answer in free text, but a question that offers options can be settled with
    a single keystroke.
    """
    opts = normalise_options(options)
    with flock(_lock(goal)):
        events = list(_iter_events(goal))
        n = sum(1 for e in events if e.get("type") == "ask") + 1
        event = {"type": "ask", "n": n, "at": now_iso(), "run": run,
                 "persona": persona, "subject": subject.strip(),
                 "body": body, "board_seq": board_seq, "options": opts}
        idx = _append(goal, events, event)
    return next(r for r in idx if r["n"] == n)


def answer(goal: Path, n: int, *, text: str | None = None, option: str | None = None,
           by: str = "human", board_seq: int | None = None) -> dict | None:
    """Answer question `n`. Returns None if there is no such question.

    Two ways in: free `text`, or `option` — the key of one of the choices the
    question offered. Picking an option is the common path for a questionnaire;
    `text` may still ride along as a note. The stored answer text always spells
    the choice out in full, because the session that wrote the options will not
    be around to map a bare letter back.

    Answering twice is allowed on purpose: a human who wants to correct or
    extend an answer should not have to withdraw and re-ask a question the
    session is already waiting on. The latest answer wins, and the log keeps
    both.
    """
    with flock(_lock(goal)):
        events = list(_iter_events(goal))
        idx = fold(events)
        rec = next((r for r in idx if r["n"] == n), None)
        if rec is None:
            return None
        chosen_key = None
        if option is not None:
            picked = find_option(rec, option)
            if picked is None:
                keys = ", ".join(o["key"] for o in rec.get("options") or [])
                raise ValueError(
                    f"Q{n} has no option {option!r}" +
                    (f" — choices are {keys}" if keys else
                     " — this question offered no options, answer in text"))
            chosen_key = picked["key"]
            text = compose_answer(picked, text or "")
        if not (text or "").strip():
            raise ValueError("an answer needs either an option key or text")
        event = {"type": "answer", "n": n, "at": now_iso(), "text": text,
                 "by": by, "option": chosen_key, "board_seq": board_seq}
        idx = _append(goal, events, event)
    return next(r for r in idx if r["n"] == n)


def withdraw(goal: Path, n: int, *, reason: str = "") -> dict | None:
    """Retract a question — it turned out not to need a human after all."""
    with flock(_lock(goal)):
        events = list(_iter_events(goal))
        idx = fold(events)
        if not any(r["n"] == n for r in idx):
            return None
        event = {"type": "withdraw", "n": n, "at": now_iso(), "reason": reason}
        idx = _append(goal, events, event)
    return next(r for r in idx if r["n"] == n)


def mark_seen(goal: Path, ns, run, *, of: str = "question") -> None:
    """Record that run `run` was actually handed these records.

    Best effort and never raises: this is bookkeeping about delivery, and
    failing it must not take down the launch path that calls it.
    """
    ns = [int(n) for n in ns]
    if not ns:
        return
    try:
        with flock(_lock(goal)):
            events = list(_iter_events(goal))
            for n in ns:
                event = {"type": "seen", "n": n, "at": now_iso(),
                         "run": str(run), "of": of}
                events = events + [event]
                with log_path(goal).open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(event, ensure_ascii=False) + "\n")
            write_json(index_path(goal), fold(events))
    except Exception as exc:                                # noqa: BLE001
        log(f"human: could not mark {ns} seen: {type(exc).__name__}: {exc}")


def is_open(rec: dict) -> bool:
    """Waiting on the human right now: asked, not answered, not withdrawn."""
    return not rec.get("answered_at") and not rec.get("withdrawn_at")


def open_questions(goal: Path) -> list[dict]:
    return [r for r in index(goal) if is_open(r)]


def undelivered_answers(goal: Path) -> list[dict]:
    """Answered, not withdrawn, and no session has been handed the answer yet.

    This is what makes an answer survive a full briefing: it stays priority-1
    content until a session has actually seen it, rather than being posted once
    into a board section that loses its budget fight.
    """
    return [r for r in index(goal)
            if r.get("answered_at") and not r.get("withdrawn_at")
            and not r.get("answer_seen_run")]


def render_open(records: list[dict], *, max_body: int = 1500) -> str:
    """The briefing's view of what this goal is waiting on."""
    if not records:
        return ""
    out = []
    for r in records:
        body = r.get("body", "")
        if len(body) > max_body:
            body = body[:max_body] + f"\n… [truncated, {len(r['body'])} chars]"
        block = (f"**Q{r['n']}: {r.get('subject', '')}** "
                 f"(asked by run {r.get('run')}, {r.get('at')})\n\n{body}")
        menu = render_options(r.get("options") or [])
        if menu:
            block += ("\n\nOptions the asking session proposed (★ = its "
                      "recommendation):\n" + menu)
        out.append(block)
    return "\n\n".join(out)


def render_answers(records: list[dict], *, max_body: int = 4000) -> str:
    """The briefing's view of answers that have come back. Deliberately generous
    with length: this is the human speaking directly to the session, which is
    the highest-value text in the whole briefing and the one thing that must
    never arrive abbreviated to the point of ambiguity."""
    if not records:
        return ""
    out = []
    for r in records:
        ans = r.get("answer", "")
        if len(ans) > max_body:
            ans = ans[:max_body] + f"\n… [truncated, {len(r['answer'])} chars]"
        block = (f"**Q{r['n']}: {r.get('subject', '')}** — asked by run "
                 f"{r.get('run')}, answered {r.get('answered_at')} by "
                 f"{r.get('answered_by') or 'human'}:\n\n{ans}")
        if r.get("answer_option") and (r.get("options") or []):
            block += ("\n\nAgainst the options offered:\n"
                      + render_options(r["options"], chosen=r["answer_option"]))
        out.append(block)
    return "\n\n".join(out)
