"""Operator views shared by the TUI and text clients; no curses or mutations."""
from __future__ import annotations

import json
import unicodedata

import pp_board
import pp_control
import pp_evidence
import pp_state
from pp_common import read_json


def clusters(text: str):
    """Small stdlib cell model: combining marks, emoji joins and flag pairs.

Terminals differ on emoji presentation; ambiguous-width characters use one cell.
Control/bidi formatting characters are made visible rather than sent to curses.
"""
    group = ''
    for ch in text:
        if unicodedata.category(ch) in {'Cc', 'Cf'} and ch != '\u200d':
            ch = '\ufffd'
        extend = (unicodedata.combining(ch) or ch in {'\ufe0e', '\ufe0f', '\u200d'}
                  or '\U0001f3fb' <= ch <= '\U0001f3ff' or group.endswith('\u200d')
                  or (len(group) == 1 and '\U0001f1e6' <= group <= '\U0001f1ff'
                      and '\U0001f1e6' <= ch <= '\U0001f1ff'))
        if group and not extend:
            yield group
            group = ''
        group += ch
    if group:
        yield group


def cluster_width(group: str) -> int:
    if '\ufe0f' in group or '\u200d' in group or any('\U0001f1e6' <= c <= '\U0001f1ff' for c in group):
        return 2
    return max((0 if unicodedata.combining(c) or c in {'\ufe0e', '\ufe0f'}
                else 2 if unicodedata.east_asian_width(c) in {'W', 'F'} else 1
                for c in group), default=0)


def cells(text: str) -> int:
    return sum(cluster_width(c) for c in clusters(text))


def clip(text: str, width: int) -> str:
    out, used = [], 0
    for group in clusters(text):
        n = cluster_width(group)
        if used + n > width:
            break
        out.append(group)
        used += n
    return ''.join(out)


def wrap_lines(lines: list[str], width: int) -> list[str]:
    out = []
    width = max(1, width)
    for line in lines:
        for paragraph in str(line).expandtabs(4).splitlines() or [""]:
            if not paragraph:
                out.append('')
                continue
            pending, used, wrapped = [], 0, False
            for group in clusters(paragraph):
                n = cluster_width(group)
                if n > width:
                    group, n = '\ufffd', 1
                if used + n > width:
                    spaces = [i for i,c in enumerate(pending) if c.isspace() and any(not v.isspace() for v in pending[:i])]
                    cut = spaces[-1] if spaces else len(pending)
                    out.append(''.join(pending[:cut]).rstrip())
                    pending = pending[cut:]
                    while pending and pending[0].isspace():
                        pending.pop(0)
                    used = sum(cluster_width(c) for c in pending)
                    wrapped = True
                if wrapped and not pending and group.isspace():
                    continue
                pending.append(group)
                used += n
            if pending:
                out.append(''.join(pending))
    return out


def plan(s: dict) -> list[str]:
    import pp_criteria
    goal = s['goal']
    receipt = pp_evidence.latest(goal)
    out = ['# Plan & evidence', '', '## Agent-claimed criteria', '',
           'Checked criteria are claims, not independent completion evidence.', '']
    out += pp_criteria.render_tree(s['state'].get('criteria', [])).splitlines()
    out += ['', '## Last recorded check', '',
            json.dumps(receipt, indent=2), '', '## Charter', '']
    charter = goal / 'GOAL.md'
    out += charter.read_text(errors='replace').splitlines() if charter.exists() else ['No charter recorded.']
    return out


def summary(s: dict) -> list[str]:
    st = s["state"]
    out = [f"# {s['id']}", ""]
    items = pp_control.inbox(s["goal"])
    if items:
        out += [f"## Needs attention: {len(items)}", ""]
        out += [(f"Paused — {i['body']}" if i['kind']=='paused' else f"{i['id']} · {i['subject']} — {i['body']}") for i in items]
        out += ["", "Open Inbox (2), or press i to decide.", ""]
    status = "waiting for model" if st.get("resource_wait") else s["status"]
    out += [f"Execution: {status}", f"Current work: {st.get('phase', 'Not recorded')}"]
    if st.get("pause_requested_at"):
        out += ["Pause requested: this session will finish its handoff first."]
    for run in pp_state.current_runs(st):
        out += [f"Run {run.get('n')} · {run.get('backend')} · started {run.get('started_at')}"]
    receipt = pp_evidence.latest(s["goal"])
    out += ["", "## Evidence", "",
            f"Last recorded check: {receipt.get('verdict', 'unknown')} · {receipt.get('at', 'never')}",
            receipt.get("detail", ""),
            "Receipt describes the recorded revision; press c to verify current work.",
            f"Agent-claimed criteria: {s['met']}/{s['total']} (not independent verification)",
            "", "## Next action", ""]
    if s["paused"]:
        out += ["Resolve the Inbox items, then press u to resume."]
    elif s["pid"]:
        out += ["a: inspect live work · p: pause after handoff · x: stop immediately"]
    else:
        out += ["s: start · S: run one session"]
    usage = st.get("usage", {})
    out += ["", f"Recorded work time: {float(usage.get('work_seconds', 0))/3600:.2f} hours",
            f"History: {s['run']} runs · {s['clean']} clean handoffs · {s['reaped']} reconstructed"]
    return out


def inbox(s: dict) -> list[str]:
    out = ["# Inbox", "", "i: decide · u: resume · A: acknowledge alert", ""]
    for item in pp_control.inbox(s["goal"]):
        if item["kind"] == "question":
            continue
        out += ['## Execution paused' if item['kind']=='paused' else f"## {item['id']} · {item['kind']}: {item['subject']}",
                item["body"], item["action"], ""]
        if item["kind"] == "amendment":
            out += [json.dumps(item["record"].get("change"), indent=2), ""]
    return out


def activity(s: dict) -> list[str]:
    out = ["# Activity", "", "/ search · empty search clears the filter", ""]
    out += pp_board.render(pp_board.read(s["goal"], limit=100), goal=s["goal"], max_body=10 ** 9).splitlines()
    return out


def controls(s: dict) -> list[str]:
    cfg = read_json(s["goal"] / "goal.json", {}) or {}
    return ["# Controls", "", "p  Pause after the current session hands off",
            "x  Stop immediately (confirmation required)", "u  Resume a paused goal",
            "A  Acknowledge the latest alert", "c  Verify current work in the background",
            "", "## Budget and model policy", "",
            json.dumps({k: cfg.get(k) for k in ("budget", "routing", "default_backend", "session_timeout_s", "backend_timeouts")}, indent=2),
            "", "Budget changes use the existing goal.json amendment path.",
            "Resume does not replenish a spent budget.",
            "Work-time limits apply at session boundaries; an active session may finish."]
