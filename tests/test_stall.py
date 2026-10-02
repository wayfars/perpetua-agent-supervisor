"""The stall watchdog distinguishes an idle session from active generation."""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import BIN, PerpetuaTestCase, write_exec   # noqa: E402


def _load(name: str):
    loader = importlib.machinery.SourceFileLoader(f"{name}_stalltest", str(BIN / name))
    spec = importlib.util.spec_from_loader(f"{name}_stalltest", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class StallBudgetFromSpec(PerpetuaTestCase):
    def setUp(self):
        super().setUp()
        self.cli = _load("perpetua")

    def test_a_spec_goal_no_longer_gets_stall_equal_to_hard(self):
        spec_file = self.tmp / "spec.json"
        spec_file.write_text(json.dumps({
            "goal_id": "sp", "title": "t", "objective": "o",
            "check_sh": "exit 1\n", "criteria": ["c"],
            "session_timeout_s": 2700,
        }))
        self.run_bin("perpetua", "new", "--from-spec", str(spec_file))
        cfg = json.loads((self.root / "goals/sp/goal.json").read_text())
        self.assertEqual(cfg["session_timeout_s"], 2700)
        self.assertLess(cfg["stall_timeout_s"], cfg["session_timeout_s"],
                        "a stall budget equal to the hard timeout can never fire")
        self.assertEqual(cfg["stall_timeout_s"], 900)

    def test_an_explicit_stall_in_the_spec_is_honoured(self):
        spec_file = self.tmp / "spec2.json"
        spec_file.write_text(json.dumps({
            "goal_id": "sp2", "title": "t", "objective": "o",
            "check_sh": "exit 1\n", "session_timeout_s": 2700,
            "stall_timeout_s": 456,
        }))
        self.run_bin("perpetua", "new", "--from-spec", str(spec_file))
        cfg = json.loads((self.root / "goals/sp2/goal.json").read_text())
        self.assertEqual(cfg["stall_timeout_s"], 456)

    def test_the_default_is_a_third_bounded_at_both_ends(self):
        self.assertEqual(self.cli.default_stall(2700), 900)
        self.assertEqual(self.cli.default_stall(900), 300, "floored at five minutes")
        self.assertEqual(self.cli.default_stall(10800), 2400, "capped at the CLI default")
        self.assertEqual(self.cli.default_stall(None), 500)
        self.assertEqual(self.cli.default_stall("nonsense"), 500)


class StalledSeesTheBackend(PerpetuaTestCase):
    """`_stalled` must not kill a run whose model is producing tokens."""

    def setUp(self):
        super().setUp()
        self.d = _load("perpetuad")
        self.log = self.tmp / "pi.log"
        self.log.write_text("x")
        # Make both file signals look long dead.
        old = time.time() - 10_000
        import os
        os.utime(self.log, (old, old))
        self.d._session_mtime = lambda sid: None
        self.spec = {"class": "fast-code", "base_url": "http://127.0.0.1:1/v1"}

    def _generating(self, verdict):
        self.d.pp_backend.generating = lambda spec, timeout=2.0: verdict

    def test_quiet_files_but_a_generating_backend_is_not_stalled(self):
        self._generating(True)
        self.assertFalse(self.d._stalled(self.log, "sid", 60, self.spec))

    def test_quiet_files_and_an_idle_backend_is_stalled(self):
        self._generating(False)
        self.assertTrue(self.d._stalled(self.log, "sid", 60, self.spec))

    def test_a_backend_that_cannot_say_falls_back_to_the_files(self):
        self._generating(None)
        self.assertTrue(self.d._stalled(self.log, "sid", 60, self.spec))

    def test_no_spec_at_all_falls_back_to_the_files(self):
        self.assertTrue(self.d._stalled(self.log, "sid", 60, None))

    def test_a_fresh_log_is_never_stalled_and_never_asks_the_backend(self):
        asked = []
        self.d.pp_backend.generating = lambda spec, timeout=2.0: asked.append(1)
        self.log.write_text("fresh")
        self.assertFalse(self.d._stalled(self.log, "sid", 600, self.spec))
        self.assertEqual(asked, [], "the backend must not be polled on every tick")

    def test_a_missing_log_is_not_a_stall(self):
        self.assertFalse(self.d._stalled(self.tmp / "gone.log", "sid", 1, self.spec))


class StallDivergenceTrace(PerpetuaTestCase):
    """#6: a generating backend overriding stale files is a divergence between
    two independent stores, and it must be traced, not silently absorbed."""

    def setUp(self):
        super().setUp()
        self.d = _load("perpetuad")
        self.log = self.tmp / "pi.log"
        self.log.write_text("x")
        old = time.time() - 10_000
        import os
        os.utime(self.log, (old, old))
        self.d._session_mtime = lambda sid: None
        self.spec = {"class": "fast-code", "base_url": "http://127.0.0.1:1/v1"}
        self.rd = self.tmp / "rundir"
        self.rd.mkdir()

    def _trace_lines(self):
        p = self.rd / "trace.jsonl"
        if not p.exists():
            return []
        return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]

    def test_divergence_is_traced_when_the_backend_overrides_stale_files(self):
        self.d.pp_backend.generating = lambda spec, timeout=2.0: True
        self.assertFalse(self.d._stalled(self.log, "sid", 60, self.spec, rd=self.rd))
        lines = self._trace_lines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["hook"], "stall-divergence")
        self.assertEqual(lines[0]["backend_says"], "generating")

    def test_agreement_traces_nothing(self):
        self.d.pp_backend.generating = lambda spec, timeout=2.0: False
        self.assertTrue(self.d._stalled(self.log, "sid", 60, self.spec, rd=self.rd))
        self.assertEqual(self._trace_lines(), [])

    def test_cannot_tell_traces_nothing_either(self):
        """Falling back to the files is not a disagreement — it is one store
        having nothing to say, which #6 treats differently from two stores
        actively contradicting each other."""
        self.d.pp_backend.generating = lambda spec, timeout=2.0: None
        self.assertTrue(self.d._stalled(self.log, "sid", 60, self.spec, rd=self.rd))
        self.assertEqual(self._trace_lines(), [])

    def test_no_run_dir_never_raises(self):
        self.d.pp_backend.generating = lambda spec, timeout=2.0: True
        self.assertFalse(self.d._stalled(self.log, "sid", 60, self.spec, rd=None))


class RunawayAmbiguityTrace(PerpetuaTestCase):
    """#6: /slots cannot say which client owns a slot, so more than one
    processing slot at once is genuinely ambiguous and must be traced."""

    def setUp(self):
        super().setUp()
        self.d = _load("perpetuad")
        self.rd = self.tmp / "rundir"
        self.rd.mkdir()

    def _progress(self, slots):
        self.d.pp_backend.slot_progress = lambda spec, timeout=2.0: slots

    def _trace_lines(self):
        p = self.rd / "trace.jsonl"
        if not p.exists():
            return []
        return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]

    def test_two_processing_slots_traces_ambiguity(self):
        self._progress([
            {"processing": True, "task_id": 1, "n_decoded": 100, "n_predict": 24000},
            {"processing": True, "task_id": 2, "n_decoded": 200, "n_predict": 24000},
        ])
        self.d._runaway_turn({"base_url": "x"}, rd=self.rd)
        lines = self._trace_lines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["hook"], "runaway-ambiguous")
        self.assertEqual(lines[0]["processing_slots"], 2)

    def test_a_single_processing_slot_traces_nothing(self):
        self._progress([{"processing": True, "task_id": 1, "n_decoded": 100,
                        "n_predict": 24000}])
        self.d._runaway_turn({"base_url": "x"}, rd=self.rd)
        self.assertEqual(self._trace_lines(), [])

    def test_ambiguity_does_not_suppress_a_real_runaway_verdict(self):
        self._progress([
            {"processing": True, "task_id": 1, "n_decoded": 21700, "n_predict": 24000},
            {"processing": True, "task_id": 2, "n_decoded": 50, "n_predict": 24000},
        ])
        hit = self.d._runaway_turn({"base_url": "x"}, rd=self.rd)
        self.assertIsNotNone(hit)
        self.assertTrue(self._trace_lines(), "ambiguity is traced alongside the verdict")


class GeneratingSignal(PerpetuaTestCase):
    """pp_backend.generating() — 'cannot tell' must never read as 'dead'."""

    def setUp(self):
        super().setUp()
        import pp_backend
        self.be = pp_backend

    def test_a_hosted_backend_cannot_say(self):
        self.assertIsNone(self.be.generating({"base_url": None}))

    def test_an_unreachable_server_cannot_say(self):
        self.assertIsNone(self.be.generating({"base_url": "http://127.0.0.1:1/v1"}))

    def test_the_v1_suffix_is_stripped_for_slots(self):
        seen = {}

        class FakeResp:
            status = 200
            def read(self): return b'[{"is_processing": true}]'
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake_urlopen(url, timeout=None):
            seen["url"] = url
            return FakeResp()
        real = self.be.urllib.request.urlopen
        self.be.urllib.request.urlopen = fake_urlopen
        try:
            self.assertTrue(self.be.generating({"base_url": "http://x:8094/v1"}))
        finally:
            self.be.urllib.request.urlopen = real
        self.assertEqual(seen["url"], "http://x:8094/slots")



class SlotProgress(PerpetuaTestCase):
    """pp_backend.slot_progress() — the in-flight token count the poll loop needs."""

    def setUp(self):
        super().setUp()
        import pp_backend
        self.be = pp_backend

    def _resp(self, body: bytes):
        class FakeResp:
            status = 200
            def read(self_inner): return body
            def __enter__(self_inner): return self_inner
            def __exit__(self_inner, *a): return False
        return FakeResp()

    def _with_slots(self, body: bytes, fn):
        real = self.be.urllib.request.urlopen
        self.be.urllib.request.urlopen = lambda url, timeout=None: self._resp(body)
        try:
            return fn()
        finally:
            self.be.urllib.request.urlopen = real

    def test_nested_next_token_decoded_is_read(self):
        body = (b'[{"id_task": 7, "is_processing": true, "params": {"n_predict": 24000},'
                b' "next_token": [{"n_decoded": 1234}]}]')
        out = self._with_slots(body, lambda: self.be.slot_progress({"base_url": "http://x:8094/v1"}))
        self.assertEqual(out, [{"processing": True, "task_id": 7,
                                "n_decoded": 1234, "n_predict": 24000}])

    def test_flat_tokens_predicted_is_a_fallback(self):
        body = b'[{"id_task": 1, "is_processing": true, "tokens_predicted": 42}]'
        out = self._with_slots(body, lambda: self.be.slot_progress({"base_url": "http://x/v1"}))
        self.assertEqual(out[0]["n_decoded"], 42)
        self.assertIsNone(out[0]["n_predict"])

    def test_a_hosted_backend_cannot_say(self):
        self.assertIsNone(self.be.slot_progress({"base_url": None}))

    def test_an_unreachable_server_cannot_say(self):
        self.assertIsNone(self.be.slot_progress({"base_url": "http://127.0.0.1:1/v1"}))


class RunawayTurn(PerpetuaTestCase):
    """`_runaway_turn` — stop a turn heading for the output cap, never a healthy one."""

    def setUp(self):
        super().setUp()
        self.d = _load("perpetuad")

    def _progress(self, slots):
        self.d.pp_backend.slot_progress = lambda spec, timeout=2.0: slots

    def test_an_idle_slot_is_not_a_runaway(self):
        self._progress([{"processing": False, "task_id": 1, "n_decoded": 0, "n_predict": 24000}])
        self.assertIsNone(self.d._runaway_turn({"base_url": "x"}))

    def test_a_turn_well_under_its_ceiling_is_not_a_runaway(self):
        self._progress([{"processing": True, "task_id": 1, "n_decoded": 5000, "n_predict": 24000}])
        self.assertIsNone(self.d._runaway_turn({"base_url": "x"}))

    def test_a_turn_past_ninety_percent_of_its_ceiling_is_a_runaway(self):
        self._progress([{"processing": True, "task_id": 1, "n_decoded": 21700, "n_predict": 24000}])
        hit = self.d._runaway_turn({"base_url": "x"})
        self.assertIsNotNone(hit)
        self.assertEqual(hit["limit"], 21600)

    def test_no_declared_ceiling_falls_back_to_the_floor(self):
        self._progress([{"processing": True, "task_id": 1, "n_decoded": 9000, "n_predict": None}])
        self.assertIsNone(self.d._runaway_turn({"base_url": "x"}), "9k is under the 14k floor")
        self._progress([{"processing": True, "task_id": 1, "n_decoded": 15000, "n_predict": None}])
        self.assertIsNotNone(self.d._runaway_turn({"base_url": "x"}))

    def test_cannot_tell_is_never_a_runaway(self):
        self._progress(None)
        self.assertIsNone(self.d._runaway_turn({"base_url": "x"}))
        self._progress([{"processing": True, "task_id": 1, "n_decoded": None, "n_predict": 24000}])
        self.assertIsNone(self.d._runaway_turn({"base_url": "x"}))

    def test_no_spec_is_never_a_runaway(self):
        self.assertIsNone(self.d._runaway_turn(None))


class ReviewFollowUps(PerpetuaTestCase):
    """The eight findings from the review of this night's own commits.

    Written from the review rather than from the code, so each one names the
    scenario it protects rather than the line it touches.
    """

    def setUp(self):
        super().setUp()
        self.d = _load("perpetuad")

    # F1 — the launch-failure spelling the W4 guard missed
    def test_tmux_launch_failed_counts_as_never_launched(self):
        self.assertTrue(self.d._never_launched("sid", "tmux-launch-failed"))

    def test_the_other_launch_failure_spellings_still_count(self):
        self.assertTrue(self.d._never_launched("sid", "launch-failed: boom"))
        self.assertTrue(self.d._never_launched("sid", "never-ran"))
        self.assertTrue(self.d._never_launched("", "exited"))

    def test_a_run_that_actually_ran_is_not_never_launched(self):
        for reason in ("exited", "timeout", "stalled", "wedged", "window-closed"):
            self.assertFalse(self.d._never_launched("sid", reason), reason)


class CheckAllowanceMessage(PerpetuaTestCase):
    """F7 — the capped message quotes what the SUPERVISOR allows."""

    def setUp(self):
        super().setUp()
        import pp_check
        self.pp_check = pp_check
        self.goal = self.make_goal("cm")
        cfg = json.loads((self.goal / "goal.json").read_text())
        cfg["check_timeout_s"] = 777
        (self.goal / "goal.json").write_text(json.dumps(cfg))
        write_exec(self.goal / "check.sh", "sleep 30\nexit 0\n")

    def test_an_explicit_timeout_does_not_become_the_quoted_allowance(self):
        res = self.pp_check.run(self.goal, timeout=5, cap=1)
        self.assertIn("777", res.detail,
                      "the message must quote the goal's allowance, not the caller's")
        self.assertNotIn("allows this goal 5s", res.detail)


class AmendableCheckTimeout(PerpetuaTestCase):
    """F8 — check_timeout_s joins the other per-goal operating limits."""

    def test_it_is_amendable_like_every_other_limit(self):
        import pp_amend
        self.assertIn("check_timeout_s", pp_amend.GOAL_JSON_LIMITS)
        self.assertIs(pp_amend.GOAL_JSON_LIMITS["check_timeout_s"], int)


class LegacyRunRecordOrdering(PerpetuaTestCase):
    """F4 — W5's fold-in must not create a phantom or displace the live run."""

    def setUp(self):
        super().setUp()
        import pp_state
        self.pp_state = pp_state

    def test_an_unparseable_legacy_record_is_not_folded_in(self):
        st = {"current_runs": [{"n": 9, "session_id": "live"}],
              "current_run": {"n": "../etc", "session_id": "corrupt"}}
        got = [r["session_id"] for r in self.pp_state.current_runs(st)]
        self.assertEqual(got, ["live"],
                         "a record that names nothing identifiable can never be "
                         "dropped again, so it must not be folded in")

    def test_a_valid_legacy_record_comes_first_not_last(self):
        st = {"current_runs": [{"n": 9, "session_id": "live"}],
              "current_run": {"n": 5, "session_id": "older"}}
        got = [r["session_id"] for r in self.pp_state.current_runs(st)]
        self.assertEqual(got, ["older", "live"],
                         "callers take in_flight[-1] as the current run")

    def test_the_w5_guarantee_still_holds(self):
        st = {"current_runs": [], "current_run": {"n": 5, "session_id": "live"}}
        self.assertEqual([r["session_id"] for r in self.pp_state.current_runs(st)],
                         ["live"])
if __name__ == "__main__":
    unittest.main()
