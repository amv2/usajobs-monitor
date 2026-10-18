#!/bin/sh
# "schedule" starts the built-in scheduler; anything else is passed to usajobs-monitor.
set -e
if [ "$1" = "schedule" ]; then
    shift
    exec python -m usajobs_monitor.schedule "$@"
fi
exec usajobs-monitor "$@"
