"""Inspectable routing policy. Infrastructure recovery stays in pp_backend.

Quality routing defaults to shadow mode. User/agent explicit choices always
win; no scalar 'confidence' can override missing authority or a resource wait.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from pp_common import flock, now_iso, read_json

HARD_TASKS = {"architecture", "migration", "concurrency", "security", "diagnosis"}
ROUTINE_TASKS = {"routine", "extraction", "documentation"}


def count(value, default=0):
    try:
        return max(0, int(value))
    except (ValueError, TypeError, OverflowError):
        return default


def decide(cfg: dict, state: dict, backends: dict) -> dict:
    policy = cfg.get("routing") or {}
    mode = policy.get("mode", "shadow")
    if mode not in {"off", "shadow", "auto"}:
        raise ValueError("routing.mode must be off, shadow or auto")
    default = cfg.get("default_backend") or backends.get("default_class")
    explicit = (state.get("next") or {}).get("backend")
    task = policy.get("task_type", "implementation")
    failures = count((state.get("routing") or {}).get("semantic_failures", 0))
    blocked = bool(policy.get("blocked_on") or (state.get("routing") or {}).get("blocked_on"))
    proposal, reason = default, "goal default"
    if not blocked:
        if task in HARD_TASKS or failures >= 2:
            proposal, reason = "deep-reason", "hard task or repeated semantic failures"
        elif task in ROUTINE_TASKS and failures == 0:
            proposal, reason = "rapid-code", "bounded execution"
        else:
            proposal, reason = "fast-code", "reasoning and implementation"
    else:
        proposal = (state.get("routing") or {}).get("last_backend", default)
        reason = "blocked: model switching cannot resolve authority or dependencies"
    classes = backends.get("classes", {})
    if proposal not in classes or classes[proposal].get("enabled") is False:
        proposal, reason = default, "candidate unavailable; goal default"
    # Only a bounded number of automatic quality escalations per goal. Explicit
    # choices and infrastructure recovery have separate accounting.
    used = count((state.get("routing") or {}).get("quality_switches", 0))
    limit = count(policy.get("max_quality_switches", 2), 2)
    previous = (state.get("routing") or {}).get("last_backend", default)
    last_switch = count((state.get("routing") or {}).get("last_quality_switch_run"))
    cooldown = count(policy.get("cooldown_runs", 2), 2)
    if mode == "auto" and not blocked and proposal != previous and last_switch and count(state.get("run")) - last_switch < cooldown:
        proposal, reason = previous, "quality switch cooldown"
    if mode == "auto" and proposal != previous and used >= limit:
        proposal, reason = previous, "quality switch budget exhausted"
    actual = explicit or (proposal if mode == "auto" else default)
    return {"schema_version": 1, "at": now_iso(), "mode": mode,
            "task_type": task, "proposed": proposal, "selected": actual,
            "reason": "explicit next-session choice" if explicit else reason,
            "semantic_failures": failures, "explicit": bool(explicit)}


def record(goal: Path, decision: dict, spec: dict, run) -> dict:
    record = {**decision, "run": str(run), "effective": {
        k: spec.get(k) for k in ("class", "provider", "model", "thinking",
                                "reasoning_effort", "context_tokens", "slots")}}
    with flock(goal / ".routing.lock"):
        with open(goal / "routing.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    return record


def effective(spec: dict, models_path: Path | None = None) -> dict:
    """Use the smaller client/server window; never rewrite global Pi settings."""
    out = dict(spec)
    path = models_path or (Path(os.environ["PERPETUA_MODELS_FILE"])
                           if os.environ.get("PERPETUA_MODELS_FILE") else None)
    if path is None:
        return out
    registry = read_json(path, {}) or {}
    provider = registry.get("providers", {}).get(spec.get("provider"), {})
    for model in provider.get("models", []):
        if model.get("id") == spec.get("model"):
            window = model.get("contextWindow")
            if isinstance(window, int) and window > 0:
                out["context_tokens"] = min(int(out.get("context_tokens") or window), window)
    return out


def run_config(cfg: dict, spec: dict) -> dict:
    out = dict(cfg)
    # Explicit per-class limits win. Otherwise the slow reasoning class gets
    # its documented multiplier, including watchdog silence and briefing clock.
    multiplier = max(1.0, float(spec.get("time_multiplier", 1)))
    limits = cfg.get("backend_timeouts") or {}
    out["session_timeout_s"] = int(limits.get(spec["class"],
        float(cfg["session_timeout_s"]) * multiplier))
    out["stall_timeout_s"] = max(float(cfg["stall_timeout_s"]),
        min(out["session_timeout_s"], float(cfg["stall_timeout_s"]) * multiplier))
    return out


def feedback(goal: Path, *, outcome: str, evidence: str, run: str) -> None:
    """Human-reviewed task outcome, distinct from a still-failing whole goal.

A large goal can fail check.sh hundreds of times while progressing normally;
that is never automatically counted as a semantic failure.
"""
    import pp_state
    from pp_common import run_dir, run_id
    run = run_id(run)
    if outcome not in {"accepted", "semantic-failure", "blocked"} or not evidence.strip():
        raise ValueError("feedback requires an outcome and nonempty evidence")
    if not (run_dir(goal, run) / "run.json").is_file():
        raise ValueError("feedback must reference a finished run")
    def apply(s):
        rt = s.setdefault("routing", {})
        outcomes = rt.setdefault("outcomes", {})
        outcomes[str(run)] = {"outcome": outcome, "evidence": evidence, "at": now_iso()}
        # Recompute the trailing streak from final outcomes: correcting or
        # resubmitting feedback cannot spend an extra failure.
        streak = 0
        ordered = sorted(outcomes, key=lambda r: tuple(int(v) for v in str(r).split('.')))
        for key in reversed(ordered):
            if outcomes[key]["outcome"] != "semantic-failure":
                break
            streak += 1
        rt["semantic_failures"] = streak
        rt["blocked_on"] = outcomes[ordered[-1]]["evidence"] if outcomes[ordered[-1]]["outcome"] == "blocked" else None
    pp_state.patch(goal, apply)
