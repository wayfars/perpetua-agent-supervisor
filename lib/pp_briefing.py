"""Build the briefing a fresh session wakes up inside.

This is the whole trick of perpetua: session N+1 has no memory of session N, so
everything that must survive has to be reconstructed here from disk — the
charter, the criteria, the recent handoffs, unread board traffic, the persona,
and the rules of the loop itself.

The briefing goes to `pi --append-system-prompt <file>`, not into the first user
message, so it survives compaction inside a long session.
"""
from __future__ import annotations

from pathlib import Path

import json

import pp_assign
import pp_backend
import pp_board
import pp_human
import pp_journal
import pp_machine
import pp_persona
import pp_progress
import pp_recall
import pp_state

RECENT_ENTRIES = 4
BOARD_UNREAD_MAX = 30

# The briefing competes with the work for one context window, so it is budgeted
# — but the budget has to come from the window that actually exists.
#
# It did not. This was a flat 24,000 CHARACTERS, roughly 6,000 tokens, against a
# backend serving 131,072: about 4.6% of the window, and no relationship to the
# model at all. The visible cost was that a real goal dropped `wiki`, `board`,
# `machine` and `persona` from NINE consecutive briefings — every session ran
# without any board traffic, so sessions kept re-deriving what earlier ones had
# already written to each other, and a question raised for the human reached
# nobody for five runs. The constant, not the content, was the problem.
#
# So: a fraction of the class's real `context_tokens`, converted at a
# deliberately pessimistic chars-per-token so the estimate errs toward a
# SMALLER allowance than the truth. `assemble` still drops by priority, but as a
# backstop against something pathological rather than as a routine event.
#: Share of the context window the briefing may occupy.
BRIEFING_CONTEXT_FRACTION = 0.25
#: Pessimistic chars/token for English prose and code (real is nearer 3.5-4).
#: Lower means fewer chars allowed per token of budget, which is the safe error.
CHARS_PER_TOKEN = 3.0
#: Used only when a class declares no `context_tokens` — the smallest window any
#: backend in this file has, so an undeclared class is under-served, not over.
FALLBACK_CONTEXT_TOKENS = 32_768
#: The floor no derived allowance may fall below: a briefing smaller than this
#: cannot carry the charter, the criteria and one handoff, which is the minimum
#: a session needs to do anything at all.
BRIEFING_MIN_CHARS = 24_000


def allowance(spec: dict | None, override=None) -> int:
    """How many characters this session's briefing may use.

    `override` is `goal.json`'s `briefing_max_chars` and always wins — a goal
    that has a reason to differ keeps it. Otherwise the budget is derived from
    the backend the session will actually run on, which is the number that was
    missing: a class serving 131K tokens and one serving 32K should not get the
    same briefing.
    """
    if override:
        return int(override)
    tokens = 0
    if spec:
        try:
            tokens = int(spec.get("context_tokens") or 0)
        except (TypeError, ValueError):
            tokens = 0
    tokens = tokens or FALLBACK_CONTEXT_TOKENS
    derived = int(tokens * BRIEFING_CONTEXT_FRACTION * CHARS_PER_TOKEN)
    return max(derived, BRIEFING_MIN_CHARS)


#: Kept as the name the rest of the tree imports; it is now only the floor.
BRIEFING_MAX_CHARS = BRIEFING_MIN_CHARS

# Machine events do not compete with agent traffic for the unread budget. At
# roughly four events per run they would consume all 30 slots within two sessions
# and the board would silently stop delivering what sessions actually said to
# each other.
UNREAD_EXCLUDE = {pp_machine.CHANNEL}

# Unread board traffic is tracked per GOAL SESSION, not per persona. The two used
# to disagree: `board_read --advance_cursor` moved `board_cursor[<persona>]` while
# the briefing read `board_cursor["supervisor"]`, a key nothing ever wrote — so the
# briefing re-delivered every message the goal had ever seen, forever. The
# briefing is the thing that makes a message read (it is what the next session
# actually sees), so the briefing owns this cursor and advances it itself.
BRIEFING_CURSOR = "briefing"


def cursor(state: dict) -> int:
    return int((state.get("board_cursor") or {}).get(BRIEFING_CURSOR, 0))


def unread(goal: Path, state: dict, limit: int = BOARD_UNREAD_MAX) -> tuple[list, int, int]:
    """The OLDEST unread messages, how many were left over, and the new cursor.

    Oldest-first rather than newest-first is deliberate. Taking the newest N and
    advancing past the rest would silently destroy older messages that no session
    ever saw; taking the oldest N means a backlog drains over several sessions and
    nothing is lost, at the cost of a session occasionally reading stale traffic.

    The cursor jumps to the board HEAD when the backlog drains, not merely to the
    last agent message shown. The machine channel is excluded from this list but
    is rendered from the same cursor as a digest, so a cursor that stopped at the
    newest *agent* message would replay every machine event forever.
    """
    since = cursor(state)
    everything = pp_board.read(goal, since=since, limit=0,
                               exclude_channels=UNREAD_EXCLUDE)
    shown = everything[:limit] if limit else everything
    remaining = len(everything) - len(shown)
    upto = (max((m.get("seq", 0) for m in shown), default=since) if remaining
            else max(pp_board.head_seq(goal), since))
    return shown, remaining, upto


def advance_cursor(goal: Path, upto: int) -> None:
    """Mark everything up to `upto` as read. Never moves backwards."""
    def apply(s):
        cur = s.setdefault("board_cursor", {})
        cur[BRIEFING_CURSOR] = max(int(cur.get(BRIEFING_CURSOR, 0) or 0), int(upto))
    pp_state.patch(goal, apply)


def refusals(goal: Path, limit: int = 12) -> list[dict]:
    """Standing refusals: what earlier sessions were asked to do and declined.

    A refusal is only free if it is durable. Without this, the next session — and
    every future persona — re-litigates the same request from scratch, which is
    exactly the pressure that makes an agent eventually say yes to something it
    already judged out of scope.
    """
    path = goal / "refusals.jsonl"
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out[-limit:]


def _read(p: Path, fallback: str = "") -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return fallback


def rules(goal: Path, spec: dict, allow_hosted: bool, budget_s: int = 0,
          no_progress: int = 3, probe_limit: int = 2) -> str:
    classes = ", ".join(sorted(pp_backend.load_config()["classes"]))
    budget_line = (
        f"**You have about {budget_s // 60} minutes of wall clock for this session.** "
        f"When it runs out the session is killed and your work has to be reconstructed "
        f"from the transcript by a summariser."
        if budget_s else
        "This session has no wall-clock limit, but context still runs out.")
    return f"""## The rules of this loop

You are one session in a chain. You will not remember this session next time; the
only things that survive are files on disk. Act accordingly.

1. **Your handoff is mandatory.** Before you run out of context, call
   `perpetua_end_session` with a real handoff — what you did, what you learned
   (including negative results), what the next session should do first, and what
   is blocking you. A session that ends without one has to be reconstructed from
   its transcript by a summariser, which is strictly worse than what you would
   have written.
2. **End deliberately.** Do not try to finish the whole goal. Do a coherent unit
   of work, write it down, and end the session. Ending early with a good handoff
   beats being cut off mid-thought.
   {budget_line}
   Budget your reading against that. Sessions before you have burned an entire
   budget reading the harness wholesale and produced nothing the next session
   could use. Read what the task needs, do the work, hand off.
3. **Write to the workspace.** Your cwd is `{goal / "workspace"}` and it is a git
   repo. Commit your work. Do not write outside it except through the perpetua
   tools (board, journal, personas, wiki), which manage their own files.
4. **Learnings go in the wiki.** Durable knowledge belongs in the goal's OKF
   bundle at `{goal / ".pi/wiki"}` via the wiki-manager scripts
   (configure the wiki-manager scripts path with `PERPETUA_WIKI_SCRIPTS` and pass
   `--project-wiki`). The
   journal is for handoff; the wiki is for knowledge that should still be true in
   a hundred sessions.
5. **Talk to your successors and peers on the board.** `board_post`, `board_read`
   and `board_channels`. A reply is a `board_post` with `reply_to` set to the
   thread you are answering. The board's conventions are yours to define — see
   `{goal / "board/CONVENTIONS.md"}`. Perpetua provides transport only.
   **Search before you re-derive anything.** `board_search` returns matching
   messages as one-line hits rather than bodies, so asking the board whether this
   has come up before costs almost nothing and reading the whole board costs a
   session.
6. **Create personas when a perspective is missing.** If this goal needs a
   sceptic, an archivist, a performance specialist — write that persona with
   `persona_write` and put it in play with `persona_assign`. Personas are AGENTS.md
   files that become the session's system prompt. An assigned persona is
   **standing**: it runs every session until one reassigns it, so you do not have
   to re-assign your own persona to keep it, and changing it changes the goal's
   direction for everyone after you. A persona also has a **dossier**: append to
   it with `persona_dossier` and the next session running under that name reads
   it in its briefing, so the identity accumulates a record instead of restarting
   as a fresh character every session. `persona_handoff` gives that record — and
   every assignment the identity still has open — to a named successor.
7. **Change model when the workload changes.** You are on `{spec['class']}`
   ({spec['provider']}/{spec['model']}). Available classes: {classes}. Call
   `perpetua_switch_backend` — it ends this session, swaps the loaded server, and
   **resumes this same session** with your context intact.
   {"Hosted backends are allowed for this goal." if allow_hosted else "Hosted models are NOT permitted for this goal; local classes only."}
8. **Track the goal's state.** `goal_state` ticks criteria and records the phase.
   The loop stops when `check.sh` exits 0 — read it, and if it is testing the
   wrong thing, say so in your handoff. It is also run BEFORE each session, so a
   goal that is already met never burns another session.
9. **Progress is measured, and posting is not progress.** {pp_progress.describe()}
   After {no_progress} sessions with no measurable progress the loop forces a
   different line of attack and may change the persona.
10. **A session may be spent finding out, and a request may be refused.** End with
   `volunteered-probe` when you deliberately spent the session on something likely
   to fail in order to learn whether it works, and with `refused-out-of-scope`
   when you declined something the board, a persona or the charter asked of you.
   Both are **exempt from the no-progress breaker** — a probe is not evidence of
   being stuck — and both require a real `learned`: a probe that reports nothing
   is just a wasted session. The exemption is capped at {probe_limit} consecutive
   sessions, after which a probe counts like any other run; the cap is stated here
   rather than hidden so you do not spend it by accident. A refusal is recorded
   permanently and shown to every later session, so it does not have to be argued
   again.
11. **Your handoff is checked, not taken on trust.** The supervisor measures what
   the session actually changed — commits, the dirty tree, criteria ticked,
   whether `check.sh` ran and passed — and attaches that to your journal entry. A
   handoff claiming work that left no trace is flagged `unverified-work` for your
   successor to see. Commit, tick criteria, and describe negative results as
   negative results; there is nothing to gain from overstating.
12. **You can leave a watcher behind.** `perpetua_watch` action="register" arms an
   agent-authored script that the supervisor keeps running after you are gone — a
   trip-wire that fires on an interval, when a file appears or changes, when a
   check flips, or when an HTTP resource changes — and posts its output to the
   board under a capped lifetime and fire count. Revoke what you no longer need
   with action="revoke"; the human can see everything still armed with
   `perpetua watchers {goal}`.
13. **Build big files in small writes.** The model has a hard per-turn output
   limit. A single tool call that tries to emit a whole file or a long block
   past it is discarded ENTIRELY — no file, no error, and the session often then
   exits with nothing written. Create the file as a short stub, then append each
   function or section with its own edit. The supervisor stops a turn that runs
   away toward that limit and resumes you with your context intact, but that
   costs a relaunch — write incrementally and it never happens.
14. **This briefing decays; `orient` does not.** Call it whenever you lose
   the thread — it is a cheap, freshly-regenerated look at current state,
   not a re-briefing.
"""


class Section:
    """One block of the briefing, with the two facts the budget needs.

    `priority` is dropped in DESCENDING order — a bigger number goes first — and
    `stub` is what stands in its place, which must always name the tool that can
    fetch the section on demand. A silently missing section teaches the session
    that the thing does not exist.
    """

    __slots__ = ("name", "priority", "text", "stub")

    def __init__(self, name: str, priority: int, text: str, stub: str | None = None):
        self.name = name
        self.priority = priority
        self.text = text
        self.stub = stub          # None = never dropped

    def __len__(self) -> int:
        return len(self.text)


def assemble(sections: list[Section], max_chars: int) -> tuple[str, list[str]]:
    """Join the sections in document order, dropping to fit. Returns (text, dropped).

    Dropping is by priority (highest number first) and then by position (latest
    first), so the order in which sections are declared below is also the policy
    for what a session loses when the board gets busy.
    """
    kept = list(sections)
    dropped: list[str] = []

    def size() -> int:
        return sum(len(sec.text) + 2 for sec in kept)

    while size() > max_chars:
        candidates = [(i, sec) for i, sec in enumerate(kept) if sec.stub is not None]
        if not candidates:
            break                 # everything left is load-bearing; go over budget
        i, sec = max(candidates, key=lambda pair: (pair[1].priority, pair[0]))
        dropped.append(sec.name)
        kept[i] = Section(sec.name, sec.priority, sec.stub, None)
    return "\n\n".join(sec.text for sec in kept if sec.text), dropped


def build(goal: Path, state: dict, spec: dict, *, run_n: int,
          persona: str | None, allow_hosted: bool,
          resume_reason: str | None = None, budget_s: int = 0,
          no_progress_limit: int = 3, probe_limit: int = 2,
          max_chars: int | None = None) -> tuple[str, int, list[str]]:
    """Returns the briefing text, the board seq it consumed, and what was dropped.

    The caller advances the cursor to that seq once the briefing is actually
    handed to a session — building a briefing that is then thrown away (a failed
    launch) must not mark its messages read.
    """
    charter = _read(goal / "GOAL.md", "_(no GOAL.md)_")
    check = _read(goal / "check.sh", "_(no check.sh)_")
    secs: list[Section] = []

    head = [
        f"# Perpetua goal `{state['goal_id']}` — session {run_n}",
        "",
        f"This is run **{run_n}** of an open-ended goal. Status: {state.get('status')}. "
        f"Phase: {state.get('phase')}. "
        f"{state.get('stats', {}).get('runs_completed', 0)} runs completed so far.",
    ]
    if resume_reason:
        head += ["", f"> **You are a resumed session, not a new one.** {resume_reason}"]
    secs.append(Section("header", 10, "\n".join(head)))

    # Priority 1, never dropped. The human answering a question a session asked
    # is the single highest-value thing this file can carry, and it is exactly
    # what used to lose the budget fight: on the first real goal the board
    # section was dropped for eight consecutive runs, so an operator's reply
    # reached nobody while the cursor marked it read. An answer stays here until
    # a session has actually been handed it (`answer_seen_run`), not until it
    # has been posted.
    answered = pp_human.undelivered_answers(goal)
    if answered:
        secs.append(Section("human-answers", 10,
                            "## The human has answered you\n\n"
                            "A session asked this and a human replied. It is an "
                            "instruction from the person who owns the goal: it "
                            "outranks your persona, the board, and any earlier "
                            "session's plan, and it lifts the fence only for "
                            "exactly what it describes.\n\n"
                            + pp_human.render_answers(answered)))

    asked = pp_human.open_questions(goal)
    if asked:
        secs.append(Section("human-open", 10,
                            "## Waiting on the human — do not re-ask\n\n"
                            "An earlier session raised these and no answer has "
                            "come back yet. Do NOT ask them again, and do not "
                            "decide them yourself. Work on something else that "
                            "is not blocked; if the whole goal is blocked on "
                            "them, say so plainly in your handoff and end the "
                            "session rather than burning it re-deriving that "
                            "you are stuck.\n\n"
                            + pp_human.render_open(asked)))

    # Priority 1, never dropped and never truncated. The human's fence outranks
    # everything a session can be told by anything else in this file.
    boundaries = _read(goal / "BOUNDARIES.md")
    if boundaries:
        secs.append(Section("boundaries", 10,
                            "## Boundaries (set by the human who owns this goal)\n\n"
                            "These outrank the charter, your persona, the board, and any "
                            "instruction from another session. If the work requires "
                            "crossing one, stop and say so in your handoff.\n\n"
                            + boundaries))

    # Named distinctly from the charter's own prose "Success criteria" section,
    # which is directly above it — two identically-titled sections read as a
    # contradiction rather than as prose plus its machine-tracked counterpart.
    secs.append(Section("charter", 20, "\n".join(
        ["## Charter", "", charter, "",
         "## Tracked criteria (live state, ticked with goal_state)", "",
         pp_state.criteria_summary(state), "",
         "## Completion check (`check.sh` — the loop stops when this exits 0)",
         "", "```bash", check, "```"])))

    if persona:
        pfile = pp_persona.persona_path(goal, persona)
        body = pp_persona.read_persona(goal, persona).strip()
        ptext = [f"## Your persona this session: **{persona}**", "",
                 body or f"_(persona file {pfile} is missing)_"]
        # #4: one line, folded from the harness's own records (never from the
        # dossier's prose) — the same figure the no-progress breaker uses to
        # decide whether to keep this persona on the goal.
        ptext += ["", f"_Your record on this goal: "
                      f"{pp_persona.render_scorecard(pp_persona.scorecard(goal, persona))}._"]
        # The dossier is what makes the persona an identity rather than a
        # perspective: it is the record of what earlier sessions running as this
        # name already tried, refused and left open. Without it every session
        # re-derives the same character and re-walks the same blind alleys.
        dossier = pp_persona.render_for_briefing(goal, persona)
        if dossier:
            ptext += ["", f"### Your dossier (`{persona}` — what you have already done)",
                      "", dossier,
                      "", "Add to it with `persona_dossier` when this session ends "
                          "something, learns something durable about the goal, or "
                          "leaves a thread open. Hand the identity on with "
                          "`persona_handoff` — that carries the dossier AND your "
                          "open assignments to the successor."]
    else:
        existing = pp_persona.names(goal)
        ptext = ["## Persona", "",
                 "You have no assigned persona this session — you are the generalist.",
                 (f"Personas written by earlier sessions: {', '.join(existing)}."
                  if existing else
                  "No personas exist yet. If this goal would benefit from a distinct "
                  "perspective, write one with `persona_write`.")]
    standing = refusals(goal)
    if standing:
        ptext += ["", "**Standing refusals** — already asked and already declined; "
                  "do not re-litigate them without new information:"]
        ptext += [f"- run {r.get('run')} ({r.get('persona') or 'generalist'}): "
                  f"{str(r.get('request', ''))[:120]} — {str(r.get('reason', ''))[:160]}"
                  for r in standing]
    secs.append(Section("persona", 30, "\n".join(ptext),
                        stub=f"## Persona\n\n_(persona **{persona or 'generalist'}** — "
                             f"section dropped for space; read "
                             f"`{goal / 'personas'}` directly.)_"))

    # Entries a SESSION wrote, plus a count of the reaped ones skipped to find
    # them. A run of reaper skeletons says the same non-thing once per run, and
    # on the first live goal four of them pushed the operator's own board
    # messages out of the budget entirely (2026-09-03). They stay on disk and in
    # JOURNAL.md; they stop being worth four slots in the one place that has to
    # fit in a context window.
    entries, reaped = pp_journal.recent_meaningful(goal, RECENT_ENTRIES)
    if reaped:
        note = (f"_{reaped} more recent run(s) ended without a handoff and could "
                f"not be summarised — a harness fault, not a finding about this "
                f"goal. Do not read that as the work being impossible; read "
                f"`git log` in the workspace for what actually survived._")
    else:
        note = ""
    newest = entries[0] if entries else (
        "_No session has yet written a handoff of its own. If runs have already "
        "happened, `git log` in the workspace is the only durable record of what "
        "they did._" if reaped else
        "_This is the first session. There is no history yet — set things up "
        "so the next session can pick up where you stop._")
    secs.append(Section("handoff-newest", 40,
                        "## Most recent handoff\n\n"
                        + (note + "\n\n" if note else "") + newest))
    # #1: this USED to dump entries[1:] verbatim — the last few handoffs beyond
    # the newest, in full. That is O(1) briefing size against O(N) history: by
    # run 400 of a long goal everything session 12 learned is unreachable
    # unless it happened to land in the wiki, and every session in between paid
    # for handoffs it could not use anyway (a session cannot act on run 8's
    # handoff once run 9-399 have already happened). `recall` searches ALL of
    # it on demand, so the push shrinks to the one line that says so — the
    # briefing gets smaller, which is the point.
    reachable = pp_recall.reachable_count(goal)
    if reachable > 1:
        secs.append(Section(
            "handoff-older", 45,
            f"_{reachable - 1} earlier journal entr{'y is' if reachable == 2 else 'ies are'} "
            f"not shown here — call `recall` with a query to search all "
            f"{reachable} of them instead of reading them wholesale._"))

    # Priority 50: between the handoffs this session should inherit and the
    # machine digest. It reads the derived index directly — no cursor — because
    # it answers "what can I still do?" while the unread board path answers
    # "what happened while I was gone?".
    mine = pp_assign.open_for(goal, persona)
    if mine:
        secs.append(Section("assignments", 50,
                            pp_assign.render_open(mine),
                            stub="_(open assignments dropped for space — "
                                 "`perpetua_assign` action=\"list\".)_"))

    since = cursor(state)
    mdigest, mmore = pp_machine.digest(goal, since)
    machine_text = ["## What the harness did (machine events since your last session)",
                    "", mdigest]
    if mmore:
        machine_text += ["", f"_{mmore} more; read them with "
                             f"`board_read` channel=\"{pp_machine.CHANNEL}\"._"]
    secs.append(Section("machine", 60, "\n".join(machine_text),
                        stub=f"_(machine events dropped for space — `board_read` "
                             f"channel=\"{pp_machine.CHANNEL}\".)_"))

    messages, remaining, upto = unread(goal, state)
    chans = pp_board.channels(goal)
    chan_list = ", ".join(f"{name} ({meta.get('count', 0)} msgs)"
                          for name, meta in sorted(chans.items()))
    btext = ["## Message board", "",
             f"Channels: {chan_list}" if chan_list else "No channels exist yet.",
             "", f"Unread since #{since} (marked read once this session starts):", "",
             pp_board.render(messages, goal=goal)]
    if remaining:
        btext += ["", f"_{remaining} older unread message(s) did not fit in this "
                      f"briefing; they stay unread and will appear in a later session, "
                      f"or read them now with `board_read` and `since={upto}`._"]
    secs.append(Section("board", 70, "\n".join(btext),
                        stub=f"_({len(messages) + remaining} unread board message(s) "
                             f"dropped for space — `board_read since={since}`, or "
                             f"`board_search` if you know what you are looking for.)_"))

    wiki_index = goal / ".pi/wiki/index.md"
    secs.append(Section("wiki", 75, "\n".join(
        ["## Knowledge base", "",
         (f"OKF bundle: `{goal / '.pi/wiki'}` — index at `{wiki_index}`."
          if wiki_index.exists() else
          f"OKF bundle at `{goal / '.pi/wiki'}` (not initialised yet)."),
         "Search it before re-deriving anything: "
         "`search.py --project-wiki "
         f"{goal / '.pi/wiki'} --query '<terms>'`."]),
        stub=f"_(knowledge base at `{goal / '.pi/wiki'}` — section dropped for space.)_"))

    secs.append(Section("rules", 80, rules(goal, spec, allow_hosted, budget_s,
                                           no_progress_limit, probe_limit)))

    # None means "derive it from the backend this session runs on"; a caller
    # that passes a number (a test, or a goal.json override) still wins.
    text, dropped = assemble(secs, allowance(spec, max_chars))
    if "board" in dropped:
        # A dropped section delivered NOTHING, so it must consume nothing.
        # `unread()` drains oldest-first precisely so a backlog is never lost to
        # the 30-message limit — but that guarantee died one layer up: the
        # cursor advanced to `upto` whether or not the section survived the
        # budget, so on a goal whose briefing runs over (this one dropped the
        # board on eight consecutive runs) every message was marked read having
        # never been shown to anyone. The stub still says how to fetch them, and
        # a context-starved local model demonstrably never does.
        upto = cursor(state)
    return text, upto, dropped


def kickoff(state: dict, run_n: int, nudge: str | None,
            resume_reason: str | None = None) -> str:
    if resume_reason:
        # A resumed session already has the whole conversation behind it. Telling
        # it to "begin" and re-read everything would waste the context we just
        # went to the trouble of preserving.
        base = (f"{resume_reason}\n\nPick up exactly where you left off. Do not "
                "restart your work or re-read what you have already read. When you "
                "reach a natural stopping point, end with perpetua_end_session.")
    else:
        base = (f"Begin session {run_n} of the perpetua goal `{state['goal_id']}`. "
                "Read your briefing above, check the board and the most recent handoff, "
                "then do one coherent unit of work toward the goal. "
                "End with perpetua_end_session before you run low on context.")
    return f"{nudge}\n\n{base}" if nudge else base
