"""Criterion trees: parents, weights, and roll-up.

`state["criteria"]` was a flat list of `{id, text, done, met_at, met_run,
met_by}`, and a session had nowhere to say "criterion 4 decomposes into these
three things, and two are done" — so over hundreds of sessions the plan itself
rotted, re-derived from prose every time because there was nowhere durable to
put the breakdown.

This module adds the arithmetic, not a new writer. Two fields are added to a
criterion record — `parent` (another criterion's id) and `weight` (default
1) — and BOTH are absent-means-default: a goal created before this existed has
neither field on any record, and every function here treats that exactly like
`parent: null, weight: 1`. Nothing here rewrites an old record to add them, so
loading and running an old `state.json` needs no migration step.

The other half of the design is what this module refuses to do: it never
persists what it computes. A parent's rolled-up `done` and `progress` are a
VIEW, folded from the leaves fresh every time `rollup()` is called, and never
written back onto the parent's own record. That is what keeps "a leaf tick
rolls a parent up to done" from ever looking like a SECOND tick of that
parent — nothing is ever written for it, so nothing can double-count it,
whether that is `pp_progress`'s content hash, `criteria_ticked` evidence, or
the no-progress breaker.
"""
from __future__ import annotations


def _pid(crit: dict) -> str | None:
    """A criterion's parent id, or None. Absent, empty, or falsy all mean root."""
    p = crit.get("parent")
    p = str(p).strip() if p else ""
    return p or None


def _weight(crit: dict) -> float:
    """A criterion's own weight, defaulting to 1 and refusing to be <= 0.

    A weight of 0 (or negative, or unparseable) would make a subtree's total
    weight collapse toward zero and turn `rollup`'s division into a divide
    that is either wrong or, at the limit, by zero — so a bad weight coerces
    to the default rather than propagating.
    """
    try:
        w = float(crit.get("weight", 1) if crit.get("weight") is not None else 1)
    except (TypeError, ValueError):
        return 1.0
    return w if w > 0 else 1.0


def by_id(criteria: list[dict]) -> dict[str, dict]:
    return {c["id"]: c for c in criteria if c.get("id")}


def children_of(criteria: list[dict], cid: str) -> list[dict]:
    return [c for c in criteria if _pid(c) == cid]


def parent_ids(criteria: list[dict]) -> set[str]:
    """Every id that is SOMEONE's parent — i.e. every non-leaf id."""
    idx = by_id(criteria)
    return {p for p in (_pid(c) for c in criteria) if p and p in idx}


def roots(criteria: list[dict]) -> list[dict]:
    """Top-level criteria: no parent, or a parent that does not exist.

    An orphan — `parent` names an id that was removed — reads as a root rather
    than vanishing from every tree view: a criterion a human can still see and
    fix is better than one silently dropped because its parent is gone.
    """
    idx = by_id(criteria)
    return [c for c in criteria if _pid(c) not in idx]


def would_cycle(criteria: list[dict], cid: str, new_parent: str | None) -> bool:
    """Would giving `cid` the parent `new_parent` make `cid` its own ancestor?

    Checked BEFORE a write, never after: refuse the write, don't discover the
    cycle later while trying to roll one up. A criterion cannot be its own
    parent, and cannot be the parent of anything on its own path to the root.
    """
    if not new_parent or new_parent == cid:
        return bool(new_parent == cid)
    idx = by_id(criteria)
    cur = idx.get(new_parent)
    seen: set[str] = set()
    while cur is not None:
        if cur.get("id") == cid:
            return True
        pid = _pid(cur)
        if not pid or pid in seen:
            return False           # ran off the tree, or hit an existing cycle elsewhere
        seen.add(pid)
        cur = idx.get(pid)
    return False


def rollup(criteria: list[dict]) -> dict[str, dict]:
    """id -> {done, progress, weight}, leaves up.

    A leaf's `done`/`progress` are its own stored `done` flag; a parent's are
    folded from its children: done when EVERY child is done, and `progress`
    the weight-average of the children's own progress. `weight` on a parent's
    result is the total weight of its subtree — what a grandparent divides by
    — not the parent's own (irrelevant) `weight` field, which only ever
    applies to a leaf.
    """
    idx = by_id(criteria)
    kids: dict[str, list[str]] = {}
    for c in criteria:
        pid = _pid(c)
        if pid and pid in idx:
            kids.setdefault(pid, []).append(c["id"])

    memo: dict[str, dict] = {}

    def node(cid: str, path: frozenset) -> dict:
        if cid in memo:
            return memo[cid]
        if cid in path:
            # A cycle that should have been refused by would_cycle at write
            # time (hand-edited state.json, a goal from an older build). Read
            # it as contributing nothing rather than recursing forever —
            # failure class 3 applied to a criterion record.
            return {"done": False, "progress": 0.0, "weight": 0.0}
        c = idx[cid]
        children = kids.get(cid) or []
        if not children:
            done = bool(c.get("done"))
            result = {"done": done, "progress": 1.0 if done else 0.0,
                      "weight": _weight(c)}
        else:
            sub = [node(k, path | {cid}) for k in children]
            total_w = sum(s["weight"] for s in sub) or 1.0
            progress = sum(s["weight"] * s["progress"] for s in sub) / total_w
            result = {"done": all(s["done"] for s in sub),
                      "progress": progress, "weight": total_w}
        memo[cid] = result
        return result

    for c in criteria:
        node(c["id"], frozenset())
    return memo


def leaf_totals(criteria: list[dict]) -> tuple[int, int]:
    """(met, total) counting only LEAF criteria — what "N/M" has always meant.

    A flat count over the raw list double-counts the moment a criterion can
    have children: a criterion decomposed into three children would read as
    4 items instead of the 3 that actually have to be done. Every "met/total"
    summary in this codebase (`perpetua status`, the dashboard) means this.
    """
    if not criteria:
        return 0, 0
    rolled = rollup(criteria)
    parents = parent_ids(criteria)
    leaves = [c["id"] for c in criteria if c.get("id") and c["id"] not in parents]
    met = sum(1 for cid in leaves if rolled.get(cid, {}).get("done"))
    return met, len(leaves)


def render_tree(criteria: list[dict]) -> str:
    """The indented, rollup-aware view: `perpetua status`, the dashboard
    Overview tab, and the briefing's criteria checklist all render this."""
    if not criteria:
        return "_No machine-tracked criteria yet — see GOAL.md and check.sh._"
    idx = by_id(criteria)
    kids: dict[str, list[dict]] = {}
    for c in criteria:
        pid = _pid(c)
        if pid and pid in idx:
            kids.setdefault(pid, []).append(c)
    rolled = rollup(criteria)
    lines: list[str] = []

    def walk(c: dict, depth: int, path: frozenset) -> None:
        cid = c.get("id", "?")
        if cid in path:
            lines.append(f"{'  ' * depth}- [!] {cid}: (cycle in stored data — "
                         f"stopped rendering this branch)")
            return
        r = rolled.get(cid, {"done": bool(c.get("done")), "progress": 0.0})
        box = "x" if r["done"] else " "
        children = kids.get(cid) or []
        pct = f" ({r['progress'] * 100:.0f}%)" if children else ""
        lines.append(f"{'  ' * depth}- [{box}] {cid}: {c.get('text', '')}{pct}")
        for child in children:
            walk(child, depth + 1, path | {cid})

    for c in roots(criteria):
        walk(c, 0, frozenset())
    met, total = leaf_totals(criteria)
    lines.append(f"\n({met}/{total} met)")
    return "\n".join(lines)
