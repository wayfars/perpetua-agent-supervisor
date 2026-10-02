#!/usr/bin/env bash
# Completion predicate for this goal. Exit 0 = the goal is accomplished and the
# supervisor stops. Anything else = keep going.
#
# It runs with cwd = the goal's workspace/ and PERPETUA_GOAL_DIR in the
# environment. Keep it cheap: it runs after every session.
#
# This script is HASHED by the supervisor and verified before every run: a
# session that edits it directly pauses the goal. The one legitimate way it
# changes is a charter amendment filed with the `perpetua_amend` tool and
# approved by the human who owns the goal. A check that passes when the goal is
# not actually met ends the chain for good.
set -uo pipefail

echo "check.sh has not been written yet for this goal — edit $0"
exit 1
