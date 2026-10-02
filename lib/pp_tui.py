"""The perpetua dashboard — `perpetua` with no arguments.

Everything a goal accumulates lives in files under goals/<id>/, which makes for a
good CLI and a bad memory: nobody recalls fifteen flags or where the journal is.
This is the front door — goals, their sessions, the board the agents built, the
personas they wrote for themselves, and the supervisor's own log, all reachable
by arrow keys.

curses rather than a framework: it is in the stdlib, it works over SSH, and it
adds no dependency to a tool whose whole point is running unattended for weeks.
"""
from __future__ import annotations

import calendar
import curses
import os
import re
import signal
import subprocess
import textwrap
import time
import threading
from concurrent.futures import ThreadPoolExecutor

import pp_board
import pp_check
import pp_criteria
import pp_human
import pp_journal
import pp_persona
import pp_state
import pp_control
import pp_evidence
import pp_views
import pp_activity
from pp_common import BIN, GOALS, ROOT, read_json, run_no, supervisor_pid

REFRESH_S = 5.0
TABS = ["Summary", "Inbox", "Plan", "Activity", "Controls", "Diagnostics"]
DIAGNOSTICS = ["Journal", "Board", "Personas", "Runs", "Log"]

# kitty (and every terminal we actually run in) does bold/underline/italic
# fine, so the journal/board/overview text — which is written as markdown by
# the agents — is worth rendering instead of showing the raw `##`/`**`/`` ` ``.
# Kept to what curses can safely do per-cell: no tables, no nested emphasis.
_ITALIC = getattr(curses, "A_ITALIC", curses.A_UNDERLINE)
_INLINE_MD = re.compile(r"\*\*[^*\n]+\*\*|`[^`\n]+`|_[^_\n]+_|\*[^*\n]+\*")


def parse_inline_md(text: str) -> list[tuple[str, int]]:
    """Split a line into (chunk, extra_attr) pairs, stripping the markers."""
    out: list[tuple[str, int]] = []
    pos = 0
    for m in _INLINE_MD.finditer(text):
        if m.start() > pos:
            out.append((text[pos:m.start()], 0))
        tok = m.group(0)
        if tok.startswith("**"):
            out.append((tok[2:-2], curses.A_BOLD))
        elif tok.startswith("`"):
            out.append((tok[1:-1], curses.A_DIM))
        else:                                       # _italic_ or *italic*
            out.append((tok[1:-1], _ITALIC))
        pos = m.end()
    if pos < len(text) or not out:
        out.append((text[pos:], 0))
    return out


def md_block(text: str) -> tuple[str, int]:
    """Strip a line's block-level markdown (heading/quote/bullet/rule) and
    return the text to render plus the attr the whole line gets before any
    inline spans are layered on top."""
    stripped = text.strip()
    m = re.match(r"^(#{1,3})\s+(.*)$", text)
    if m:
        level = len(m.group(1))
        attr = curses.A_BOLD | (curses.A_UNDERLINE if level == 1 else 0)
        return m.group(2), attr
    if re.match(r"^-{3,}$|^_{3,}$", stripped) and stripped:
        return "─" * len(stripped), curses.A_DIM
    if text.startswith("> "):
        return text[2:], curses.A_DIM
    if re.match(r"^[-*]\s", text):
        return "• " + text[2:], 0
    return text, 0


# ── data ──────────────────────────────────────────────────────────────────
def goal_ids() -> list[str]:
    if not GOALS.exists():
        return []
    return sorted(p.name for p in GOALS.iterdir() if (p / "state.json").exists())





def summarize(gid: str) -> dict:
    goal = GOALS / gid
    st = read_json(goal / "state.json", {}) or {}
    crit = st.get("criteria", [])
    met, total = pp_criteria.leaf_totals(crit)   # #2: leaf-counted, tree-aware
    stats = st.get("stats", {})
    # state.json says "running" for as long as a session is in flight — but a
    # supervisor killed mid-run never gets to clear it, and a dashboard that shows
    # a dead goal as running is worse than one that shows nothing.
    status = st.get("status", "?")
    if status == "running" and supervisor_pid(goal) is None:
        status = "interrupted"
    return {
        "id": gid, "goal": goal, "state": st,
        "status": status,
        "run": st.get("run", 0),
        "met": met, "total": total,
        "clean": stats.get("sessions_ended_cleanly", 0),
        "reaped": stats.get("reaped", 0),
        "wedges": stats.get("wedges_healed", 0),
        "persona": st.get("persona"),
        "pid": supervisor_pid(goal),
        "paused": ((st.get("breaker") or {}).get("paused_reason") or 'Paused') if pp_state.is_paused(st) else None,
        # Read once here so the goals list, the tab badge and the Questions tab
        # agree on the count without three separate ledger reads per refresh.
        "open_qs": pp_human.open_questions(goal),
        "undelivered_answers": pp_human.undelivered_answers(goal),
        "inbox": pp_control.inbox(goal),
        "evidence": pp_evidence.latest(goal),
    }


def _age_str(ts) -> str:
    """' (3h ago)' — the elapsed-time hint an operator actually reacts to."""
    if not ts:
        return ""
    try:
        then = calendar.timegm(time.strptime(str(ts)[:19], "%Y-%m-%dT%H:%M:%S"))
    except (ValueError, TypeError):
        return ""
    secs = max(0.0, time.time() - then)
    if secs < 3600:
        return f" ({int(secs // 60)}m ago)"
    if secs < 86400:
        return f" ({secs / 3600:.1f}h ago)"
    return f" ({secs / 86400:.1f}d ago)"


# ── content for each tab ──────────────────────────────────────────────────
def overview_lines(s: dict) -> list[str]:
    goal, st = s["goal"], s["state"]
    out = [f"# {s['id']}", ""]
    hint = ("   (a session was in flight when its supervisor stopped; "
            "starting again picks up from the last handoff)"
            if s["status"] == "interrupted" else "")
    out += [f"status        {s['status']}{hint}"
            + (f"   ⛔ {s['paused']}" if s["paused"] else ""),
            f"supervisor    {'running (pid %d)' % s['pid'] if s['pid'] else 'stopped'}",
            f"sessions      {s['run']} run · {s['clean']} handed off cleanly · "
            f"{s['reaped']} reaped · {s['wedges']} wedges healed",
            f"phase         {st.get('phase', '?')}",
            f"persona       {s['persona'] or 'generalist'}",
            f"workspace     {goal / 'workspace'}", ""]

    for cur in pp_state.current_runs(st):
        out += [f"in flight     run {cur.get('n')} on {cur.get('backend')} "
                f"since {str(cur.get('started_at', ''))[11:19]}"
                + ("  [resumed session]" if cur.get("resumed") else ""), ""]

    # Anything waiting on the HUMAN goes above the criteria, because it is the
    # only thing on this screen the human is the bottleneck for. It used to
    # appear nowhere at all: a session's question sat in a topic channel while
    # the dashboard reported a goal that looked perfectly healthy.
    waiting = s.get("open_qs") or pp_human.open_questions(goal)
    if waiting:
        out += ["## ⚠ Waiting on YOU", "",
                f"  {len(waiting)} question(s) need a decision — open the "
                f"**Inbox** tab (2) or press  i  to answer them now.", ""]
        for q in waiting:
            out += [f"  Q{q['n']}  {q.get('subject', '')}"]
        out += [""]

    out += ["## Criteria", ""]
    out += (pp_state.criteria_summary(st).splitlines() or ["(none)"])
    out += ["", "## Charter", ""]
    charter = (goal / "GOAL.md")
    if charter.exists():
        for para in charter.read_text(encoding="utf-8", errors="replace").splitlines():
            out += textwrap.wrap(para, 96) or [""]
    return out


def questions_lines(s: dict) -> list[str]:
    """The dedicated home for everything `ask_human` raised — the questionnaires
    a session left for the operator, in full, and the answers already given.

    This used to be four cramped lines on the Overview tab that a session's
    question shared with the criteria and the charter. It is the one thing on
    the dashboard the human is the bottleneck for, so it gets its own tab.
    """
    goal = s["goal"]
    idx = pp_human.index(goal)
    if not idx:
        return ["No questions raised.", "",
                "When a session hits a decision only you can make — a gate that",
                "needs changing and is not the fence, a trade-off the charter does",
                "not settle — it calls ask_human and it lands here. It usually",
                "comes with 2–4 options and a recommendation; press  i  to answer."]

    open_recs = [r for r in idx if pp_human.is_open(r)]
    answered = [r for r in idx if r.get("answered_at") and not r.get("withdrawn_at")]
    withdrawn = [r for r in idx if r.get("withdrawn_at")]

    out: list[str] = []
    if open_recs:
        out += [f"## ⚠ Open — waiting on you ({len(open_recs)})", "",
                "  Press  i  to decide (explicit choice; Enter skips). "
                "Or from a shell:", ""]
        for r in open_recs:
            out += ["", f"### Q{r['n']}: {r.get('subject', '')}",
                    f"_asked by run {r.get('run')} · {r.get('at')}"
                    f"{_age_str(r.get('at'))}_", ""]
            out += textwrap.indent((r.get("body") or "").strip(), "  ").splitlines()
            opts = r.get("options") or []
            if opts:
                out += ["", "  Options (★ = the session's recommendation):"]
                out += pp_human.render_options(opts, indent="  ").splitlines()
                out += ["", f"  answer: perpetua answer {s['id']} {r['n']} -o "
                        "<KEY>"]
            else:
                out += ["", f"  answer: perpetua answer {s['id']} {r['n']} \"...\""]
        out += [""]

    if answered:
        out += ["", f"## Answered ({len(answered)})", ""]
        for r in sorted(answered, key=lambda r: r.get("answered_at") or "",
                        reverse=True):
            delivered = "delivered in briefing; application not verified" if r.get("answer_seen_run") else "not yet seen by a session"
            out += ["", f"### Q{r['n']}: {r.get('subject', '')}",
                    f"_answered {r.get('answered_at')}{_age_str(r.get('answered_at'))}"
                    f" by {r.get('answered_by') or 'human'} · {delivered}_", ""]
            if r.get("answer_option") and (r.get("options") or []):
                out += pp_human.render_options(
                    r["options"], chosen=r["answer_option"], indent="  ").splitlines()
                out += [""]
            out += textwrap.indent((r.get("answer") or "").strip(), "  ").splitlines()
        out += [""]

    if withdrawn:
        out += ["", f"## Withdrawn ({len(withdrawn)})", ""]
        for r in withdrawn:
            out += [f"  Q{r['n']}: {r.get('subject', '')}"
                    + (f" — {r['withdrawn_reason']}" if r.get("withdrawn_reason") else "")]
    return out


def journal_lines(s: dict) -> list[str]:
    entries = pp_journal.recent(s["goal"], 10)
    if not entries:
        return ["No handoffs yet.", "",
                "Each session writes one before it ends; if it dies first, the",
                "supervisor's reaper reconstructs it from the transcript."]
    out = []
    for e in entries:
        out += e.splitlines() + [""]
    return out


def board_lines(s: dict) -> list[str]:
    chans = pp_board.channels(s["goal"])
    out = ["## Channels (created by the agents themselves)", ""]
    out += [f"  {n:<20} {m.get('count', 0):>4} messages" for n, m in sorted(chans.items())] \
        or ["  (none yet)"]
    conv = s["goal"] / "board/CONVENTIONS.md"
    stub = conv.exists() and "belongs to the agents" in conv.read_text(errors="replace")
    out += ["", f"  conventions: {'still the stub — no session has written them' if stub else 'written by a session'}",
            "", "## Messages (newest last)", ""]
    msgs = pp_board.read(s["goal"], limit=40)
    # This pane scrolls, so there is no reason to abbreviate. `render`'s 1200
    # default is a briefing budget rule that leaked into the human's own view:
    # an operator looking for a message a session had raised for them found
    # "… [truncated, 2330 chars total]" and no way to see the rest.
    out += (pp_board.render(msgs, goal=s["goal"], max_body=10 ** 9).splitlines()
            if msgs else ["  (no messages)"])
    return out


def personas_lines(s: dict) -> list[str]:
    names = pp_persona.names(s["goal"])
    if not names:
        return ["No personas yet.", "",
                "A session writes one when it decides the goal needs a perspective it",
                "lacks — a sceptic, an archivist, a performance specialist. The persona",
                "then becomes the system prompt of every session after it."]
    out = []
    for name in names:
        marker = "  ← standing" if name == s["persona"] else ""
        out += [f"═══ {name}{marker} ═══", ""]
        out += pp_persona.read_persona(s["goal"], name).splitlines() + [""]
        dossier = pp_persona.read_dossier(s["goal"], name).strip()
        if dossier:
            out += [f"─── {name}: dossier ───", ""]
            out += dossier.splitlines() + [""]
    return out


def runs_lines(s: dict) -> list[str]:
    dirs = sorted((s["goal"] / "runs").glob("[0-9]*"), reverse=True)
    if not dirs:
        return ["No runs yet."]
    out = [f"{'run':>5}  {'backend':<14} {'persona':<12} {'exit':>4}  {'watchdog':<16} outcome", ""]
    for d in dirs[:60]:
        r = read_json(d / "run.json", {}) or {}
        if not r:
            launch = read_json(d / "launch.json", {}) or {}
            out.append(f"{run_no(d.name):>5}  {str(launch.get('backend', '?')):<14} "
                       f"{str(launch.get('persona') or '-'):<12} {'':>4}  {'in flight':<16} -")
            continue
        kind = (r.get("exit") or {}).get("kind") or ("wedge" if r.get("wedged") else "-")
        out.append(f"{r.get('run', d.name):>5}  {str(r.get('backend', '?')):<14} "
                   f"{str(r.get('persona') or '-'):<12} {str(r.get('exit_code', '?')):>4}  "
                   f"{str(r.get('watchdog', '?')):<16} {kind}"
                   + ("  [resumed]" if r.get("resumed") else ""))
    return out


def log_lines(s: dict) -> list[str]:
    log = s["goal"] / "supervisor.log"
    if not log.exists():
        return ["No supervisor log yet — this goal has never been started."]
    return log.read_text(encoding="utf-8", errors="replace").splitlines()[-400:]


DIAGNOSTIC_RENDERERS = [journal_lines, board_lines, personas_lines, runs_lines, log_lines]
TAB_RENDERERS = [pp_views.summary, lambda s: pp_views.inbox(s) + questions_lines(s),
                 pp_views.plan, pp_views.activity, pp_views.controls, log_lines]
assert len(TAB_RENDERERS) == len(TABS)


# ── actions ───────────────────────────────────────────────────────────────
def start_supervisor(gid: str, once: bool = False) -> None:
    pp_control.start_background(GOALS / gid, once=once)


def stop_supervisor(gid: str) -> str:
    """SIGTERM the supervisor itself — not its process group.

    The supervisor's SIGTERM handler kills the exact pi run it owns and then
    releases the goal. Signalling the whole group instead would race that cleanup
    (and, when the dashboard shares a group with it, stop the dashboard too).
    """
    pid = supervisor_pid(GOALS / gid)
    if not pid:
        return "not running"
    try:
        os.kill(pid, signal.SIGTERM)
        return f"asked supervisor {pid} to stop (it kills its session first)"
    except OSError as exc:
        return f"could not stop {pid}: {exc}"


def shell_out(stdscr, fn) -> None:
    """Leave curses, run something that owns the terminal, come back."""
    curses.def_prog_mode()
    curses.endwin()
    try:
        fn()
    finally:
        print("\n[press enter to return to the dashboard]", end="", flush=True)
        try:
            input()
        except EOFError:
            pass
        curses.reset_prog_mode()
        stdscr.clearok(True)
        stdscr.refresh()


# ── screens ───────────────────────────────────────────────────────────────
class Dashboard:
    def __init__(self, stdscr):
        self.scr = stdscr
        self.sel = 0
        self.goal: str | None = None       # None = list view
        self.tab = 0
        self.scroll = 0
        self.flash = ""
        self.rows: list[dict] = []
        self.refreshed = 0.0
        self.query = ""
        self.diagnostic = 0
        self.view_positions = {}
        self.activity_cursors = [None]
        self.activity_next = None
        self.activity_expanded = False
        self.help_open = False
        self.help_scroll = 0
        self.stop_target = None
        self.check_goal = None
        self.confirm_stop = False
        self.check_executor = ThreadPoolExecutor(max_workers=1)
        self.check_future = None
        self.check_cancel = threading.Event()

    # -- data
    def reload(self) -> None:
        selected = self.rows[self.sel]["id"] if self.rows else None
        self.rows = sorted((summarize(g) for g in goal_ids()),
                           key=lambda s: (not bool(s["inbox"]), s["id"]))
        if selected:
            self.sel = next((i for i, s in enumerate(self.rows) if s["id"] == selected), self.sel)
        self.sel = max(0, min(self.sel, len(self.rows) - 1))
        self.refreshed = time.time()

    def current(self) -> dict | None:
        if self.goal:
            for r in self.rows:
                if r["id"] == self.goal:
                    return r
            return None
        return self.rows[self.sel] if self.rows else None

    def view_key(self):
        return (self.goal, self.tab, self.diagnostic if self.tab == 5 else None)

    def navigate(self, *, goal=None, tab=None, diagnostic=None):
        self.view_positions[self.view_key()] = (self.scroll, self.query, list(self.activity_cursors))
        if goal is not None:
            self.goal = goal
        if tab is not None:
            self.tab = tab
        if diagnostic is not None:
            self.diagnostic = diagnostic
        self.scroll, self.query, self.activity_cursors = self.view_positions.get(self.view_key(), (0, '', [None]))
        self.activity_next = None

    # -- drawing
    def line(self, y: int, x: int, text: str, attr=0) -> None:
        h, w = self.scr.getmaxyx()
        if 0 <= y < h and w - x - 1 > 0:
            clipped = pp_views.clip(text, w - x - 1)
            try:
                self.scr.addstr(y, x, clipped + ' ' * (w - x - 1 - pp_views.cells(clipped)), attr)
            except curses.error:
                pass

    def md_line(self, y: int, x: int, text: str) -> None:
        """Like line(), but the journal/board/overview text is markdown the
        agents wrote — render headings/bold/code/quotes/bullets instead of
        showing the raw `##`/`**`/`` ` `` markers."""
        h, w = self.scr.getmaxyx()
        maxlen = w - x - 1
        if not (0 <= y < h) or maxlen <= 0:
            return
        body, base_attr = md_block(text)
        cx = 0
        for chunk, extra in parse_inline_md(body):
            room = maxlen - cx
            if room <= 0:
                break
            chunk = pp_views.clip(chunk, room)
            if chunk:
                try:
                    self.scr.addstr(y, x + cx, chunk, base_attr | extra)
                except curses.error:
                    pass
            cx += pp_views.cells(chunk)

    def header(self, text: str, right: str = "") -> None:
        _, w = self.scr.getmaxyx()
        self.line(0, 0, f" {text}", curses.A_REVERSE | curses.A_BOLD)
        if right and pp_views.cells(right) + pp_views.cells(text) + 4 < w:
            self.line(0, w - pp_views.cells(right) - 2, right, curses.A_REVERSE)

    def footer(self, keys: str) -> None:
        h, w = self.scr.getmaxyx()
        compact = '? help · q back' if self.goal else 'Enter open · ? help · q quit'
        label = keys if pp_views.cells(keys) < w - 2 else compact
        if self.check_future and not self.check_future.done():
            label = f"Checking {self.check_goal} · " + label
        self.line(h - 1, 0, f" {self.flash or label}",
                  curses.A_REVERSE if self.flash else curses.A_DIM)

    def draw_list(self) -> None:
        h, w = self.scr.getmaxyx()
        self.header("perpetua — goals", f"{len(self.rows)} goal(s) ")
        self.line(2, 2, 'Goal · state · inbox' if w < 72 else f"{'goal':<24}{'state':<16}{'inbox':>6}  {'claimed':>9}  last check", curses.A_BOLD)
        if not self.rows:
            self.line(4, 4, "No goals yet.")
            self.line(6, 4, "Press  n  to create one — you will be interviewed rather than")
            self.line(7, 4, "asked for flags, and the agent writes the completion check itself.")
        first = max(0, self.sel - max(1, h - 7) + 1)
        for i in range(first, len(self.rows)):
            r = self.rows[i]
            y = 4 + i - first
            if y >= h - 3:
                break
            sup = f"running (pid {r['pid']})" if r["pid"] else "stopped"
            if r["paused"]:
                sup = "PAUSED"
            qflag = f"  ❓{len(r['open_qs'])} waiting" if r.get("open_qs") else ""
            # A long id must not run into the status column: the row is the only
            # place a goal's state is legible at a glance, and a name that eats
            # the next field silently changes what the row appears to say.
            gid = r["id"] if len(r["id"]) <= 23 else r["id"][:22] + "…"
            status = "waiting model" if r["state"].get("resource_wait") else r["status"]
            text = (f"{gid:<24}{status:<16}{len(r['inbox']):>6}  "
                    f"{str(r['met']) + '/' + str(r['total']):>9}  "
                    f"{r['evidence'].get('verdict', 'unknown')} {r['evidence'].get('at', '')}")
            if w < 72:
                text = f"{status} · inbox {len(r['inbox'])} · {r['id']}"
            attr = curses.A_REVERSE if i == self.sel else 0
            if qflag and i != self.sel:
                attr |= curses.A_BOLD
            if r["status"] == "accomplished":
                attr |= curses.A_DIM
            self.line(y, 2, text, attr)
        self.footer("↑↓ select  ⏎ open  n new goal  i decide  s start  "
                    "S start-one  x stop  a attach  c check  r refresh  q quit")

    def draw_detail(self) -> None:
        s = self.current()
        if not s:
            self.goal = None
            return
        h, w = self.scr.getmaxyx()
        sup = f"supervisor running (pid {s['pid']})" if s["pid"] else "supervisor stopped"
        self.header(f"perpetua — {s['id']}", f"{sup} ")

        n_open = len(s.get("inbox") or [])
        x = 2
        # Slide the strip until the active tab is visible on narrow terminals.
        start = 0
        labels = [f" {name} ●{n_open} " if name == "Inbox" and n_open else f" {name} " for name in TABS]
        while start < self.tab and sum(len(v) + 1 for v in labels[start:self.tab + 1]) > w - 4:
            start += 1
        for i, name in enumerate(TABS):
            if i < start:
                continue
            selected = i == self.tab
            attr = curses.A_REVERSE | curses.A_BOLD if selected else curses.A_DIM
            label = f" {name} "
            # The Questions tab shouts when something is waiting: a ●N badge and
            # full brightness (bold, not dim) even when it is not the open tab,
            # so an operator scanning the dashboard cannot miss it.
            if name == "Inbox" and n_open:
                label = f" {name} ●{n_open} "
                if not selected:
                    attr = curses.A_BOLD
            if x + len(label) < w - 1:
                self.scr.addnstr(2, x, label, len(label), attr)
            x += len(label) + 1

        try:
            if self.tab == 5:
                body = [f"# Diagnostics / {DIAGNOSTICS[self.diagnostic]}",
                        '[ previous source · ] next source', ''] + DIAGNOSTIC_RENDERERS[self.diagnostic](s)
            elif self.tab == 3:
                rows, self.activity_next = pp_activity.page(s['goal'], before=self.activity_cursors[-1], query=self.query)
                body = [f"# Activity · page {len(self.activity_cursors)} · newest first",
                        '[ newer · ] older · e expand/collapse · / search all history', ''] + pp_activity.render(rows, expanded=self.activity_expanded, query=self.query)
            else:
                body = TAB_RENDERERS[self.tab](s)
        except Exception as exc:                       # noqa: BLE001
            body = [f"(could not render this tab: {type(exc).__name__}: {exc})"]

        if self.query and self.tab != 3:
            matches = [line for line in body if self.query.casefold() in line.casefold()]
            body = [f"Filter: {self.query} (/ to change)", ""] + (matches or ['No matches. Use / and Enter to clear this view’s filter.'])
        body = pp_views.wrap_lines(body, max(1, w - 4))
        view_h = max(1, h - 6)
        self.scroll = max(0, min(self.scroll, max(0, len(body) - view_h)))
        for i, text in enumerate(body[self.scroll:self.scroll + view_h]):
            self.md_line(4 + i, 2, text.replace("\t", "    "))

        pos = f"{self.scroll + 1}-{min(len(body), self.scroll + view_h)}/{len(body)}"
        self.line(h - 2, 2, f"{TABS[self.tab]} · {pos} · " + ('/ '+self.query if self.query else '? keyboard help'), curses.A_DIM)
        self.footer("←→ views · ↑↓ scroll · i decide · / search · ? help · q back")

    # -- input
    def draw_help(self):
        self.header('perpetua — keyboard help')
        h, w = self.scr.getmaxyx()
        lines = pp_views.wrap_lines([
            '1–5: Summary, Inbox, Plan, Activity, Controls. 6 or d: Diagnostics.',
            'Left/Right/Tab: change main view. Up/Down: scroll or select a goal.',
            'PageUp/PageDown: scroll a screen. g/G: beginning/end.',
            'Activity: [ newer page, ] older page. / searches all recorded history.',
            'Activity: e expands/collapses the full event bodies on this page.',
            'Diagnostics: [ or ] selects Journal, Board, Personas, Runs or Log.',
            '/: search this view. Empty search clears it. Filters stay with their view.',
            'i: explicit decisions. A: acknowledge alert. p: pause after handoff.',
            'x: stop immediately, then y to confirm the named goal. u: resume.',
            's/S: start continuous/one session. a: attach live work. n: new goal.',
            'c: background check. r: refresh. q/Esc: back; q on goals quits.',
        ], max(1, w-4))
        self.help_scroll = max(0, min(self.help_scroll, max(0,len(lines)-(h-4))))
        for i, line in enumerate(lines[self.help_scroll:self.help_scroll+max(0,h-4)]):
            self.line(i+2, 2, line)
        self.footer('↑↓ scroll help · other keys close without action')

    def key(self, ch: int) -> bool:
        """Returns False to quit."""
        self.flash = ""
        s = self.current()

        if self.help_open:
            if ch in (curses.KEY_DOWN, ord('j')):
                self.help_scroll += 1
            elif ch in (curses.KEY_UP, ord('k')):
                self.help_scroll = max(0,self.help_scroll-1)
            elif ch == curses.KEY_NPAGE:
                self.help_scroll += max(1,self.scr.getmaxyx()[0]-4)
            elif ch == curses.KEY_PPAGE:
                self.help_scroll = max(0,self.help_scroll-max(1,self.scr.getmaxyx()[0]-4))
            else:
                self.help_open = False
            return True
        if ch == ord('?') and not self.confirm_stop:
            self.help_open = True
            self.help_scroll = 0
            return True

        if self.confirm_stop:
            self.confirm_stop = False
            self.flash = stop_supervisor(self.stop_target) if ch == ord("y") and self.stop_target else "Stop cancelled"
            self.stop_target = None
            return True
        if ch == ord("/"):
            if not self.goal:
                self.flash = "Open a goal to search its current view"
                return True
            curses.echo()
            try:
                h, w = self.scr.getmaxyx()
                self.line(h - 1, 0, "Search: ")
                if w > 10:
                    self.query = self.scr.getstr(h - 1, 8, w - 10).decode(errors="replace")
                    self.scroll = 0
                    self.activity_cursors, self.activity_next = [None], None
            finally:
                curses.noecho()
            return True
        if s and ch == ord("p"):
            pp_control.request_pause(s["goal"])
            self.flash = "Pause requested after handoff"
            self.reload()
            return True
        if s and ch in (ord("u"), ord("A")):
            command = "resume" if ch == ord("u") else "ack"
            argv = [str(BIN / "perpetua"), command, s["id"]] + (["--background"] if command == "resume" else [])
            shell_out(self.scr, lambda: subprocess.call(argv))
            self.reload()
            return True

        if ch in (ord("q"), ord("Q")):
            if self.goal:
                self.view_positions[self.view_key()] = (self.scroll, self.query, list(self.activity_cursors))
                self.goal = None
                self.scroll = 0
                return True
            return False
        if ch == 27:                                    # esc
            self.view_positions[self.view_key()] = (self.scroll, self.query, list(self.activity_cursors))
            self.goal, self.scroll = None, 0
            return True
        if ch in (ord("r"), ord("R")):
            self.reload()
            self.flash = "refreshed"
            return True

        if ch == ord("n") and not self.goal:
            shell_out(self.scr, lambda: subprocess.call(
                [str(BIN / "perpetua"), "new"]))
            self.reload()
            return True

        if s and ch == ord("s"):
            if s["pid"]:
                self.flash = "already running"
            else:
                start_supervisor(s["id"])
                self.flash = f"started supervisor for {s['id']}"
            return True
        if s and ch == ord("S"):
            if s["pid"]:
                self.flash = "already running"
            else:
                start_supervisor(s["id"], once=True)
                self.flash = f"started ONE session for {s['id']}"
            return True
        if s and ch == ord("a"):
            shell_out(self.scr, lambda: subprocess.call(
                [str(BIN / "perpetua"), "attach", s["id"]]))
            return True
        if s and ch == ord("x"):
            self.confirm_stop = True
            self.stop_target = s['id']
            self.flash = f"Stop {s['id']} immediately? y confirms; any other key cancels."
            return True
        if s and ch == ord("c"):
            # The same runner the supervisor uses, so the dashboard cannot report a
            # different verdict from the thing that actually decides. Bounded, too:
            # an unbounded check.sh would hang the whole curses UI.
            if self.check_future and not self.check_future.done():
                self.flash = "A check is already running"
            else:
                self.check_cancel.clear()
                self.check_goal = s['id']
                self.check_future = self.check_executor.submit(pp_check.run, s["goal"], cancel=self.check_cancel)
                self.flash = "Checking in background; navigation remains available"
            return True

        if ch in (ord("i"), ord("I")):
            # The questionnaire is a normal terminal prompt loop, so drop out of
            # curses for it exactly as `n` does for the interview.
            argv = [str(BIN / "perpetua"), "decide"]
            if self.goal and s:
                argv.append(s["id"])
            shell_out(self.scr, lambda: subprocess.call(argv))
            self.reload()
            return True

        if not self.goal:                               # list view
            if ch in (curses.KEY_DOWN, ord("j")):
                self.sel = min(self.sel + 1, max(0, len(self.rows) - 1))
            elif ch in (curses.KEY_UP, ord("k")):
                self.sel = max(0, self.sel - 1)
            elif ch in (curses.KEY_ENTER, 10, 13, ord("l")) and s:
                self.navigate(goal=s['id'], tab=0)
            return True

        # detail view
        if ch in (curses.KEY_RIGHT, ord("\t")):
            self.navigate(tab=(self.tab + 1) % len(TABS))
        elif ch == curses.KEY_LEFT:
            self.navigate(tab=(self.tab - 1) % len(TABS))
        elif ch in (ord('['), ord(']')):
            if self.tab == 5:
                self.navigate(diagnostic=(self.diagnostic + (1 if ch == ord(']') else -1)) % len(DIAGNOSTICS))
            elif self.tab == 3:
                if ch == ord(']') and self.activity_next:
                    self.activity_cursors.append(self.activity_next)
                elif ch == ord('[') and len(self.activity_cursors) > 1:
                    self.activity_cursors.pop()
                else:
                    self.flash = 'No older activity' if ch == ord(']') else 'Already on newest page'
                self.scroll = 0
                self.activity_next = None
        elif ch == ord('d'):
            self.navigate(tab=5)
        elif ch == ord('e') and self.tab == 3:
            self.activity_expanded = not self.activity_expanded
            self.scroll = 0
        elif ch in (curses.KEY_DOWN, ord("j")):
            self.scroll += 1
        elif ch in (curses.KEY_UP, ord("k")):
            self.scroll = max(0, self.scroll - 1)
        elif ch == curses.KEY_NPAGE:
            self.scroll += max(1, self.scr.getmaxyx()[0] - 8)
        elif ch == curses.KEY_PPAGE:
            self.scroll = max(0, self.scroll - max(1, self.scr.getmaxyx()[0] - 8))
        elif ch == ord("g"):
            self.scroll = 0
        elif ch == ord("G"):
            self.scroll = 10 ** 6
        elif ord("1") <= ch <= ord("6"):
            self.navigate(tab=int(chr(ch))-1)
        elif ord('7') <= ch <= ord('9'):
            self.navigate(tab=5, diagnostic=int(chr(ch))-6)
        elif ch == ord("0"):
            self.navigate(tab=5, diagnostic=4)
        return True

    def loop(self) -> None:
        curses.curs_set(0)
        self.scr.timeout(int(REFRESH_S * 1000))
        self.reload()
        while True:
            if self.check_future and self.check_future.done():
                try:
                    res = self.check_future.result()
                    self.flash = f"{self.check_goal}: " + ("PASS" if res.passed else "FAIL" if res.ran else "UNKNOWN") + ": " + res.detail[-180:]
                except Exception as exc:
                    self.flash = f"Check error: {exc}"
                self.check_future = None
                self.reload()
            self.scr.erase()
            h, w = self.scr.getmaxyx()
            if h < 8 or w < 24:
                self.line(0, 0, 'Resize to at least 24×8')
            elif self.help_open:
                self.draw_help()
            else:
                (self.draw_detail if self.goal else self.draw_list)()
            self.scr.refresh()
            ch = self.scr.getch()
            if ch == -1:                                # timeout: live refresh
                self.reload()
                continue
            if ch == curses.KEY_RESIZE:
                continue
            if not self.key(ch):
                return
            if time.time() - self.refreshed > REFRESH_S:
                self.reload()


def main() -> int:
    if not GOALS.exists():
        GOALS.mkdir(parents=True, exist_ok=True)
    def run(scr):
        dash = Dashboard(scr)
        try:
            dash.loop()
        finally:
            dash.check_cancel.set()
            dash.check_executor.shutdown(wait=True)
    try:
        curses.wrapper(run)
    except KeyboardInterrupt:
        pass
    return 0
