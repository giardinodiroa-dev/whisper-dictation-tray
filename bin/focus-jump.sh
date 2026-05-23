#!/bin/bash
# Super+Space — send CAPTURE to focus daemon, or fall back to original behaviour.

# Try daemon first
if python3 -c "import socket,sys; s=socket.socket(socket.AF_UNIX); s.connect('/tmp/focus.sock'); s.sendall(b'CAPTURE'); s.close()" 2>/dev/null; then
    exit 0
fi

# Fallback: original window-attention jump + toast
WIN=$(for wid in $(xprop -root 32x '\t$0' _NET_CLIENT_LIST 2>/dev/null | grep -o '0x[0-9a-f]*'); do
    xprop -id "$wid" _NET_WM_STATE 2>/dev/null | grep -q DEMANDS_ATTENTION && { echo "$wid"; break; }
done)
[ -z "$WIN" ] && WIN=$(xdotool getactivewindow 2>/dev/null)
[ -z "$WIN" ] && exit 0
NAME=$(xdotool getwindowname "$WIN" 2>/dev/null)
python3 ~/bin/toast.py "${NAME:-window}" &
xdotool windowactivate --sync "$WIN"
