"""Test scaffolding: a throwaway perpetua root, and fakes for pi/tmux/systemctl.

Two rules shape everything here.

* No test may touch a user's configured runtime tree, the real tmux server, or real model
  backends. PERPETUA_ROOT is redirected per test, and every external command is a
  script on a prepended PATH that records its argv instead of doing anything.
* Nothing may depend on a model. The behaviour under test is lifecycle and
  bookkeeping, so `pi` is a shell script whose behaviour each test dictates.
"""
from __future__ import annotations

import importlib
import json
import os
import signal
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LIB = REPO / "lib"
BIN = REPO / "bin"

if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))


def reload_modules():
    """pp_common caches ROOT at import time, so a new root means a re-import."""
    for name in ("pp_common", "pp_routing", "pp_evidence", "pp_state", "pp_control", "pp_board", "pp_journal", "pp_briefing",
                 "pp_holds", "pp_backend", "pp_progress", "pp_check", "pp_interview", "pp_machine",
                 "pp_amend", "pp_integrity", "pp_watch", "pp_assign",
                 "pp_persona", "pp_probe", "pp_swarm", "pp_reaper", "pp_recall",
                 "pp_criteria", "pp_notify", "pp_escalate", "pp_refine", "pp_views", "pp_activity",
                 "pp_attribute", "pp_orient", "pp_human", "pp_tui"):
        if name in sys.modules:
            importlib.reload(sys.modules[name])
        else:
            importlib.import_module(name)
    return {n: sys.modules[n] for n in sys.modules if n.startswith("pp_")}


def write_exec(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body if body.startswith("#!") else "#!/usr/bin/env bash\n" + body)
    path.chmod(0o755)
    return path


class PerpetuaTestCase(unittest.TestCase):
    """A fresh PERPETUA_ROOT, a fake PATH, and the library re-imported against it."""

    #: subclasses that need a real tmux/systemctl on PATH set this to False
    fake_path = True

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="perpetua-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = self.tmp / "root"
        (self.root / "goals").mkdir(parents=True)
        (self.root / "config").mkdir(parents=True)
        # A backend with no unit and no base_url: pp_backend.ensure() probes it,
        # probe() short-circuits to "hosted backend, not probed", and nothing ever
        # reaches systemctl or a real model server.
        (self.root / "config/backends.json").write_text(json.dumps({
            "default_class": "test",
            "classes": {
                "test": {"provider": "fake", "model": "fake-1"},
                "test-big": {"provider": "fake", "model": "fake-2"},
            },
            "hosted_classes": {},
            "escalation": {"test": "test-big"},
        }, indent=2))
        (self.root / "templates").mkdir(parents=True)
        for name in ("GOAL.md", "check.sh", "BOUNDARIES.md"):
            shutil.copy(REPO / "templates" / name, self.root / "templates" / name)
        # The shipped personas (today: the auditor #9 installs on first use) are
        # copied whole, so a test root looks like a real one rather than like a
        # root that happens to be missing the file under test.
        shutil.copytree(REPO / "templates" / "personas",
                        self.root / "templates" / "personas", dirs_exist_ok=True)
        (self.root / "bin").mkdir(parents=True, exist_ok=True)

        self.fakebin = self.tmp / "fakebin"
        self.fakebin.mkdir()
        self.calls = self.tmp / "calls"
        self.calls.mkdir()

        self._env = dict(os.environ)
        os.environ["PERPETUA_ROOT"] = str(self.root)
        os.environ["PERPETUA_TEST_CALLS"] = str(self.calls)
        os.environ["HOME"] = str(self.tmp / "home")
        (self.tmp / "home").mkdir(exist_ok=True)
        if self.fake_path:
            os.environ["PATH"] = f"{self.fakebin}:{os.environ['PATH']}"
        self.addCleanup(self._restore_env)

        self.tmuxdir = self.tmp / "tmux"
        self.tmuxdir.mkdir()
        os.environ["PERPETUA_TEST_TMUX"] = str(self.tmuxdir)

        self.mods = reload_modules()
        self.pp_common = sys.modules["pp_common"]
        self.pp_state = sys.modules["pp_state"]

    def _restore_env(self) -> None:
        os.environ.clear()
        os.environ.update(self._env)
        reload_modules()

    # ── fakes ────────────────────────────────────────────────────────────
    def fake_cmd(self, name: str, body: str) -> Path:
        """Install a fake external command that also logs its argv."""
        return write_exec(self.fakebin / name, f"""#!/usr/bin/env bash
printf '%s\\0' "$@" >> "$PERPETUA_TEST_CALLS/{name}.argv"
{body}
""")

    def calls_to(self, name: str) -> list[list[str]]:
        path = self.calls / f"{name}.argv"
        if not path.exists():
            return []
        args = [a for a in path.read_text().split("\0") if a]
        return [args]

    def stub_systemctl(self, *, unit_file_state: str = "disabled") -> None:
        self.fake_cmd("systemctl", f"""
case "$*" in
  *"is-active"*) exit 1 ;;
  *"show"*"UnitFileState"*) echo "{unit_file_state}" ;;
esac
exit 0
""")

    # ── goals ────────────────────────────────────────────────────────────
    def make_goal(self, gid: str = "t", *, check_passes: bool = False,
                  criteria: list[str] | None = None, **cfg) -> Path:
        g = self.root / "goals" / gid
        for sub in ("journal", "runs", "personas", "board/channels", "workspace"):
            (g / sub).mkdir(parents=True, exist_ok=True)
        (g / "GOAL.md").write_text(f"# {gid}\n\nDo the thing.\n")
        write_exec(g / "check.sh",
                   "exit 0\n" if check_passes else "echo not yet\nexit 1\n")
        goal_cfg = {"goal_id": gid, "allow_hosted": False, "default_backend": None,
                    "consolidate_every": 10, "max_runs": None,
                    "session_timeout_s": 60, "stall_timeout_s": 30,
                    "no_progress_limit": 3, "notify": False}
        goal_cfg.update(cfg)
        (g / "goal.json").write_text(json.dumps(goal_cfg, indent=2))
        state = self.pp_state.default_state(gid)
        state["criteria"] = [{"id": f"c{i+1}", "text": t, "done": False}
                             for i, t in enumerate(criteria or [])]
        (g / "state.json").write_text(json.dumps(state, indent=2))
        sys.modules["pp_board"].ensure(g)
        self.git(g / "workspace", "init", "-q", ".")
        (g / "workspace/.gitkeep").touch()
        self.commit_workspace(g, "initial")
        return g

    def git(self, ws: Path, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(ws), *args], capture_output=True,
                              text=True, env={**os.environ,
                                              "GIT_AUTHOR_NAME": "t",
                                              "GIT_AUTHOR_EMAIL": "t@t",
                                              "GIT_COMMITTER_NAME": "t",
                                              "GIT_COMMITTER_EMAIL": "t@t"})

    def commit_workspace(self, goal: Path, message: str) -> None:
        ws = goal / "workspace"
        self.git(ws, "add", "-A")
        self.git(ws, "commit", "-qm", message)

    def state(self, goal: Path) -> dict:
        return json.loads((goal / "state.json").read_text())

    # ── running the real binaries ────────────────────────────────────────
    def run_bin(self, name: str, *args: str, timeout: int = 120,
                env: dict | None = None) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(BIN / name), *args],
                              capture_output=True, text=True, timeout=timeout,
                              env={**os.environ, **(env or {})})

    # ── fake pi ──────────────────────────────────────────────────────────
    def fake_pi(self, body: str) -> Path:
        """Install a `pi` that does whatever the test needs instead of thinking.

        The body runs with the same PERPETUA_* environment a real session gets,
        so it can call pp-tool exactly as the extension would.
        """
        return write_exec(self.fakebin / "pi", f"""#!/usr/bin/env bash
printf '%s\\0' "$@" >> "$PERPETUA_TEST_CALLS/pi.argv"
PP_TOOL="{BIN}/pp-tool"
{body}
""")

    def pi_writes_handoff(self, *, extra: str = "") -> Path:
        return self.fake_pi(f"""
"$PP_TOOL" end-session '{{"reason":"handoff","done":["a thing"],"next_steps":["another"]}}' >/dev/null
{extra}
exit 0
""")

    def use_fake_tmux(self) -> None:
        write_exec(self.fakebin / "tmux",
                   f"#!/usr/bin/env bash\nexec {sys.executable} "
                   f"{Path(__file__).resolve().parent / 'faketmux.py'} \"$@\"\n")
        self.addCleanup(self._cleanup_fake_tmux)

    def _cleanup_fake_tmux(self) -> None:
        """Stop every detached process the per-test fake tmux started."""
        for pidfile in self.tmuxdir.rglob("*.pid"):
            try:
                pid = int(pidfile.read_text().strip())
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except (OSError, ValueError):
                pass
            pidfile.unlink(missing_ok=True)

    def no_tmux(self) -> dict:
        """Env that forces the direct-child launch path."""
        return {"PERPETUA_NO_TMUX": "1"}

    def supervise(self, gid: str, *args: str, timeout: int = 120,
                  env: dict | None = None) -> subprocess.CompletedProcess:
        return self.run_bin("perpetuad", gid, *args, timeout=timeout, env=env)
