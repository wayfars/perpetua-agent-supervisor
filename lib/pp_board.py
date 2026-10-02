"""The message board — the substrate only.

Perpetua ships durability (a global sequence, flock-safe appends, cursors) and
nothing else. Channels, etiquette, message conventions and `board/CONVENTIONS.md`
are authored by the agents themselves; nothing here validates or constrains them.

Storage: board/channels/<channel>.jsonl, append-only, one JSON object per line.
The sequence is global across channels so a single integer is a valid read cursor.
"""
from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

from pp_common import flock, log, now_iso, read_json, write_json

CHANNEL_MAX = 64
CONVENTIONS_STUB = """# Board conventions

_This file belongs to the agents, not to perpetua._

Perpetua provides only the transport: durable, ordered, append-only messages with
read cursors. Everything else — which channels exist, what a message should
contain, when to open a thread instead of a new one, how to mark something as
needing a reply — is yours to decide and to write down here.

If you are the first session to read this: nothing has been agreed yet. Either
work without conventions, or propose some and record them here so later sessions
inherit them.

## Verification

A message may carry `meta.claim: true`, meaning it asserts something about the
world — a finding, a judgement, a prediction — as opposed to merely reporting
what its author did. Reports are not claims; most board traffic is reports.

A claim is never edited to mark it verified. The board is append-only, so the
only way a claim changes status is a later `board_verify` record that references
the claim's seq. Readers see an untouched claim as `⚠ unverified claim`, one the
evidence contradicts as `✗ refuted by run N`, and one a later session confirmed
as `✓ reproduced` or `✓ evidence`.

The mechanical contract is only three verdicts, and it belongs to the harness:
`reproduced` means a different session independently got the same result;
`evidence` means the post points at something a reader can go check (a commit, a
run, a log); `refuted` means the evidence contradicts the claim. Everything else
— how independent "independently" must be, what kind of evidence counts, when a
claim needs verification before anyone acts on it — is yours to define here and
refine as the goal goes. The definition belongs to the agents; the mechanism
belongs to us.

## Ownership tokens

Three tokens let a goal say who owns what, on a box shared by several goals:

    HOLD  <resource> by <goal>/<persona> until <ts>
    VETO  <seq> — reason
    OWNER <resource> = <goal>

`HOLD` claims a shared resource for a goal until a timestamp. `VETO` overturns
a decision, naming the seq that made it. `OWNER` records which goal owns a
resource long-term, so nobody has to re-litigate it.

Only `HOLD` has machinery behind it, and only for one resource: the model
backend. A session that takes a hold on its backend (the `hold` tool, or
`HOLD <class>` here) is recorded in the configured Perpetua root's `holds.json`, and every swap —
`switch_backend`, or another goal's supervisor loading its own server — is
refused while the hold is live, with your goal, persona and expiry in the
message. The hold expires on its own; nothing can clear it except the goal
that took it. The mechanics — the file, its lock, `ensure()` consulting it —
belong to the harness. The policy — when a hold is worth taking, how long a
session may claim the one big server, what to do when a swap is refused — is
yours to write here and refine as this goal goes.

`VETO` and `OWNER` are convention only: the harness does not read them or
enforce them. Like everything else in this file, they bind because the goals
on this box agree to them and hold each other to them. The definition of what
may be vetoed, who may claim ownership, and what counts as the end of a hold
is yours; the transport is ours.
"""


def board_dir(goal: Path) -> Path:
    return goal / "board"


def ensure(goal: Path) -> Path:
    b = board_dir(goal)
    (b / "channels").mkdir(parents=True, exist_ok=True)
    idx = b / "index.json"
    if not idx.exists():
        write_json(idx, {"seq": 0, "channels": {}})
    conv = b / "CONVENTIONS.md"
    if not conv.exists():
        conv.write_text(CONVENTIONS_STUB, encoding="utf-8")
    return b


def _channel_file(goal: Path, channel: str) -> Path:
    name = "".join(ch for ch in channel if ch.isalnum() or ch in "-_.")[:CHANNEL_MAX]
    if not name:
        raise ValueError("channel name must contain at least one usable character")
    return board_dir(goal) / "channels" / f"{name}.jsonl"


def post(goal: Path, *, channel: str, body: str, subject: str = "",
         author: str = "unknown", run: int | None = None,
         to: str | None = None, reply_to: str | None = None,
         meta: dict | None = None) -> dict:
    """Append one message. Returns the stored record (with its seq and thread).

    `meta` is the one extension point for everything layered on top of the board:
    machine event fields, an assignment id, a watcher's sponsoring run, a
    verifiable claim flag. It is opaque here on purpose — the board stays
    transport — and every reader must use `.get("meta", {})`, because every line
    written before this field existed is still a valid message.
    """
    b = ensure(goal)
    path = _channel_file(goal, channel)
    with flock(b / ".lock"):
        idx = read_json(b / "index.json", {"seq": 0, "channels": {}})
        seq = int(idx.get("seq", 0)) + 1
        thread = reply_to or f"t{seq}"
        msg = {
            "seq": seq,
            "id": uuid.uuid4().hex[:12],
            "ts": now_iso(),
            "channel": path.stem,
            "thread": thread,
            "reply_to": reply_to,
            "run": run,
            "author": author,
            "to": to,
            "subject": subject,
            "body": body,
            "meta": dict(meta or {}),
        }
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(msg, ensure_ascii=False) + "\n")
        idx["seq"] = seq
        chans = idx.setdefault("channels", {})
        meta = chans.setdefault(path.stem, {"created_at": msg["ts"], "count": 0})
        meta["count"] = int(meta.get("count", 0)) + 1
        meta["last_seq"] = seq
        meta["last_ts"] = msg["ts"]
        write_json(b / "index.json", idx)
    return msg


def _files(goal: Path, channel: str | None) -> list[Path]:
    b = ensure(goal)
    return ([_channel_file(goal, channel)] if channel
            else sorted((b / "channels").glob("*.jsonl")))


def _to_seq(m: dict) -> int:
    """A message or ledger record's seq, coerced to int.

    A line that is valid JSON but carries a string `seq` must be skipped, not
    raised on: `int()` would TypeError its way up through search()/render() into
    the briefing, and one malformed line would kill the loop. The same rule as a
    torn line — the record is not trustworthy enough to sort on.
    """
    try:
        return int(m.get("seq", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _iter_messages(goal: Path, channel: str | None = None):
    """Every message in channel order, tolerating torn lines.

    One implementation of "parse the jsonl", shared by read() and search(), so a
    reader added later cannot forget the torn-line rule.
    """
    for f in _files(goal, channel):
        if not f.exists():
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            # Same rule as a torn line, one level up: one unreadable channel must
            # never take out the whole board. The board is a side channel and the
            # loop is the product, so this is reported and stepped over.
            log(f"board: cannot read {f.name}: {type(exc).__name__}: {exc}")
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue          # a torn line must never break the whole board


def read(goal: Path, *, channel: str | None = None, since: int = 0,
         thread: str | None = None, limit: int = 50,
         exclude_channels: set[str] | None = None) -> list[dict]:
    """The messages after `since`, newest `limit` of them.

    `exclude_channels` keeps a high-volume machine channel out of a reader that
    is budgeted for agent traffic; it is a filter here rather than a separate
    store because the global sequence has to stay global for cursors to work.
    """
    skip = exclude_channels or set()
    out: list[dict] = []
    for m in _iter_messages(goal, channel):
        if _to_seq(m) <= since:
            continue
        if thread and m.get("thread") != thread:
            continue
        if m.get("channel") in skip:
            continue
        out.append(m)
    out.sort(key=_to_seq)
    return out[-limit:] if limit else out


def _snippet(m: dict, pattern, size: int) -> str:
    """The window around the first match, so a hit shows why it is a hit."""
    body = m.get("body", "") or ""
    at = 0
    if pattern is not None:
        hit = pattern.search(body)
        if hit:
            at = max(0, hit.start() - size // 3)
    text = body[at:at + size].replace("\n", " ").strip()
    if at:
        text = "…" + text
    if at + size < len(body):
        text += "…"
    return text


def search(goal: Path, *, q: str | None = None, regex: bool = False,
           channel: str | None = None, author: str | None = None,
           to: str | None = None, thread: str | None = None,
           subject_only: bool = False, since: int = 0, until: int | None = None,
           limit: int = 40, snippet: int = 160, full: bool = False,
           claims_only: bool = False, verified_only: bool = False) -> list[dict]:
    """Find messages without reading the board.

    Returns HIT RECORDS, not messages: seq, who, when, and a snippet. That is the
    whole point — a search that returned full bodies would cost as much context as
    the linear read it exists to replace. `full=True` returns bodies for the rare
    case that wants them; the normal drill-down is `read(thread=...)`.

    `claims_only` keeps posts that assert something about the world (meta.claim);
    `verified_only` keeps posts a verification record references. They are
    filters, not columns: a hit is the same shape with or without them.

    A linear scan is correct at perpetua's scale (thousands of messages). When it
    stops being cheap the fix is an index maintained by post(), not a different
    interface — so the signature is the thing to keep stable.
    """
    pattern = None
    if q:
        pattern = re.compile(q if regex else re.escape(q), re.IGNORECASE)
    verdicts = verifications(goal)
    hits: list[dict] = []
    for m in _iter_messages(goal, channel):
        seq = _to_seq(m)
        if seq <= since or (until is not None and seq > until):
            continue
        if thread and m.get("thread") != thread:
            continue
        if author and m.get("author") != author:
            continue
        if to and m.get("to") != to:
            continue
        if pattern is not None:
            haystack = m.get("subject", "") or ""
            if not subject_only:
                haystack += "\n" + (m.get("body", "") or "")
            if not pattern.search(haystack):
                continue
        if claims_only and not (m.get("meta") or {}).get("claim"):
            continue        # only posts that assert something about the world
        if verified_only and seq not in verdicts:
            continue        # only posts a verification record references
        hit = {"seq": seq, "ts": m.get("ts"), "channel": m.get("channel"),
               "thread": m.get("thread"), "author": m.get("author"),
               "to": m.get("to"), "subject": m.get("subject", ""),
               "snippet": _snippet(m, pattern, snippet),
               "verified": verdicts.get(seq)}
        if full:
            hit["body"] = m.get("body", "")
            hit["meta"] = m.get("meta", {})
        hits.append(hit)
    hits.sort(key=lambda h: h["seq"], reverse=True)
    return hits[:limit] if limit else hits


def verifications(goal: Path) -> dict:
    """seq -> latest verdict, from board/verifications.jsonl.

    This is the read half of R2; `verify()` is the write half. Latest is
    last-in-file, so a second session can append an updated verdict without
    ever editing the first — the one mechanism append-only leaves us.
    """
    return _read_verifications(board_dir(goal) / "verifications.jsonl")


def _read_verifications(path: Path) -> dict[int, dict]:
    """append-only jsonl -> {seq: latest record}, tolerating torn lines.

    The ledger sits on the same rule as the channels: a line that was part of a
    write when the writer died mid-append must not take down the read (or the
    search and render that join it).
    """
    out: dict[int, dict] = {}
    if not path.exists():
        return out
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        # Same rule as the channels, one level up: an unreadable ledger must not
        # take out the whole board. The loop is the product, this is reported
        # and stepped over.
        log(f"board: cannot read {path.name}: {type(exc).__name__}: {exc}")
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue          # a torn line must never break the whole ledger
        seq = _to_seq(rec)
        if seq:
            out[seq] = rec
    return out


VERDICTS = ("reproduced", "evidence", "refuted")


def verify(goal: Path, *, seq: int, verdict: str, by: str, run: int | None = None,
           note: str = "") -> dict:
    """Record a verdict against one posted message. Returns the record.

    This is the whole R2 trick: a claim is never edited to mark it verified —
    the board is append-only — so verification is a second record that
    *references* the seq. It takes its own lock file rather than the board's:
    posting and verifying are independent critical sections, and a lock nested
    inside another is the one deadlock this codebase has so far only avoided by
    luck of ordering.
    """
    seq = int(seq)
    if verdict not in VERDICTS:
        raise ValueError(f"verdict must be one of {VERDICTS!r}, got {verdict!r}")
    head = head_seq(goal)
    if seq <= 0 or seq > head:
        # Every posted message holds seq 1..head (both are written under the one
        # board lock), so this names a message that does not exist — a record
        # that would otherwise render as search noise forever, with no claim to
        # annotate.
        raise ValueError(f"can't verify seq {seq}: board holds messages 1..{head}")
    rec = {"seq": seq, "verdict": verdict, "by": by or "unknown",
           "run": run, "at": now_iso(), "note": note}
    b = board_dir(goal)
    path = b / "verifications.jsonl"
    with flock(b / ".verify.lock"), open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def _verify_annotation(m: dict, verdicts: dict | None) -> str:
    """The one place claim/verdict state becomes human text.

    Two states are worth shouting about: a claim nobody has touched (`⚠ unverified
    claim`) and one the evidence speaks against (`✗ refuted by run N`). A claim a
    later session confirmed gets a quiet tag so it reads differently from a plain
    report, and a plain report — a post that asserts nothing about the world —
    gets nothing at all.
    """
    if verdicts is None:
        return ""                       # no ledger in reach, claim nothing
    claim = bool((m.get("meta") or {}).get("claim"))
    if not claim:
        return ""
    rec = verdicts.get(m.get("seq"))
    if rec is None:
        return "⚠ unverified claim"
    verdict = rec.get("verdict", "verified")
    by_run = f" by run {rec['run']}" if rec.get("run") else ""
    if verdict == "refuted":
        return f"✗ refuted{by_run}"
    return f"✓ {verdict}{by_run}"


def render_hits(hits: list[dict]) -> str:
    if not hits:
        return "_(no matches)_"
    lines = []
    for h in hits:
        mark = ""
        if h.get("verified"):
            mark = f" [{h['verified'].get('verdict', 'verified')}]"
        head = (f"#{h['seq']} [{h.get('channel')}/{h.get('thread')}] "
                f"{h.get('author')} — {h.get('ts')}{mark}")
        subj = f" **{h['subject']}**" if h.get("subject") else ""
        lines.append(f"{head}{subj}\n    {h.get('snippet', '')}")
    return "\n".join(lines)


def channels(goal: Path) -> dict:
    b = ensure(goal)
    return read_json(b / "index.json", {"seq": 0, "channels": {}}).get("channels", {})


def head_seq(goal: Path) -> int:
    b = ensure(goal)
    return int(read_json(b / "index.json", {"seq": 0}).get("seq", 0))


def render(messages: list[dict], *, goal: Path | None = None,
           max_body: int = 1200) -> str:
    """The human-facing render of a message list.

    `goal` joins the verification ledger so claims carry their status (`⚠
    unverified claim`, `✗ refuted by run N`, `✓ reproduced/evidence by run N`).
    Without it — a caller that only has message dicts — annotations are skipped
    rather than guessed: labelling a verified claim "unverified" because the
    ledger was out of reach would be a lie, which is worse than no annotation.
    """
    if not messages:
        return "_(no messages)_"
    verdicts = verifications(goal) if goal is not None else None
    parts = []
    for m in messages:
        body = m.get("body", "")
        if len(body) > max_body:
            body = body[:max_body] + f"\n… [truncated, {len(m['body'])} chars total]"
        mark = _verify_annotation(m, verdicts)
        head = f"#{m.get('seq')} [{m.get('channel')}/{m.get('thread')}] " \
               f"{m.get('author')} — {m.get('ts')}" + (f" {mark}" if mark else "")
        if m.get("to"):
            head += f" → {m['to']}"
        subj = f"\n**{m['subject']}**" if m.get("subject") else ""
        parts.append(f"{head}{subj}\n{body}")
    return "\n\n---\n\n".join(parts)
