"""Recall — a search over the goal's own past, not a push of it.

`pp_briefing` pushes the last few handoffs; history is everything else, and by
run 400 of a thousand-session goal everything session 12 learned is unreachable
unless it happened to be promoted into `.pi/wiki/`. Gas Town's answer ("seance")
is a query over predecessors' own event log instead of a summary handed down at
spawn. This is perpetua's version of that query.

Deliberately NO index file. A goal's journal is thousands of small markdown
files at the outside, and grep-shaped scanning over them on every call is fast
enough that a cache would be solving a problem that does not exist yet — and a
cache is one more thing that can go stale. If a real goal's profile says
otherwise, the fix is a cache bolted onto this module, not a different shape
for it.

Ranking is deliberately naive: term-frequency over whitespace/punctuation-split
tokens, case-insensitive, with ties broken toward the more recent run. A goal
this size does not need TF-IDF or embeddings, and a search a session cannot
predict the ranking of is a search it stops trusting.

Reads must survive exactly what the board and the journal already survive: a
run killed mid-write. A half-written journal entry is read as whatever bytes
made it to disk, the same way `pp_journal.rebuild_digest` already treats one —
it is a value to score, never a reason to raise.
"""
from __future__ import annotations

import re
from pathlib import Path

from pp_common import UnsafeRunId, read_json, run_dir, run_id

#: Session tool default and hard cap (item #1). A session that asks for more
#: than this is asking for the wrong thing — recall is meant to return a
#: handful of the right hits, not a second copy of the journal.
LIMIT_DEFAULT = 8
LIMIT_MAX = 25

#: Lines of context kept on each side of the best-scoring line in an entry, so
#: a hit reads as a sentence in its paragraph rather than as an isolated line.
CONTEXT_LINES = 2

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall((text or "").lower())


def _journal_dir(goal: Path) -> Path:
    return goal / "journal"


def _entry_files(goal: Path) -> list[Path]:
    """Every journal entry file, tolerating anything that is not one.

    Same filter as `pp_journal._journal_entries`: a stray `notes.md` or a
    directory dropped in `journal/` must not become a search hit, and a name
    that merely LOOKS like a run id (`0042x.md`) must not either.
    """
    jdir = _journal_dir(goal)
    if not jdir.exists():
        return []
    out = []
    for p in jdir.glob("[0-9]*.md"):
        try:
            if not p.is_file():
                continue
        except OSError:
            continue
        left, _, right = p.stem.partition(".")
        if not left.isdigit():
            continue
        if right and not right.isdigit():
            continue
        out.append(p)
    return out


def _run_key(path: Path) -> str:
    """The canonical run id this entry belongs to — its own filename stem."""
    return path.stem


def _run_int(run_key: str) -> int:
    try:
        return int(run_key.partition(".")[0])
    except ValueError:
        return 0


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _handoff_meta(goal: Path, run_key: str) -> dict:
    """persona / outcome / written-at for one run, from its own handoff.

    Read from `handoff.json`, not `run.json`: it is written before the journal
    entry is rendered and it is what the entry itself was rendered from, so
    they can never disagree about what a run's own reason was.
    """
    try:
        rid = run_id(run_key)
    except UnsafeRunId:
        return {}
    handoff = read_json(run_dir(goal, rid) / "handoff.json", {}) or {}
    return {"persona": handoff.get("persona"),
            "outcome": handoff.get("reason"),
            "at": handoff.get("written_at")}


def _best_window(lines: list[str], tokens: set[str]) -> tuple[int, str]:
    """The (score, excerpt) for the line that best matches `tokens`.

    Score is the count of query tokens found in that one line — not the whole
    entry — because the excerpt has to be the line that justifies being shown,
    not merely a line from an entry that happens to score well elsewhere.
    """
    best_i, best_score = 0, -1
    for i, line in enumerate(lines):
        line_tokens = _tokens(line)
        score = sum(1 for t in line_tokens if t in tokens)
        if score > best_score:
            best_i, best_score = i, score
    lo = max(0, best_i - CONTEXT_LINES)
    hi = min(len(lines), best_i + CONTEXT_LINES + 1)
    excerpt = "\n".join(lines[lo:hi]).strip()
    return best_score, excerpt


def search(goal: Path, query: str, *, limit: int = LIMIT_DEFAULT,
           before_run: int | None = None, since_run: int | None = None
           ) -> list[dict]:
    """The goal's own past, ranked by term frequency against `query`.

    Every hit: `run` (canonical id), `at`, `persona`, `outcome`, `excerpt` (the
    matched line in context), `path` (the whole entry, for a session that wants
    more than the excerpt), and `score`. A query with no usable tokens, or a
    goal with no journal, returns empty — never raises.
    """
    limit = max(1, min(int(limit or LIMIT_DEFAULT), LIMIT_MAX))
    q_tokens = set(_tokens(query))
    if not q_tokens:
        return []
    hits: list[dict] = []
    for path in _entry_files(goal):
        run_key = _run_key(path)
        rn = _run_int(run_key)
        if before_run is not None and rn > int(before_run):
            continue
        if since_run is not None and rn <= int(since_run):
            continue
        text = _read(path)
        if not text.strip():
            continue                      # a torn or empty entry scores nothing
        doc_tokens = _tokens(text)
        doc_score = sum(1 for t in doc_tokens if t in q_tokens)
        if doc_score <= 0:
            continue
        _, excerpt = _best_window(text.splitlines(), q_tokens)
        meta = _handoff_meta(goal, run_key)
        hits.append({
            "run": run_key,
            "at": meta.get("at"),
            "persona": meta.get("persona"),
            "outcome": meta.get("outcome"),
            "excerpt": excerpt,
            "path": str(path),
            "score": doc_score,
        })
    # Recency tiebreak: among equal scores, the more recent run sorts first —
    # the same bias `pp_journal._journal_entries` already uses for the digest,
    # so the two views of "recent" never disagree.
    hits.sort(key=lambda h: (h["score"], _run_int(h["run"])), reverse=True)
    return hits[:limit]


def reachable_count(goal: Path) -> int:
    """How many journal entries exist to search — what the briefing quotes."""
    return len(_entry_files(goal))


def render(hits: list[dict]) -> str:
    if not hits:
        return "_(no matches)_"
    lines = []
    for h in hits:
        head = (f"run {h['run']}" + (f" ({h['persona']})" if h.get("persona") else "")
                + (f" — {h['outcome']}" if h.get("outcome") else "")
                + (f" @ {h['at']}" if h.get("at") else ""))
        lines.append(f"**{head}**\n> {h['excerpt']}\n({h['path']})")
    return "\n\n".join(lines)
