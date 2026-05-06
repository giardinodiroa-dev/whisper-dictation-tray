# whisper-dictation-tray

A Linux system tray dictation tool powered by **Groq Whisper** (`whisper-large-v3-turbo`) for near-perfect speech-to-text accuracy, with optional AI punctuation formatting via Kilo.ai.

Click the tray icon to start listening. Speak. It stops automatically after 2 seconds of silence, transcribes your audio, and types the result into whatever window was focused before you started.

![idle](https://img.shields.io/badge/state-idle-gray) ![recording](https://img.shields.io/badge/state-recording-red) ![thinking](https://img.shields.io/badge/state-transcribing-blue)

---

## Features

- **Groq Whisper** — cloud STT with ~300ms latency, far better accuracy than local VOSK
- **Live audio meter** — subtitle overlay shows your audio level while speaking
- **AI formatting** — optional pass through Kilo.ai to add punctuation and fix capitalisation (free, no key needed)
- **Dictation history** — panel showing all sessions with copy buttons
- **Static rejection** — peak RMS gate prevents hardware mute static from triggering false transcriptions
- **No nerd-dictation dependency** — records via `parec` (PulseAudio) directly

---

## Requirements

**System packages**
```bash
# Arch
sudo pacman -S python-pyqt5 pulseaudio-utils xdotool

# Ubuntu / Debian
sudo apt install python3-pyqt5 pulseaudio-utils xdotool
```

**Python** — stdlib only, no pip installs needed.

---

## Setup

**1. Get a free Groq API key**

Sign up at [console.groq.com](https://console.groq.com) → API Keys → Create key. The free tier gives you 2,000 requests/day and 8 hours of audio/day — plenty for daily dictation.

**2. Create your `.env` file**

```bash
cp .env.example .env
```

Edit `.env` and paste your key:

```
GROQ_API_KEY=gsk_xxxxxxxxxxxxxxxxxxxxxxxx
```

The script loads this file at startup. Keep `.env` out of version control — it's in `.gitignore`.

**3. Place the script and run it**

```bash
cp dictation-tray.py ~/bin/
chmod +x ~/bin/dictation-tray.py
cp .env ~/bin/.env          # script looks for .env next to itself in ~/bin/
python3 ~/bin/dictation-tray.py &
```

Or add a shell alias for easy restart:

```bash
alias voice='pkill -f dictation-tray.py 2>/dev/null; nohup python3 ~/bin/dictation-tray.py &>/dev/null &'
```

---

## Usage

| Action | Result |
|---|---|
| **Left-click** tray icon | Toggle dictation on/off |
| Right-click → **AI Formatting** | Toggle punctuation pass (on by default) |
| Right-click → **Show History** | Open session history panel |
| Right-click → **Quit** | Exit |

**Tray icon colours:**
- Grey — idle
- Red — listening
- Blue — transcribing / formatting

---

## Tuning

All tunable constants are at the top of the script:

| Constant | Default | Purpose |
|---|---|---|
| `SILENCE_SECS` | `2` | Seconds of silence before stopping recording |
| `SILENCE_THRESHOLD` | `400` | RMS level that counts as "not silence" |
| `MIN_SPEECH_RMS` | `1500` | Peak RMS required to send audio to Groq — raise this if muted-mic static still triggers transcription |
| `MAX_RECORD_SECS` | `60` | Hard cap on a single recording session |
| `GROQ_MODEL` | `whisper-large-v3-turbo` | Swap to `whisper-large-v3` for maximum accuracy |

---

## How it works

```
parec (PulseAudio) → silence detection → WAV file → Groq Whisper API
                                                           ↓
                              xdotool type ← Kilo.ai format ← transcription
```

1. `parec` streams raw 16kHz mono PCM from the default PulseAudio source
2. RMS is computed per chunk to detect speech vs silence
3. When silence exceeds `SILENCE_SECS`, recording stops
4. If peak RMS clears `MIN_SPEECH_RMS`, the WAV is sent to Groq
5. The transcription is optionally formatted then typed into the previously focused window

---

## License

MIT
