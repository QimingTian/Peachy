#!/usr/bin/env bash
# Start the Peachy web dashboard (same as dashboard/run.sh).
exec "$(dirname "$0")/dashboard/run.sh" "$@"
