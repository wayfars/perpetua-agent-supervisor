"""Model-backend control — the ONLY path from perpetua to systemctl.

Everything here is keyed on a workload *class* from config/backends.json. An
agent asks for "heavy-reason"; it never names a unit, and a unit that is not in
that file can never be touched. Two hard facts from this box shape the design:

  * Only one big local server fits in ~125 GiB (GTT is carved from the same RAM),
    which is why the units already carry Conflicts=. We stop the incumbent
    explicitly anyway, so we never pay for a doomed parallel load.
  * Flash-Next can wedge into emitting one punctuation character forever when its
    RAM prompt-state cache restores a corrupt recurrent slot. /health stays green
    through it. Only a restart clears it — hence probe(), not just health().

Every liveness/health verdict here follows the rule stated in full in
`pp_watch`'s module docstring (#6): two independent stores must agree before a
verdict kills anything, and "cannot tell" is never "dead". `generating()` and
`slot_progress()` return `None` — never `False` — on any ambiguity for exactly
that reason; no caller may treat `None` as a negative.
"""
from __future__ import annotations

import json
import re
import socket
from collections import Counter
import subprocess
import time
import threading
import urllib.error
import urllib.request
from pathlib import Path
from contextlib import contextmanager

from pp_common import CONFIG, ROOT, LockBusy, lifetime_lock, log, read_json
import pp_holds

CONFIG_FILE = CONFIG / "backends.json"
_lease_state = threading.local()


@contextmanager
def lease(goal: Path | None = None):
    """One kernel-owned inference lease, shared by all Perpetua entry points.

Reentrant within the supervisor; its probes use the parent's lease. This does
not coordinate unrelated inference clients outside Perpetua.
"""
    if getattr(_lease_state, "owned", False):
        yield
        return
    with lifetime_lock(ROOT / "inference.lock",
                       {"goal": goal.name if goal else "operator"}, acquire_grace_s=0):
        _lease_state.owned = True
        try:
            yield
        finally:
            _lease_state.owned = False


def load_config() -> dict:
    cfg = read_json(CONFIG_FILE)
    if cfg is None:
        raise FileNotFoundError(f"missing {CONFIG_FILE}")
    return cfg


def resolve(class_name: str | None, *, allow_hosted: bool = False) -> dict:
    cfg = load_config()
    name = class_name or cfg.get("default_class")
    classes = dict(cfg.get("classes", {}))
    if allow_hosted:
        classes.update(cfg.get("hosted_classes", {}))
    if name not in classes:
        known = ", ".join(sorted(classes))
        raise KeyError(
            f"unknown workload class {name!r}"
            + ("" if allow_hosted
               else " (hosted classes need \"allow_hosted\": true in the goal's goal.json)")
            + f". Known: {known}"
        )
    spec = dict(classes[name])
    if spec.get("enabled") is False:
        raise KeyError(f"workload class {name!r} is disabled; finish its runtime setup first")
    spec["class"] = name
    import pp_routing
    return pp_routing.effective(spec)


def escalation_for(class_name: str) -> str | None:
    return load_config().get("escalation", {}).get(class_name)


def known_units() -> set[str]:
    cfg = load_config()
    units = set()
    for group in ("classes", "hosted_classes"):
        for spec in cfg.get(group, {}).values():
            if spec.get("unit"):
                units.add(spec["unit"])
    return units


# ── systemd (restricted to units named in backends.json) ──────────────────
def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", "--user", *args],
                          capture_output=True, text=True)


def _guard_unit(unit: str) -> None:
    if unit not in known_units():
        raise PermissionError(
            f"refusing to touch unit {unit!r}: not declared in {CONFIG_FILE}")


def unit_active(unit: str) -> bool:
    _guard_unit(unit)
    return _systemctl("is-active", "--quiet", unit).returncode == 0


def unit_failed(unit: str) -> bool:
    """Is the unit in systemd's `failed` state? (Not the same as "not active".)"""
    _guard_unit(unit)
    return _systemctl("is-failed", "--quiet", unit).returncode == 0


def start_unit(unit: str) -> str:
    """Start the unit, clearing a start-limit lockout first. Returns "" or why not.

    A unit that has hit StartLimitBurst does not merely fail to start — systemd
    REFUSES the start request ("Start request repeated too quickly") until
    something calls `reset-failed`, and it stays refused for as long as nobody
    does. That is not hypothetical here: the model units restart-loop whenever
    another class holds the RAM, which is exactly the state `ensure()` finds
    them in when it wants to swap one in. Unguarded, the swap stops the working
    server, is refused the new one, and leaves the box with no backend loaded
    at all — recoverable only by the next ensure() paying a full model load.

    The return code was also being thrown away, so a refusal surfaced two
    seconds later as "died while loading — check its journal", which is the
    wrong journal and the wrong question.
    """
    _guard_unit(unit)
    if unit_failed(unit):
        log(f"{unit} is in failed state — clearing it before start")
        _systemctl("reset-failed", unit)
    res = _systemctl("start", unit)
    if res.returncode != 0:
        why = (res.stderr or res.stdout or "").strip().splitlines()
        detail = why[-1] if why else f"systemctl start returned {res.returncode}"
        log(f"{unit}: start refused: {detail}")
        return detail
    return ""


def stop_unit(unit: str) -> None:
    _guard_unit(unit)
    _systemctl("stop", unit)


def restart_unit(unit: str) -> str:
    """Restart the unit. Same start-limit lockout as start_unit — heal() is the
    caller that most needs it, because a wedge-restart loop is precisely how a
    unit reaches its start limit."""
    _guard_unit(unit)
    if unit_failed(unit):
        log(f"{unit} is in failed state — clearing it before restart")
        _systemctl("reset-failed", unit)
    res = _systemctl("restart", unit)
    if res.returncode != 0:
        why = (res.stderr or res.stdout or "").strip().splitlines()
        detail = why[-1] if why else f"systemctl restart returned {res.returncode}"
        log(f"{unit}: restart refused: {detail}")
        return detail
    return ""


def mem_available_gib() -> float | None:
    """MemAvailable, in GiB, or None if /proc/meminfo cannot be read."""
    try:
        with open("/proc/meminfo", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return float(line.split()[1]) / (1024 * 1024)
    except (OSError, ValueError, IndexError):
        return None
    return None


def settle_memory(timeout_s: float = 90.0, quiet_polls: int = 2,
                  verbose: bool = True) -> float | None:
    """Wait for the box to finish handing back a stopped server's RAM.

    A big llama-server holds tens of GiB, and `systemctl stop` returns when the
    PROCESS is gone, not when the kernel has finished reclaiming what it held.
    Starting the next server into that window is how a swap fails on a box that
    has plenty of memory a second later: observed live 2026-09-03, muse-server
    stopped by flashnext's own Conflicts= and flashnext then dying 3ms later on
    "only 88 GiB available", which demoted the session to the fallback class for
    a race rather than for a reason.

    Polls MemAvailable until it stops rising (`quiet_polls` polls with no
    material gain) or the deadline passes. Returns the last reading.
    """
    last = mem_available_gib()
    if last is None:
        time.sleep(5)                       # cannot measure; give it a moment
        return None
    quiet = 0
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        time.sleep(2)
        now = mem_available_gib()
        if now is None:
            return last
        if now - last < 0.5:                # under half a GiB is not progress
            quiet += 1
            if quiet >= quiet_polls:
                return now
        else:
            quiet = 0
        last = now
    if verbose:
        log(f"memory did not settle within {timeout_s:.0f}s "
            f"({last:.1f} GiB available)")
    return last


# ── health and sanity ─────────────────────────────────────────────────────
def health(spec: dict, timeout: float = 3.0) -> bool:
    url = spec.get("base_url")
    if not url:
        return True                      # hosted: nothing local to check
    try:
        with urllib.request.urlopen(f"{url}/models", timeout=timeout) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError, TimeoutError):
        return False


def generating(spec: dict, timeout: float = 2.0) -> bool | None:
    """Is this backend producing tokens RIGHT NOW? None when it cannot say.

    The stall watchdog watches two file mtimes — the pane log and the session
    JSONL — and both are blind to a turn in progress. Measured on the shakedown
    (run 0002, 2026-09-03): the model had been generating one answer for nine
    minutes at 12.8 tok/s while the transcript's mtime sat 547 seconds stale and
    the pane log 17 minutes stale, because pi writes a turn when the turn ENDS.
    A slow local model can legitimately spend twenty minutes on one answer, so a
    stall timeout short enough to be useful would kill healthy sessions — unless
    something can see the generation. llama.cpp's /slots can.

    None (not False) when there is no local server, when /slots is disabled, or
    when anything at all goes wrong: the caller must treat "cannot tell" as "do
    not kill on this signal alone", never as "dead".
    """
    url = spec.get("base_url")
    if not url:
        return None                      # hosted; nothing local to ask
    base = url.rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3].rstrip("/")
    try:
        with urllib.request.urlopen(f"{base}/slots", timeout=timeout) as r:
            if r.status != 200:
                return None
            slots = json.loads(r.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, OSError, TimeoutError, ValueError):
        return None
    if not isinstance(slots, list):
        return None
    return any(isinstance(slot, dict) and slot.get("is_processing")
               for slot in slots)


def _slot_decoded(slot: dict):
    """Tokens generated so far in the slot's CURRENT request, across builds.

    This llama.cpp build nests it as slot["next_token"][0]["n_decoded"]; older
    ones put n_decoded or tokens_predicted on the slot directly. Returns None
    when no field is present.
    """
    nt = slot.get("next_token")
    if isinstance(nt, list) and nt and isinstance(nt[0], dict):
        if nt[0].get("n_decoded") is not None:
            return nt[0]["n_decoded"]
    if isinstance(nt, dict) and nt.get("n_decoded") is not None:
        return nt["n_decoded"]
    for key in ("n_decoded", "tokens_predicted", "n_past"):
        if slot.get(key) is not None:
            return slot[key]
    return None


def slot_progress(spec: dict, timeout: float = 2.0) -> list[dict] | None:
    """Per-slot generation progress from llama.cpp /slots.

    One dict per slot: {processing, task_id, n_decoded, n_predict}. n_decoded is
    tokens produced so far in the CURRENT request (it resets when task_id
    changes); n_predict is that request's own output ceiling. None on any error
    or a non-local backend — same "cannot tell is not idle" contract as
    generating(): a caller must never kill on None alone.
    """
    url = spec.get("base_url")
    if not url:
        return None
    base = url.rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3].rstrip("/")
    try:
        with urllib.request.urlopen(f"{base}/slots", timeout=timeout) as r:
            if r.status != 200:
                return None
            slots = json.loads(r.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, OSError, TimeoutError, ValueError):
        return None
    if not isinstance(slots, list):
        return None
    out: list[dict] = []
    for slot in slots:
        if not isinstance(slot, dict):
            continue
        params = slot.get("params") if isinstance(slot.get("params"), dict) else {}
        out.append({
            "processing": bool(slot.get("is_processing")),
            "task_id": slot.get("id_task"),
            "n_decoded": _slot_decoded(slot),
            "n_predict": params.get("n_predict") or params.get("max_tokens"),
        })
    return out or None


BUSY = "busy"          # probe verdict meaning "alive but queued", not "broken"
DEGENERATE = "degenerate"   # the wedge's signature in a probe verdict
DEGENERATE_MIN_LEN = 40


def is_degenerate(text: str) -> tuple[bool, str]:
    """True when a completion looks like the Flash-Next wedge.

    The failure mode is an all-NaN logit distribution serialised as a repeat of
    one low-id token — in practice a single punctuation character, thousands of
    times. We look for a long run of one non-alphanumeric character dominating
    the output rather than for '/' specifically, because the token id that wins
    an invalid distribution is not guaranteed to be the same one every time.
    """
    if not text or len(text) < DEGENERATE_MIN_LEN:
        return False, ""
    stripped = re.sub(r"\s+", "", text)
    if len(stripped) < DEGENERATE_MIN_LEN:
        return False, ""
    most, count = Counter(stripped).most_common(1)[0]
    share = count / len(stripped)
    if share >= 0.95 and not most.isalnum():
        return True, most
    # A very long unbroken run also counts, even inside longer output.
    run = re.search(r"([^\w\s])\1{199,}", text)
    if run:
        return True, run.group(1)
    return False, ""


def probe(spec: dict, timeout: float = 300.0) -> tuple[bool, str]:
    """Ask for a real completion and assert it is not degenerate.

    /v1/models answering 200 is not evidence the model can still produce a valid
    distribution — that is exactly the wedge's signature.
    """
    url = spec.get("base_url")
    if not url:
        return True, "hosted backend, not probed"
    payload = json.dumps({
        "model": spec["model"],
        "messages": [{"role": "user",
                      "content": "Reply with exactly: PERPETUA PROBE OK"}],
        # Generous, because these are thinking models: with a small budget the
        # whole allowance is spent in reasoning and `content` comes back empty,
        # which is indistinguishable from a real failure.
        "max_tokens": 256,
        "temperature": 0,
        "stream": False,
    }).encode()
    req = urllib.request.Request(f"{url}/chat/completions", data=payload,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read())
    except (TimeoutError, socket.timeout):
        # A timeout is NOT evidence of a wedge. llama.cpp serves one slot here, so
        # a probe queued behind someone else's 90K-token prefill times out on a
        # perfectly healthy server. Treating that as "unusable" would escalate to
        # another class, and escalation STOPS the incumbent unit — evicting the
        # very job we were waiting on. Report it as busy and let the caller wait.
        if health(spec):
            return False, f"{BUSY}: no reply within {timeout:.0f}s but /v1/models is up"
        return False, f"probe timed out after {timeout:.0f}s and the server is not answering"
    except Exception as exc:                       # noqa: BLE001 - report anything
        return False, f"probe request failed: {exc}"
    try:
        msg = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        return False, f"probe returned no completion: {str(data)[:200]}"
    # Reasoning counts as evidence of a healthy distribution: a thinking model
    # that reasoned and then ran out of budget is alive, a wedged one is not.
    text = (msg.get("content") or "") + (msg.get("reasoning_content") or "")
    bad, ch = is_degenerate(text)
    if bad:
        return False, f"probe output is degenerate (repeating {ch!r})"
    if not text.strip():
        return False, "probe output was empty"
    return True, text.strip()[:80].replace("\n", " ")


def complete(spec: dict, prompt: str, *, max_tokens: int = 1500,
             timeout: float = 300.0, temperature: float = 0.2,
             accept_reasoning: bool = False,
             think: bool = True) -> tuple[str, str]:
    """One bounded, tool-less completion. Returns (text, error).

    The reaper used to get this through `pi -p --no-tools --no-session`, which
    is a whole agent process for a single question — and, decisively, gives no
    way to bound the answer. Measured twice on 2026-09-03: a reap of a 24k-char
    transcript generated to pi's 16,384-token ceiling and returned nothing
    parseable, twice, each time costing about fifteen minutes of the goal's wall
    clock between sessions. The request the reaper actually makes has no tools,
    no session and no memory; it is the same shape `probe` already sends, and
    sending it directly is what lets max_tokens exist.

    Returns ("", reason) rather than raising: every caller of this is on a
    best-effort path where a failure must degrade, not propagate.
    """
    url = spec.get("base_url")
    if not url:
        return "", "no base_url (hosted backend)"
    payload = json.dumps({
        "model": spec["model"],
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": int(max_tokens),
        "temperature": temperature,
        "stream": False,
        # Thinking OFF, and this is the fix the whole reaper saga was looking
        # for. Measured on this box: asked for a small JSON object, this model
        # spends its entire allowance reasoning about producing JSON and never
        # produces it — 12,964 characters of "We need answer user's request:
        # produce JSON object only with keys..." and then the ceiling. With
        # enable_thinking false the same request returns the object itself,
        # finish_reason "stop", zero reasoning tokens. Servers that do not
        # understand the key ignore it, so it costs nothing where it is not
        # needed.
        "chat_template_kwargs": {"enable_thinking": think},
    }).encode()
    req = urllib.request.Request(f"{url}/chat/completions", data=payload,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read())
    except (TimeoutError, socket.timeout):
        return "", f"no reply within {timeout:.0f}s"
    except Exception as exc:                       # noqa: BLE001
        return "", f"request failed: {exc}"
    try:
        msg = data["choices"][0]["message"]
        finish = data["choices"][0].get("finish_reason")
    except (KeyError, IndexError, TypeError):
        return "", f"no completion in the reply: {str(data)[:200]}"
    text = (msg.get("content") or "").strip()
    reasoning = (msg.get("reasoning_content") or "").strip()
    if not text and reasoning and accept_reasoning:
        # Measured on this box: qwen3.8-flash-next answers a summarisation
        # prompt entirely inside reasoning_content and emits no content at all,
        # so a caller that only reads `content` sees nothing however long it
        # waits. `probe` already treats reasoning as real output; a caller that
        # is going to search the text for a JSON object does not care which
        # field carried it, and must opt in rather than be surprised by it.
        return reasoning, ""
    if not text:
        # Say WHICH failure it was: "empty" and "truncated" want different
        # responses, and so does "it answered, in the other field".
        why = "empty content"
        if reasoning:
            why = ("all output went to reasoning_content"
                   if not accept_reasoning else "reasoning_content was empty too")
        if finish == "length":
            why += f" (hit the {max_tokens}-token ceiling)"
        return "", why
    return text, ""


def check_guard(spec: dict) -> str | None:
    """Warn if a known-required launch flag has gone missing from a server script."""
    gf, needle = spec.get("guard_file"), spec.get("guard_must_contain")
    if not gf or not needle:
        return None
    p = Path(gf)
    if not p.exists():
        return f"guard file {gf} is missing"
    if needle not in p.read_text(encoding="utf-8", errors="replace"):
        return (f"{gf} no longer contains {needle!r} — the fix for the "
                f"repeating-character wedge is not in place")
    return None


# ── the two operations the supervisor actually calls ──────────────────────
def hold_refusal(spec: dict, *, goal: Path | None) -> str | None:
    """Why this swap is refused, or None if the slot is free for this goal.

    The one place the HOLD token gets teeth (D5). Only one big local server
    fits on this box — the units carry Conflicts= — so a live hold by a
    DIFFERENT goal on any current local class claims the slot: this swap would
    stop that class's unit, which is exactly the contention the token exists
    to prevent. A hold by the requesting goal itself never refuses, or a goal
    would deadlock against its own supervisor (re-entrancy). Holds on hosted
    classes, or on resources that are not a local class today, are convention
    only — the harness never interprets what it cannot route to a unit.
    `goal` is None for callers that cannot prove who they are (pp-backend, a
    human), and for them every live hold blocks: the safe failure is a loud
    refusal naming the holder, not a silent swap.
    """
    if not spec.get("unit"):
        return None                     # hosted: nothing local is swapped
    local = {n for n, s in load_config().get("classes", {}).items()
             if s.get("unit")}
    mine = goal.name if goal is not None else None
    for h in pp_holds.all_holds():      # already live-only; a read never mutates
        holder = pp_holds.goal_id(h.get("goal"))
        if not holder or holder == mine:
            continue
        if h.get("resource") in local:
            return (f"the local model server is held by "
                    f"{pp_holds.describe_hold(h)} — swapping `{spec['class']}` "
                    f"would take it out from under another goal's session. "
                    f"Wait for the hold to expire, or have `{holder}` release "
                    f"it (`perpetua holds` on the box).")
    return None


def _start_and_wait(spec: dict, unit: str, *, verbose: bool = True
                    ) -> tuple[bool, str, bool]:
    """Start the unit and wait for it to answer. Returns (ok, detail, died_fast).

    `died_fast` distinguishes "the server refused to load" — which on this box
    is almost always the memory of the server it replaced, not yet reclaimed —
    from "it loaded and then broke", which is a fault worth escalating on.
    """
    started = time.time()
    refused = start_unit(unit)
    if refused:
        return False, f"{unit} would not start: {refused}", True
    deadline = started + float(spec.get("load_timeout_s", 900))
    while time.time() < deadline:
        if health(spec):
            return True, "", False
        if not unit_active(unit):
            fast = (time.time() - started) < 30
            return (False,
                    f"{unit} died while loading — check its journal",
                    fast)
        time.sleep(2)
    return False, f"{unit} did not become healthy within its load timeout", False


def ensure(spec: dict, *, verbose: bool = True,
           goal: Path | None = None) -> tuple[bool, str]:
    try:
        with lease(goal):
            ok, detail = _ensure(spec, verbose=verbose, goal=goal)
            if ok and spec.get("verify_capabilities"):
                problem = verify_capabilities(spec)
                if problem:
                    return False, problem
            return ok, detail
    except LockBusy as exc:
        return False, f"resource-busy: inference owned by {exc.holder.get('goal', 'another supervisor')}"


def verify_capabilities(spec: dict) -> str | None:
    """Check the live deployment, not the model's advertised capacity."""
    base = str(spec.get("base_url", "")).removesuffix("/v1").rstrip("/")
    try:
        with urllib.request.urlopen(f"{base}/props", timeout=5) as response:
            props = json.load(response)
        slots = int(props["total_slots"])
        context = int(props["default_generation_settings"]["n_ctx"])
        if slots < int(spec.get("slots", 1)) or context < int(spec.get("context_tokens", 0)):
            return f"capability mismatch: live slots={slots}, context={context}; configuration overstates capacity"
        if spec.get("reasoning_effort") and not (props.get("chat_template_caps") or {}).get("supports_reasoning_effort"):
            return "capability mismatch: server template does not support explicit reasoning effort"
        spec["observed_capabilities"] = {"slots": slots, "context_tokens": context,
                                         "reasoning_effort": bool((props.get("chat_template_caps") or {}).get("supports_reasoning_effort"))}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return f"could not verify backend capabilities: {exc}"
    return None


def _ensure(spec: dict, *, verbose: bool = True,
           goal: Path | None = None) -> tuple[bool, str]:
    """Make `spec`'s backend the loaded one, and prove it answers sanely.

    The first gate is not health but ownership: before a single unit is
    stopped or started, `hold_refusal` consults the root-level holds.json and
    refuses a swap that another goal's live hold would be yanked out from
    under, naming the holder and the expiry — a refusal a human can act on.
    `goal` is the requesting goal's dir (the supervisor passes it); a caller
    that passes None gets the conservative answer that every live hold blocks.
    """
    refuse = hold_refusal(spec, goal=goal)
    if refuse:
        log(f"⚠️  {refuse}")
        return False, "resource-busy: " + refuse
    warn = check_guard(spec)
    if warn and verbose:
        log(f"⚠️  {warn}")

    unit = spec.get("unit")
    if not unit:
        ok, detail = probe(spec)
        return (ok or detail.startswith(BUSY)), detail

    if not (health(spec) and unit_active(unit)):
        stopped_any = False
        for other in known_units() - {unit}:
            if unit_active(other):
                if verbose:
                    log(f"stopping {other} (only one big local server fits in RAM)")
                stop_unit(other)
                stopped_any = True
        # Only when WE stopped something. `systemctl stop` returns when the
        # process is gone, not when the kernel has handed its memory back, so
        # that wait is real — but a cold start stopped nothing and should not be
        # charged for it. The other eviction path, a unit this config does not
        # know being conflicted out by the target's own Conflicts=, happens
        # INSIDE the start job and is therefore invisible here however long we
        # wait; that one is recovered by the died_fast retry below, which is
        # where its memory wait belongs.
        if stopped_any:
            settle_memory(verbose=verbose)
        if verbose:
            log(f"starting {unit}")
        ok, detail, died_fast = _start_and_wait(spec, unit, verbose=verbose)
        if not ok and died_fast:
            # One retry, and only for the fast death: that is the shape of a
            # server refusing to load because the memory it needs has not been
            # handed back yet. A slow death is a real fault and escalating away
            # from it is correct.
            avail = settle_memory(timeout_s=120, verbose=verbose)
            if verbose:
                log(f"{unit} died immediately; retrying once with "
                    f"{avail:.1f} GiB available" if avail is not None
                    else f"{unit} died immediately; retrying once")
            ok, detail, _ = _start_and_wait(spec, unit, verbose=verbose)
        if not ok:
            return False, detail

    ok, detail = probe(spec)
    if not ok and detail.startswith(BUSY):
        log(f"{spec['class']} is healthy but busy — {detail}. Proceeding; the "
            f"session's own request will queue behind it.")
        return True, detail
    if not ok and DEGENERATE in detail and unit:
        # A degenerate probe is a WEDGE, and a wedge is the one backend failure
        # this box knows how to fix: the poison is in the server's memory, and a
        # restart clears it. Escalating instead — which is what happened twice on
        # the night of 2026-09-03 — abandons the fast class for a whole session
        # and lands the goal on a model roughly five times slower per turn, to
        # avoid a fault that takes forty seconds to repair. Heal once, re-probe,
        # and only then escalate.
        log(f"{spec['class']} probe is degenerate — this is the wedge. "
            f"Restarting {unit} once before giving up on this class.")
        healed, why = heal(spec, verbose=verbose)
        if healed:
            log(f"{unit} came back clean after the restart")
            return True, why
        log(f"{unit} is still wedged after a restart ({why}) — escalating")
        return False, f"{detail}; still wedged after one restart: {why}"
    if not ok:
        return False, detail
    return True, detail


def heal(spec: dict, *, verbose: bool = True) -> tuple[bool, str]:
    try:
        with lease():
            return _heal(spec, verbose=verbose)
    except LockBusy:
        return False, "resource-busy: another supervisor owns inference"


def _heal(spec: dict, *, verbose: bool = True) -> tuple[bool, str]:
    """Clear a wedged server. The poisoned state is in RAM; only a restart clears it."""
    unit = spec.get("unit")
    if not unit:
        return False, "hosted backend cannot be restarted from here"
    if verbose:
        log(f"restarting {unit} to clear the wedge")
    refused = restart_unit(unit)
    if refused:
        return False, f"{unit} would not restart: {refused}"
    time.sleep(3)
    deadline = time.time() + float(spec.get("load_timeout_s", 900))
    while time.time() < deadline:
        if health(spec):
            return probe(spec)
        if not unit_active(unit):
            return False, f"{unit} failed to come back after restart"
        time.sleep(2)
    return False, f"{unit} did not become healthy after restart"
