"""Out-of-band notification. Best-effort: a failed notify must never stop the loop.

#3: three severities, not a single channel fired at every event. A goal that
pauses at 2am with the phone on silent used to be dead until a human happened
to look — Discord and XMPP fired identically for "check.sh is a bit slow"
and "the goal has been paused for three days", so there was no way to say one
of those is more urgent than the other.

  INFO    -> Discord only.
  WARN    -> Discord + XMPP. This is also what a bare `notify(text)` — the
             pre-#3 call shape, still used everywhere that has not been
             taught about severity — behaves as, so nothing already calling
             this module needed to change.
  URGENT  -> Discord + XMPP + a voice call through the existing xmpp-bridge
             Jingle path. The call entry point is DISCOVERED, never assumed:
             a fixed path here would silently stop working the moment the
             bridge's own layout changes, which is exactly the kind of
             failure a notification path must not have. Its absence degrades
             to WARN rather than raising — a goal that cannot reach a phone
             must not lose Discord and XMPP over it.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.request
from pathlib import Path

from pp_common import log

WEBHOOK_FILE = (Path(os.environ["PERPETUA_DISCORD_WEBHOOK_FILE"])
                if os.environ.get("PERPETUA_DISCORD_WEBHOOK_FILE") else None)
XMPP_SEND = Path(os.environ["PERPETUA_XMPP_SEND"]) if os.environ.get("PERPETUA_XMPP_SEND") else None

INFO = "INFO"
WARN = "WARN"
URGENT = "URGENT"
LEVELS = (INFO, WARN, URGENT)

#: Optional voice-call integration. It is discovered only through an explicit
#: environment override or PATH; notifications remain opt-in at every layer.
VOICE_CALL_ENV = "PERPETUA_VOICE_CALL_BIN"


def discord(text: str) -> bool:
    if WEBHOOK_FILE is None:
        return False
    try:
        url = WEBHOOK_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return False
    if not url:
        return False
    try:
        req = urllib.request.Request(
            url, data=json.dumps({"content": text[:1900]}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status in (200, 204)
    except Exception:                                  # noqa: BLE001
        return False


def xmpp(text: str) -> bool:
    if XMPP_SEND is None or not XMPP_SEND.exists():
        return False
    try:
        return subprocess.run([str(XMPP_SEND), text[:1900]],
                              capture_output=True, timeout=20).returncode == 0
    except Exception:                                  # noqa: BLE001
        return False


def voice_call_bin() -> Path | None:
    """The xmpp-bridge voice-call entry point, discovered rather than assumed.

    Checked fresh on every call, not cached at import: the whole point is
    that an operator can install or move the binary without a code change or
    a supervisor restart picking it up.
    """
    override = os.environ.get(VOICE_CALL_ENV)
    if override:
        p = Path(override)
        return p if p.exists() else None
    found = shutil.which("xmpp-call")
    return Path(found) if found else None


def voice_call(text: str) -> bool:
    bin_path = voice_call_bin()
    if bin_path is None:
        return False
    try:
        return subprocess.run([str(bin_path), text[:200]],
                              capture_output=True, timeout=30).returncode == 0
    except Exception:                                  # noqa: BLE001
        return False


def notify(text: str, *, level: str = WARN, enabled: bool = False) -> list[str]:
    """Send `text` through the channels its severity earns. Never raises.

    `level` defaults to WARN, which is exactly the two-channel behaviour this
    function had before #3 — every existing call site (a pause, a wedge, an
    amendment needing a human) keeps working unchanged.
    """
    if not enabled:
        return []
    if level not in LEVELS:
        level = WARN
    sent = []
    if discord(text):
        sent.append("discord")
    if level in (WARN, URGENT) and xmpp(text):
        sent.append("xmpp")
    if level == URGENT:
        if voice_call(text):
            sent.append("voice")
        else:
            log("notify: URGENT asked for a voice call but no call binary was "
                f"found ({VOICE_CALL_ENV}, or a known xmpp-bridge path) — "
                f"degrading to WARN for this channel")
    return sent
