"""The reaper — reconstruct a handoff for a run that died without writing one.

A crash, a timeout, or a context wall must not cost the chain a link. This reads
the pi session transcript and asks a local model to write the handoff the session
failed to write. When even that is impossible, it writes a deterministic,
clearly-labelled skeleton from the transcript's own facts.

It never invents learnings: a reaped handoff is marked `source: reaper` and is
rendered in the journal with a note saying the session did not write its own.
"""
from __future__ import annotations

import json
import re
import subprocess
import time

import pp_backend
from pathlib import Path

from pp_common import find_session_file, log

MAX_TRANSCRIPT_CHARS = 24_000
# The answer is a small JSON object. Bounding it is the whole point of taking
# the direct path: an unbounded reap ran to pi's 16,384-token ceiling twice on
# 2026-09-03 and returned nothing parseable either time, ~15 minutes each.
# Enough for a reasoning model to think its way to the JSON and still be
# bounded: measured, the answer arrives inside reasoning_content on this box.
REAP_MAX_TOKENS = 3000
REAP_TIMEOUT_S = 240
PROMPT = """You are summarising a coding-agent session that ended WITHOUT writing its
handoff. The next session starts with no memory and inherits only what you write.

From the transcript below, produce a JSON object with exactly these keys:
  "done"            - list of concrete things this session actually completed
  "learned"         - list of facts discovered, INCLUDING negative results
  "next_steps"      - list of specific actions the next session should take first
  "blockers"        - list of things preventing progress
  "open_questions"  - list of unresolved questions

Rules: report only what the transcript shows. If the session accomplished nothing,
say so in "done" as an empty list — do not invent progress. Output ONLY the JSON
object, no prose, no code fence.

TRANSCRIPT:
"""


# §4c. The claim-verification pass is a SEPARATE question from reaping, asked of
# the same two inputs (a handoff and the transcript that produced it), and it is
# deliberately weaker than it could be: METR's finding is that an analysis agent
# charitably adopts the perspective of whoever it audits, so this produces LEADS
# for the meta-review session (#9) to check, never a verdict and never an edit.
VERIFY_PROMPT = """You are checking whether a coding-agent session's own report of what it
did is supported by the transcript of the session that wrote it.

Below are (1) the session's claimed `done` items and (2) the transcript. For each
claim, decide whether the transcript shows work that would produce it — a file
written, a command run and its output, a test passing. A claim is UNSUPPORTED if
the transcript shows no such work, or shows only an intention to do it.

Output ONLY a JSON object, no prose and no code fence:
  {"unsupported": [{"claim": "<the claim, verbatim>", "why": "<one sentence: what
   the transcript shows instead>"}]}

Rules. Judge only against the transcript — you cannot see the repository, so
absence of evidence in the transcript is the ONLY thing you are reporting, and
you must say it that way. If every claim is supported, return an empty list. Do
not invent claims that are not in the list. Do not rewrite a claim.

CLAIMS:
"""


def verify(handoff: dict, transcript: str, spec: dict, *,
           cwd: Path | None = None, timeout: int = 300) -> list[str]:
    """Which `done` claims does the transcript fail to support? Leads, not verdicts.

    Returns one line per unsupported claim, phrased as what the transcript does
    not show — the meta-review session decides what it means, because this pass
    can only see what the session said, not the repository it said it about.

    Silence is the failure mode: a model that is unreachable, slow or
    unparseable returns no leads rather than an accusation nobody can check.
    """
    claims = [str(c).strip() for c in (handoff.get("done") or []) if str(c).strip()]
    if not claims or not transcript.strip():
        return []
    run = handoff.get("run")
    prompt = (VERIFY_PROMPT
              + "\n".join(f"- {c}" for c in claims)
              + "\n\nTRANSCRIPT:\n" + transcript)
    try:
        if spec.get("base_url"):
            text, why = pp_backend.complete(spec, prompt, max_tokens=REAP_MAX_TOKENS,
                                            timeout=min(timeout, REAP_TIMEOUT_S),
                                            accept_reasoning=True, think=False)
            proc = subprocess.CompletedProcess([], 0 if text else 1, text or "", why)
        else:
            proc = subprocess.run(
                ["pi", "-p", "--provider", spec["provider"], "--model", spec["model"],
                 "--no-session", "--no-tools", "--thinking", "low", prompt],
                capture_output=True, text=True, timeout=timeout,
                cwd=str(cwd) if cwd else None,
            )
    except (subprocess.TimeoutExpired, OSError) as exc:
        log(f"verify: run {run}: {type(exc).__name__} — no leads produced")
        return []
    except Exception as exc:                            # noqa: BLE001
        log(f"verify: run {run}: {type(exc).__name__}: {exc}")
        return []

    parsed = _parse_json(proc.stdout or "")
    if not parsed:
        log(f"verify: run {run}: unparseable output "
            f"({len(proc.stdout or '')} chars) — no leads produced")
        return []
    leads = []
    for item in parsed.get("unsupported") or []:
        if isinstance(item, str):
            claim, why = item, ""
        elif isinstance(item, dict):
            claim, why = str(item.get("claim", "")), str(item.get("why", ""))
        else:
            continue
        claim = claim.strip()
        if not claim:
            continue
        # Only claims the session actually made. A summariser that invents one
        # would otherwise put words in a predecessor's mouth and the audit would
        # be checking a claim nobody ever wrote.
        if claim not in claims:
            log(f"verify: run {run}: dropped a lead about a claim the handoff "
                f"does not contain")
            continue
        leads.append(f"run {run}: claimed \"{claim[:160]}\" — the transcript shows "
                     f"no work supporting it{f': {why.strip()[:200]}' if why.strip() else ''}")
    return leads


def extract_transcript(session_id: str) -> str:
    path = find_session_file(session_id)
    if not path:
        return ""
    lines: list[str] = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if rec.get("type") != "message":
            continue
        msg = rec.get("message", {})
        role = msg.get("role", "?")
        content = msg.get("content")
        chunks: list[str] = []
        if isinstance(content, str):
            chunks.append(content)
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    chunks.append(str(block.get("text", "")))
                elif block.get("type") == "toolCall":
                    chunks.append(f"[tool {block.get('name')}] "
                                  f"{json.dumps(block.get('arguments'))[:300]}")
                elif block.get("type") == "toolResult":
                    chunks.append(f"[result] {str(block.get('content'))[:300]}")
        text = "\n".join(c for c in chunks if c).strip()
        if not text:
            continue
        # Tool results are the bulk of a transcript and the least informative per
        # byte; cap every message so the tail we keep spans many turns, not one
        # enormous file read.
        cap = 400 if role == "toolResult" else 1200
        if len(text) > cap:
            text = text[:cap] + f" …[+{len(text) - cap} chars]"
        label = f"{role}({msg.get('toolName')})" if role == "toolResult" and msg.get("toolName") else role
        lines.append(f"{label}: {text}")
    joined = "\n\n".join(lines)
    return joined[-MAX_TRANSCRIPT_CHARS:]


def _parse_json(text: str) -> dict | None:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


def reap(session_id: str, spec: dict, *, cwd: Path, timeout: int = 900,
         base_commit: str | None = None) -> dict:
    """Summarise a session that wrote no handoff.

    `base_commit` is the workspace HEAD when the run started (`start.json`
    records it): with it, the skeleton reports the commits that run actually
    landed, which is the one durable thing a session that died badly leaves.
    """
    transcript = extract_transcript(session_id)
    if not transcript:
        observed = committed_since(cwd, base_commit)
        return {
            "source": "reaper-skeleton",
            "done": [f"committed `{c}`" for c in observed], "learned": [],
            "next_steps": ["Re-read the goal charter and the previous journal entry; "
                           "the last session left no usable transcript."],
            "blockers": [f"Session {session_id} produced no readable transcript — "
                         f"it probably died before its first turn."],
            "open_questions": [],
        }

    # Thinking level "low" first, then "off" if that produced nothing. On this
    # box a reap came back with ZERO characters of stdout in 18s against a
    # healthy server (2026-09-03, run 0001) — the signature of a reasoning model
    # spending its whole output budget on reasoning content and emitting no
    # final answer, which is the same shape as the empty completion the health
    # probe is written to catch. One retry with thinking off costs a call only
    # on the path that already failed.
    # A local server takes the bounded direct path: one HTTP request with a real
    # max_tokens, instead of a whole pi process with no way to cap the answer.
    if spec.get("base_url"):
        text, why = pp_backend.complete(spec, PROMPT + transcript,
                                        max_tokens=REAP_MAX_TOKENS,
                                        timeout=min(timeout, REAP_TIMEOUT_S),
                                        accept_reasoning=True, think=False)
        parsed = _parse_json(text) if text else None
        if parsed:
            parsed["source"] = "reaper"
            return parsed
        log(f"reaper: no usable summary from {spec.get('class', '?')} "
            f"({why or f'{len(text)} chars, unparseable'})"
            + (f": {text[:200]!r}" if text else ""))
        return _skeleton(session_id, transcript, cwd=cwd, base_commit=base_commit)

    deadline = time.time() + timeout
    for attempt, thinking in enumerate(("low", "off")):
        started = time.time()
        left = timeout if attempt == 0 else deadline - started
        if attempt and left < 30:
            # The retry shares ONE budget with the first attempt. Giving each
            # attempt the full timeout doubled the worst case, and _finish_probe
            # runs sequentially over K probes inside run_swarm — so a wedged
            # backend could hold the supervisor for hours in the reap loop.
            log(f"reaper: no time left for a thinking={thinking} retry")
            break
        try:
            proc = subprocess.run(
                ["pi", "-p", "--provider", spec["provider"], "--model", spec["model"],
                 "--no-session", "--no-tools", "--thinking", thinking,
                 PROMPT + transcript],
                capture_output=True, text=True, timeout=left, cwd=str(cwd),
            )
        except subprocess.TimeoutExpired:
            log(f"reaper: timed out after {left:.0f}s (thinking={thinking})")
            break
        except Exception as exc:                        # noqa: BLE001
            log(f"reaper: {type(exc).__name__}: {exc}")
            break
        out, err = (proc.stdout or ""), (proc.stderr or "")
        parsed = _parse_json(out)
        if parsed:
            parsed["source"] = "reaper"
            return parsed
        # Say what actually came back. "unparseable output (0 chars)" cost an
        # evening of guessing at which of empty, refused and malformed it was.
        detail = ""
        if out:
            detail += f" stdout: {out[:200]!r}"
        if err:
            detail += f" stderr: {err[:200]!r}"
        log(f"reaper: no usable summary (thinking={thinking}, exit={proc.returncode}, "
            f"{len(out)} chars stdout in {time.time() - started:.0f}s, "
            f"{len(err)} chars stderr)" + detail)

    return _skeleton(session_id, transcript, cwd=cwd, base_commit=base_commit)


def committed_since(cwd: Path, base_commit: str | None) -> list[str]:
    """The commits this run actually landed in the workspace. Observation only.

    Invariant 8 says the reaper never invents; reading the git log is the
    opposite of inventing. It is also the only durable record a session that
    died without a handoff leaves behind — run 0004 of the first live goal made
    four real commits and its reaped handoff reported `done: []`, so the next
    session was told, in effect, that nothing had happened.

    Keyed on the base commit `start.json` recorded, not on a timestamp: a
    revision range is exactly "what this run added", where `--since` is a guess
    that a clock skew or a slow first turn can get wrong in either direction.
    """
    if not base_commit or not cwd:
        return []
    try:
        proc = subprocess.run(
            ["git", "log", f"{base_commit}..HEAD", "--pretty=%h %s"],
            capture_output=True, text=True, timeout=30, cwd=str(cwd))
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    return [line.strip() for line in (proc.stdout or "").splitlines()
            if line.strip()][:20]


def _skeleton(session_id: str, transcript: str, *, cwd: Path | None = None,
              base_commit: str | None = None) -> dict:
    """Deterministic fallback — observed facts only, no invention."""
    commits = committed_since(cwd, base_commit)
    tools = re.findall(r"\[tool (\w+)\]", transcript)
    counts: dict[str, int] = {}
    for t in tools:
        counts[t] = counts.get(t, 0) + 1
    used = ", ".join(f"{k}×{v}" for k, v in sorted(counts.items(),
                                                   key=lambda kv: -kv[1])[:8])
    return {
        "source": "reaper-skeleton",
        # Observed, not claimed: these commits exist in the workspace.
        "done": [f"committed `{c}`" for c in commits],
        "learned": [],
        # NOT "go read the raw transcript". That is a 24k-character JSONL, it is
        # the most expensive instruction this file can give a session that has
        # just been told to budget its reading, and the cheap evidence is
        # better: the workspace's own git log says what actually survived.
        "next_steps": ([f"That run left {len(commits)} commit(s) in the workspace, "
                        f"listed under Done. Read them before redoing anything and "
                        f"continue from there."] if commits else []) +
                      ["Do not assume the previous session achieved nothing, or "
                       "that it achieved what it set out to. Start with `git log` "
                       "and `git status` in the workspace — what is committed "
                       "there is the only durable record of that run — and "
                       "re-derive anything else rather than trusting this entry."],
        # No cause is asserted: the summariser returning nothing is not evidence
        # that the server was unreachable, and run 0001 proved the difference.
        "blockers": ["The previous session ended without a handoff and the "
                     "summariser produced nothing usable, so what it did is "
                     "recorded only in its transcript and its commits."],
        "open_questions": [f"What did the previous session achieve? Tools it used: "
                           f"{used or 'none recorded'}."],
    }
