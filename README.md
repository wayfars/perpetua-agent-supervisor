# Perpetua

Perpetua is a local supervisor for long-running goals. It launches bounded
agent sessions, records handoffs, reconciles interrupted runs, and repeats until
the goal's `check.sh` reports completion. The command line and supervisor use
the same on-disk goal state, so a stopped process can be restarted and recovered.

This export contains the CLI, supervisor, libraries, starter templates, and
model-free lifecycle tests. It does not include any user's goals, model registry,
agent configuration, notification credentials, transcripts, or runtime data.

## Requirements

- Python 3.10 or newer
- Bash and Git
- `pi` on `PATH` to run agent sessions
- `tmux` for detached sessions (or set `PERPETUA_NO_TMUX=1` to run directly)
- `systemd --user` only when enabling a goal as a service

No third-party Python package is needed by the core CLI or supervisor.

## Install and configure

Keep the source package in a stable directory and put its `bin` directory on
`PATH`. The package directory contains the executable scripts and templates;
`PERPETUA_ROOT` is a separate runtime directory for goals, state, and local
configuration. The default runtime location is
`$XDG_STATE_HOME/perpetua`, or `$HOME/.local/state/perpetua` when
`XDG_STATE_HOME` is unset. This keeps a new installation separate from older
`$HOME/perpetua` data.

```sh
export PERPETUA_PACKAGE="/path/to/perpetua"
export PERPETUA_ROOT="${XDG_STATE_HOME:-$HOME/.local/state}/perpetua-demo"
export PATH="$PERPETUA_PACKAGE/bin:$PATH"
mkdir -p "$PERPETUA_ROOT/config"
cp "$PERPETUA_PACKAGE/examples/backends.example.json" \
  "$PERPETUA_ROOT/config/backends.json"
```

The copied backend file is deliberately disabled and contains no real model
endpoint or system service. Edit the runtime copy with the provider, model ID,
and endpoint you intend to use, then change `enabled` to `true`. Add a systemd
unit name only if you have installed and reviewed that unit yourself. The
example file in the package is safe to keep unchanged.

Runtime goals and configuration are created under `PERPETUA_ROOT` and are not
part of this source export. The default goal configuration disables
notifications. Delivery requires both an explicit `notify: true` in a goal and
the corresponding environment variables below.

Optional integration settings:

| Variable | Purpose |
| --- | --- |
| `PERPETUA_PACKAGE` | Stable source package directory; use its `bin` directory on `PATH` |
| `PERPETUA_ROOT` | Runtime root; defaults to `$XDG_STATE_HOME/perpetua` or `$HOME/.local/state/perpetua` |
| `PERPETUA_PI_SESSIONS` | Directory where `pi` stores session transcripts; defaults to `$PERPETUA_ROOT/pi-sessions` |
| `PERPETUA_MODELS_FILE` | Optional Pi model registry used to read context-window metadata |
| `PERPETUA_PI_SKILLS` | Optional directory containing the `grilling` skill for interactive goal setup |
| `PERPETUA_WIKI_INIT` | Optional path to a wiki initialization script |
| `PERPETUA_WIKI_SCRIPTS` | Optional directory containing wiki `review.py` and `promote.py` scripts |
| `PERPETUA_DISCORD_WEBHOOK_FILE` | Optional file containing a Discord webhook URL |
| `PERPETUA_XMPP_SEND` | Optional XMPP send executable |
| `PERPETUA_VOICE_CALL_BIN` | Optional voice-call executable for urgent notifications |

Do not commit runtime config, goal directories, session logs, or credential
files. The package `.gitignore` excludes these runtime paths and common local
Python artifacts. The webhook file contains a secret and should have
restrictive permissions.

## Quick start

Create a goal with a concrete completion condition and a bounded run budget. This
example passes only after the agent creates a nonempty `result.txt` in the goal
workspace:

```sh
CHECK_SCRIPT="$PERPETUA_ROOT/sample-check.sh"
cat > "$CHECK_SCRIPT" <<'EOF'
#!/usr/bin/env bash
set -eu
test -s result.txt
EOF
chmod +x "$CHECK_SCRIPT"
perpetua new sample \
  --title "Create a result file" \
  --objective "Create a nonempty result.txt in the workspace" \
  --criterion "The workspace contains a nonempty result.txt" \
  --check "$CHECK_SCRIPT" \
  --max-runs 3 \
  --no-notify
perpetua check sample
```

The check initially returns incomplete. After configuring the backend and
installing `pi`, run `perpetua start sample --max-runs 3`; the supervisor stops
after at most three sessions, even if the check still fails. `perpetua run
sample` runs one session.

`perpetua new --help` lists goal fields, and `perpetua --help` lists status,
journal, and recovery commands. `perpetua start <id>` keeps the supervisor in
the foreground. This release does not package a systemd unit: `perpetua enable`
only enables a unit already installed by the operator, so service autostart is
outside this release's documented setup.

## Tests

The tests redirect runtime state to temporary directories and replace `pi`,
`tmux`, and `systemctl` with local fakes. They exercise real subprocess start,
stop, signal cleanup, crash recovery, and handoff reconciliation without a
model backend or notification endpoint:

```sh
python3 -m unittest discover -s tests -v
```

Run the crash-recovery demonstration with:

```sh
python3 demo/recovery_demo.py
```

It invokes the production supervisor in two subprocess scenarios, terminates
the supervisor while a fake agent is active, and verifies recovery after
restart for both direct-child and tmux-owned sessions. It prints a concise
scenario summary. All runtime state and external commands are confined to
temporary test directories.

## Scope and safety

Perpetua supervises processes and writes state; it is not a security sandbox.
Agent sessions can modify their goal workspace and should be run only with the
permissions intended for that agent. Notifications are opt-in per goal and
transport. Review notification destinations before enabling them.

## License

MIT. See [LICENSE](LICENSE).

## Related portfolio

See [the portfolio map](PORTFOLIO.md) for related application, evaluation, inference, and agent operations projects.
