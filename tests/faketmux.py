#!/usr/bin/env python3
"""A tmux stand-in that really does run things, so the tmux path is tested.

Only the six verbs perpetuad uses are implemented, and only in the forms it uses
them. Sessions and windows are directories under $PERPETUA_TEST_TMUX; a window
records the pid of the process it started. Like real tmux, a window's process is
NOT a child of the caller — it is setsid'd — which is the whole reason the
supervisor needs an explicit cleanup contract rather than relying on wait().
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

BASE = Path(os.environ.get("PERPETUA_TEST_TMUX", "/tmp/perpetua-faketmux"))


def target_parts(target: str) -> tuple[str, str | None]:
    t = target.lstrip("=")
    if ":" in t:
        sess, win = t.split(":", 1)
        return sess, win
    return t, None


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def window_pid(sess: str, win: str) -> int | None:
    f = BASE / sess / f"{win}.pid"
    try:
        pid = int(f.read_text().strip())
    except (OSError, ValueError):
        return None
    return pid if alive(pid) else None


def opt(argv: list[str], flag: str) -> str | None:
    return argv[argv.index(flag) + 1] if flag in argv else None


def spawn(cwd: str | None, env: dict, command: str) -> int:
    proc = subprocess.Popen(["bash", "-lc", command], cwd=cwd or None,
                            env=env, start_new_session=True,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return proc.pid


def main(argv: list[str]) -> int:
    if not argv:
        return 1
    verb, rest = argv[0], argv[1:]

    if verb == "has-session":
        sess, _ = target_parts(opt(rest, "-t") or "")
        return 0 if (BASE / sess).is_dir() else 1

    if verb == "new-session":
        sess = opt(rest, "-s") or "default"
        win = opt(rest, "-n") or "0"
        (BASE / sess).mkdir(parents=True, exist_ok=True)
        command = rest[-1] if rest and not rest[-1].startswith("-") else "sleep 3600"
        (BASE / sess / f"{win}.pid").write_text(str(spawn(None, os.environ.copy(), command)))
        return 0

    if verb == "new-window":
        sess, _ = target_parts(opt(rest, "-t") or "")
        win = opt(rest, "-n") or "w"
        if not (BASE / sess).is_dir():
            sys.stderr.write("can't find session\n")
            return 1
        if (BASE / sess / f"{win}.pid").exists() and window_pid(sess, win):
            sys.stderr.write("window already exists\n")   # the race, made testable
            return 1
        env = os.environ.copy()
        i = 0
        while i < len(rest):
            if rest[i] == "-e" and i + 1 < len(rest):
                key, _, value = rest[i + 1].partition("=")
                env[key] = value
                i += 2
                continue
            i += 1
        (BASE / sess / f"{win}.pid").write_text(
            str(spawn(opt(rest, "-c"), env, rest[-1])))
        return 0

    if verb == "list-panes":
        sess, win = target_parts(opt(rest, "-t") or "")
        pid = window_pid(sess, win) if win else None
        if pid is None:
            sys.stderr.write("can't find window\n")
            return 1
        if "-F" in rest:
            # Real tmux can report a window as existing before its pane has a pid
            # to report. PERPETUA_TEST_PANE_BLANKS makes the first N such queries
            # come back empty so the supervisor's launch-time identity capture is
            # tested against that race instead of assuming it away.
            blanks = int(os.environ.get("PERPETUA_TEST_PANE_BLANKS", "0"))
            counter = BASE / sess / f"{win}.panequeries"
            seen = int(counter.read_text()) if counter.exists() else 0
            counter.write_text(str(seen + 1))
            if seen < blanks:
                return 0
            print(pid)
        return 0

    if verb == "pipe-pane":
        return 0

    if verb == "kill-window":
        sess, win = target_parts(opt(rest, "-t") or "")
        pid = window_pid(sess, win) if win else None
        if pid:
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except OSError:
                pass
        if win:
            (BASE / sess / f"{win}.pid").unlink(missing_ok=True)
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
