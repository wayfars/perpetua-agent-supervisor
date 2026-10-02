"""The transitions that already existed: handoff, wedge, backend switch, check."""
from __future__ import annotations

import json
import os
import importlib
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import PerpetuaTestCase, write_exec   # noqa: E402


class PackageAndRuntimePaths(PerpetuaTestCase):
    def test_default_runtime_root_is_separate_from_installed_code(self):
        os.environ.pop("PERPETUA_ROOT", None)
        os.environ.pop("XDG_STATE_HOME", None)
        common = importlib.reload(sys.modules["pp_common"])
        self.assertEqual(common.ROOT, Path(os.environ["HOME"]) / ".local/state/perpetua")
        self.assertEqual(common.BIN, Path(__file__).resolve().parents[1] / "bin")
        self.assertEqual(common.TEMPLATES, Path(__file__).resolve().parents[1] / "templates")


class NotificationDefaults(PerpetuaTestCase):
    def test_notification_is_disabled_unless_a_call_site_opts_in(self):
        notify = sys.modules["pp_notify"]
        with patch.object(notify, "discord") as discord, patch.object(notify, "xmpp") as xmpp:
            self.assertEqual(notify.notify("synthetic test message"), [])
            discord.assert_not_called()
            xmpp.assert_not_called()


class SessionTools(PerpetuaTestCase):
    def env_for(self, g: Path, run: int = 1, **extra) -> dict:
        return {"PERPETUA_GOAL_DIR": str(g), "PERPETUA_RUN": str(run),
                "PERPETUA_SESSION_ID": "sess-1", "PERPETUA_BACKEND_CLASS": "test",
                **extra}

    def test_end_session_writes_the_handoff_and_the_exit_record(self):
        g = self.make_goal("ends")
        got = self.run_bin("pp-tool", "end-session",
                           json.dumps({"reason": "handoff", "done": ["x"],
                                       "next_steps": ["y"]}),
                           env=self.env_for(g))
        self.assertEqual(got.returncode, 0, got.stdout + got.stderr)
        handoff = json.loads((g / "runs/0001/handoff.json").read_text())
        self.assertEqual(handoff["source"], "agent")
        self.assertEqual(handoff["next_steps"], ["y"])
        self.assertEqual(json.loads((g / "runs/0001/exit.json").read_text())["kind"],
                         "end_session")

    def test_switch_backend_records_a_resume_of_the_same_session(self):
        g = self.make_goal("switches")
        got = self.run_bin("pp-tool", "switch-backend",
                           json.dumps({"workload_class": "test-big",
                                       "reason": "needs more room"}),
                           env=self.env_for(g))
        self.assertEqual(got.returncode, 0, got.stdout + got.stderr)
        st = self.state(g)
        self.assertEqual(st["next"]["backend"], "test-big")
        self.assertEqual(st["next"]["session_id"], "sess-1")
        self.assertIn("continue where you stopped", st["next"]["resume_reason"])
        self.assertEqual(st["stats"]["backend_swaps"], 1)
        self.assertEqual(json.loads((g / "runs/0001/exit.json").read_text())["kind"],
                         "switch_backend")

    def test_switch_backend_refuses_an_unknown_class(self):
        g = self.make_goal("badswitch")
        got = self.run_bin("pp-tool", "switch-backend",
                           json.dumps({"workload_class": "no-such-class"}),
                           env=self.env_for(g))
        self.assertEqual(got.returncode, 1)
        self.assertIn("unknown workload class", got.stdout)
        self.assertIn("goal.json", got.stdout,
                      "the error still points at GOAL.md for allow_hosted")

    def test_a_switched_backend_run_resumes_the_session_and_swaps_the_class(self):
        g = self.make_goal("resumed-run")
        self.fake_pi('''
if [ -f "$PERPETUA_GOAL_DIR/switched" ]; then
  "$PP_TOOL" end-session '{"reason":"handoff"}' >/dev/null
else
  touch "$PERPETUA_GOAL_DIR/switched"
  "$PP_TOOL" switch-backend '{"workload_class":"test-big","reason":"bigger"}' >/dev/null
fi
exit 0
''')
        got = self.supervise("resumed-run", "--max-runs", "2", env=self.no_tmux())
        self.assertEqual(got.returncode, 0, got.stdout + got.stderr)
        run2 = json.loads((g / "runs/0002/run.json").read_text())
        self.assertEqual(run2["backend"], "test-big")
        self.assertTrue(run2["resumed"], "run 2 started a fresh session instead of resuming")
        run1 = json.loads((g / "runs/0001/run.json").read_text())
        self.assertEqual(run2["session_id"], run1["session_id"])

    def test_a_wedge_heals_and_resumes_rather_than_starting_over(self):
        g = self.make_goal("wedged")
        self.fake_pi('''
if [ ! -f "$PERPETUA_GOAL_DIR/wedged-once" ]; then
  touch "$PERPETUA_GOAL_DIR/wedged-once"
  "$PP_TOOL" wedge '{"char":"/","sample":"////////"}' >/dev/null
  exit 1
fi
"$PP_TOOL" end-session '{"reason":"handoff"}' >/dev/null
exit 0
''')
        got = self.supervise("wedged", "--max-runs", "2", env=self.no_tmux())
        self.assertEqual(got.returncode, 0, got.stdout + got.stderr)
        run2 = json.loads((g / "runs/0002/run.json").read_text())
        self.assertTrue(run2["resumed"], "a healed wedge lost the session's context")
        self.assertEqual(self.state(g)["breaker"]["consecutive_wedges"], 0,
                         "the wedge counter did not reset after a good run")

    def test_a_runaway_turn_resumes_the_same_session_with_a_corrective_nudge(self):
        g = self.make_goal("runaway")
        # Stand in for the supervisor's poll loop having written runaway.json.
        self.fake_pi('''
RD="$PERPETUA_GOAL_DIR/runs/$PERPETUA_RUN"
if [ ! -f "$PERPETUA_GOAL_DIR/ran-away" ]; then
  touch "$PERPETUA_GOAL_DIR/ran-away"
  printf '%s' '{"n_decoded":21700,"task_id":9,"n_predict":24000,"limit":21600}' > "$RD/runaway.json"
  exit 1
fi
"$PP_TOOL" end-session '{"reason":"handoff"}' >/dev/null
exit 0
''')
        got = self.supervise("runaway", "--max-runs", "2", env=self.no_tmux())
        self.assertEqual(got.returncode, 0, got.stdout + got.stderr)
        run1 = json.loads((g / "runs/0001/run.json").read_text())
        run2 = json.loads((g / "runs/0002/run.json").read_text())
        self.assertTrue(run2["resumed"], "the runaway lost the session's context")
        self.assertEqual(run2["session_id"], run1["session_id"])
        self.assertEqual(self.state(g)["breaker"]["consecutive_runaway"], 0,
                         "the runaway counter did not reset after a good run")

    def test_three_runaways_in_a_row_pause_the_goal(self):
        g = self.make_goal("runaway-loop")
        self.fake_pi('''
RD="$PERPETUA_GOAL_DIR/runs/$PERPETUA_RUN"
printf '%s' '{"n_decoded":21700,"task_id":9,"n_predict":24000,"limit":21600}' > "$RD/runaway.json"
exit 1
''')
        got = self.supervise("runaway-loop", "--max-runs", "5", env=self.no_tmux())
        self.assertEqual(got.returncode, 75, got.stdout + got.stderr)
        self.assertEqual(self.state(g)["breaker"]["paused_reason"], "runaway-loop")

    def test_goal_state_ticks_criteria(self):
        g = self.make_goal("ticks", criteria=["one", "two"])
        got = self.run_bin("pp-tool", "goal-state",
                           json.dumps({"mark_done": ["c1"], "phase": "building"}),
                           env=self.env_for(g))
        self.assertEqual(got.returncode, 0, got.stdout + got.stderr)
        st = self.state(g)
        self.assertTrue(st["criteria"][0]["done"])
        self.assertFalse(st["criteria"][1]["done"])
        self.assertEqual(st["phase"], "building")

    def test_a_persona_assignment_is_standing(self):
        g = self.make_goal("persona")
        self.run_bin("pp-tool", "persona-write",
                     json.dumps({"name": "archivist", "description": "keeps records",
                                 "body": "You file things."}),
                     env=self.env_for(g))
        got = self.run_bin("pp-tool", "persona-assign", json.dumps({"name": "archivist"}),
                           env=self.env_for(g))
        self.assertEqual(got.returncode, 0, got.stdout + got.stderr)
        self.assertEqual(self.state(g)["persona"], "archivist")
        self.pi_writes_handoff()
        self.supervise("persona", "--once", env=self.no_tmux())
        self.assertEqual(json.loads((g / "runs/0001/run.json").read_text())["persona"],
                         "archivist")


class CheckRunner(PerpetuaTestCase):
    def test_a_missing_check_cannot_run_and_is_not_a_clean_negative(self):
        g = self.make_goal("nocheck")
        (g / "check.sh").unlink()
        res = sys.modules["pp_check"].run(g)
        self.assertFalse(res.passed)
        self.assertFalse(res.ran)
        self.assertEqual(self.run_bin("perpetua", "check", "nocheck").returncode, 2)

    def test_a_hanging_check_times_out_instead_of_hanging_the_loop(self):
        g = self.make_goal("slowcheck")
        write_exec(g / "check.sh", "sleep 30\nexit 0\n")
        res = sys.modules["pp_check"].run(g, timeout=1)
        self.assertFalse(res.passed)
        self.assertFalse(res.ran)
        self.assertIn("timed out", res.detail)

    def test_a_timed_out_check_does_not_leave_child_processes_running(self):
        g = self.make_goal("childcheck")
        child_pid = g / "check-child.pid"
        write_exec(g / "check.sh",
                   f'sleep 30 &\necho $! > "{child_pid}"\nwait\n')
        res = sys.modules["pp_check"].run(g, timeout=1)
        self.assertFalse(res.ran)
        pid = int(child_pid.read_text())
        deadline = time.time() + 3
        while time.time() < deadline and sys.modules["pp_common"].pid_alive(pid):
            time.sleep(0.05)
        self.assertFalse(sys.modules["pp_common"].pid_alive(pid),
                         f"check.sh child {pid} survived its timeout")

    def test_the_check_runs_in_the_workspace_and_sees_the_goal_dir(self):
        g = self.make_goal("envcheck")
        write_exec(g / "check.sh",
                   'test "$PWD" = "$PERPETUA_GOAL_DIR/workspace" && exit 0\n'
                   'echo "cwd=$PWD goal=$PERPETUA_GOAL_DIR"\nexit 1\n')
        self.assertTrue(sys.modules["pp_check"].run(g).passed)

    def test_the_cli_and_the_supervisor_agree(self):
        g = self.make_goal("agree", check_passes=True)
        self.assertEqual(self.run_bin("perpetua", "check", "agree").returncode, 0)
        self.assertTrue(sys.modules["pp_check"].run(g).passed)

    def test_a_result_still_unpacks_as_a_pair(self):
        # Older call sites unpack (ok, detail); the Result must stay compatible.
        g = self.make_goal("pair", check_passes=True)
        ok, detail = sys.modules["pp_check"].run(g)
        self.assertTrue(ok)
        self.assertIsInstance(detail, str)


class JournalReconciliation(PerpetuaTestCase):
    def test_a_handoff_without_a_journal_entry_is_rendered_on_the_next_start(self):
        g = self.make_goal("halfwritten")
        pp_journal = sys.modules["pp_journal"]
        pp_journal.write_handoff(g, 1, {"reason": "handoff", "done": ["a"]})
        st = self.state(g)
        st["run"] = 1
        (g / "state.json").write_text(json.dumps(st, indent=2))
        self.assertFalse((g / "journal/0001.md").exists())
        self.pi_writes_handoff()
        got = self.supervise("halfwritten", "--once", env=self.no_tmux())
        self.assertEqual(got.returncode, 0, got.stdout + got.stderr)
        self.assertTrue((g / "journal/0001.md").exists(),
                        "an orphaned handoff was never committed to the journal")
        self.assertTrue((g / "journal/0002.md").exists())


if __name__ == "__main__":
    unittest.main()


class CheckTimeoutIsOneNumberPerGoal(PerpetuaTestCase):
    """600s in the supervisor, 120s in pp-tool, 60s in the dashboard.

    Each had a good local reason, and together they meant a goal whose check.sh
    takes 130s was PASSING for the supervisor and "could not run" for the agent
    trying to decide whether it was finished. The goal owns the number now; a
    caller may cap its own wait, and a capped timeout must say so rather than
    reading as a failing check.
    """

    def setUp(self):
        super().setUp()
        import pp_check
        self.pp_check = pp_check
        self.goal = self.make_goal("ct")

    def _set(self, value):
        cfg = json.loads((self.goal / "goal.json").read_text())
        cfg["check_timeout_s"] = value
        (self.goal / "goal.json").write_text(json.dumps(cfg))

    def test_the_allowance_comes_from_goal_json(self):
        self._set(4242)
        self.assertEqual(self.pp_check.allowance(self.goal), 4242)

    def test_a_goal_without_the_key_keeps_the_default(self):
        self.assertEqual(self.pp_check.allowance(self.goal),
                         self.pp_check.DEFAULT_TIMEOUT_S)

    def test_a_corrupt_goal_json_does_not_take_the_check_down(self):
        (self.goal / "goal.json").write_text("{ not json")
        self.assertEqual(self.pp_check.allowance(self.goal),
                         self.pp_check.DEFAULT_TIMEOUT_S)

    def test_a_zero_or_negative_allowance_falls_back(self):
        for bad in (0, -1, "", None):
            self._set(bad)
            self.assertEqual(self.pp_check.allowance(self.goal),
                             self.pp_check.DEFAULT_TIMEOUT_S)

    def test_a_slow_check_passes_when_the_goal_allows_the_time(self):
        write_exec(self.goal / "check.sh", "sleep 2\nexit 0\n")
        self._set(30)
        self.assertTrue(self.pp_check.run(self.goal).passed)

    def test_a_capped_caller_says_the_supervisor_may_still_pass_it(self):
        write_exec(self.goal / "check.sh", "sleep 30\nexit 0\n")
        self._set(600)
        res = self.pp_check.run(self.goal, cap=1)
        self.assertFalse(res.passed)
        self.assertFalse(res.ran, "a cut-short check is 'could not run', not 'failed'")
        self.assertIn("600", res.detail)
        self.assertIn("may still pass", res.detail)

    def test_an_uncapped_timeout_does_not_claim_a_cap(self):
        write_exec(self.goal / "check.sh", "sleep 30\nexit 0\n")
        self._set(1)
        res = self.pp_check.run(self.goal)
        self.assertNotIn("may still pass", res.detail)
        self.assertIn("timed out after 1s", res.detail)

    def test_a_cap_above_the_allowance_changes_nothing(self):
        write_exec(self.goal / "check.sh", "sleep 30\nexit 0\n")
        self._set(1)
        res = self.pp_check.run(self.goal, cap=600)
        self.assertNotIn("may still pass", res.detail)
