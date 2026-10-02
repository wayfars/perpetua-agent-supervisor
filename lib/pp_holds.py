"""Cross-goal ownership tokens: live holds on shared resources, one per box.

The incident-plan grammar (`HOLD <resource> by <goal>/<persona> until <ts>`,
written into every board's CONVENTIONS_STUB) is convention until a swap would
yank a server out from under a goal that is using it. That one contention is
real: the local model backends carry Conflicts=, so only one big server can be
loaded at a time, and pp_backend.ensure() used to swap freely. This module is
the machinery half of the HOLD token — the file lives at the perpetua ROOT,
NOT in a goal dir, because a hold claims something no single goal owns.

Three failure classes shaped it, all already pinned in
`.pi/wiki/harness/review-failure-classes.md`:

* The file is one `cd ..` from any agent's shell, so every read is
  lstat-guarded and size-capped: a FIFO at holds.json would make open() block
  forever, and a hang is not an exception — no try/except can catch it.
* The file is JSON-lines, so a torn or hand-edited line is skipped the way the
  board skips one, and a VALID line whose expiry is a string (or garbage)
  must coerce to "long expired" rather than raise out of float(). Only
  `JSONDecodeError` is a torn line; the malformed-but-valid record is the
  same class one type deeper.
* Reads never mutate. Expiry is evaluated at read time; records are only
  cleaned up by acquire/release, which already hold the write lock. A reader
  that rewrote would need the lock, and then every health check would contend
  with every swap.
"""

from __future__ import annotations

import errno
import json
import os
import stat
import time
from datetime import datetime, timezone
from pathlib import Path

from pp_common import ROOT, flock, log

DEFAULT_TTL_S = 8 * 3600  # covers a session plus the gap to the next
MAX_TTL_S = 48 * 3600  # a hold that long is a claim someone should review
HOLDS_FILE = ROOT / "holds.json"
HOLDS_LOCK = ROOT / "holds.lock"
HOLDS_MAX_BYTES = 1 << 20  # a few KB of holds; anything bigger is a plant


class HoldRefused(ValueError):
    """Another goal holds the resource live. Carries the record that refused."""

    def __init__(self, message: str, record: dict | None = None):
        super().__init__(message)
        self.record = record or {}


def goal_id(goal) -> str:
    """The identity a hold is stored and judged by: the goal dir's name.

    Accepts a goal directory Path or a bare id string, so the supervisor
    (which has only the dir) and pp-tool/CLI (which pass what they have)
    cannot disagree about who holds what. A garbage value — a record a session
    hand-edited — coerces to a string that matches nobody, so it can never
    impersonate your goal.
    """
    if isinstance(goal, Path):
        return goal.name
    return str(goal or "").strip()


def _to_epoch(v) -> float:
    """A record's timestamp, coerced to epoch seconds.

    Same rule as `_to_seq` in pp_board, one type deeper: a VALID line whose
    expires_at is a string must not raise out of float() and take the reader —
    and with it ensure() and the supervisor's poll loop — down. Anything
    uncoercible reads as 0.0, i.e. "expired since 1970", which can never
    block a swap.
    """
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    return f if f > 0 else 0.0


def effective_expiry(rec: dict) -> float:
    """A record's expiry, clamped to something this box can actually honour.

    `_to_epoch` makes the *parse* total; this makes the *value* sane, and the
    two failures it prevents are different. `acquire` caps a hold at MAX_TTL_S,
    but nothing caps what a hand-written or corrupted record may claim, and the
    read path accepts whatever is there:

    * A year-2100 expiry is a permanent, unroutable block — only the named
      holder may release it, and the holder is a string in a file anyone with
      a shell can write. Clamping means the worst a plant can do is hold the
      server for MAX_TTL_S rather than forever.
    * A representable-but-absurd float (1e300 parses fine) is past what
      `datetime.fromtimestamp` can render, and that raises OverflowError —
      which `_to_epoch`'s tolerance does not catch because the value coerced
      perfectly well. The tolerant parse and the intolerant *display* of the
      parsed value are two different guards; this is the second one.
    """
    return min(_to_epoch(rec.get("expires_at")), time.time() + MAX_TTL_S)


def _is_live(rec: dict) -> bool:
    """A hold is live when its expiry is still in the future — judged NOW."""
    return effective_expiry(rec) > time.time()


def _format_epoch(epoch: float) -> str:
    if epoch <= 0:
        return "(no expiry)"
    try:
        return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        # Belt and braces behind effective_expiry: this string is built inside
        # the backend swap gate, which runs in the supervisor's poll loop, so
        # nothing here may raise however the record got its value.
        return "(unrenderable expiry)"


def describe_hold(rec: dict) -> str:
    """The human string for one record: `goal/persona` until <ts>.

    The swap refusal must name exactly this — a refusal a human cannot act on
    is a refusal that gets worked around, and the message's whole job is to
    say who owns the server and when it frees.
    """
    g = goal_id(rec.get("goal")) or "unknown-goal"
    p = str(rec.get("persona") or "?").strip() or "?"
    return f"`{g}/{p}` until {_format_epoch(effective_expiry(rec))}"


def _load() -> list[dict]:
    """Every parseable record, tolerantly. Never raises, never blocks.

    Only a plain regular file under HOLDS_MAX_BYTES is ever read: a symlink,
    directory or FIFO at the path reads as "no holds", because open() on a
    fifo BLOCKS and the supervisor's poll loop cannot catch it. Lines that do
    not parse are skipped like a torn board line; a line that parses but
    carries garbage fields is tolerated by the per-field coercions, not raised
    on. An unreadable-but-regular file gets one log line and an empty read —
    a broken holds file must not take down every goal on the box.

    The guard is on the OPEN, not before it (D6 W2). lstat-then-read_text is
    check-then-use: a fifo planted in the window between the two still hangs
    the reader, and this read runs inside the backend swap gate. O_NONBLOCK
    makes the open itself return immediately even if what we opened turned out
    to be a fifo, O_NOFOLLOW refuses a symlink at open time, and the fstat is
    on the descriptor we actually hold — so what we checked and what we read
    cannot be two different files.
    """
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(HOLDS_FILE, flags)
    except OSError as exc:
        # Absent, or a symlink we refuse to follow: both are ordinary and quiet.
        # A PERMISSION failure is not — it means holds.json exists and this
        # process cannot read it, so every swap gate silently sees "no holds"
        # and a server can be pulled out from under a live session with nothing
        # in the log to explain it. Moving the guard onto the open made the
        # log line below unreachable for exactly that case; this restores it.
        if exc.errno not in (errno.ENOENT, errno.ELOOP, errno.ENOTDIR):
            log(f"holds: cannot open {HOLDS_FILE.name}: "
                f"{type(exc).__name__}: {exc}")
        return []
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > HOLDS_MAX_BYTES:
            return []                  # fifo, directory, device, or a plant
        chunks: list[bytes] = []
        got = 0
        while got <= HOLDS_MAX_BYTES:
            block = os.read(fd, 1 << 16)
            if not block:
                break
            chunks.append(block)
            got += len(block)
        text = b"".join(chunks).decode("utf-8", errors="replace")
    except OSError as exc:
        log(f"holds: cannot read {HOLDS_FILE.name}: {type(exc).__name__}: {exc}")
        return []
    finally:
        os.close(fd)
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue  # a torn line must never break the holds reader
        if isinstance(rec, dict):
            out.append(rec)
    return out


def _rewrite(records: list[dict]) -> None:
    """Atomically replace holds.json with `records`, one JSON object per line.

    Same shape as pp_common.write_json (tmp + fsync + os.replace): a
    supervisor killed mid-rewrite must never leave a half-written file for the
    next reader to guess at. Callers hold HOLDS_LOCK — this is a write path,
    and the read path must stay mutation-free or it would need this lock.
    """
    HOLDS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = HOLDS_FILE.with_name(f"{HOLDS_FILE.name}.tmp.{os.getpid()}")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.writelines(json.dumps(rec, ensure_ascii=False) + "\n" for rec in records)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, HOLDS_FILE)


def acquire(
    resource: str, *, goal, persona: str = "", ttl_s: int | None = None
) -> dict:
    """Claim `resource` for this goal until now+ttl_s (default DEFAULT_TTL_S).

    Refuses (HoldRefused) while a DIFFERENT goal holds the same resource live
    — one resource, one live holder, with an expiry so a claim can never be
    permanent. The same goal re-acquiring refreshes its own hold instead of
    deadlocking against itself: a supervisor re-running ensure() on a goal
    that holds the server must be a no-op, not a refusal. Expired records are
    swept here, under the lock, because the read path never mutates.
    """
    resource = str(resource or "").strip()
    if not resource:
        raise ValueError("a hold needs a resource to claim")
    ttl = DEFAULT_TTL_S if ttl_s is None else int(ttl_s)
    if ttl <= 0 or ttl > MAX_TTL_S:
        raise ValueError(f"ttl_s must be 1..{MAX_TTL_S} (default {DEFAULT_TTL_S}s)")
    holder = goal_id(goal)
    if not holder:
        raise ValueError("a hold needs the holding goal's id")
    with flock(HOLDS_LOCK):
        live = [r for r in _load() if _is_live(r)]
        for rec in live:
            if rec.get("resource") == resource and goal_id(rec.get("goal")) != holder:
                raise HoldRefused(
                    f"`{resource}` is held by {describe_hold(rec)} — one live "
                    f"hold per resource and it is theirs. Wait for it to expire, "
                    f"or have `{goal_id(rec.get('goal'))}` release it.",
                    rec,
                )
        now = time.time()
        mine = next(
            (
                r
                for r in live
                if r.get("resource") == resource and goal_id(r.get("goal")) == holder
            ),
            None,
        )
        if mine:
            mine["expires_at"] = now + ttl
            mine["ttl_s"] = ttl
            _rewrite(live)  # renew in place, one record changed
            return mine
        rec = {
            "resource": resource,
            "goal": holder,
            "persona": str(persona or "").strip(),
            "acquired_at": now,
            "expires_at": now + ttl,
            "ttl_s": ttl,
        }
        _rewrite(live + [rec])
        return rec


def release(resource: str, *, goal) -> dict | None:
    """Drop this goal's live hold on `resource`. Returns the released record.

    Only the holder is released — a bystander's release is a no-op returning
    None, because a token anyone could clear would protect nothing. Expired
    records are swept here too, under the lock (the read path never mutates);
    if there is nothing to drop, the file is left alone.
    """
    resource = str(resource or "").strip()
    holder = goal_id(goal)
    with flock(HOLDS_LOCK):
        recs = _load()
        keep: list[dict] = []
        removed: dict | None = None
        for r in recs:
            if not _is_live(r):
                continue  # expired: swept, whoever held it
            if (
                removed is None
                and r.get("resource") == resource
                and goal_id(r.get("goal")) == holder
            ):
                removed = r  # the holder's own live claim
                continue
            keep.append(r)
        if removed is not None or len(keep) != len(recs):
            _rewrite(keep)
        return removed


def current(resource: str) -> dict | None:
    """The live holder of `resource`, or None. Read-only, never rewrites."""
    resource = str(resource or "").strip()
    for rec in reversed(_load()):
        if rec.get("resource") == resource and _is_live(rec):
            return rec
    return None


def all_holds() -> list[dict]:
    """Every live hold, longest-remaining first, for display and the swap gate."""
    out = [r for r in _load() if _is_live(r)]
    out.sort(key=lambda r: _to_epoch(r.get("expires_at")), reverse=True)
    return out


def render_list(items: list[dict]) -> str:
    if not items:
        return (
            "_(no live holds — the model server is free to swap; "
            'a session takes one with `hold` action="take")_'
        )
    lines = []
    for h in items:
        lines.append(f"- **{h.get('resource')}** — {describe_hold(h)}")
    return "\n".join(lines)
