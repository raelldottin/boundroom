#!/usr/bin/env bash
# The name Tools/boundroom-sync.sh had before 0.3.0 (SPEC-005). It runs that script with the same
# arguments, so a consumer whose Makefile still names this path keeps working until its owner
# updates the Makefile. Removing this file is a later, separate decision.
set -euo pipefail
echo "repo-automation-sync.sh: renamed to boundroom-sync.sh in 0.3.0; running it" >&2
exec "$(dirname "$0")/boundroom-sync.sh" "$@"
