"""#2 criterion trees — ids, parents, weights, and roll-up.

`state["criteria"]` stays a flat list; `parent` and `weight` are the only new
fields, both absent-means-default, so an old `state.json` loads and behaves
exactly as before. `lib/pp_criteria.py` is the arithmetic; `pp_amend` is the
only writer, and decomposition is a SOFT amendment (only ever adds nodes)
while re-parenting/removing is HARD (waits for a human).
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))


class RollupArithmetic(unittest.TestCase):
    def setUp(self):
        super().setUp()
        import pp_criteria
        self.pc = pp_criteria

    def test_a_flat_legacy_list_has_no_parents_and_rolls_up_to_itself(self):
        crit = [{"id": "c1", "text": "a", "done": True},
                {"id": "c2", "text": "b", "done": False}]
        rolled = self.pc.rollup(crit)
        self.assertTrue(rolled["c1"]["done"])
        self.assertFalse(rolled["c2"]["done"])
        self.assertEqual(self.pc.leaf_totals(crit), (1, 2))

    def test_a_parent_is_done_only_when_every_child_is(self):
        crit = [
            {"id": "c1", "text": "parent"},
            {"id": "c2", "text": "child a", "parent": "c1", "done": True},
            {"id": "c3", "text": "child b", "parent": "c1", "done": False},
        ]
        rolled = self.pc.rollup(crit)
        self.assertFalse(rolled["c1"]["done"])
        crit[2]["done"] = True
        rolled = self.pc.rollup(crit)
        self.assertTrue(rolled["c1"]["done"])

    def test_weights_bias_the_progress_fraction(self):
        crit = [
            {"id": "c1", "text": "parent"},
            {"id": "c2", "text": "heavy", "parent": "c1", "weight": 3, "done": True},
            {"id": "c3", "text": "light", "parent": "c1", "weight": 1, "done": False},
        ]
        rolled = self.pc.rollup(crit)
        self.assertAlmostEqual(rolled["c1"]["progress"], 0.75)

    def test_default_weight_is_one_and_a_bad_weight_does_not_break_it(self):
        crit = [
            {"id": "c1", "text": "parent"},
            {"id": "c2", "text": "a", "parent": "c1", "done": True},
            {"id": "c3", "text": "b", "parent": "c1", "weight": "nonsense", "done": False},
        ]
        rolled = self.pc.rollup(crit)
        self.assertAlmostEqual(rolled["c1"]["progress"], 0.5)

    def test_a_grandparent_rolls_up_through_two_levels(self):
        crit = [
            {"id": "c1", "text": "root"},
            {"id": "c2", "text": "mid", "parent": "c1"},
            {"id": "c3", "text": "leaf a", "parent": "c2", "done": True},
            {"id": "c4", "text": "leaf b", "parent": "c2", "done": True},
        ]
        rolled = self.pc.rollup(crit)
        self.assertTrue(rolled["c2"]["done"])
        self.assertTrue(rolled["c1"]["done"])

    def test_leaf_totals_only_counts_leaves_not_parents(self):
        crit = [
            {"id": "c1", "text": "parent"},
            {"id": "c2", "text": "a", "parent": "c1", "done": True},
            {"id": "c3", "text": "b", "parent": "c1", "done": False},
        ]
        met, total = self.pc.leaf_totals(crit)
        self.assertEqual((met, total), (1, 2), "the parent must not inflate the count")

    def test_an_orphaned_parent_reference_is_treated_as_a_root(self):
        crit = [{"id": "c1", "text": "orphan", "parent": "nonexistent"}]
        roots = self.pc.roots(crit)
        self.assertEqual([c["id"] for c in roots], ["c1"])

    def test_render_tree_is_indented_and_shows_percentages(self):
        crit = [
            {"id": "c1", "text": "top"},
            {"id": "c2", "text": "child", "parent": "c1", "done": True},
        ]
        text = self.pc.render_tree(crit)
        self.assertIn("- [x] c1: top (100%)", text, "a parent rolls up done too")
        self.assertIn("  - [x] c2: child", text)
        self.assertIn("(1/1 met)", text)

    def test_empty_criteria_render_the_old_placeholder(self):
        self.assertIn("No machine-tracked criteria", self.pc.render_tree([]))


class CycleRefusal(unittest.TestCase):
    def setUp(self):
        super().setUp()
        import pp_criteria
        self.pc = pp_criteria
        self.crit = [
            {"id": "c1", "text": "a"},
            {"id": "c2", "text": "b", "parent": "c1"},
            {"id": "c3", "text": "c", "parent": "c2"},
        ]

    def test_a_criterion_cannot_be_its_own_parent(self):
        self.assertTrue(self.pc.would_cycle(self.crit, "c1", "c1"))

    def test_a_criterion_cannot_be_parented_under_its_own_descendant(self):
        self.assertTrue(self.pc.would_cycle(self.crit, "c1", "c3"))

    def test_a_criterion_may_be_reparented_under_an_unrelated_node(self):
        self.crit.append({"id": "c4", "text": "unrelated"})
        self.assertFalse(self.pc.would_cycle(self.crit, "c1", "c4"))

    def test_clearing_a_parent_is_never_a_cycle(self):
        self.assertFalse(self.pc.would_cycle(self.crit, "c2", None))

    def test_rollup_does_not_hang_on_a_cycle_that_slipped_through(self):
        # A cycle that pre-dates the refusal (a hand-edited state.json, an
        # old build): rollup must terminate, not spin forever.
        crit = [{"id": "c1", "text": "a", "parent": "c2"},
                {"id": "c2", "text": "b", "parent": "c1"}]
        rolled = self.pc.rollup(crit)
        self.assertIn("c1", rolled)
        self.assertIn("c2", rolled)



if __name__ == "__main__":
    unittest.main()
