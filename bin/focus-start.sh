#!/bin/bash
# Launch the focus-capture daemon (idempotent — won't double-start)
if pgrep -f "focus/daemon.py" > /dev/null; then
    exit 0
fi
nohup python3 /home/CROWN/bin/focus/daemon.py > /tmp/focus-daemon.log 2>&1 &
