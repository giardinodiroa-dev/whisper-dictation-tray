# voice

A Linux system tray dictation tool powered by **Groq Whisper** — near-perfect speech-to-text, free, no subscription, no Electron, no cloud account beyond a free API key.

Click the tray icon, speak, stop — your words appear in whatever window you were using.

---

## How it works

```
Mic (parec) → silence detection → WAV → Groq Whisper → raw transcript
                                                              ↓
                              xdotool type ← Kilo.ai LLM ← punctuation + caps
```

One API key, zero local model downloads:

| Stage | Service | Model | Cost |
|---|---|---|---|
| Transcription | [Groq](https://console.groq.com) | `whisper-large-v3-turbo` | Free tier: 8 hrs audio/day |
| Formatting | built-in | — | Free, no key needed |

The formatting step adds punctuation and capitalisation but is explicitly instructed never to change, reorder, or remove a single word — it only cleans up the transcript.

---

## Install

```bash
git clone https://github.com/giardinodiroa-dev/whisper-dictation-tray
cd whisper-dictation-tray
chmod +x install.sh
./install.sh
```

The installer will:
- Check dependencies (`PyQt5`, `parec`, `xdotool`)
- Copy the script to `~/bin/voice`
- Walk you through getting a free Groq API key
- Add a `~/.config/autostart` entry so it starts on login
- Add a `voice` shell alias for quick restarts
- Optionally launch it immediately

---

## Manual setup

**1. Install system dependencies**

```bash
# Arch
sudo pacman -S python-pyqt5 libpulse xdotool

# Ubuntu / Debian
sudo apt install python3-pyqt5 pulseaudio-utils xdotool
```

**2. Get a free Groq API key**

Sign up at [console.groq.com](https://console.groq.com) → API Keys → Create key.
Free tier: 2,000 requests/day, 8 hours of audio/day.

**3. Create `~/bin/.env`**

```bash
cp .env.example ~/bin/.env
chmod 600 ~/bin/.env
```

Edit `~/bin/.env`:
```
GROQ_API_KEY=gsk_xxxxxxxxxxxxxxxxxxxxxxxx
```

**4. Install and autostart**

```bash
cp dictation-tray.py ~/bin/voice
chmod +x ~/bin/voice

# Autostart on login
mkdir -p ~/.config/autostart
cat > ~/.config/autostart/voice-dictation.desktop <<EOF
[Desktop Entry]
Type=Application
Name=Voice Dictation
Exec=$HOME/bin/voice
Icon=audio-input-microphone
X-GNOME-Autostart-enabled=true
EOF
```

**5. Run**

```bash
voice   # alias: kills any existing instance and restarts
```

---

## Usage

| Action | Result |
|---|---|
| Left-click tray icon | Toggle listening on/off |
| Right-click → **AI Formatting** | Toggle punctuation pass (on by default) |
| Right-click → **Show History** | Session history with copy buttons |
| Right-click → **Quit** | Exit |

**Tray icon colours:**
- Grey — idle
- Red — listening (live audio level meter shown at bottom of screen)
- Blue — transcribing / formatting

---

## Tuning

| Constant | Default | What to change it to |
|---|---|---|
| `SILENCE_SECS` | `2` | Lower for snappier response, raise if it cuts you off |
| `SILENCE_THRESHOLD` | `400` | Raise if background noise triggers recording |
| `MIN_SPEECH_RMS` | `1500` | Raise if muted-mic static still produces transcriptions |
| `MAX_RECORD_SECS` | `60` | Hard cap on a single session |
| `GROQ_MODEL` | `whisper-large-v3-turbo` | Swap to `whisper-large-v3` for maximum accuracy |

---

## Requirements

- Linux with PulseAudio or PipeWire (PipeWire's PulseAudio compat layer works)
- X11 (xdotool for typing) — Wayland support via `ydotool` is possible but not wired up yet
- Python 3.8+
- PyQt5

---

## License

MIT — free to use, modify, and distribute.
