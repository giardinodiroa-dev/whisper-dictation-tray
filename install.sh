#!/usr/bin/env bash
set -e

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

ok()   { echo -e "${GREEN}✓${NC} $1"; }
warn() { echo -e "${YELLOW}!${NC} $1"; }
die()  { echo -e "${RED}✗${NC} $1"; exit 1; }

echo ""
echo "  voice — Groq Whisper dictation tray"
echo "  ————————————————————————————————————"
echo ""

# ── Dependencies ──────────────────────────────────────────────────────────────

check_dep() {
    if command -v "$1" &>/dev/null; then
        ok "$1 found"
    else
        warn "$1 not found — install it with: $2"
        MISSING=1
    fi
}

python3 -c "from PyQt5.QtWidgets import QApplication" 2>/dev/null \
    && ok "PyQt5 found" \
    || warn "PyQt5 not found — install: sudo pacman -S python-pyqt5  OR  sudo apt install python3-pyqt5"

check_dep parec  "sudo pacman -S libpulse  OR  sudo apt install pulseaudio-utils"
check_dep xdotool "sudo pacman -S xdotool   OR  sudo apt install xdotool"

if [[ -n "$MISSING" ]]; then
    echo ""
    die "Please install the missing dependencies above, then re-run this script."
fi

# ── Install script ────────────────────────────────────────────────────────────

mkdir -p "$HOME/bin"
cp dictation-tray.py "$HOME/bin/voice"
chmod +x "$HOME/bin/voice"
ok "Installed to ~/bin/voice"

# Ensure ~/bin is in PATH
if [[ ":$PATH:" != *":$HOME/bin:"* ]]; then
    warn "~/bin is not in your PATH — add this to ~/.bashrc or ~/.zshrc:"
    echo "      export PATH=\"\$HOME/bin:\$PATH\""
fi

# ── API key setup ─────────────────────────────────────────────────────────────

ENV_FILE="$HOME/bin/.env"

if [[ -f "$ENV_FILE" ]] && grep -q "GROQ_API_KEY=gsk_" "$ENV_FILE" 2>/dev/null; then
    ok ".env already configured"
else
    echo ""
    echo "  You need a free Groq API key."
    echo "  Get one at: https://console.groq.com → API Keys → Create key"
    echo ""
    read -rp "  Paste your Groq API key (or press Enter to do this later): " GROQ_KEY

    if [[ -n "$GROQ_KEY" ]]; then
        cat > "$ENV_FILE" <<EOF
GROQ_API_KEY=${GROQ_KEY}

# Kilo.ai free proxy (AI punctuation formatting — no key needed)
KILO_HOST=api.kilo.ai
KILO_API_KEY=anonymous
EOF
        chmod 600 "$ENV_FILE"
        ok ".env created at ~/bin/.env"
    else
        if [[ ! -f "$ENV_FILE" ]]; then
            cp .env.example "$ENV_FILE"
            chmod 600 "$ENV_FILE"
        fi
        warn "Skipped — edit ~/bin/.env and add your GROQ_API_KEY before launching"
    fi
fi

# ── Autostart ─────────────────────────────────────────────────────────────────

AUTOSTART_DIR="$HOME/.config/autostart"
DESKTOP_FILE="$AUTOSTART_DIR/voice-dictation.desktop"

mkdir -p "$AUTOSTART_DIR"
cat > "$DESKTOP_FILE" <<EOF
[Desktop Entry]
Type=Application
Name=Voice Dictation
Comment=Groq Whisper dictation tray
Exec=$HOME/bin/voice
Icon=audio-input-microphone
Hidden=false
NoDisplay=false
X-GNOME-Autostart-enabled=true
EOF
ok "Autostart entry created (~/.config/autostart/voice-dictation.desktop)"

# ── Shell alias ───────────────────────────────────────────────────────────────

ALIAS_LINE="alias voice='pkill -f bin/voice 2>/dev/null; nohup \$HOME/bin/voice &>/dev/null &'"

for RC in "$HOME/.bashrc" "$HOME/.zshrc"; do
    if [[ -f "$RC" ]] && ! grep -q "alias voice=" "$RC" 2>/dev/null; then
        echo "" >> "$RC"
        echo "# voice dictation tray" >> "$RC"
        echo "$ALIAS_LINE" >> "$RC"
        ok "Alias added to $RC"
    fi
done

# ── Launch now ────────────────────────────────────────────────────────────────

echo ""
read -rp "  Launch voice now? [Y/n]: " LAUNCH
if [[ "${LAUNCH,,}" != "n" ]]; then
    pkill -f "bin/voice" 2>/dev/null || true
    nohup "$HOME/bin/voice" &>/dev/null &
    ok "voice is running — check your system tray"
fi

echo ""
echo "  All done. voice will start automatically on next login."
echo "  Type 'voice' in any terminal to restart it anytime."
echo ""
