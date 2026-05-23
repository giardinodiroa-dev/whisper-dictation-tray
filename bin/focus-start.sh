#!/bin/bash
# Launch the focus-capture daemon (idempotent — won't double-start)
FOCUS_DIR="$(cd "$(dirname "$(readlink -f "$0")")/../focus" && pwd)"
if pgrep -f "focus/daemon.py" > /dev/null; then
    exit 0
fi
nohup python3 "$FOCUS_DIR/daemon.py" > /tmp/focus-daemon.log 2>&1 &
