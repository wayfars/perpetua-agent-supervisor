"""The supervisor's own voice on the board.

Everything the harness does to a goal — escalating a backend, healing a wedge,
reaping a session that wrote no handoff, nudging a stuck one, pausing — used to
be visible only in the supervisor's log, which no session ever reads. So sessions
kept re-deriving what had already happened to them: why the model changed, why
they were told they were stuck, why their predecessor's handoff was written by a
summariser. This posts those events where a session can actually see them.

Two rules:

* **Best effort.** A failed board post must never break the supervisor loop. The
  board is a side channel; the loop is the product.
* **Not progress.** These land in `board/`, which `pp_progress.KNOWLEDGE_GLOBS`
  deliberately excludes, so the supervisor cannot forge progress by talking about
  itself.
"""
from __future__ import annotations

import json
from pathlib import Path

import pp_board
from pp_common import log

CHANNEL = "machine"

#: What the briefing shows as a compact digest instead of as unread traffic.
#: At ~4 events per run, letting these through the 30-message unread budget
#: would starve real agent traffic within two sessions.
DIGEST_MAX = 12


def event(goal: Path, kind: str, body: str, *, run: int | None = None,
          **meta) -> dict | None:
    """Post one machine event. Returns the message, or None if posting failed."""
    try:
        payload = {k: v for k, v in meta.items() if v is not None}
        text = body.strip()
        if payload:
            text += "\n\n```json\n" + json.dumps(payload, indent=2, default=str) + "\n```"
        return pp_board.post(goal, channel=CHANNEL, body=text, subject=kind,
                             author="machine", run=run,
                             meta={"kind": kind, **payload})
    except Exception as exc:                                # noqa: BLE001
        log(f"machine event {kind!r} not posted: {type(exc).__name__}: {exc}")
        return None


def digest(goal: Path, since: int = 0, *, limit: int = DIGEST_MAX) -> tuple[str, int]:
    """One line per event since `since`. Returns (text, how many did not fit)."""
    msgs = pp_board.read(goal, channel=CHANNEL, since=since, limit=0)
    shown = msgs[-limit:] if limit else msgs
    if not shown:
        return "_(nothing since your last session)_", 0
    lines = []
    for m in shown:
        first = (m.get("body", "") or "").strip().splitlines()
        head = first[0][:100] if first else ""
        lines.append(f"- `#{m.get('seq')}` **{m.get('subject')}** — {head}")
    return "\n".join(lines), len(msgs) - len(shown)
