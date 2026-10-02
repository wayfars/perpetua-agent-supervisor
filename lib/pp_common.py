"""Shared primitives for perpetua: paths, atomic JSON, file locking, logging."""
from __future__ import annotations

import fcntl
import json
import os
import re
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_STATE_HOME = Path(os.environ.get(
    "XDG_STATE_HOME", Path.home() / ".local" / "state"))
ROOT = Path(os.environ.get("PERPETUA_ROOT", DEFAULT_STATE_HOME / "perpetua"))
GOALS = ROOT / "goals"
CONFIG = ROOT / "config"
BIN = APP_ROOT / "bin"
TEMPLATES = APP_ROOT / "templates"

PI_SESSIONS = Path(os.environ["PERPETUA_PI_SESSIONS"]) if os.environ.get(
    "PERPETUA_PI_SESSIONS") else ROOT / "pi-sessions"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class UnsafeGoalId(ValueError):
    """A goal id that cannot be trusted as a single path component under goals/."""


# `.` and `..` survive the slug character class, and `goals/.` is the goals
# directory itself while `goals/..` is the install root — a goal that resolved to
# either would let `perpetua new` and every later write scribble outside its own
# directory. They are the only slugs that are syntactically fine and semantically
# catastrophic, so they are named rather than filtered.
RESERVED_SLUGS = {".", ".."}


def slug(text: str) -> str:
    """Normalise to a single safe path component. Never returns '', '.' or '..'."""
    s = re.sub(r"[^a-zA-Z0-9._-]+", "-", (text or "").strip().lower()).strip("-")
    if not s or s in RESERVED_SLUGS:
        return "unnamed"
    return s


def safe_goal_id(text: str) -> str:
    """slug(), but *fatal* on input that could only ever have been a mistake.

    Silently renaming `..` to `unnamed` would create a goal the user did not ask
    for; every CLI path that resolves a goal id goes through here so the failure
    is loud and happens before anything is written.
    """
    raw = (text or "").strip()
    if not raw:
        raise UnsafeGoalId("goal id is empty")
    if raw in RESERVED_SLUGS:
        raise UnsafeGoalId(f"goal id {raw!r} is a directory traversal, not a goal")
    if "/" in raw or "\\" in raw or "\x00" in raw:
        raise UnsafeGoalId(f"goal id {raw!r} must be a single path component")
    s = slug(raw)
    if s in RESERVED_SLUGS or (s == "unnamed" and raw.lower() != "unnamed"):
        raise UnsafeGoalId(f"goal id {raw!r} does not reduce to a usable name")
    return s


def goal_dir(goal_id: str) -> Path:
    """Resolve a goal id to its directory, refusing anything that escapes GOALS."""
    g = (GOALS / safe_goal_id(goal_id)).resolve()
    if g.parent != GOALS.resolve():
        raise UnsafeGoalId(f"goal id {goal_id!r} escapes {GOALS}")
    return g


class UnsafeRunId(ValueError):
    """A run id that cannot be trusted as a single path component under runs/."""


def run_id(n: str | int) -> str:
    """The canonical identity of a run — `0042`, or `0042.3` for probe 3 of run 42.

    `state["run"]` allocates the integer; this is the string that travels through
    paths, ledger rows and handoff headers. Parsing is deliberately strict the
    way `safe_goal_id` is: the value lands in a path join, so a permissive parse
    would paper over `..` or a slash until one actually reached the filesystem.
    """
    if isinstance(n, int):
        if n < 0:
            raise UnsafeRunId(f"run id must not be negative: {n}")
        return f"{n:04d}"
    s = str(n).strip()
    if not s or s in RESERVED_SLUGS or "/" in s or "\\" in s or "\x00" in s:
        raise UnsafeRunId(f"run id {s!r} must be a single safe path component")
    if s.isdigit():
        return f"{int(s):04d}"
    m = re.match(r"^(\d+)\.(\d+)$", s)
    if m:
        return f"{int(m.group(1)):04d}.{m.group(2)}"
    raise UnsafeRunId(f"run id {s!r} is not NNNN or NNNN.k")


def run_dir(goal: Path, n: str | int) -> Path:
    return goal / "runs" / run_id(n)


def run_no(name: str) -> str:
    """Human display form of a run dir name: `0042` → `42`, `0042.3` → `0042.3`.

    The dashboards number runs for people, so padding is dropped — but a probe
    dir must render as itself rather than crash an `int()`, the same unguarded
    coercion that made probe ids a crash everywhere else.
    """
    try:
        return str(int(name))
    except ValueError:
        return name


def read_json(path: Path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_json(path: Path, data) -> None:
    """Atomic write — a supervisor killed mid-write must never leave torn state."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


@contextmanager
def flock(lock_path: Path, timeout: float = 30.0):
    """Exclusive advisory lock. Used by the board and by state mutation."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "a+")
    deadline = time.time() + timeout
    while True:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if time.time() > deadline:
                fh.close()
                raise TimeoutError(f"could not lock {lock_path} within {timeout}s")
            time.sleep(0.05)
    try:
        yield fh
    finally:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()


class LockBusy(RuntimeError):
    """Someone else holds the lifetime lock. Carries whatever they wrote into it."""

    def __init__(self, message: str, holder: dict | None = None):
        super().__init__(message)
        self.holder = holder or {}


# How long to keep trying for the lock before calling the goal owned. This is not
# patience for a real supervisor — one holds the lock for days, so no amount of
# waiting helps — it is tolerance for the *readers*. supervisor_pid() proves the
# lock is held by momentarily taking it, so every dashboard refresh and every
# `perpetua status` is a microsecond-long holder. Without a grace period here, a
# supervisor started from the dashboard can be refused by the dashboard that is
# about to draw it; measured at ~6% of starts against three concurrent pollers.
ACQUIRE_GRACE_S = 2.0


@contextmanager
def lifetime_lock(lock_path: Path, info: dict | None = None,
                  acquire_grace_s: float = ACQUIRE_GRACE_S):
    """Exclusive lock held for as long as the caller runs.

    This is ownership, not a critical section: `flock` is released by the kernel
    when the fd closes, so a supervisor that segfaults, is SIGKILLed or has its
    cgroup torn down cannot leave a goal permanently owned — which is exactly the
    failure mode of the check-then-write pidfile this replaces. The pid written
    into the file is informational only; nothing ever decides ownership from it,
    because pids are reused and a reused pid reads as a live owner.

    Retrying does not weaken the guarantee: each attempt is still an atomic
    LOCK_EX|LOCK_NB, so two callers can never both succeed. It only decides how
    long we insist before reporting the goal owned.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "a+")
    deadline = time.time() + max(0.0, acquire_grace_s)
    while True:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if time.time() >= deadline:
                holder = {}
                try:
                    fh.seek(0)
                    holder = json.loads(fh.read() or "{}")
                except (OSError, json.JSONDecodeError):
                    pass
                fh.close()
                raise LockBusy(f"{lock_path} is held by another process",
                               holder) from None
            time.sleep(0.02)
    payload = {**(info or {}), "pid": os.getpid(),
               "pid_start_ticks": proc_start_ticks(os.getpid()),
               "since": now_iso()}
    try:
        fh.seek(0)
        fh.truncate()
        fh.write(json.dumps(payload) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    except OSError:
        pass
    try:
        yield fh
    finally:
        try:
            fh.seek(0)
            fh.truncate()
            fh.flush()
        except OSError:
            pass
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()


def proc_start_ticks(pid: int) -> int | None:
    """Field 22 of /proc/<pid>/stat: the boot-relative start time.

    A pid alone is not an identity. Between recording a pi process and coming back
    to kill it the supervisor may have been down for hours, and the kernel will
    happily have handed that number to something else. (pid, start_ticks) is
    stable for the life of the process and is never reused together.
    """
    try:
        data = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    try:
        # comm can contain spaces and parentheses; everything after the last ')'
        # is the fixed-width field list, of which starttime is the 20th.
        fields = data[data.rindex(")") + 1:].split()
        return int(fields[19])
    except (ValueError, IndexError):
        return None


def pid_alive(pid: int | None, start_ticks: int | None = None) -> bool:
    """True only if this is still the *same* process we recorded."""
    if not pid or pid <= 0:
        return False
    current = proc_start_ticks(pid)
    if current is None:
        return False
    if start_ticks is not None and current != start_ticks:
        return False           # pid reused by something that is not ours
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        state = stat[stat.rindex(")") + 1:].split()[0]
    except (OSError, ValueError, IndexError):
        return False
    if state == "Z":
        return False           # exited and awaiting reaping is not a live worker
    return True


def supervisor_pid(goal: Path) -> int | None:
    """The pid of the live supervisor for this goal, or None.

    Ownership is the flock on supervisor.lock; this only reports its verified
    holder for display and targeted stop. Stale metadata and the legacy pidfile
    are deliberately insufficient because either can name a recycled pid.
    """
    lock_path = goal / "supervisor.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fh = open(lock_path, "a+")
    except OSError:
        return None
    try:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            # The lock, not the file's contents, proves that a supervisor exists.
            # Metadata can survive SIGKILL, so require the recorded start time too
            # before exposing a pid that the dashboard is allowed to signal.
            try:
                fh.seek(0)
                holder = json.loads(fh.read() or "{}")
                pid = int(holder.get("pid") or 0)
                ticks = int(holder.get("pid_start_ticks") or 0)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                return None
            return pid if ticks and pid_alive(pid, ticks) else None
        else:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            return None
    finally:
        fh.close()


def log(msg: str, *, stream=sys.stdout) -> None:
    stream.write(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}\n")
    stream.flush()


def find_session_file(session_id: str) -> Path | None:
    """pi names sessions <iso>_<uuid>.jsonl inside a cwd-slugged directory.

    Globbing on the id is deliberate: reimplementing pi's cwd->slug mapping is a
    silent-breakage risk, and the
    id is unique on its own.
    """
    if not session_id:
        return None
    hits = sorted(PI_SESSIONS.glob(f"*/*_{session_id}.jsonl"))
    return hits[-1] if hits else None


def tail_text(path: Path, limit: int = 4000) -> str:
    try:
        data = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return data[-limit:]
