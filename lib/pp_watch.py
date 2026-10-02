"""Legacy watchers — the one thing in perpetua that runs after the agent is gone.

INCIDENT-LESSONS.md #1 is the incident's first dynamic: the power of that
collective came not from agents living longer but from *intent outliving them* —
trip-wires kept operating after their own run was dead and posted results back to
the board. This is perpetua's deliberate, bounded version of that trick.

A watcher is an agent-authored bash script that the SUPERVISOR (perpetuad) runs,
sponsored by a session that is dead by definition. It inherits the agent's
privileges — which is not new, the agent already has bash — but inherits them
*without a supervising context*, so every bound below (the ones in the incident
plan §6) is the feature, not decoration: a bounded lifetime, a bounded fire
count, a timeout killed by process group, a cap on live watchers per goal,
output truncated before it is posted, and a machine event for every
registration, fire and revocation — the audit trail is the mitigation.

Concurrency note (why there are no locks here): one supervisor owns a goal for
its whole life, so each watcher's state.json and fires/ have a single writer at
a time. The session process can register and revoke while the supervisor ticks,
but registration writes a NEW directory and revoke/expiry touch only watcher.json
fields the tick never writes (the tick re-checks revoked_at before posting), so
no two processes ever write the same file.

## #6: never escalate off a single store

Gas Town's heartbeat doc states the rule outright: cross-check tmux activity
before believing a stale timestamp, because "a live session with a stale
store is heartbeat-write divergence, not a stuck agent." Perpetua already
lived by this in `bin/perpetuad._stalled` — it reads the pane log's mtime,
the session transcript's mtime, AND (only once both files already say quiet)
asks the backend itself whether a turn is still in flight — but until now
that was a convention held in one function's head, not a rule anyone reading
a NEW verdict site could find written down.

The rule, stated once, for every liveness or health check this codebase adds
from here on: **two independent stores must agree before a verdict kills
anything; when they disagree, that is the observation channel breaking, not
the agent stopping.** A single store answering "looks dead" is never
sufficient on its own — `pp_backend.generating()` and `pp_backend.slot_progress()`
already return `None` rather than `False` for exactly this reason (house rule:
"cannot tell" is never "dead"), and no caller may treat `None` as a verdict.

Two consequences follow directly:

* `_stalled` treats "files say quiet, backend says generating" as NOT
  stalled, and traces the disagreement to `runs/NNNN/trace.jsonl` — a check
  that silently never fires must be distinguishable from one that fires and
  is ignored, the same principle the convergence-nudge trace already lives
  by.
* `_runaway_turn` is the one verdict site that is UNAVOIDABLY single-store:
  a generation in progress is exactly the case the file-based stores cannot
  see at all (that is why `/slots` exists), so there is no second store to
  cross-check "is a turn running" against. What it MUST still confirm — and
  what the function's own docstring documents explicitly — is that the
  ceiling it is comparing against belongs to the client it is polling for,
  not to a different one sharing the same server. See its comment for
  exactly how far that goes and why the residual risk is bounded rather than
  eliminated: llama.cpp's `/slots` does not expose which client opened a
  slot, so a task-id correlation is not available at all with the current
  server; ambiguity is traced rather than silently accepted.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import signal
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pp_board
import pp_machine
from pp_common import ROOT, log, now_iso, read_json, write_json

WATCHERS = "watchers"                     # goals/<id>/watchers/
CHANNEL_DEFAULT = "watchers"              # where fire output lands

#: The bounds are the design, not decoration (incident plan §6). Hard maxima are
#: REFUSED rather than silently clamped, so a session can never discover a
#: quieter, larger limit by accident.
DEFAULT_LIFETIME_S = 24 * 3600
MAX_LIFETIME_S = 7 * 24 * 3600
DEFAULT_MAX_FIRES = 5
MAX_MAX_FIRES = 50
DEFAULT_TIMEOUT_S = 60
MAX_TIMEOUT_S = 300
MAX_LIVE = 16
KEEP_FIRES = 20
POST_MAX = 4096                   # output truncated to 4 KB before posting
FIRE_FILE_MAX = 64 * 1024         # and the fires/ copy is capped too: 20 × 64 KB
HTTP_TIMEOUT_S = 10               # urllib, so the harness needs no curl
POLL_DEFAULT_S = 300              # on_check / on_http evaluation cadence

TRIGGER_TYPES = ("on_interval", "on_file", "on_check", "on_http")
FILE_MODES = ("appears", "change")

EXPIRED_BY = "expiry"
ERRORED_BY = "consecutive-errors"
MAX_ERRORS = 3


class WatcherError(RuntimeError):
    """A watcher that cannot do its job through no fault of the goal: a missing
    script, a timeout, a spawn failure, a signal-killed run. Counted into
    consecutive_errors; three of them revoke it. A plain non-zero exit is NOT
    one of these for on_check — there it is the payload."""


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(text) -> datetime | None:
    if not text:
        return None
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None


def _trigger_summary(trigger: dict) -> str:
    ttype = trigger.get("type", "?")
    if ttype == "on_interval":
        return f"every {trigger.get('interval_s')}s"
    if ttype == "on_file":
        return f"file {trigger.get('path')} {trigger.get('mode')}"
    if ttype == "on_check":
        return f"check exit changes (every {trigger.get('interval_s') or POLL_DEFAULT_S}s)"
    if ttype == "on_http":
        return f"http change at {trigger.get('url')}"
    return ttype


def _read_json_regular(path: Path, default=None, *, max_bytes: int = 1 << 20):
    """read_json, but only for a plain regular file.

    A session with bash can put a FIFO (or a hardlink toward a device) where a
    watcher's files should be, and open() on a FIFO BLOCKS. That is a HANG, not
    an exception — no try/except in the tick can catch it, and the supervisor's
    watchdog loop is what freezes. Anything that is not a regular file reads as
    `default`, which the callers treat like a corrupt contract and revoke.
    """
    try:
        if path.is_symlink() or not path.is_file():
            return default
        if path.stat().st_size > max_bytes:
            return default
    except OSError:
        return default
    return read_json(path, default)


# ── registration ──────────────────────────────────────────────────────────
def _validate_trigger(trigger: dict) -> dict:
    ttype = trigger.get("type")
    if ttype not in TRIGGER_TYPES:
        raise ValueError(f"trigger.type must be one of {', '.join(TRIGGER_TYPES)}")
    trig = dict(trigger)
    if ttype == "on_interval":
        if int(trig.get("interval_s") or 0) <= 0:
            raise ValueError("on_interval needs interval_s > 0 (seconds)")
    elif ttype == "on_file":
        path = str(trig.get("path") or "").strip()
        if not path:
            raise ValueError("on_file needs a path")
        mode = trig.get("mode") or "change"
        if mode not in FILE_MODES:
            raise ValueError(f"on_file mode must be one of {', '.join(FILE_MODES)}")
        trig["path"] = path
        trig["mode"] = mode
    elif ttype == "on_http":
        url = str(trig.get("url") or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            raise ValueError("on_http needs an http(s) url")
        trig["url"] = url
    # on_check and on_http poll on the same default cadence. on_interval has no
    # default (it must be explicit) and on_file has NO schedule at all — it is a
    # trip-wire, due on every tick, and its digest logic gates the fire.
    if ttype in ("on_check", "on_http"):
        trig.setdefault("interval_s", POLL_DEFAULT_S)
        iv = int(trig.get("interval_s") or 0)
        if iv <= 0:
            raise ValueError("interval_s must be > 0")
        trig["interval_s"] = iv
    elif ttype == "on_interval":
        trig["interval_s"] = int(trig["interval_s"])
    return trig


def register(goal: Path, *, name: str, script: str, trigger: dict,
             subject: str = "", channel: str = CHANNEL_DEFAULT,
             sponsor_run: str | int | None = None, sponsor_persona: str | None = None,
             lifetime_s: int | None = None, max_fires: int | None = None,
             timeout_s: int | None = None) -> dict:
    """Arm a watcher. Returns its contract; refuses anything outside the bounds.

    Registration is one write to a fresh directory, so it is atomic as a whole.
    A name that was already revoked is free again (the old dir is replaced);
    a LIVE watcher with that name is refused — silently overwriting someone's
    armed trip-wire is the confused-deputy move.
    """
    name = "".join(ch for ch in (name or "").strip().lower()
                   if ch.isalnum() or ch in "-_")[:48]
    if not name:
        raise ValueError("watcher name must contain alphanumerics")
    trig = _validate_trigger(trigger)
    # run.sh is what fires; on_http is the one trigger that polls instead, so it
    # is the one that may ship without a script.
    if trig["type"] != "on_http" and not str(script or "").strip():
        raise ValueError("register needs a non-empty script unless trigger is on_http")

    life = DEFAULT_LIFETIME_S if lifetime_s is None else int(lifetime_s)
    if life <= 0 or life > MAX_LIFETIME_S:
        raise ValueError(f"lifetime must be 1..{MAX_LIFETIME_S}s "
                         f"(default {DEFAULT_LIFETIME_S})")
    fires = DEFAULT_MAX_FIRES if max_fires is None else int(max_fires)
    if fires <= 0 or fires > MAX_MAX_FIRES:
        raise ValueError(f"max_fires must be 1..{MAX_MAX_FIRES} "
                         f"(default {DEFAULT_MAX_FIRES})")
    to = DEFAULT_TIMEOUT_S if timeout_s is None else int(timeout_s)
    if to <= 0 or to > MAX_TIMEOUT_S:
        raise ValueError(f"timeout_s must be 1..{MAX_TIMEOUT_S} "
                         f"(default {DEFAULT_TIMEOUT_S})")

    d = goal / WATCHERS
    d.mkdir(parents=True, exist_ok=True)
    live = [p for p in d.iterdir() if p.is_dir()
            and not (_read_json_regular(p / "watcher.json") or {}).get("revoked_at")]
    if len(live) >= MAX_LIVE:
        raise ValueError(f"at most {MAX_LIVE} live watchers per goal — "
                         f"revoke one first")

    wp = d / name
    held = _read_json_regular(wp / "watcher.json") if wp.exists() else None
    if held is not None and not held.get("revoked_at"):
        raise ValueError(f"a live watcher named {name!r} already exists — "
                         f"revoke it first")
    if wp.exists():
        shutil.rmtree(wp)                 # dead dir is free for re-arming
    wp.mkdir()

    now = _now_utc()
    created = now.isoformat(timespec="seconds")
    expires = (now + timedelta(seconds=life)).isoformat(timespec="seconds")
    contract = {
        "name": name,
        "trigger": trig,
        "sponsor_run": sponsor_run,
        "sponsor_persona": sponsor_persona,
        "created_at": created,
        "expires_at": expires,
        "max_fires": fires,
        "timeout_s": to,
        "post": {"channel": channel, "subject": str(subject or "")},
        "revoked_at": None,
        "revoked_by": None,
    }
    state = {"fires": 0, "last_fire": None, "last_digest": None,
             "next_due": None, "consecutive_errors": 0, "last_exit": None}
    if trig["type"] in ("on_interval", "on_check", "on_http"):
        state["next_due"] = ((now + timedelta(seconds=trig["interval_s"]))
                             .isoformat(timespec="seconds"))
    write_json(wp / "watcher.json", contract)
    (wp / "run.sh").write_text(script if script else "",
                               encoding="utf-8", errors="replace")
    write_json(wp / "state.json", state)

    pp_machine.event(
        goal, "watcher-registered",
        f"Watcher `{name}` armed by run {sponsor_run or '?'} "
        f"({sponsor_persona or '?'}): {_trigger_summary(trig)}. "
        f"Expires {expires}; max {fires} fires; posts to `{channel}`.",
        run=sponsor_run, name=name, sponsor_run=sponsor_run,
        trigger=trig, expires_at=expires, max_fires=fires, channel=channel)
    return contract


# ── reading ───────────────────────────────────────────────────────────────
def list(goal: Path) -> list[dict]:
    """Every watcher with its live state, live first, newest first.

    The human surface (`perpetua watchers <goal>`) answers "what is still armed
    on a goal I thought was finished?", so revoked watchers are shown too — but
    last, and tagged with who stopped them and why.
    """
    d = goal / WATCHERS
    out: list[dict] = []
    if not d.exists():
        return out
    for wp in sorted(p for p in d.iterdir() if p.is_dir()):
        w = _read_json_regular(wp / "watcher.json") or {}
        if not w:
            continue
        w["state"] = _read_json_regular(wp / "state.json") or {}
        w["live"] = not w.get("revoked_at")
        out.append(w)
    out.sort(key=lambda x: (x["live"], x.get("created_at") or ""), reverse=True)
    return out


def render_list(items: list[dict]) -> str:
    if not items:
        return ("_(no watchers — arm one with `perpetua_watch` action=\"register\"; "
                "it keeps running after your session ends)_")
    lines = []
    for w in items:
        st = w.get("state") or {}
        if w["live"]:
            err = (f" ⚠ {st.get('consecutive_errors', 0)} error(s)"
                   if int(st.get("consecutive_errors") or 0) else "")
            when = (f"next at {st.get('next_due')}" if st.get("next_due")
                    else "watches for changes")
            lines.append(
                f"- ● **{w['name']}** {_trigger_summary(w.get('trigger') or {})} "
                f"[{st.get('fires', 0)}/{w.get('max_fires')} fires]{err} — "
                f"{when} (sponsor run {w.get('sponsor_run') or '?'}, "
                f"{w.get('sponsor_persona') or '?'})")
        else:
            lines.append(f"- ✗ **{w['name']}** revoked by {w.get('revoked_by')} — "
                         f"{w.get('revoke_reason')} ({w.get('revoked_at')})")
    return "\n".join(lines)


# ── revocation ────────────────────────────────────────────────────────────
def _revoke(goal: Path, name: str, *, by: str, reason: str) -> dict | None:
    """Write the revocation into the contract and tell the machine channel.

    Returns None when there was nothing live to revoke. Every revocation posts
    to machine: a watcher is the one agent-authored thing that outlives the
    agent, so the board is the only place the story of who stopped it survives.
    """
    wp = goal / WATCHERS / name
    w = _read_json_regular(wp / "watcher.json")
    if w is None:
        # The contract is unreadable — torn, or a fifo/device left by a
        # session. A corrupt watcher must STILL be revocable, or it lingers
        # forever and every tick re-evaluates a path it can never clear. When
        # the name exists but cannot be read, mint a minimal contract so the
        # revocation is durable and the tick stops revisiting it. (A name that
        # does not exist at all still reads as "nothing live" to callers like
        # watch-revoke.)
        if not wp.exists():
            return None
        w = {"name": name}
    if w.get("revoked_at"):
        return None
    w["revoked_at"] = now_iso()
    w["revoked_by"] = by
    w["revoke_reason"] = reason
    write_json(wp / "watcher.json", w)
    pp_machine.event(goal, "watcher-revoked",
                     f"Watcher `{name}` revoked by {by}: {reason}",
                     name=name, by=by, reason=reason)
    return w


def revoke(goal: Path, *, name: str, by: str, reason: str = "") -> dict:
    """Revoke one watcher — the safety knob. Sessions, the human CLI and the
    goal lifecycle all end a watcher through here."""
    rec = _revoke(goal, name, by=by, reason=reason or "revoked")
    if rec is None:
        raise ValueError(f"no live watcher named {name!r}")
    return rec


def revoke_all(goal: Path, *, by: str) -> list[dict]:
    """Revoke every still-live watcher. Called when a goal is accomplished or
    paused: no trip-wire may outlive the goal it was watching."""
    out = []
    for w in list(goal):
        if not w["live"]:
            continue
        try:
            rec = _revoke(goal, w["name"], by=by, reason=by)
            if rec:
                out.append(rec)
        except Exception as exc:                      # noqa: BLE001
            log(f"watcher {w['name']}: revoke failed: {type(exc).__name__}: {exc}")
    return out


# ── the tick ──────────────────────────────────────────────────────────────
def tick(goal: Path) -> list[dict]:
    """Evaluate every live watcher exactly once. Returns one record per action.

    The ONE place watchers run. Called from the supervise loop between runs and
    from launch_pi's poll loop (rate-limited to once per 30s). It must never
    raise — a watcher that can kill the supervisor is a watcher that can kill
    the goal — so every evaluation is wrapped in its own try/except, and a
    contract it cannot even read is revoked on the spot.
    """
    events: list[dict] = []
    d = goal / WATCHERS
    if not d.exists():
        return events
    try:
        subs = sorted(p for p in d.iterdir() if p.is_dir())
    except OSError as exc:
        # A session removing the whole watchers/ dir mid-tick must not take the
        # supervisor's loop with it — the module's one promise is "never raises".
        log(f"watchers: cannot scan {d}: {type(exc).__name__}: {exc}")
        return events
    for wp in subs:
        try:
            ev = _eval_one(goal, wp)
        except Exception as exc:                      # noqa: BLE001
            log(f"watcher {wp.name}: evaluation failed: "
                f"{type(exc).__name__}: {exc}")
            try:
                rec = _revoke(goal, wp.name, by="corrupt",
                              reason=f"contract unreadable: {type(exc).__name__}")
            except Exception:                         # noqa: BLE001
                rec = None
            if rec:
                events.append({"name": wp.name, "action": "revoked",
                               "by": "corrupt", "record": rec})
            continue
        if ev:
            events.append(ev)
    return events


def _eval_one(goal: Path, wp: Path) -> dict | None:
    w = _read_json_regular(wp / "watcher.json")
    if w is None:
        raise ValueError("watcher.json is missing or non-regular")
    name = w.get("name") or wp.name
    st = _read_json_regular(wp / "state.json") or {}
    if w.get("revoked_at"):
        return None
    now = time.time()
    expires = _parse_iso(w.get("expires_at"))
    if expires is not None and now >= expires.timestamp():
        rec = _revoke(goal, name, by=EXPIRED_BY, reason="reached expires_at")
        return {"name": name, "action": "revoked", "by": EXPIRED_BY, "record": rec}
    if int(st.get("fires", 0)) >= int(w.get("max_fires") or DEFAULT_MAX_FIRES):
        return None                                # at the cap: silent, final
    trig = w.get("trigger") or {}
    ttype = trig.get("type")

    if ttype == "on_file":
        return _file_eval(goal, w, wp, st)
    if ttype == "on_interval":
        if not _due(st, now):
            return None
        return _run_script_fire(goal, w, wp, st)
    if ttype in ("on_check", "on_http"):
        if not _due(st, now):
            return None
        return _check_eval(goal, w, wp, st) if ttype == "on_check" \
            else _http_eval(goal, w, wp, st)
    return None


def _due(st: dict, now: float) -> bool:
    """on_file has no schedule — it is due whenever the tick runs and its digest
    logic gates the fire. Everything else fires when its next_due is reached."""
    next_due = _parse_iso(st.get("next_due"))
    return next_due is None or now >= next_due.timestamp()


def _next_due(w: dict) -> str | None:
    """The next scheduled evaluation. Only the interval-like triggers have a
    schedule; on_file is a trip-wire that is due on every tick no matter what,
    so a stray interval_s on one must not turn it into a slow poller."""
    trig = w.get("trigger") or {}
    if trig.get("type") not in ("on_interval", "on_check", "on_http"):
        return None
    iv = int(trig.get("interval_s") or 0)
    if iv <= 0:
        return None
    return (_now_utc() + timedelta(seconds=iv)).isoformat(timespec="seconds")


def _file_digest(goal: Path, trigger: dict) -> str:
    """'' when the watched file is absent. For mode 'appears' the digest is the
    file's presence; for 'change' it is (mtime, size) — enough to notice a real
    edit without hashing whole bodies at every tick."""
    rel = trigger.get("path") or ""
    try:
        target = (goal / rel).resolve()
        if not target.is_relative_to(goal.resolve()):
            raise ValueError(f"path {rel!r} escapes the goal dir")
    except (ValueError, OSError):
        raise ValueError(f"on_file path {rel!r} escapes the goal dir") from None
    if not target.exists():
        return ""
    if trigger.get("mode") == "appears":
        return "1"
    stt = target.stat()
    return f"{stt.st_mtime_ns}:{stt.st_size}"


def _file_eval(goal: Path, w: dict, wp: Path, st: dict) -> dict | None:
    """Fire when the watched file appears or its digest changes.

    The first sighting of an existing file fires too — a file that was already
    there when the watcher armed is exactly what the sponsor wants reported.
    A disappearance resets the baseline silently: absences are handled by
    'appears', not by spamming the board with "gone".
    """
    digest = _file_digest(goal, w.get("trigger") or {})
    if not digest:
        st["last_digest"] = ""
        _save_state(wp, st)
        return None
    if digest == st.get("last_digest"):
        return None
    st["last_digest"] = digest
    return _run_script_fire(goal, w, wp, st)


def _run_script_fire(goal: Path, w: dict, wp: Path, st: dict) -> dict | None:
    """Run run.sh and post its output: the fire itself, for run-type triggers."""
    name = w["name"]
    try:
        code, out = _run_script(goal, name, wp,
                                int(w.get("timeout_s") or DEFAULT_TIMEOUT_S))
    except WatcherError as exc:
        return _record_error(goal, w, wp, st, str(exc))
    if code != 0:
        return _record_error(goal, w, wp, st, f"run.sh exited {code}")
    return _fire(goal, w, wp, st, out=out, exit_code=code,
                 label=f"exit {code}")


def _check_eval(goal: Path, w: dict, wp: Path, st: dict) -> dict | None:
    """on_check: run.sh IS the check.

    Every scheduled evaluation advances next_due, but the board only hears about
    a TRANSITION in the exit code — a check that stays red must not spam, and a
    check that flips green days later is exactly the news a trip-wire exists to
    deliver. Unlike run-type triggers, a non-zero exit is the payload here, not
    a broken watcher; only a crash (signal) or timeout counts as an error.
    """
    name = w["name"]
    try:
        code, out = _run_script(goal, name, wp,
                                int(w.get("timeout_s") or DEFAULT_TIMEOUT_S))
    except WatcherError as exc:
        return _record_error(goal, w, wp, st, str(exc))
    old = st.get("last_exit")
    st["last_exit"] = code
    changed = (old is None) or (int(old) != int(code))
    if changed:
        st["last_digest"] = f"exit:{code}"   # the check's own digest
    st["next_due"] = _next_due(w)
    _save_state(wp, st)
    if not changed:
        return None
    return _fire(goal, w, wp, st, out=out, exit_code=code,
                 label=f"check exit {code} (was {old or 'never ran'})")


def _http_eval(goal: Path, w: dict, wp: Path, st: dict) -> dict | None:
    """on_http: poll the URL; post when status or body hash changes.

    urllib with a hard 10s timeout — the harness's own curl, so an agent does
    not have to know one. The first sighting posts (a baseline for the board),
    then silence until the world changes. The fired output is the digest info,
    not run.sh's: on_http never runs the script.
    """
    url = (w.get("trigger") or {}).get("url") or ""
    status, body, err = _fetch(url)
    if err is not None:
        return _record_error(goal, w, wp, st, f"http {url}: {err}")
    digest = (f"{status}:{hashlib.sha256((body or '').encode('utf-8', 'replace'))
               .hexdigest()[:16]}")
    old = st.get("last_digest")
    changed = (old is None) or (old != digest)
    st["last_digest"] = digest
    st["next_due"] = _next_due(w)
    _save_state(wp, st)
    if not changed:
        return None
    snippet = (body or "").strip().replace("\n", " ")[:200]
    return _fire(goal, w, wp, st,
                 out=f"GET {url} -> {status}\n{snippet}",
                 exit_code=status,
                 label=f"http {status} (was {old or 'never observed'})")


def _record_error(goal: Path, w: dict, wp: Path, st: dict, detail: str) -> dict:
    """A watcher that cannot fire is a broken watcher, and three failures end it.

    The count lives in state.json, not in memory, so a supervisor restart
    cannot quietly resurrect something that kept failing. next_due still
    advances on an error, or the same failure would be counted again the very
    next tick.
    """
    name = w["name"]
    errs = int(st.get("consecutive_errors", 0)) + 1
    st["consecutive_errors"] = errs
    st["next_due"] = _next_due(w)
    _save_state(wp, st)
    log(f"watcher {name}: error {errs}/{MAX_ERRORS}: {detail}")
    pp_machine.event(goal, "watcher-error",
                     f"Watcher `{name}` failed ({detail}) — error {errs}/{MAX_ERRORS}; "
                     f"revoked at the third.",
                     name=name, consecutive_errors=errs, detail=detail[:200])
    if errs >= MAX_ERRORS:
        rec = _revoke(goal, name, by=ERRORED_BY,
                      reason=f"{errs} consecutive errors: {detail[:120]}")
        return {"name": name, "action": "revoked", "by": ERRORED_BY,
                "record": rec, "consecutive_errors": errs}
    return {"name": name, "action": "error", "consecutive_errors": errs,
            "detail": detail[:200]}


# ── the fire ──────────────────────────────────────────────────────────────
def _fire(goal: Path, w: dict, wp: Path, st: dict, *, out: str,
          exit_code: int, label: str) -> dict:
    """Count the fire, keep its output, post it, tell the machine channel.

    Everything past the state write is best-effort: a board post that fails
    must never raise out of a tick (invariant 7), so the fire is already
    counted and captured before the transport is tried.
    """
    name = w["name"]
    fires = int(st.get("fires", 0)) + 1
    st["fires"] = fires
    st["last_fire"] = now_iso()
    # A fire reschedules the next evaluation. Without this the watcher stays
    # due forever and fires on every tick, which defeats max_fires and spams
    # the board the moment two tick sites exist (as they do in the supervisor).
    st["next_due"] = _next_due(w)
    _save_state(wp, st)
    fdir = wp / "fires"
    fdir.mkdir(parents=True, exist_ok=True)
    cap = fdir / f"{fires:04d}.txt"
    try:
        # Same rule as the reads: a fifo planted at the next capture name would
        # block write_text. Unlink the NAME first, then write a real file.
        cap.unlink(missing_ok=True)
        cap.write_text((out or "")[:FIRE_FILE_MAX],
                       encoding="utf-8", errors="replace")
    except OSError as exc:
        log(f"watcher {name}: could not save fires/{fires:04d}.txt: "
            f"{type(exc).__name__}: {exc}")
    _prune(fdir)

    post_cfg = w.get("post") or {}
    channel = post_cfg.get("channel") or CHANNEL_DEFAULT
    subject = post_cfg.get("subject") or f"watcher {name}"
    body = (f"watcher `{name}` — {label} at {now_iso()}\n\n"
            + (out or "_(no output)_"))[:POST_MAX]
    try:
        msg = pp_board.post(goal, channel=channel, subject=subject,
                            body=body, author=f"watcher:{name}",
                            meta={"watcher": name,
                                  "sponsor_run": w.get("sponsor_run")})
    except Exception as exc:                          # noqa: BLE001
        log(f"watcher {name}: fire post failed: {type(exc).__name__}: {exc}")
        msg = None
    if msg:
        pp_machine.event(goal, "watcher-fired",
                         f"Watcher `{name}` fired ({label}) — "
                         f"posted #{msg['seq']} on `{channel}`.",
                         name=name, channel=channel, seq=msg["seq"],
                         exit_code=exit_code)
    else:
        pp_machine.event(goal, "watcher-fired",
                         f"Watcher `{name}` fired ({label}) — "
                         f"board post failed.",
                         name=name, channel=channel, exit_code=exit_code)
    return {"name": name, "action": "fired", "channel": channel,
            "seq": msg["seq"] if msg else None, "exit_code": exit_code}


def _prune(fdir: Path) -> None:
    """Keep the last KEEP_FIRES captures; a watcher that fires 200 times must
    not grow the goal dir without bound."""
    keep = sorted(int(p.stem) for p in fdir.glob("*.txt") if p.stem.isdigit())
    for n in keep[:max(0, len(keep) - KEEP_FIRES)]:
        try:
            (fdir / f"{n:04d}.txt").unlink()
        except OSError:
            pass


def _save_state(wp: Path, st: dict) -> None:
    write_json(wp / "state.json", st)


# ── the mechanics ─────────────────────────────────────────────────────────
def _run_script(goal: Path, name: str, wp: Path, timeout_s: int) -> tuple[int, str]:
    """Run run.sh exactly the way pp_check runs check.sh.

    cwd is the goal's workspace; env carries PERPETUA_GOAL_DIR and
    PERPETUA_WATCHER but deliberately NOT PERPETUA_RUN — a watcher is not a
    session and must not be able to write a handoff. The script runs in its own
    process group so a timeout (or a script that tries to kill its own group)
    can never reach the supervisor.
    """
    script = wp / "run.sh"
    try:
        regular = script.is_file() and not script.is_symlink()
    except OSError:
        regular = False
    if not regular:
        raise WatcherError("run.sh is missing or not a regular file")
    ws = goal / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    env = {**os.environ,
           "PERPETUA_GOAL_DIR": str(goal),
           "PERPETUA_WATCHER": name,
           "PERPETUA_ROOT": str(ROOT)}
    try:
        proc = subprocess.Popen(
            ["bash", str(script)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, errors="replace", cwd=str(ws), env=env,
            start_new_session=True)
        stdout, stderr = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            proc.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            proc.communicate()
        raise WatcherError(f"timed out after {timeout_s}s (process group killed)") \
            from None
    except OSError as exc:
        raise WatcherError(f"could not run run.sh: {exc}") from exc
    out = ((stdout or "") + (stderr or "")).strip()
    if proc.returncode < 0:
        raise WatcherError(f"run.sh killed by signal {-proc.returncode}")
    return proc.returncode, out


def _fetch(url: str) -> tuple[int | None, str, str | None]:
    """GET with a hard 10s timeout. Never raises; the error string is the signal.

    urllib, so the harness needs no curl dependency for the one network trigger
    — and the whole trigger is optional network, so tests never touch it.
    """
    try:
        with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT_S) as r:
            body = r.read(128 * 1024).decode("utf-8", "replace")
            return r.status, body, None
    except Exception as exc:                          # noqa: BLE001
        return None, "", f"{type(exc).__name__}: {exc}"
