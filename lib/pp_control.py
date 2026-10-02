"""Shared operator commands and durable goal-level budget checks."""
from __future__ import annotations

from pathlib import Path
import math
import subprocess

import pp_state
from pp_common import BIN, ROOT, now_iso, read_json, supervisor_pid


def start_background(goal: Path, once: bool = False) -> int:
    """Launch detached; supervisor ownership remains its kernel lock's job."""
    cmd = [str(BIN / "perpetuad"), goal.name] + (["--once"] if once else [])
    with open(goal / "supervisor.log", "ab") as fh:
        proc = subprocess.Popen(cmd, stdout=fh, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, start_new_session=True)
    return proc.pid


def validate_policy(cfg: dict) -> None:
    """Validate new nested limits before an amendment can enter the ledger."""
    allowed = {"budget": {"total_runs", "work_seconds"},
               "routing": {"mode", "task_type", "blocked_on", "max_quality_switches", "cooldown_runs"}}
    for section in ("budget", "routing", "backend_timeouts"):
        value = cfg.get(section)
        if value is None:
            continue
        if not isinstance(value, dict):
            raise ValueError(f"{section} must be an object")
        if section in allowed and value.keys() - allowed[section]:
            raise ValueError(f"unknown {section} fields: {sorted(value.keys() - allowed[section])}")
        for key, item in value.items():
            if section == "routing" and key in {"mode", "task_type", "blocked_on"}:
                if key == "blocked_on" and item is None:
                    continue
                if not isinstance(item, str) or not item.strip():
                    raise ValueError(f"routing.{key} must be nonempty text")
                if key == "mode" and item not in {"off", "shadow", "auto"}:
                    raise ValueError("routing.mode must be off, shadow or auto")
                continue
            if isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(item) or item < 0:
                raise ValueError(f"{section}.{key} must be finite and nonnegative")
            if key in {"total_runs", "max_quality_switches", "cooldown_runs"} and not isinstance(item, int):
                raise ValueError(f"{section}.{key} must be an integer")
            if section == "backend_timeouts" and item < 1:
                raise ValueError("backend timeouts must be at least one second")


def request_pause(goal: Path) -> None:
    running = supervisor_pid(goal) is not None
    def apply(s):
        s["pause_requested_at"] = now_iso()
        if not running:
            s['status'] = pp_state.STATUS_PAUSED
            s.setdefault('breaker', {})['paused_reason'] = 'operator requested pause'
    pp_state.patch(goal, apply)


def budget_reason(goal: Path, cfg: dict, state: dict) -> str | None:
    try:
        validate_policy(cfg)
    except ValueError as exc:
        return f"invalid operating policy: {exc}"
    limits = cfg.get("budget") or {}
    runs = limits.get("total_runs")
    if runs is not None and int(state.get("run", 0)) >= int(runs):
        return f"budget: total run limit {runs} reached"
    seconds = limits.get("work_seconds")
    spent = float((state.get("usage") or {}).get("work_seconds", 0))
    if seconds is not None and spent >= float(seconds):
        return f"budget: work time limit {seconds}s reached ({spent:.0f}s recorded)"
    return None


def usage_start(goal: Path, epoch: float) -> None:
    def apply(s):
        s.setdefault("usage", {})["active_since"] = epoch
    pp_state.patch(goal, apply)


def usage_finish(goal: Path, epoch: float) -> None:
    def apply(s):
        usage = s.setdefault("usage", {})
        start = usage.pop("active_since", None)
        if start is not None:
            usage["work_seconds"] = float(usage.get("work_seconds", 0)) + max(0, epoch - float(start))
    pp_state.patch(goal, apply)


def inbox(goal: Path) -> list[dict]:
    import pp_amend
    import pp_escalate
    import pp_human
    out = [{"kind": "question", "id": f"Q{q['n']}",
            "subject": q.get("subject", ""), "body": q.get("body", ""),
            "action": f"perpetua questionnaire {goal.name}", "record": q}
           for q in pp_human.open_questions(goal)]
    out += [{"kind": "amendment", "id": f"A{a['n']}",
             "subject": str(a.get("target", "")), "body": str(a.get("rationale", "")),
             "action": f"perpetua amend {goal.name} --approve {a['n']}", "record": a}
            for a in pp_amend.list_records(goal) if a.get("pending")]
    alert = pp_escalate.latest_unacknowledged(goal)
    if alert:
        out.append({"kind": "alert", "id": f"E{alert['n']}",
                    "subject": str(alert.get("reason") or alert.get("level", "Alert")),
                    "body": str(alert.get("text", "")), "record": alert,
                    "action": f"perpetua ack {goal.name}"})
    state = read_json(goal / "state.json", {}) or {}
    if pp_state.is_paused(state):
        out.append({"kind": "paused", "id": "pause", "subject": "Paused",
                    "body": (state.get("breaker") or {}).get("paused_reason", ""),
                    "action": f"perpetua resume {goal.name}", "record": {}})
    return out
