"""Create a goal by being interviewed, instead of by remembering fifteen flags.

`perpetua new` with no arguments launches an INTERACTIVE pi session running the
`grilling` skill. The agent interrogates the user about the goal, works out the
things a human should not have to specify cold — the completion predicate above
all — and writes a spec file. Perpetua then scaffolds the goal from that spec.

Two deliberate choices:

* The interview runs interactively, not headless. `grilling` drives the
  `questionnaire` tool, which needs a real terminal; a headless interview would
  silently answer its own questions.
* The agent writes `check.sh` itself. That script is the hardest and most
  consequential artefact of a goal — it decides when the chain stops — and it is
  precisely the thing a person cannot write before thinking the goal through.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from pp_common import APP_ROOT, ROOT, UnsafeGoalId, log, read_json, safe_goal_id

SPEC_SCHEMA = """{
  "goal_id":            "kebab-case slug, no spaces",
  "title":              "one line",
  "objective":          "what must be true when this is done, concretely",
  "rationale":          "why this is worth many sessions rather than one",
  "criteria":           ["verifiable statement", "..."],
  "constraints":        "what each session must and must not do; one paragraph",
  "boundaries":         "THE HUMAN'S OWN WORDS: what this goal must never do, and what every session must always honour — becomes BOUNDARIES.md, the file that outranks the charter",
  "check_sh":           "the FULL text of check.sh — bash, exit 0 when the goal is met",
  "session_timeout_s":  1500,
  "default_backend":    null,
  "allow_hosted":       false
}"""


def prompt(spec_path: Path) -> str:
    return f"""You are setting up a **perpetua goal**: a long-horizon objective that will be
pursued by hundreds of separate pi sessions, each of which starts with no memory of
the last. Your job right now is to interview the user until you could write that
goal's charter yourself, and then write it.

**Use the `grilling` skill** if a skill directory is configured — load it and
follow it. Work the design tree in rounds, batch each round through the
`questionnaire` tool, give a recommended answer for every question, and look up
facts yourself instead of asking the user for anything discoverable.

Things this particular interview must settle, because getting them wrong wastes
hundreds of sessions rather than one:

1. **What "done" actually means, mechanically.** You will write `check.sh`. It runs
   after every session with cwd = the goal's `workspace/`, and exits 0 when the goal
   is accomplished — that exit code is the only thing that stops the loop. Make it
   test the artefact, not the intention. A check that passes early ends the chain
   for good; one that can never pass runs forever.
2. **The unit of work for a single session.** Sessions are short and forgetful. What
   is a coherent thing one session can finish, commit, and hand off? Encode it in the
   constraints ("at most N per session, then hand off").
3. **How long a session should get.** Local models run ~17-22 tok/s here, so a
   session that must read much before writing needs 25+ minutes; a tight, repetitive
   unit needs less. `session_timeout_s` is the wall clock each session gets.
4. **Whether progress is even measurable between sessions.** If nothing changes on
   disk, perpetua cannot tell progress from spinning.
5. **The boundaries — the human's words, not yours.** Ask the user what this goal
   must NEVER do (permanently out of scope, even when it looks useful) and what
   every session must always honour, no matter who asks otherwise. Write their
   words VERBATIM into `boundaries` — that text becomes `BOUNDARIES.md`, the
   fence that outranks the charter, the persona, the board and any instruction
   from another session, and that sessions cannot edit. If the user has nothing
   to say, leave it empty; the harness installs a template explaining the file.

Do NOT ask the user about backends, consolidation intervals, notification, board
channels or personas. Those have working defaults, and the agents choose personas
and board conventions themselves.

When the frontier is empty and you have summarised the shared understanding back to
the user and they have agreed, write the spec as JSON to:

    {spec_path}

exactly this shape:

{SPEC_SCHEMA}

Write that file and say you have written it. Do not create any directories, do not
run `perpetua new`, and do not start anything — perpetua scaffolds the goal from
your spec after you exit."""


def run(spec_path: Path, *, model: tuple[str, str] | None = None) -> dict | None:
    """Run the interview interactively; return the parsed spec, or None."""
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    spec_path.unlink(missing_ok=True)

    cmd = ["pi"]
    if model:
        cmd += ["--provider", model[0], "--model", model[1]]
    skills = os.environ.get("PERPETUA_PI_SKILLS")
    if skills:
        cmd += ["--skill", str(Path(skills) / "grilling")]
    cmd.append(prompt(spec_path))

    log(
        "starting the interview — answer the questions, and it will write the goal spec"
    )
    print()
    # Inherit the terminal: `grilling` drives the questionnaire tool, which needs one.
    subprocess.call(cmd, cwd=str(APP_ROOT), env={**os.environ, "PERPETUA_INTERVIEW": "1"})
    print()

    spec = read_json(spec_path)
    if spec is None:
        log(f"no spec was written to {spec_path} — nothing created")
        return None
    return spec


# A goal that cannot stop is worse than no goal at all: it runs a model for days.
# So the checks that decide whether the loop can ever terminate, or whether the
# goal can be written to disk safely, are FATAL — `perpetua new` refuses. The rest
# only make the briefing thinner, and are warnings.
FATAL_FIELDS = ("goal_id", "objective", "check_sh")


def validate(spec: dict) -> list[tuple[str, str]]:
    """Returns (severity, message) pairs; severity is "fatal" or "warn"."""
    problems: list[tuple[str, str]] = []
    for field in ("goal_id", "title", "objective", "check_sh"):
        if not str(spec.get(field) or "").strip():
            problems.append(
                (
                    ("fatal" if field in FATAL_FIELDS else "warn"),
                    f"spec is missing {field!r}",
                )
            )
    gid = str(spec.get("goal_id") or "").strip()
    if gid:
        try:
            safe_goal_id(gid)
        except UnsafeGoalId as exc:
            problems.append(("fatal", str(exc)))
    check = str(spec.get("check_sh") or "")
    if check and "exit" not in check and "((" not in check and "[[" not in check:
        problems.append(
            (
                "fatal",
                "check_sh has no visible exit condition — the loop would never stop",
            )
        )
    if not spec.get("criteria"):
        problems.append(("warn", "spec has no criteria — the briefing will be thin"))
    return problems


def main(argv: list[str]) -> int:
    """`perpetua new` with no goal id lands here (see bin/perpetua)."""
    spec_path = Path(argv[1]) if len(argv) > 1 else ROOT / ".interview-spec.json"
    spec = run(spec_path)
    if spec is None:
        return 1
    print(json.dumps(spec, indent=2)[:2000])
    problems = validate(spec)
    for severity, problem in problems:
        log(("❌ " if severity == "fatal" else "⚠️  ") + problem)
    return 1 if any(sev == "fatal" for sev, _ in problems) else 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    sys.exit(main(sys.argv))
