#!/usr/bin/env python3
"""Run two real supervisor crash-recovery scenarios against temporary fakes."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parents[1] / "tests"
sys.path.insert(0, str(TESTS))

from test_lifecycle import Recovery  # noqa: E402


def main() -> int:
    names = (
        "test_an_abandoned_direct_run_is_reconciled_and_killed_on_restart",
        "test_an_abandoned_tmux_run_is_reconciled_and_killed_on_restart",
    )
    suite = unittest.TestSuite(Recovery(name) for name in names)
    print("Recovery demo: start a real supervisor and fake agent, kill the supervisor, "
          "then restart it to reconcile and stop the abandoned process.")
    print("Scenarios: direct child and tmux-owned session; all runtime state is temporary.\n")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    print("\nScenario summary: "
          f"{result.testsRun} crash-recovery scenario(s), "
          f"{len(result.failures)} failure(s), {len(result.errors)} error(s).")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
