"""Supervisor lifecycle: a run starts, ends, is recovered, is stopped, is paused."""
from __future__ import annotations

import argparse
import importlib.machinery
import importlib.util
import json
import os
import signal
import subprocess
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import BIN, PerpetuaTestCase   # noqa: E402


def load_perpetuad():
    """Import bin/perpetuad as a module — it has no .py suffix, so this is the way."""
    spec = importlib.util.spec_from_loader(
        "perpetuad_under_test",
        importlib.machinery.SourceFileLoader("perpetuad_under_test", str(BIN / "perpetuad")))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_cli():
    spec = importlib.util.spec_from_loader(
        "perpetua_cli_under_test",
        importlib.machinery.SourceFileLoader("perpetua_cli_under_test", str(BIN / "perpetua")))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def wait_for(predicate, timeout: float = 60.0, interval: float = 0.2):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return None


class OneRun(PerpetuaTestCase):
    def test_a_clean_session_produces_a_handoff_a_journal_entry_and_a_run_record(self):
        g = self.make_goal("clean")
        self.pi_writes_handoff()
        got = self.supervise("clean", "--once", env=self.no_tmux())
        self.assertEqual(got.returncode, 0, got.stdout + got.stderr)

        st = self.state(g)
        self.assertEqual(st["run"], 1)
        self.assertEqual(self.pp_state.current_runs(st), [])
        self.assertEqual(st["status"], "idle")
        self.assertEqual(st["stats"]["sessions_ended_cleanly"], 1)
        self.assertEqual(st["stats"]["reaped"], 0)
        self.assertTrue((g / "runs/0001/handoff.json").exists())
        self.assertTrue((g / "runs/0001/run.json").exists())
        self.assertTrue((g / "journal/0001.md").exists())

    def test_a_session_that_writes_nothing_is_reaped(self):
        g = self.make_goal("reapme")
        self.fake_pi("exit 0")          # no handoff, and no transcript to summarise
        got = self.supervise("reapme", "--once", env=self.no_tmux())
        self.assertEqual(got.returncode, 0, got.stdout + got.stderr)
        st = self.state(g)
        self.assertEqual(st["stats"]["reaped"], 1)
        handoff = json.loads((g / "runs/0001/handoff.json").read_text())
        self.assertTrue(handoff["source"].startswith("reaper"))
        self.assertTrue((g / "journal/0001.md").exists())

    def test_once_does_not_sleep_for_a_retry_it_will_not_run(self):
        self.make_goal("once-no-wait")
        self.fake_pi("exit 0")
        got = self.supervise("once-no-wait", "--once", env=self.no_tmux())
        self.assertEqual(got.returncode, 0, got.stdout + got.stderr)
        self.assertNotIn("backing off", got.stdout,
                         "--once waited before returning even though no retry follows")

    def test_the_tmux_path_runs_the_session_and_leaves_no_window_behind(self):
        self.use_fake_tmux()
        g = self.make_goal("tmuxed")
        self.pi_writes_handoff()
        got = self.supervise("tmuxed", "--once")
        self.assertEqual(got.returncode, 0, got.stdout + got.stderr)
        self.assertTrue((g / "runs/0001/handoff.json").exists())
        windows = list((self.tmuxdir / "perpetua-tmuxed").glob("run-*.pid"))
        self.assertEqual(windows, [], "the run window outlived the run")


class Preflight(PerpetuaTestCase):
    def test_a_goal_whose_check_already_passes_launches_no_session(self):
        g = self.make_goal("done-already", check_passes=True)
        self.fake_pi("exit 0")
        got = self.supervise("done-already", "--once", env=self.no_tmux())
        self.assertEqual(got.returncode, 0, got.stdout + got.stderr)
        self.assertEqual(self.state(g)["status"], "accomplished")
        self.assertEqual(self.state(g)["run"], 0)
        self.assertFalse((self.calls / "pi.argv").exists(),
                         "pi was launched for a goal that was already finished")

    def test_a_goal_that_becomes_done_during_a_session_stops_after_it(self):
        g = self.make_goal("finishes")
        # The session itself makes check.sh pass, the way a real one would.
        self.pi_writes_handoff(extra=f"printf 'exit 0\\n' > {g / 'check.sh'}")
        got = self.supervise("finishes", env=self.no_tmux())
        self.assertEqual(got.returncode, 0, got.stdout + got.stderr)
        self.assertEqual(self.state(g)["status"], "accomplished")
        self.assertEqual(self.state(g)["run"], 1)


class Stopping(PerpetuaTestCase):
    def _start_long_session(self, gid: str, *, tmux: bool):
        if tmux:
            self.use_fake_tmux()
        g = self.make_goal(gid, session_timeout_s=900, stall_timeout_s=900)
        marker = self.tmp / f"{gid}.alive"
        self.fake_pi(f'touch "{marker}"; sleep 600; exit 0')
        env = {**os.environ, **({} if tmux else self.no_tmux())}
        proc = subprocess.Popen([sys.executable, str(BIN / "perpetuad"), gid],
                                env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True,
                                start_new_session=True)
        self.addCleanup(proc.stdout.close)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        self.assertTrue(wait_for(marker.exists), "the fake session never started")
        rec = wait_for(lambda: (self.pp_state.current_runs(self.state(g))
                                or [{}])[0].get("pid"))
        self.assertIsNotNone(rec, "the supervisor never recorded its run's identity")
        return g, proc, rec

    def _assert_session_dead(self, pid: int):
        gone = wait_for(lambda: not Path(f"/proc/{pid}").exists(), timeout=60)
        self.assertTrue(gone, f"pi pid {pid} survived the supervisor")

    def test_sigterm_kills_the_direct_child_before_releasing_the_goal(self):
        g, proc, pid = self._start_long_session("stopme", tmux=False)
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=90)
        self._assert_session_dead(pid)
        # And the goal is free: another supervisor can take it immediately.
        with self.pp_common.lifetime_lock(g / "supervisor.lock"):
            pass

    def test_sigterm_kills_the_tmux_owned_session_too(self):
        # This is the failure the review found: a tmux pane is in its own cgroup,
        # so nothing systemd or the shell does reaches it. Only the supervisor's
        # own cleanup can.
        g, proc, pid = self._start_long_session("stopme-tmux", tmux=True)
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=90)
        self._assert_session_dead(pid)
        self.assertEqual(list((self.tmuxdir / "perpetua-stopme-tmux").glob("run-*.pid")), [])

    def test_a_stop_is_prompt_because_the_session_can_still_receive_sigterm(self):
        """The session must not inherit a blocked SIGTERM.

        A signal mask survives fork *and* exec, so masking stop signals around the
        launch (to close the "started but not yet recorded" gap) will silently give
        pi a blocked SIGTERM for its whole life unless the child restores it. The
        symptom is not a failure but a delay: _kill_pid falls through its 30s grace
        to SIGKILL every single time, and pi never gets to shut down cleanly.
        """
        _g, proc, pid = self._start_long_session("prompt-stop", tmux=False)
        started = time.monotonic()
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=90)
        self._assert_session_dead(pid)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 15.0,
                        f"stopping took {elapsed:.1f}s — SIGTERM was ignored and the "
                        f"session had to be SIGKILLed after the grace period")

    def test_sigint_is_handled_the_same_way(self):
        _g, proc, pid = self._start_long_session("intme", tmux=False)
        proc.send_signal(signal.SIGINT)
        proc.wait(timeout=90)
        self._assert_session_dead(pid)


class IncompleteIdentity(PerpetuaTestCase):
    """A run we cannot fully identify must still be stoppable by the supervisor
    that started it.

    Termination refuses to act without a matching (pid, start-time) identity, which
    is right for a record inherited from a dead supervisor: that window name could
    belong to anyone. It is wrong for a window THIS process minted seconds ago with
    a freshly generated name — refusing there means SIGTERM leaves a live agent
    session running forever, which is the exact failure the signal handling exists
    to prevent.
    """

    def test_a_session_is_killed_even_if_tmux_never_reported_its_pane_pid(self):
        self.use_fake_tmux()
        self.make_goal("blindspot", session_timeout_s=900, stall_timeout_s=900)
        pidfile = self.tmp / "pi.pid"
        self.fake_pi(f'echo $$ > "{pidfile}"; sleep 600; exit 0')
        proc = subprocess.Popen(
            [sys.executable, str(BIN / "perpetuad"), "blindspot"],
            env={**os.environ, "PERPETUA_TEST_PANE_BLANKS": "99"},
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            start_new_session=True)
        self.addCleanup(proc.stdout.close)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        self.assertTrue(wait_for(pidfile.exists), "the fake session never started")
        pi_pid = int(pidfile.read_text().strip())
        self.addCleanup(lambda: os.kill(pi_pid, signal.SIGKILL)
                        if Path(f"/proc/{pi_pid}").exists() else None)

        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=90)
        self.assertTrue(
            wait_for(lambda: not Path(f"/proc/{pi_pid}").exists(), timeout=60),
            "the supervisor stopped but left its own session running because it "
            "could not read the pane pid")

    def test_a_stop_signal_cannot_land_between_creating_a_run_and_owning_it(self):
        """deferred_signals() must hold a stop until the record exists.

        The gap it covers is small but fatal: tmux has returned, so an agent is
        running, and nothing has written down which window it is in.
        """
        mod = load_perpetuad()
        mod._install_signal_handlers()
        self.addCleanup(signal.signal, signal.SIGTERM, signal.SIG_DFL)
        self.addCleanup(signal.signal, signal.SIGINT, signal.SIG_DFL)
        reached_end = False
        with self.assertRaises(mod.Stopping):
            with mod.deferred_signals():
                os.kill(os.getpid(), signal.SIGTERM)
                time.sleep(0.2)
                reached_end = True      # must still get here: the signal is held
        self.assertTrue(reached_end,
                        "the stop fired inside the critical section instead of after it")

    def test_a_foreign_record_without_a_verified_identity_is_never_killed(self):
        mod = load_perpetuad()
        # Someone else's window, inherited from a previous supervisor: no proof it
        # is ours, so it must be left alone even though that leaks a process.
        self.assertEqual(
            mod.terminate_run({"kind": "direct", "pid": os.getpid(),
                               "pid_start_ticks": None}),
            "already gone")
        self.assertFalse(mod.run_process_alive({"kind": "direct", "pid": os.getpid(),
                                                "pid_start_ticks": None}))


class Recovery(PerpetuaTestCase):
    def _abandon_a_run(self, gid: str, *, tmux: bool):
        """Start a supervisor, then SIGKILL it — leaving its pi alive."""
        if tmux:
            self.use_fake_tmux()
        g = self.make_goal(gid, session_timeout_s=900, stall_timeout_s=900)
        marker = self.tmp / f"{gid}.alive"
        self.fake_pi(f'touch "{marker}"; sleep 600; exit 0')
        env = {**os.environ, **({} if tmux else self.no_tmux())}
        proc = subprocess.Popen([sys.executable, str(BIN / "perpetuad"), gid],
                                env=env, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=True)
        self.assertTrue(wait_for(marker.exists))
        pid = wait_for(lambda: (self.pp_state.current_runs(self.state(g))
                               or [{}])[0].get("pid"))
        self.assertIsNotNone(pid)
        proc.kill()                      # no cleanup, no handoff, no run.json
        proc.wait(timeout=30)
        self.addCleanup(lambda: os.kill(pid, signal.SIGKILL)
                        if Path(f"/proc/{pid}").exists() else None)
        return g, pid

    def _assert_recovered(self, g: Path, pid: int):
        st = self.state(g)
        self.assertEqual(self.pp_state.current_runs(st), [],
                         "current_runs was not cleared")
        self.assertEqual(st["run"], 1, "the interrupted run number was skipped")
        self.assertTrue((g / "runs/0001/run.json").exists(),
                        "the interrupted run left no run record")
        self.assertTrue((g / "runs/0001/handoff.json").exists(),
                        "the interrupted run left no handoff")
        self.assertTrue((g / "journal/0001.md").exists())
        self.assertTrue(wait_for(lambda: not Path(f"/proc/{pid}").exists(), timeout=60),
                        "the abandoned session was left running")

    def test_an_abandoned_direct_run_is_reconciled_and_killed_on_restart(self):
        g, pid = self._abandon_a_run("crashed", tmux=False)
        self.assertTrue(Path(f"/proc/{pid}").exists(), "precondition: pi still alive")
        self.fake_pi("exit 0")
        got = self.supervise("crashed", "--max-runs", "0", env=self.no_tmux())
        self.assertEqual(got.returncode, 0, got.stdout + got.stderr)
        self._assert_recovered(g, pid)

    def test_an_abandoned_tmux_run_is_reconciled_and_killed_on_restart(self):
        g, pid = self._abandon_a_run("crashed-tmux", tmux=True)
        self.assertTrue(Path(f"/proc/{pid}").exists(), "precondition: pi still alive")
        self.fake_pi("exit 0")
        got = self.supervise("crashed-tmux", "--max-runs", "0")
        self.assertEqual(got.returncode, 0, got.stdout + got.stderr)
        self._assert_recovered(g, pid)

    def test_the_next_run_is_n_plus_one_not_n_plus_two(self):
        g, pid = self._abandon_a_run("nogap", tmux=False)
        self.pi_writes_handoff()
        got = self.supervise("nogap", "--once", env=self.no_tmux())
        self.assertEqual(got.returncode, 0, got.stdout + got.stderr)
        self.assertEqual(self.state(g)["run"], 2)
        self.assertTrue((g / "journal/0001.md").exists())
        self.assertTrue((g / "journal/0002.md").exists())

    def test_run_process_alive_requires_matching_start_time(self):
        mod = load_perpetuad()
        mine = os.getpid()
        ticks = self.pp_common.proc_start_ticks(mine)
        self.assertTrue(mod.run_process_alive({"kind": "direct", "pid": mine,
                                               "pid_start_ticks": ticks}))
        self.assertFalse(mod.run_process_alive({"kind": "direct", "pid": mine,
                                                "pid_start_ticks": (ticks or 0) + 1}),
                         "a recycled pid was treated as our own live run")
        self.assertEqual(mod.terminate_run({"kind": "direct", "pid": mine,
                                            "pid_start_ticks": (ticks or 0) + 1}),
                         "already gone")

    def test_tmux_cleanup_refuses_a_window_when_its_pane_cannot_be_verified(self):
        mod = load_perpetuad()
        calls = []
        mod.tmux_available = lambda: True
        mod.tmux_window_alive = lambda _target: True
        mod.tmux_pane_pid = lambda _target: None
        mod.tmux = lambda *args, **_kw: calls.append(args)
        result = mod.terminate_run({"kind": "tmux", "pid": 999999999,
                                    "pid_start_ticks": 1,
                                    "tmux_target": "=perpetua-x:run-0001-token"})
        self.assertEqual(result, "already gone")
        self.assertFalse(any(call and call[0] == "kill-window" for call in calls),
                         "an unverified tmux window was killed")

    def test_tmux_cleanup_refuses_a_record_without_process_identity(self):
        mod = load_perpetuad()
        calls = []
        mod.tmux_available = lambda: True
        mod.tmux_window_alive = lambda _target: True
        mod.tmux_pane_pid = lambda _target: os.getpid()
        mod.tmux = lambda *args, **_kw: calls.append(args)
        result = mod.terminate_run({"kind": "tmux",
                                    "tmux_target": "=perpetua-x:run-0001-token"})
        self.assertEqual(result, "already gone")
        self.assertFalse(any(call and call[0] == "kill-window" for call in calls))

    def test_recovery_preserves_an_agent_written_handoff_as_clean(self):
        g = self.make_goal("clean-crash")
        st = self.state(g)
        st["run"] = 1
        st["status"] = "running"
        # Legacy shape, on purpose: a pre-rename state.json carries no
        # current_runs key, and the accessor must read the old dict.
        st["current_run"] = {"n": 1, "session_id": "s1", "backend": "test",
                             "kind": "direct", "pid": 999999999,
                             "pid_start_ticks": 1}
        st.pop("current_runs", None)
        (g / "state.json").write_text(json.dumps(st, indent=2))
        sys.modules["pp_journal"].write_handoff(
            g, 1, {"run": 1, "source": "agent", "done": ["finished"]})
        got = self.supervise("clean-crash", "--max-runs", "0", env=self.no_tmux())
        self.assertEqual(got.returncode, 0, got.stdout + got.stderr)
        recovered = self.state(g)
        self.assertEqual(recovered["stats"]["sessions_ended_cleanly"], 1)
        self.assertEqual(recovered["stats"]["reaped"], 0)
        self.assertEqual(recovered["breaker"]["consecutive_failures"], 0)


class CliRunLimit(PerpetuaTestCase):
    def test_start_forwards_zero_max_runs(self):
        goal = self.make_goal("zero-via-cli")
        target = self.calls / "perpetuad-forwarded.argv"
        script = self.root / "bin/perpetuad"
        script.write_text("#!/usr/bin/env bash\nprintf '%s\\0' \"$@\" > "
                          + str(target) + "\n")
        script.chmod(0o755)
        cli = load_cli()
        cli.BIN = self.root / "bin"
        got = cli.cmd_start(argparse.Namespace(goal_id=goal.name, once=False, max_runs=0))
        self.assertEqual(got, 0)
        self.assertEqual(target.read_text().split("\0")[:-1],
                         ["zero-via-cli", "--max-runs", "0"])


if __name__ == "__main__":
    unittest.main()
