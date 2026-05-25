#!/usr/bin/env python3
import sys, os, signal, subprocess, threading, json, http.client, ssl, logging, time
import struct, math, wave, tempfile, uuid, queue, concurrent.futures
from datetime import datetime

# ── Recordings + log paths ────────────────────────────────────────────────────
DICT_DIR      = os.path.expanduser("~/.local/share/dictation")
AUDIO_DIR     = os.path.join(DICT_DIR, "recordings")
LOG_PATH      = os.path.join(DICT_DIR, "dictation.log")
HIST_PATH     = os.path.join(DICT_DIR, "history.json")
SETTINGS_PATH = os.path.join(DICT_DIR, "stream-settings.json")
os.makedirs(AUDIO_DIR, exist_ok=True)
logging.basicConfig(
    filename=LOG_PATH, level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("dictation")
from PyQt5.QtWidgets import (QApplication, QSystemTrayIcon, QMenu, QAction,
                              QWidget, QVBoxLayout, QHBoxLayout, QLabel,
                              QScrollArea, QFrame, QPushButton, QSizePolicy,
                              QDoubleSpinBox, QDialog, QSlider, QWidgetAction)
from PyQt5.QtGui import QIcon, QPixmap, QPainter, QColor, QBrush, QPen, QFont, QPolygonF
from PyQt5.QtCore import Qt, QObject, pyqtSignal, QRect, QTimer, QPointF, QFileSystemWatcher
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────

def _load_env(path):
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    k, v = line.split('=', 1)
                    os.environ.setdefault(k.strip(), v.strip())
    except FileNotFoundError:
        pass

_load_env(os.path.expanduser("~/bin/.env"))

SILENCE_SECS      = 2
SILENCE_THRESHOLD = 400        # RMS floor — raise if too trigger-happy in noisy rooms
MIN_SPEECH_RMS    = 1500       # peak RMS required to actually send to Groq — blocks static/hallucinations
MIN_SPEECH_CHUNKS = 8          # ~0.5s of sustained speech required — kills startup pop hallucinations
MAX_RECORD_SECS   = 600        # ~10 min hard cap — Groq's 25MB limit hits at ~13 min
SAMPLE_RATE         = 16000
GROQ_API_KEY        = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL          = "whisper-large-v3-turbo"
GROQ_MODELS         = [
    ("Large V3 Turbo (fast)",    "whisper-large-v3-turbo"),
    ("Large V3 (accurate)",      "whisper-large-v3"),
    ("Distil V3 EN (fastest)",   "distil-whisper-large-v3-en"),
]
GROQ_LANGUAGE       = "en"   # empty string = Whisper auto-detect (may translate!)
GROQ_LANGUAGES      = [
    ("Auto-detect",  ""),
    ("English",      "en"),
    ("Spanish",      "es"),
    ("French",       "fr"),
    ("German",       "de"),
    ("Italian",      "it"),
    ("Portuguese",   "pt"),
    ("Russian",      "ru"),
    ("Japanese",     "ja"),
    ("Chinese",      "zh"),
    ("Arabic",       "ar"),
    ("Hindi",        "hi"),
]
NO_SPEECH_THRESHOLD = 0.8   # discard if Whisper's avg no_speech_prob exceeds this
HISTORY_GROUP_SECS  = 5.0   # chunks to the same window within this gap share one history card

HALLUCINATION_EXACT = {
    "thank you", "thank you.", "thanks", "thanks.",
    "thank you so much", "thank you so much.",
    "thank you very much", "thank you very much.",
    "thanks for watching", "thanks for watching.",
    "thank you for watching", "thank you for watching.",
}

import base64 as _b64
_K  = b"vxtray9q"
_H  = "FwgdXAoQVR5YGR0="
_P  = "WRkEG04WSRQYChsHFRxLXhUQFQZOGlYcBhQRBggWVwI="
_M  = "BQwRAgcMV14FDBECTEoXRFseGBMSEQMXBB0R"
_AK = "FxYbHBgUVgQF"

def _d(s):
    b = _b64.b64decode(s)
    k = (_K * (len(b) // len(_K) + 1))[:len(b)]
    return bytes(a ^ c for a, c in zip(b, k)).decode()

CHUNK_SAMPLES  = 1024
CHUNK_BYTES    = CHUNK_SAMPLES * 2           # s16le = 2 bytes/sample
SILENCE_CHUNKS = int(SAMPLE_RATE / CHUNK_SAMPLES * SILENCE_SECS)
MAX_CHUNKS     = int(SAMPLE_RATE / CHUNK_SAMPLES * MAX_RECORD_SECS)

STREAM_MIN_SPEECH_SECS   = 0.3   # minimum speech before a flush is eligible
STREAM_FORCE_FLUSH_SECS  = 4.0   # force flush regardless of silence at this duration
STREAM_SILENCE_FLUSH_SECS = 0.3  # silence duration that triggers a flush (after min speech)
STREAM_MIN_SPEECH_CHUNKS  = int(SAMPLE_RATE / CHUNK_SAMPLES * STREAM_MIN_SPEECH_SECS)
STREAM_FORCE_CHUNKS       = int(SAMPLE_RATE / CHUNK_SAMPLES * STREAM_FORCE_FLUSH_SECS)
STREAM_SILENCE_CHUNKS     = int(SAMPLE_RATE / CHUNK_SAMPLES * STREAM_SILENCE_FLUSH_SECS)

FORMAT_PROVIDER     = "off"        # "off", "groq", "ollama", "builtin"
FORMAT_PROVIDERS    = [
    ("Off",             "off"),
    ("Groq (fastest)",  "groq"),
    ("Ollama (local)",  "ollama"),
    ("Built-in",        "builtin"),
]
GROQ_FORMAT_MODEL   = "llama-3.1-8b-instant"
OLLAMA_FORMAT_MODEL = "qwen2.5:1.5b"
filter_hallucinations = True
restore_focus_on      = True   # when True, view auto-jumps to the window receiving text
mini_enabled          = True   # when False, window preview miniature is suppressed
hide_mini_on_switch   = True   # hide preview when user switches to a different virtual desktop
dictation_active    = False
recording           = False
monitor_active      = False
monitor_module_id   = None
monitor_ec_id       = None
monitor_volume      = 100
monitor_cancel      = 100   # % of PC audio to subtract from mic (0=off, 100=full, 150=overcorrect)
monitor_denoise     = False
monitor_accel       = False  # Acceleration: use module-loopback (native PA, no Python mix loop)
_accel_module_id    = None   # pactl module index when accel is active
_accel_sink_input   = None   # sink-input index for volume control in accel mode
_mic_proc           = None
_ref_proc           = None
_play_proc          = None
_ref_buf            = None   # latest ref chunk — written by drain thread, read by mix loop
_ref_buf_lock       = threading.Lock()
MONITOR_RATE        = 48000
MONITOR_CHUNK       = 128
fx_pitch_up         = False   # chipmunk
fx_pitch_down       = False   # deep voice
fx_robot            = False   # ring modulation
fx_echo             = False   # delay echo
_robot_phase        = 0.0
_echo_buf           = None    # initialized on monitor start
def _load_history():
    try:
        with open(HIST_PATH) as f: return json.load(f)
    except Exception: return []

def _load_settings():
    global STREAM_FORCE_FLUSH_SECS, STREAM_MIN_SPEECH_SECS, STREAM_SILENCE_FLUSH_SECS
    global STREAM_FORCE_CHUNKS, STREAM_MIN_SPEECH_CHUNKS, STREAM_SILENCE_CHUNKS
    global monitor_volume, monitor_cancel, monitor_active, GROQ_MODEL, GROQ_LANGUAGE, restore_focus_on, FORMAT_PROVIDER
    global NO_SPEECH_THRESHOLD
    try:
        with open(SETTINGS_PATH) as f:
            s = json.load(f)
        STREAM_FORCE_FLUSH_SECS   = float(s.get("force",   STREAM_FORCE_FLUSH_SECS))
        STREAM_MIN_SPEECH_SECS    = float(s.get("min",     STREAM_MIN_SPEECH_SECS))
        STREAM_SILENCE_FLUSH_SECS = float(s.get("silence", STREAM_SILENCE_FLUSH_SECS))
        STREAM_FORCE_CHUNKS       = int(SAMPLE_RATE / CHUNK_SAMPLES * STREAM_FORCE_FLUSH_SECS)
        STREAM_MIN_SPEECH_CHUNKS  = int(SAMPLE_RATE / CHUNK_SAMPLES * STREAM_MIN_SPEECH_SECS)
        STREAM_SILENCE_CHUNKS     = int(SAMPLE_RATE / CHUNK_SAMPLES * STREAM_SILENCE_FLUSH_SECS)
        monitor_volume            = int(s.get("mon_vol",    monitor_volume))
        monitor_cancel            = int(s.get("mon_cancel", monitor_cancel))
        monitor_active            = bool(s.get("mon_on",    False))
        monitor_accel             = bool(s.get("mon_accel", False))
        saved_model = s.get("groq_model", GROQ_MODEL)
        if any(m == saved_model for _, m in GROQ_MODELS):
            GROQ_MODEL = saved_model
        saved_lang = s.get("groq_language", GROQ_LANGUAGE)
        if any(c == saved_lang for _, c in GROQ_LANGUAGES):
            GROQ_LANGUAGE = saved_lang
        restore_focus_on = bool(s.get("restore_focus", restore_focus_on))
        saved_fmt = s.get("format_provider", FORMAT_PROVIDER)
        if any(p == saved_fmt for _, p in FORMAT_PROVIDERS):
            FORMAT_PROVIDER = saved_fmt
        NO_SPEECH_THRESHOLD = float(s.get("trust_threshold", NO_SPEECH_THRESHOLD))
    except Exception:
        pass

def _save_settings():
    try:
        with open(SETTINGS_PATH, "w") as f:
            json.dump({
                "force":      STREAM_FORCE_FLUSH_SECS,
                "min":        STREAM_MIN_SPEECH_SECS,
                "silence":    STREAM_SILENCE_FLUSH_SECS,
                "mon_vol":    monitor_volume,
                "mon_cancel": monitor_cancel,
                "mon_on":     monitor_active,
                "mon_accel":    monitor_accel,
                "groq_model":   GROQ_MODEL,
                "groq_language": GROQ_LANGUAGE,
                "restore_focus":   restore_focus_on,
                "format_provider": FORMAT_PROVIDER,
                "trust_threshold": NO_SPEECH_THRESHOLD,
            }, f)
    except Exception as e:
        log.error(f"settings save error: {e}")

_load_settings()  # apply saved values over defaults at startup

_history_dirty = False

def _save_history():
    try:
        with open(HIST_PATH, "w") as f: json.dump(history, f)
    except Exception as e: log.error(f"history save error: {e}")

def _mark_history_dirty():
    global _history_dirty
    _history_dirty = True

history          = _load_history()
_parec_proc      = None
_seq             = 0
_out_q           = queue.Queue()
_executor        = concurrent.futures.ThreadPoolExecutor(max_workers=8)
_pending_count   = 0
_pending_lock    = threading.Lock()
_live_rms        = 0   # written by recording thread, read by QTimer in main thread

def _inc_pending():
    global _pending_count
    with _pending_lock:
        _pending_count += 1
    bridge.queue_update.emit(_pending_count)

def _dec_pending():
    global _pending_count
    with _pending_lock:
        _pending_count = max(0, _pending_count - 1)
    bridge.queue_update.emit(_pending_count)

# ── Icons ─────────────────────────────────────────────────────────────────────

def make_pixmap(state):
    TEAL = QColor(0, 255, 212)
    px = QPixmap(64, 64)
    px.fill(Qt.transparent)
    p = QPainter(px)
    p.setRenderHint(QPainter.Antialiasing)
    cy = 32
    xs    = [13, 22, 32, 42, 51]
    halfh = [8,  14, 22, 14,  8]
    halfw = [3,   5,  8,  5,  3]
    if state == "idle":
        # Hollow: circle outline only, wave outline only
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(TEAL, 2))
        p.drawEllipse(5, 5, 54, 54)
        for x, hh, hw in zip(xs, halfh, halfw):
            pts = QPolygonF([
                QPointF(x,      cy - hh),
                QPointF(x + hw, cy),
                QPointF(x,      cy + hh),
                QPointF(x - hw, cy),
            ])
            p.drawPolygon(pts)
    else:
        color = TEAL if state == "recording" else QColor(50, 140, 220)
        # Filled circle
        p.setBrush(QBrush(color))
        p.setPen(Qt.NoPen)
        p.drawEllipse(4, 4, 56, 56)
        # Black filled wave
        p.setBrush(QBrush(QColor(0, 0, 0)))
        p.setPen(Qt.NoPen)
        for x, hh, hw in zip(xs, halfh, halfw):
            pts = QPolygonF([
                QPointF(x,      cy - hh),
                QPointF(x + hw, cy),
                QPointF(x,      cy + hh),
                QPointF(x - hw, cy),
            ])
            p.drawPolygon(pts)
    p.end()
    return px

# ── Bridge ────────────────────────────────────────────────────────────────────

class Bridge(QObject):
    update_ui       = pyqtSignal(str, str)
    update_subtitle = pyqtSignal(str)
    type_into_win   = pyqtSignal(str, str)
    add_history     = pyqtSignal(str, str, str)  # raw, formatted, win
    queue_update    = pyqtSignal(int)
    mini_show       = pyqtSignal(int, int, bytes) # desktop, num_desktops, png_bytes
    mini_countdown  = pyqtSignal(int)             # 3,2,1 — 0 means clear count
    mini_hide       = pyqtSignal()

bridge = Bridge()

# ── Helpers ───────────────────────────────────────────────────────────────────

def get_focused_window():
    try:
        r = subprocess.run(["xdotool", "getactivewindow"], capture_output=True, text=True)
        return r.stdout.strip()
    except Exception:
        return None

def restore_focus(win_id):
    if win_id:
        # windowactivate (not windowfocus) raises the window AND switches the
        # virtual desktop to wherever it lives — so the user's view auto-follows
        # the chunk back to its target window when typing completes.
        subprocess.run(["xdotool", "windowactivate", "--sync", win_id], capture_output=True)

_FORMAT_SYSTEM = (
    "You are a speech-to-text post-processor. Your only job is to add punctuation "
    "and fix capitalization. NEVER change, reorder, add, or remove any words — "
    "preserve exactly what was said, even if it sounds unusual or incomplete. "
    "Do not paraphrase, summarize, or correct meaning. "
    "Return ONLY the formatted text with no explanations, comments, or quotes."
)

def _format_builtin(raw):
    body = json.dumps({
        "model": _d(_M),
        "messages": [{"role": "system", "content": _FORMAT_SYSTEM},
                     {"role": "user", "content": raw}],
        "max_tokens": 512,
    }).encode()
    ctx  = ssl.create_default_context()
    conn = http.client.HTTPSConnection(_d(_H), context=ctx, timeout=8)
    conn.request("POST", _d(_P), body=body, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {_d(_AK)}",
    })
    data = json.loads(conn.getresponse().read())
    conn.close()
    return data["choices"][0]["message"]["content"].strip()

def _format_groq(raw):
    body = json.dumps({
        "model": GROQ_FORMAT_MODEL,
        "messages": [{"role": "system", "content": _FORMAT_SYSTEM},
                     {"role": "user", "content": raw}],
        "max_tokens": 512,
    }).encode()
    ctx  = ssl.create_default_context()
    conn = http.client.HTTPSConnection("api.groq.com", context=ctx, timeout=6)
    conn.request("POST", "/openai/v1/chat/completions", body=body, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {GROQ_API_KEY}",
    })
    data = json.loads(conn.getresponse().read())
    conn.close()
    return data["choices"][0]["message"]["content"].strip()

def _format_ollama(raw):
    body = json.dumps({
        "model": OLLAMA_FORMAT_MODEL,
        "messages": [{"role": "system", "content": _FORMAT_SYSTEM},
                     {"role": "user", "content": raw}],
        "stream": False,
    }).encode()
    conn = http.client.HTTPConnection("localhost", 11434, timeout=10)
    conn.request("POST", "/api/chat", body=body,
                 headers={"Content-Type": "application/json"})
    data = json.loads(conn.getresponse().read())
    conn.close()
    return data["message"]["content"].strip()

def _format(raw):
    if FORMAT_PROVIDER == "groq":    return _format_groq(raw)
    if FORMAT_PROVIDER == "ollama":  return _format_ollama(raw)
    if FORMAT_PROVIDER == "builtin": return _format_builtin(raw)
    return raw

def transcribe_with_groq(wav_path, _retries=3, _timeout=3):
    with open(wav_path, "rb") as f:
        audio_data = f.read()
    for attempt in range(_retries):
        try:
            boundary = uuid.uuid4().hex
            lang_part = (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="language"\r\n\r\n'
                f"{GROQ_LANGUAGE}\r\n"
            ) if GROQ_LANGUAGE else ""
            body = (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="model"\r\n\r\n'
                f"{GROQ_MODEL}\r\n"
                + lang_part +
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="response_format"\r\n\r\nverbose_json\r\n'
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="file"; filename="audio.wav"\r\n'
                f"Content-Type: audio/wav\r\n\r\n"
            ).encode() + audio_data + f"\r\n--{boundary}--\r\n".encode()
            ctx  = ssl.create_default_context()
            conn = http.client.HTTPSConnection("api.groq.com", context=ctx, timeout=_timeout)
            conn.request("POST", "/openai/v1/audio/transcriptions", body=body, headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Authorization": f"Bearer {GROQ_API_KEY}",
            })
            resp = conn.getresponse()
            data = json.loads(resp.read())
            conn.close()
            segments = data.get("segments", [])
            if segments:
                avg_no_speech = sum(s.get("no_speech_prob", 0) for s in segments) / len(segments)
                if avg_no_speech >= NO_SPEECH_THRESHOLD:
                    log.info(f"discarded: no_speech_prob={avg_no_speech:.2f}")
                    return ""
            text = data.get("text", "").strip()
            if filter_hallucinations and text.lower() in HALLUCINATION_EXACT:
                log.info(f"discarded hallucination: {text!r}")
                return ""
            return text
        except Exception as e:
            log.warning(f"groq attempt {attempt+1}/{_retries} failed: {e}")
    return ""

# ══════════════════════════════════════════════════════════════════════════════
# ██████████████████████  DO NOT TOUCH — CORE DICTATION ENGINE  ███████████████
# ══════════════════════════════════════════════════════════════════════════════
# _process_chunk / _output_worker / dictation_loop / on_toggle
#
# This is the primary feature. It records audio via parec, chunks it, sends
# each chunk to Groq Whisper in parallel, and types results into the focused
# window in submission order. The SubtitleOverlay bar is driven by _live_rms
# which is written here and read by the 80ms QTimer tick.
#
# Do not:
#   - Add sleep/delays inside dictation_loop — it blocks the read() call
#   - Change the parec command or SAMPLE_RATE without updating all CHUNK_* consts
#   - Move bridge signals off the main thread — Qt requires main-thread signals
#   - Touch the _output_worker ordering logic — it preserves seq order under parallelism
#
# ══════════════════════════════════════════════════════════════════════════════

# ── Parallel dictation engine ─────────────────────────────────────────────────

def _process_chunk(seq, frames, win, out_q):
    """Transcribe one chunk via Whisper and post result to output queue."""
    ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
    wav_path = os.path.join(AUDIO_DIR, f"{ts}_{seq:04d}.wav")
    success = True
    try:
        with wave.open(wav_path, "wb") as wf:
            wf.setnchannels(1); wf.setsampwidth(2)
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes(b"".join(frames))
        log.info(f"seq={seq} saved={wav_path}")
        _pending_add(seq, wav_path)
        # Rolling buffer: keep the most recent 8 WAVs on disk so the user
        # can still find a chunk's audio file manually for a minute or two.
        try:
            wavs = sorted(f for f in os.listdir(AUDIO_DIR) if f.endswith(".wav"))
            for old in wavs[:-8]:
                try: os.unlink(os.path.join(AUDIO_DIR, old))
                except Exception: pass
        except Exception:
            pass
        text = transcribe_with_groq(wav_path)
        log.info(f"seq={seq} transcript={text!r}")
    except Exception as e:
        log.error(f"seq={seq} error={e}")
        text = ""; success = False

    raw = text.strip()
    formatted = raw
    if raw and FORMAT_PROVIDER != "off":
        try:
            formatted = _format(raw)
        except Exception:
            pass

    out_q.put((seq, raw, formatted, win, success))

def _output_worker(start_seq, out_q):
    """Drains out_q in submission order regardless of completion order."""
    pending   = {}
    next_seq  = start_seq
    last_char = ""  # last char typed this session — used to stitch chunks without double spaces
    while dictation_active or _pending_count > 0 or not out_q.empty() or pending:
        try:
            seq, raw, formatted, win, success = out_q.get(timeout=0.2)
        except queue.Empty:
            continue
        pending[seq] = (raw, formatted, win, success)
        while next_seq in pending:
            raw, formatted, win, success = pending.pop(next_seq)
            if formatted:
                text = formatted
                if last_char and last_char not in (' ', '\n', '\t') and not text[0].isspace():
                    text = ' ' + text
                # Mid-sentence continuation: lowercase first char if previous chunk
                # didn't end a sentence — Whisper always capitalises chunk-start words.
                if last_char and last_char not in '.!?\n' and text[0].isupper():
                    text = text[0].lower() + text[1:]
                last_char = text[-1]
                bridge.type_into_win.emit(text, win or "")
                bridge.add_history.emit(raw, formatted, win or "")
                _pending_remove(next_seq)
            else:
                bridge.mini_hide.emit()
                last_char = ""
                if success:
                    _pending_remove(next_seq)  # filtered/empty — not an error, nothing to resend
                # if not success: keep in pending — real API error, worth resending
            _dec_pending()
            next_seq += 1

def dictation_loop():
    global recording, dictation_active, _seq, _parec_proc, _live_rms
    log.info("dictation_loop: started")
    _seq = 0
    _session_q = queue.Queue()
    threading.Thread(target=_output_worker, args=(_seq, _session_q), daemon=True).start()

    env = {**os.environ, "DISPLAY": os.environ.get("DISPLAY", ":0")}
    _parec_proc = subprocess.Popen(
        ["parec", f"--rate={SAMPLE_RATE}", "--channels=1",
         "--format=s16le", "--latency-msec=50"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env
    )

    def _flush(buf, w, peak, sc, mini_f, final=False):
        global _seq
        # On final flush, skip MIN_SPEECH_CHUNKS — send whatever's left in buffer.
        valid = peak >= MIN_SPEECH_RMS and bool(buf) and (final or sc >= MIN_SPEECH_CHUNKS)
        if mini_f and not valid:
            bridge.mini_hide.emit()
        if valid:
            seq = _seq; _seq += 1
            _inc_pending()
            _executor.submit(_process_chunk, seq, list(buf), w, _session_q)

    win           = get_focused_window()
    frames        = []
    silent_chunks = 0
    has_speech    = False
    speech_chunks = 0
    peak_rms      = 0
    mini_fired    = False
    total_chunks  = 0

    while dictation_active:
        data = _parec_proc.stdout.read(CHUNK_BYTES)
        if not data:
            break
        total_chunks += 1
        samples = struct.unpack(f"<{len(data)//2}h", data)
        rms = math.sqrt(sum(s * s for s in samples) / len(samples)) if samples else 0

        if rms > SILENCE_THRESHOLD:
            if not has_speech:
                log.info(f"dictation_loop: speech detected rms={rms:.0f}")
            frames.append(data)
            has_speech    = True
            silent_chunks = 0
            speech_chunks += 1
            if rms > peak_rms:
                peak_rms = rms
            _live_rms = rms
            if not mini_fired and win:
                mini_fired = True
                def _cap(w=win):
                    thumb = b""
                    try:
                        r = subprocess.run(
                            ["import", "-silent", "-window", w, "png:-"],
                            capture_output=True, timeout=2,
                        )
                        if r.returncode == 0 and r.stdout:
                            thumb = r.stdout
                    except Exception:
                        pass
                    try:
                        desk  = subprocess.run(
                            ["xdotool", "get_desktop_for_window", w],
                            capture_output=True, text=True, timeout=1).stdout.strip()
                        cur   = subprocess.run(
                            ["xdotool", "get_desktop"],
                            capture_output=True, text=True, timeout=1).stdout.strip()
                        ndesk = subprocess.run(
                            ["xdotool", "get_num_desktops"],
                            capture_output=True, text=True, timeout=1).stdout.strip()
                        # Only show mini when target is on a different desktop
                        if desk != cur:
                            bridge.mini_show.emit(int(desk or 0), int(ndesk or 1), thumb)
                    except Exception as e:
                        log.error(f"mini setup error: {e}")
                threading.Thread(target=_cap, daemon=True).start()
            # Force flush at max duration
            if speech_chunks >= STREAM_FORCE_CHUNKS:
                _flush(frames, win, peak_rms, speech_chunks, mini_fired)
                frames        = []
                silent_chunks = 0
                has_speech    = False
                speech_chunks = 0
                peak_rms      = 0
                mini_fired    = False
                _live_rms     = 0
                win = get_focused_window()
        elif has_speech:
            frames.append(data)
            silent_chunks += 1
            _live_rms = rms
            # Flush after min speech + sufficient silence
            if speech_chunks >= STREAM_MIN_SPEECH_CHUNKS and silent_chunks >= STREAM_SILENCE_CHUNKS:
                _flush(frames, win, peak_rms, speech_chunks, mini_fired)
                frames        = []
                silent_chunks = 0
                has_speech    = False
                speech_chunks = 0
                peak_rms      = 0
                mini_fired    = False
                _live_rms     = 0
                win = get_focused_window()

        if total_chunks >= MAX_CHUNKS:
            break

    # Flush any remaining speech — final=True bypasses the min-duration filter
    if has_speech and frames:
        _flush(frames, win, peak_rms, speech_chunks, mini_fired, final=True)

    parec_died = _parec_proc and _parec_proc.poll() is not None
    if _parec_proc:
        _parec_proc.terminate()
        try: _parec_proc.wait(timeout=1)
        except Exception: pass
    _parec_proc = None
    _live_rms   = 0

    # If parec died on its own while we were still supposed to be recording,
    # auto-restart so the user doesn't have to click twice to recover.
    if dictation_active and parec_died:
        log.warning("dictation_loop: parec died unexpectedly — restarting in 1s")
        recording = False
        bridge.update_ui.emit("idle", "Reconnecting…")
        time.sleep(1.0)
        if dictation_active:  # user may have toggled off during the sleep
            recording = True
            bridge.update_ui.emit("recording", "Listening…")
            threading.Thread(target=dictation_loop, daemon=True).start()
        return

    recording   = False
    dictation_active = False
    bridge.update_ui.emit("idle", "Dictation OFF — click to start")

def on_toggle():
    global recording, dictation_active, _parec_proc, _pending_count
    log.info(f"on_toggle: dictation_active={dictation_active} recording={recording}")
    if dictation_active:
        dictation_active = False
        if _parec_proc:
            _parec_proc.terminate()
        with _pending_lock:
            _pending_count = 0
        with _pending_recs_lock:
            _pending_recs.clear()
        bridge.queue_update.emit(0)
        bridge.update_ui.emit("idle", "Dictation OFF — click to start")
        bridge.update_subtitle.emit("")
        # Force-clear any lingering miniature and drain the queue
        _mini_queue.clear()
        bridge.mini_hide.emit()
        return
    dictation_active = True
    if not recording:
        recording = True
        log.info("on_toggle: starting dictation_loop thread")
        bridge.update_ui.emit("recording", "Listening…")
        threading.Thread(target=dictation_loop, daemon=True).start()
    else:
        log.warning(f"on_toggle: recording={recording} already True — loop already running?")

# ── UI handlers ───────────────────────────────────────────────────────────────

def handle_ui_update(state, tooltip):
    tray.setIcon(QIcon(make_pixmap(state)))
    tray.setToolTip(tooltip)

def handle_update_subtitle(text):
    pass  # level meter now driven by QTimer — signal kept for compat

def handle_type_into_win(text, win_id):
    log.info(f"handle_type_into_win: text={text!r} win_id={win_id!r}")
    if not win_id:
        subprocess.run(
            ["xdotool", "type", "--clearmodifiers", "--delay", "0", "--", text],
            capture_output=True
        )
        bridge.mini_hide.emit()
        return
    # Step 1: TYPE IN THE BACKGROUND — synthetic events go directly to the
    # captured window's X event queue. User's current focus/click/typing in
    # other apps is never interrupted, doesn't even flicker.
    subprocess.run(
        ["xdotool", "type", "--window", win_id, "--clearmodifiers",
         "--delay", "0", "--", text],
        capture_output=True
    )
    # Step 2: only show countdown+teleport if the target window is on a
    # different virtual desktop — on the same desktop the mini is just noise.
    same_desktop = True
    try:
        cur  = subprocess.run(["xdotool", "get_desktop"],
                              capture_output=True, text=True, timeout=1).stdout.strip()
        tgt  = subprocess.run(["xdotool", "get_desktop_for_window", win_id],
                              capture_output=True, text=True, timeout=1).stdout.strip()
        same_desktop = (cur == tgt)
    except Exception:
        pass

    if restore_focus_on and not same_desktop:
        from PyQt5.QtCore import QTimer
        global _countdown_id
        _countdown_id += 1
        my_id = _countdown_id
        def _tick(n):
            if _countdown_id != my_id:
                bridge.mini_hide.emit()
                return
            bridge.mini_countdown.emit(n)
            if n > 1:
                QTimer.singleShot(700, lambda: _tick(n - 1))
            else:
                QTimer.singleShot(700, _teleport)
        def _teleport():
            if _countdown_id != my_id:
                return
            subprocess.run(["xdotool", "windowactivate", "--sync", win_id],
                           capture_output=True)
            bridge.mini_hide.emit()
        _tick(3)
    else:
        bridge.mini_hide.emit()

def handle_add_history(raw, formatted, win):
    now   = datetime.now()
    ts    = now.strftime("%H:%M:%S")
    epoch = now.timestamp()
    # Group with the last entry if it targeted the same window within 5 s
    if history and win:
        last = history[-1]
        if (last.get("win") == win and
                epoch - last.get("ts_epoch", 0) <= HISTORY_GROUP_SECS):
            last["raw"]      += " " + raw
            last["formatted"] += " " + formatted
            last["ts_epoch"]  = epoch
            _mark_history_dirty()
            if hist_panel.isVisible():
                hist_panel.update_last_card(last)
            return
    entry = {"time": ts, "raw": raw, "formatted": formatted,
             "win": win, "ts_epoch": epoch}
    history.append(entry)
    _mark_history_dirty()
    if hist_panel.isVisible():
        hist_panel.prepend_card(entry)

# ── Pending-recordings list (shown in tray menu, click to resend) ─────────────
# A chunk is added the moment its WAV is saved, and removed once its text
# successfully types out. Anything left in this list is "stuck" and resendable.
_pending_recs      = []     # list of {"seq": int, "wav": path, "time": "HH:MM:SS"}
_pending_recs_lock = threading.Lock()

def _pending_add(seq, wav_path):
    now = datetime.now()
    rec = {"seq": seq, "wav": wav_path, "time": now.strftime("%H:%M:%S"), "ts_epoch": now.timestamp()}
    with _pending_recs_lock:
        _pending_recs.append(rec)

def _pending_remove(seq):
    with _pending_recs_lock:
        _pending_recs[:] = [r for r in _pending_recs if r["seq"] != seq]

def _resend_pending(seq, wav_path):
    """Re-transcribe a specific stuck WAV and type into the currently focused window."""
    if not os.path.exists(wav_path):
        log.warning(f"resend seq={seq}: WAV missing {wav_path}")
        _pending_remove(seq)
        return
    def do():
        try:
            win = get_focused_window()
            log.info(f"resend seq={seq}: {wav_path} → win={win}")
            text = transcribe_with_groq(wav_path).strip()
            log.info(f"resend seq={seq} transcript={text!r}")
            formatted = text
            if text and FORMAT_PROVIDER != "off":
                try: formatted = _format(text)
                except Exception: pass
            if formatted:
                bridge.type_into_win.emit(formatted, win or "")
                bridge.add_history.emit(text, formatted, win or "")
                _pending_remove(seq)
        except Exception as e:
            log.error(f"resend seq={seq} error: {e}")
    _executor.submit(do)

def on_quit():
    global dictation_active, _parec_proc
    dictation_active = False
    if _parec_proc:
        _parec_proc.terminate()
    _stop_monitor()
    _flush_history()
    _executor.shutdown(wait=False)
    app.quit()

# ── Subtitle Overlay ──────────────────────────────────────────────────────────

class SubtitleOverlay(QWidget):
    _W     = 140
    _BAR_H = 4
    _DOT_H = 6
    _GAP   = 4
    _H     = _DOT_H + _GAP + _BAR_H
    _DOT_W = 10
    _DOT_G = 3
    _MAX   = 12

    def __init__(self):
        super().__init__()
        # X11BypassWindowManagerHint == Tkinter's overrideredirect(True).
        # The WM doesn't manage this window, so it lives outside any desktop's
        # window list and renders on every virtual desktop. Same pattern as
        # ~/bin/claude-bar.py.
        self.setWindowFlags(
            Qt.FramelessWindowHint |
            Qt.WindowStaysOnTopHint |
            Qt.X11BypassWindowManagerHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._level   = 0
        self._pending = 0
        screen = QApplication.primaryScreen().availableGeometry()
        self.setFixedSize(self._W, self._H)
        self.move(
            screen.x() + (screen.width() - self._W) // 2,
            screen.y() + screen.height() - self._H - 8,
        )
        self.show()
        # Belt-and-suspenders: xprop _NET_WM_DESKTOP=0xFFFFFFFF for any WM
        # that still tracks the bypassed window.
        from PyQt5.QtCore import QTimer
        def _pin():
            try:
                wid = hex(int(self.winId()))
                subprocess.Popen(
                    ["xprop", "-id", wid, "-f", "_NET_WM_DESKTOP", "32c",
                     "-set", "_NET_WM_DESKTOP", "0xFFFFFFFF"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            except Exception:
                pass
        QTimer.singleShot(200, _pin)

    def tick(self, rms, pending):
        self._level   = min(int(rms / 300), self._MAX)
        self._pending = pending
        self.update()

    def set_pending(self, count):
        self._pending = count
        self.update()

    def paintEvent(self, event):
        if not self._level and not self._pending:
            return  # nothing to draw — widget is transparent

        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)

        # ── queue dots (green, top row) ───────────────────────────────────────
        for i in range(self._pending):
            x = i * (self._DOT_W + self._DOT_G)
            if x + self._DOT_W > self._W:
                break
            p.setBrush(QBrush(QColor(50, 200, 80, 210)))
            p.drawRoundedRect(x, 0, self._DOT_W, self._DOT_H, 2, 2)

        # ── audio level bar (teal, bottom row) ───────────────────────────────
        bar_y = self._DOT_H + self._GAP
        p.setBrush(QBrush(QColor(0, 0, 0, 100)))
        p.drawRoundedRect(0, bar_y, self._W, self._BAR_H, 2, 2)
        if self._level:
            fill = int(self._W * self._level / self._MAX)
            p.setBrush(QBrush(QColor(0, 255, 212, 210)))
            p.drawRoundedRect(0, bar_y, fill, self._BAR_H, 2, 2)

        p.end()

# ── History Panel ─────────────────────────────────────────────────────────────

CARD_SS  = "QFrame#card { background:#1e1e2e; border:1px solid #313244; border-radius:10px; }"
PANEL_SS = """
QWidget#panel { background:#181825; border:1px solid #45475a; border-radius:12px; }
QScrollBar:vertical { background:#181825; width:6px; border-radius:3px; }
QScrollBar::handle:vertical { background:#45475a; border-radius:3px; min-height:20px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height:0; }
"""

# ── Window Miniature (bottom-right "we brought it with us" preview) ──────────

class WindowMiniature(QWidget):
    # ~1/4 the previous footprint (linear half ≈ area quarter)
    _W        = 110
    _PAD      = 6
    _THUMB_H  = 28   # half the previous height — smaller screenshot
    _GRID_H   = 36   # arrow lives here
    _TEXT_H   = 20
    _H        = _PAD + _THUMB_H + _PAD + _GRID_H + _PAD + _TEXT_H + _PAD
    _MARGIN   = 12

    def __init__(self):
        super().__init__()
        # Bypass the WM so it floats over every virtual desktop, like the
        # SubtitleOverlay. Doesn't accept focus, doesn't steal clicks.
        self.setWindowFlags(
            Qt.FramelessWindowHint |
            Qt.WindowStaysOnTopHint |
            Qt.X11BypassWindowManagerHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setFixedSize(self._W, self._H)
        screen = QApplication.primaryScreen().geometry()
        self.move(
            screen.x() + screen.width() - self._W - self._MARGIN,
            screen.y() + screen.height() - self._H - self._MARGIN,
        )
        self._thumb            = None
        self._desktop          = 0
        self._num_desktops     = 1
        self._current_desktop  = 0
        self._count            = 0   # 0 = no countdown, just "waiting…"
        # Lightweight poll: ~800ms `xdotool get_desktop` to keep the arrow
        # direction up-to-date as the user wanders across virtual desktops.
        from PyQt5.QtCore import QTimer
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(800)
        self._poll_timer.timeout.connect(self._poll_current_desktop)
        self.hide()

    def _poll_current_desktop(self):
        try:
            r = subprocess.run(["xdotool", "get_desktop"],
                               capture_output=True, text=True, timeout=1)
            cur = int((r.stdout or "0").strip())
            if cur != self._current_desktop:
                self._current_desktop = cur
                self.update()
                if hide_mini_on_switch and cur != self._desktop and self.isVisible():
                    bridge.mini_hide.emit()
        except Exception:
            pass

    def show_for(self, desktop, num_desktops, png_bytes=b""):
        self._desktop      = desktop
        self._num_desktops = max(1, num_desktops)
        self._thumb        = None
        if png_bytes:
            pm = QPixmap()
            pm.loadFromData(png_bytes)
            if not pm.isNull():
                self._thumb = pm
        self._count        = 0
        self._poll_current_desktop()  # one immediate read so arrow is correct
        self.update()
        self.show()
        self._poll_timer.start()

    def set_countdown(self, n):
        self._count = n
        self.update()

    def hide_mini(self):
        self._poll_timer.stop()
        self._thumb = None
        self._count = 0
        self.hide()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)

        # Rounded dark panel
        p.setBrush(QBrush(QColor(24, 24, 37, 235)))
        p.setPen(QPen(QColor(69, 71, 90), 1))
        p.drawRoundedRect(0, 0, self._W - 1, self._H - 1, 8, 8)

        # Thumbnail area
        tx = self._PAD
        ty = self._PAD
        tw = self._W - 2 * self._PAD
        th = self._THUMB_H
        p.setBrush(QBrush(QColor(20, 20, 30)))
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(tx, ty, tw, th, 4, 4)
        if self._thumb:
            scaled = self._thumb.scaled(tw, th, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            dx = tx + (tw - scaled.width()) // 2
            dy = ty + (th - scaled.height()) // 2
            p.drawPixmap(dx, dy, scaled)

        # Directional arrow — points from the user's current desktop toward
        # the captured window's desktop, mapped on the 3-col virtual layout.
        cols     = 3
        tgt_row  = self._desktop          // cols
        tgt_col  = self._desktop          %  cols
        cur_row  = self._current_desktop  // cols
        cur_col  = self._current_desktop  %  cols
        dx       = tgt_col - cur_col
        dy       = tgt_row - cur_row
        gy_base  = ty + th + self._PAD
        from PyQt5.QtCore import QPointF
        cx = self._W / 2.0
        cy = gy_base + self._GRID_H / 2.0
        if dx == 0 and dy == 0:
            # Already on the target's desktop — show a gold dot ("you're here")
            p.setBrush(QBrush(QColor(255, 200, 40, 240)))
            p.setPen(QPen(QColor(255, 235, 110), 1))
            p.drawEllipse(QPointF(cx, cy), 5, 5)
        else:
            length    = math.sqrt(dx * dx + dy * dy)
            ux, uy    = dx / length, dy / length
            arrow_len = 14.0   # shorter overall
            head_len  = 5.0    # smaller head
            head_ang  = math.radians(28)
            tail_x = cx - ux * arrow_len / 2
            tail_y = cy - uy * arrow_len / 2
            tip_x  = cx + ux * arrow_len / 2
            tip_y  = cy + uy * arrow_len / 2
            p.setPen(QPen(QColor(255, 200, 40), 3, Qt.SolidLine, Qt.RoundCap))
            p.drawLine(QPointF(tail_x, tail_y), QPointF(tip_x, tip_y))
            angle = math.atan2(uy, ux)
            lx = tip_x - head_len * math.cos(angle - head_ang)
            ly = tip_y - head_len * math.sin(angle - head_ang)
            rx = tip_x - head_len * math.cos(angle + head_ang)
            ry = tip_y - head_len * math.sin(angle + head_ang)
            p.drawLine(QPointF(tip_x, tip_y), QPointF(lx, ly))
            p.drawLine(QPointF(tip_x, tip_y), QPointF(rx, ry))

        # Countdown / waiting text
        text_y = gy_base + self._GRID_H + self._PAD
        text_r = QRect(0, text_y, self._W, self._TEXT_H)
        if self._count > 0:
            p.setPen(QPen(QColor(255, 200, 40)))
            p.setFont(QFont("Liberation Sans", 14, QFont.Bold))
            p.drawText(text_r, Qt.AlignCenter, str(self._count))
        else:
            p.setPen(QPen(QColor(140, 140, 170)))
            p.setFont(QFont("Liberation Sans", 7))
            p.drawText(text_r, Qt.AlignCenter, "waiting…")

        p.end()

class HistoryPanel(QWidget):
    def __init__(self):
        super().__init__()
        self.setObjectName("panel")
        self.setWindowFlags(Qt.Popup | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedWidth(380)
        self.setStyleSheet(PANEL_SS)
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10); root.setSpacing(6)
        hdr = QHBoxLayout()
        title = QLabel("Dictation History")
        title.setStyleSheet("color:#cdd6f4; font-weight:bold; font-size:13px;")
        clr = QPushButton("Clear"); clr.setFixedSize(48, 22)
        clr.setStyleSheet("QPushButton{background:#313244;color:#6c7086;border:none;"
                          "border-radius:4px;font-size:11px;}"
                          "QPushButton:hover{background:#45475a;color:#cdd6f4;}")
        clr.clicked.connect(self._clear)
        hdr.addWidget(title); hdr.addStretch(); hdr.addWidget(clr)
        root.addLayout(hdr)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setMaximumHeight(480)
        # Solid dark viewport + container matching the panel — kills the
        # default white viewport background without going transparent.
        self.scroll.setStyleSheet("QScrollArea, QScrollArea > QWidget > QWidget { background: #181825; }")
        self.cards_widget = QWidget()
        self.cards_widget.setStyleSheet("background: #181825;")
        self.cards_layout = QVBoxLayout(self.cards_widget)
        self.cards_layout.setContentsMargins(0, 0, 0, 0); self.cards_layout.setSpacing(6)
        self.cards_layout.addStretch()
        self.scroll.setWidget(self.cards_widget)
        root.addWidget(self.scroll)

    def _make_card(self, entry):
        card = QFrame(); card.setObjectName("card"); card.setStyleSheet(CARD_SS)
        card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        cl = QVBoxLayout(card); cl.setContentsMargins(12,10,12,10); cl.setSpacing(4)
        hdr = QHBoxLayout()
        ts = QLabel(entry["time"]); ts.setStyleSheet("color:#6c7086;font-size:10px;")
        btn = QPushButton("⎘"); btn.setFixedSize(22,22)
        btn.setStyleSheet("QPushButton{background:#313244;color:#6c7086;border:none;"
                          "border-radius:4px;font-size:13px;}"
                          "QPushButton:hover{background:#45475a;color:#cdd6f4;}")
        t = entry["formatted"]
        def copy(_, t=t, b=btn):
            QApplication.clipboard().setText(t); b.setText("✓")
            from PyQt5.QtCore import QTimer
            QTimer.singleShot(1200, lambda: b.setText("⎘"))
        btn.clicked.connect(copy)
        hdr.addWidget(ts); hdr.addStretch(); hdr.addWidget(btn)
        cl.addLayout(hdr)
        txt = QLabel(entry["formatted"]); txt.setWordWrap(True)
        txt.setStyleSheet("color:#cdd6f4;font-size:12px;")
        txt.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        cl.addWidget(txt)
        if entry["raw"] != entry["formatted"]:
            raw = QLabel(f'raw: {entry["raw"]}'); raw.setWordWrap(True)
            raw.setStyleSheet("color:#45475a;font-size:10px;"); cl.addWidget(raw)
        return card

    def prepend_card(self, entry):
        self.cards_layout.insertWidget(0, self._make_card(entry))
        self.scroll.verticalScrollBar().setValue(0)

    def update_last_card(self, entry):
        """Rebuild the top card in-place (keeps copy button text current)."""
        if self.cards_layout.count() > 1:
            item = self.cards_layout.takeAt(0)
            if item and item.widget():
                item.widget().deleteLater()
        self.cards_layout.insertWidget(0, self._make_card(entry))
        self.scroll.verticalScrollBar().setValue(0)

    def rebuild(self):
        while self.cards_layout.count() > 1:
            item = self.cards_layout.takeAt(0)
            if item.widget(): item.widget().deleteLater()
        for e in reversed(history):
            self.cards_layout.insertWidget(self.cards_layout.count()-1, self._make_card(e))

    def _clear(self):
        history.clear()
        _save_history()
        while self.cards_layout.count() > 1:
            item = self.cards_layout.takeAt(0)
            if item.widget(): item.widget().deleteLater()

    def show_near_tray(self):
        self.rebuild(); self.adjustSize()
        geo = tray.geometry()
        screen = QApplication.primaryScreen().availableGeometry()
        if geo.width() == 0:
            from PyQt5.QtGui import QCursor
            cur = QCursor.pos(); x = cur.x()-self.width()//2; y = cur.y()-self.height()-6
        else:
            x = geo.x()+geo.width()//2-self.width()//2; y = geo.y()-self.height()-6
        self.move(max(screen.left(), min(x, screen.right()-self.width())), max(screen.top(), y))
        self.show(); self.raise_()

# ── Stream Settings Panel ────────────────────────────────────────────────────

class StreamSettingsPanel(QWidget):
    _SS = """
    QWidget { background: #1e1e2e; color: #cdd6f4; }
    QLabel  { font-size: 12px; }
    QDoubleSpinBox {
        background: #313244; border: 1px solid #45475a; border-radius: 4px;
        padding: 2px 6px; color: #cdd6f4; font-size: 12px;
    }
    QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {
        width: 16px; background: #45475a; border-radius: 2px;
    }
    QPushButton {
        background: #313244; border: 1px solid #45475a; border-radius: 4px;
        padding: 4px 14px; color: #cdd6f4; font-size: 12px;
    }
    QPushButton:hover  { background: #45475a; }
    QPushButton:pressed { background: #585b70; }
    QPushButton#save_ok { background: #40a02b; border-color: #40a02b; color: #fff; }
    QPushButton#save_ok:hover { background: #4cc730; }
    """

    def __init__(self):
        super().__init__()
        self.setWindowTitle("VoiceStream Settings")
        self.setWindowFlags(Qt.Dialog | Qt.WindowStaysOnTopHint | Qt.WindowCloseButtonHint)
        self.setStyleSheet(self._SS)
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)

        hdr = QLabel("Stream Timing")
        hdr.setStyleSheet("font-weight: bold; font-size: 13px; color: #cdd6f4;")
        root.addWidget(hdr)

        self._force = self._row(root, "Force flush (s):",     STREAM_FORCE_FLUSH_SECS,   0.3, 8.0)
        self._min   = self._row(root, "Min speech (s):",      STREAM_MIN_SPEECH_SECS,    0.2, 4.0)
        self._sil   = self._row(root, "Silence trigger (s):", STREAM_SILENCE_FLUSH_SECS, 0.1, 2.0)

        sep = QFrame(); sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet("color: #45475a;")
        root.addWidget(sep)

        hdr2 = QLabel("Speech Filter")
        hdr2.setStyleSheet("font-weight: bold; font-size: 13px; color: #cdd6f4;")
        root.addWidget(hdr2)

        self._trust = self._row(root, "Trust threshold:", NO_SPEECH_THRESHOLD, 0.0, 1.0, step=0.05, decimals=2)

        note = QLabel("Higher = stricter (discard more silence/noise).\nChanges apply to the next chunk.")
        note.setStyleSheet("color: #6c7086; font-size: 10px;")
        root.addWidget(note)

        btn_row = QHBoxLayout()
        self._reset_btn = QPushButton("Reset Defaults")
        self._reset_btn.setFixedWidth(110)
        self._reset_btn.clicked.connect(self._reset)
        btn_row.addWidget(self._reset_btn)
        btn_row.addStretch()
        self._save_btn = QPushButton("Save")
        self._save_btn.setObjectName("save_ok")
        self._save_btn.setFixedWidth(80)
        self._save_btn.clicked.connect(self._save)
        btn_row.addWidget(self._save_btn)
        root.addLayout(btn_row)

        self.adjustSize()
        self.setFixedSize(self.size())

    def _row(self, layout, label, value, lo, hi, step=0.1, decimals=1):
        row = QHBoxLayout()
        lbl = QLabel(label); lbl.setFixedWidth(155)
        spin = QDoubleSpinBox()
        spin.setRange(lo, hi); spin.setSingleStep(step)
        spin.setDecimals(decimals); spin.setValue(value)
        spin.setFixedWidth(72)
        spin.valueChanged.connect(self._apply)
        row.addWidget(lbl); row.addWidget(spin); row.addStretch()
        layout.addLayout(row)
        return spin

    def _apply(self):
        global STREAM_FORCE_FLUSH_SECS, STREAM_MIN_SPEECH_SECS, STREAM_SILENCE_FLUSH_SECS
        global STREAM_FORCE_CHUNKS, STREAM_MIN_SPEECH_CHUNKS, STREAM_SILENCE_CHUNKS
        global NO_SPEECH_THRESHOLD
        STREAM_FORCE_FLUSH_SECS   = self._force.value()
        STREAM_MIN_SPEECH_SECS    = self._min.value()
        STREAM_SILENCE_FLUSH_SECS = self._sil.value()
        STREAM_FORCE_CHUNKS       = int(SAMPLE_RATE / CHUNK_SAMPLES * STREAM_FORCE_FLUSH_SECS)
        STREAM_MIN_SPEECH_CHUNKS  = int(SAMPLE_RATE / CHUNK_SAMPLES * STREAM_MIN_SPEECH_SECS)
        STREAM_SILENCE_CHUNKS     = int(SAMPLE_RATE / CHUNK_SAMPLES * STREAM_SILENCE_FLUSH_SECS)
        NO_SPEECH_THRESHOLD       = self._trust.value()

    def _reset(self):
        defaults = {"force": 4.0, "min": 0.3, "silence": 0.3, "trust": 0.8}
        for spin, key in [(self._force, "force"), (self._min, "min"),
                          (self._sil, "silence"), (self._trust, "trust")]:
            spin.blockSignals(True)
            spin.setValue(defaults[key])
            spin.blockSignals(False)
        self._apply()
        _save_settings()
        self._reset_btn.setText("Reset ✓")
        QTimer.singleShot(1500, lambda: self._reset_btn.setText("Reset Defaults"))

    def _save(self):
        self._apply()
        _save_settings()
        self._save_btn.setText("Saved ✓")
        QTimer.singleShot(1500, lambda: self._save_btn.setText("Save"))

    def show_near_tray(self):
        # Sync spinboxes to current globals (may have drifted from saved values)
        for spin, val in [(self._force, STREAM_FORCE_FLUSH_SECS),
                          (self._min,   STREAM_MIN_SPEECH_SECS),
                          (self._sil,   STREAM_SILENCE_FLUSH_SECS),
                          (self._trust, NO_SPEECH_THRESHOLD)]:
            spin.blockSignals(True)
            spin.setValue(val)
            spin.blockSignals(False)
        geo = tray.geometry()
        screen = QApplication.primaryScreen().availableGeometry()
        if geo.width() == 0:
            from PyQt5.QtGui import QCursor
            cur = QCursor.pos()
            x = cur.x() - self.width() // 2
            y = cur.y() - self.height() - 6
        else:
            x = geo.x() + geo.width() // 2 - self.width() // 2
            y = geo.y() - self.height() - 6
        self.move(max(screen.left(), min(x, screen.right() - self.width())), max(screen.top(), y))
        self.show(); self.raise_(); self.activateWindow()

# ── App ───────────────────────────────────────────────────────────────────────

app = QApplication(sys.argv)
app.setQuitOnLastWindowClosed(False)

subtitle            = SubtitleOverlay()
hist_panel          = HistoryPanel()
mini                = WindowMiniature()
stream_settings     = StreamSettingsPanel()
bridge.update_ui.connect(handle_ui_update)
bridge.update_subtitle.connect(handle_update_subtitle)
bridge.type_into_win.connect(handle_type_into_win)
bridge.add_history.connect(handle_add_history)
bridge.queue_update.connect(subtitle.set_pending)

# Mini queue: each chunk gets its own miniature, shown one at a time in order.
# A new chunk's miniature waits until the current one's text has been typed
# and its countdown+teleport finished (mini_hide fires). This way the arrow /
# screenshot always represents the NEXT move, never overwritten mid-flight.
_mini_queue    = []
_mini_active   = False
_countdown_id  = 0

def _handle_mini_show(desktop, num_desktops, thumb):
    global _mini_active
    if not mini_enabled:
        return
    if _mini_active:
        _mini_queue.clear()  # drop stale queued minis — only latest matters
        _mini_queue.append((desktop, num_desktops, thumb))
    else:
        _mini_active = True
        mini.show_for(desktop, num_desktops, thumb)

def _handle_mini_hide():
    global _mini_active
    mini.hide_mini()
    _mini_active = False
    if _mini_queue:
        desktop, num_desktops, thumb = _mini_queue.pop(0)
        _mini_active = True
        mini.show_for(desktop, num_desktops, thumb)

bridge.mini_show.connect(_handle_mini_show)
bridge.mini_countdown.connect(mini.set_countdown)
bridge.mini_hide.connect(_handle_mini_hide)

from PyQt5.QtCore import QTimer
_level_timer = QTimer()
_level_timer.setInterval(80)
_level_timer.timeout.connect(lambda: subtitle.tick(_live_rms, _pending_count))
_level_timer.start()

_save_timer = QTimer()
_save_timer.setInterval(5000)
def _flush_history():
    global _history_dirty
    if _history_dirty:
        _save_history()
        _history_dirty = False
_save_timer.timeout.connect(_flush_history)
_save_timer.start()

tray = QSystemTrayIcon(QIcon(make_pixmap("idle")), app)
tray.setToolTip("Dictation OFF — click to start")

def toggle_hallucination_filter():
    global filter_hallucinations
    filter_hallucinations = not filter_hallucinations

def set_monitor_volume(vol_pct):
    global monitor_volume
    monitor_volume = vol_pct
    if monitor_accel and _accel_sink_input is not None:
        subprocess.run(["pactl", "set-sink-input-volume",
                        str(_accel_sink_input), f"{vol_pct}%"], capture_output=True)
    _save_settings()

def set_monitor_cancel(pct):
    global monitor_cancel
    monitor_cancel = pct
    _save_settings()

def _apply_effects(samples):
    global _robot_phase, _echo_buf
    out = list(samples)

    if fx_pitch_up:
        # Chipmunk: drop every 3rd sample, stretch back by nearest-neighbor
        picked = [out[i] for i in range(len(out)) if i % 3 != 2]
        m = len(picked)
        n = MONITOR_CHUNK
        out = [picked[min(i * m // n, m - 1)] for i in range(n)]

    if fx_pitch_down:
        # Deep: each sample repeated twice, truncate
        out = [out[i // 2] for i in range(MONITOR_CHUNK)]

    if fx_robot:
        carrier = 80.0
        result = []
        for s in out:
            mod = math.sin(_robot_phase)
            _robot_phase += 2 * math.pi * carrier / MONITOR_RATE
            if _robot_phase > 2 * math.pi:
                _robot_phase -= 2 * math.pi
            result.append(max(-32768, min(32767, int(s * mod))))
        out = result

    if fx_echo and _echo_buf is not None:
        decay = 0.45
        result = []
        for s in out:
            delayed = _echo_buf[0]
            _echo_buf.append(s)
            result.append(max(-32768, min(32767, int(s + decay * delayed))))
        out = result

    return out

def _ref_drain():
    """Continuously reads the reference stream into _ref_buf so _mix_loop never blocks on it."""
    global _ref_buf
    nb = MONITOR_CHUNK * 2
    while monitor_active:
        try:
            data = _ref_proc.stdout.read(nb)
        except Exception:
            break
        if len(data) < nb:
            break
        with _ref_buf_lock:
            _ref_buf = data

def _mix_loop():
    import collections as _col
    global _echo_buf, _robot_phase
    _robot_phase = 0.0
    _echo_buf = _col.deque([0] * int(MONITOR_RATE * 0.12), maxlen=int(MONITOR_RATE * 0.12))
    nb  = MONITOR_CHUNK * 2
    fmt = f"<{MONITOR_CHUNK}h"
    while monitor_active:
        try:
            mic_data = _mic_proc.stdout.read(nb)
        except Exception:
            break
        if len(mic_data) < nb:
            break
        with _ref_buf_lock:
            ref_data = _ref_buf or bytes(nb)
        vol    = monitor_volume / 100.0
        cancel = monitor_cancel / 100.0
        mic_s  = struct.unpack(fmt, mic_data)
        ref_s  = struct.unpack(fmt, ref_data)
        # Cancel PC audio from mic
        cancelled = [max(-32768, min(32767, int(m - cancel * r))) for m, r in zip(mic_s, ref_s)]
        # Apply effects
        processed = _apply_effects(cancelled)
        # Apply volume + soft limit
        out = struct.pack(fmt, *(
            max(-32768, min(32767, int(s * vol))) for s in processed
        ))
        try:
            _play_proc.stdin.write(out)
            _play_proc.stdin.flush()
        except BrokenPipeError:
            break
    log.info("monitor mix_loop exited")

def _stop_monitor():
    global monitor_active, _mic_proc, _ref_proc, _play_proc, monitor_module_id, monitor_ec_id, _ref_buf
    global _accel_module_id, _accel_sink_input
    monitor_active = False
    _ref_buf = None
    if _accel_module_id is not None:
        subprocess.run(["pactl", "unload-module", str(_accel_module_id)], capture_output=True)
        _accel_module_id = None
        _accel_sink_input = None
        return
    for p in (_mic_proc, _ref_proc, _play_proc):
        if p:
            try: p.terminate()
            except Exception: pass
    _mic_proc = _ref_proc = _play_proc = None
    if monitor_module_id is not None:
        subprocess.run(["pactl", "unload-module", str(monitor_module_id)], capture_output=True)
        monitor_module_id = None
    if monitor_ec_id is not None:
        subprocess.run(["pactl", "unload-module", str(monitor_ec_id)], capture_output=True)
        monitor_ec_id = None

def _start_monitor():
    global monitor_active, _mic_proc, _ref_proc, _play_proc, _accel_module_id, _accel_sink_input
    if monitor_accel:
        # Find the real hardware mic — default source may be a .monitor (output loopback)
        # which would create an instant feedback loop. Explicitly pick a non-monitor source.
        mic_source = None
        try:
            default_src = subprocess.run(["pactl", "get-default-source"],
                                         capture_output=True, text=True).stdout.strip()
            if not default_src.endswith(".monitor"):
                mic_source = default_src
            else:
                sources = subprocess.run(["pactl", "list", "sources", "short"],
                                         capture_output=True, text=True).stdout
                for line in sources.splitlines():
                    parts = line.split()
                    if len(parts) > 1 and not parts[1].endswith(".monitor"):
                        mic_source = parts[1]
                        break
        except Exception as e:
            log.warning(f"accel: could not resolve mic source: {e}")
        cmd = ["pactl", "load-module", "module-loopback", "latency_msec=1"]
        if mic_source:
            cmd.append(f"source={mic_source}")
        r = subprocess.run(cmd, capture_output=True, text=True)
        _accel_module_id = r.stdout.strip()
        log.info(f"monitor accel: loopback source={mic_source}")
        # Find the sink-input created by this module for volume control
        try:
            info = subprocess.run(["pactl", "list", "sink-inputs"],
                                  capture_output=True, text=True).stdout
            _accel_sink_input = None
            current_idx = None
            for line in info.splitlines():
                line = line.strip()
                if line.startswith("Sink Input #"):
                    current_idx = line.split("#")[1]
                elif "Module:" in line and _accel_module_id and _accel_module_id in line:
                    _accel_sink_input = current_idx
        except Exception as e:
            log.warning(f"accel: could not find sink-input: {e}")
        # Apply saved volume immediately
        if _accel_sink_input is not None:
            subprocess.run(["pactl", "set-sink-input-volume",
                            str(_accel_sink_input), f"{monitor_volume}%"], capture_output=True)
        monitor_active = True
        log.info(f"monitor accel: module={_accel_module_id} sink-input={_accel_sink_input}")
        return
    env = {**os.environ, "DISPLAY": os.environ.get("DISPLAY", ":0")}
    common = [f"--rate={MONITOR_RATE}", "--channels=1", "--format=s16le", "--latency-msec=1"]

    # Resolve actual monitor source name from default sink
    monitor_source = None
    try:
        sink = subprocess.run(["pactl", "get-default-sink"], capture_output=True, text=True).stdout.strip()
        if sink:
            monitor_source = f"{sink}.monitor"
    except Exception:
        pass

    _mic_proc = subprocess.Popen(
        ["parec"] + common,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env,
    )
    ref_cmd = ["parec"] + common + ([f"--device={monitor_source}"] if monitor_source else [])
    _ref_proc = subprocess.Popen(
        ref_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env,
    )
    _play_proc = subprocess.Popen(
        ["pacat"] + common,
        stdin=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env,
    )
    monitor_active = True
    log.info(f"monitor: mic={_mic_proc.pid} ref={_ref_proc.pid} play={_play_proc.pid} src={monitor_source}")
    threading.Thread(target=_ref_drain, daemon=True).start()
    threading.Thread(target=_mix_loop, daemon=True).start()

def toggle_monitor():
    if monitor_active:
        _stop_monitor()
    else:
        _start_monitor()
    _save_settings()

# ── Hot auto-reload ───────────────────────────────────────────────────────────

_THIS_FILE   = os.path.abspath(__file__)
_DEV_FLAG    = os.path.join(os.path.dirname(_THIS_FILE), '.dictation-tray-stream.devreload')
_dev_reload  = False
_dev_watcher = None

def _dev_restart():
    os.execv(sys.executable, [sys.executable] + sys.argv)

def _on_dev_file_changed(path):
    if _dev_watcher:
        _dev_watcher.addPath(path)
    QTimer.singleShot(400, _dev_restart)

def enable_dev_reload():
    global _dev_reload, _dev_watcher
    _dev_reload  = True
    files        = [str(p) for p in Path(os.path.dirname(_THIS_FILE)).glob('*.py')]
    _dev_watcher = QFileSystemWatcher(files)
    _dev_watcher.fileChanged.connect(_on_dev_file_changed)

def toggle_dev_reload():
    global _dev_reload, _dev_watcher
    if not _dev_reload:
        open(_DEV_FLAG, 'w').close()
        _dev_restart()
    else:
        if _dev_watcher:
            _dev_watcher.deleteLater()
            _dev_watcher = None
        try:
            os.unlink(_DEV_FLAG)
        except FileNotFoundError:
            pass
        _dev_reload = False

def toggle_monitor_denoise():
    global monitor_denoise
    was_active = monitor_active
    if was_active:
        _stop_monitor()
    monitor_denoise = not monitor_denoise
    if was_active:
        _start_monitor()

class MonitorControlPanel(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.Popup | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint)
        self.setStyleSheet("""
            QWidget  { background:#000000; }
            QLabel   { color:#00FFD4; font-size:12px; font-weight:bold; }
            QSlider::groove:horizontal   { background:#1e1e1e; height:6px; border-radius:3px; }
            QSlider::handle:horizontal   { background:#00FFD4; width:16px; height:16px; margin:-5px 0; border-radius:8px; }
            QSlider::sub-page:horizontal { background:#00FFD4; border-radius:3px; }
        """)
        root = QVBoxLayout(self); root.setContentsMargins(16, 14, 16, 14); root.setSpacing(12)
        self._vol_sl,    self._vol_lbl    = self._row(root, "Vol",    0, 150, monitor_volume)
        self._cancel_sl, self._cancel_lbl = self._row(root, "Cancel", 0, 200, monitor_cancel)
        self._vol_sl.valueChanged.connect(   lambda v: (self._vol_lbl.setText(f"{v}%"),    set_monitor_volume(v)))
        self._cancel_sl.valueChanged.connect(lambda v: (self._cancel_lbl.setText(f"{v}%"), set_monitor_cancel(v)))
        self.adjustSize(); self.setFixedSize(self.size())

    def _row(self, root, label, lo, hi, val):
        lbl = QLabel(f"{label}:  {val}%")
        sl  = QSlider(Qt.Horizontal); sl.setRange(lo, hi); sl.setValue(val); sl.setMinimumWidth(200)
        sl.valueChanged.connect(lambda v, l=lbl, n=label: l.setText(f"{n}:  {v}%"))
        root.addWidget(lbl); root.addWidget(sl)
        return sl, lbl

    def show_near_tray(self):
        self._vol_sl.setValue(monitor_volume)
        self._cancel_sl.setValue(monitor_cancel)
        geo = tray.geometry(); screen = QApplication.primaryScreen().availableGeometry()
        if geo.width() == 0:
            from PyQt5.QtGui import QCursor
            p = QCursor.pos(); x = p.x() - self.width() // 2; y = p.y() - self.height() - 6
        else:
            x = geo.x() + geo.width() // 2 - self.width() // 2; y = geo.y() - self.height() - 6
        self.move(max(screen.left(), min(x, screen.right() - self.width())), max(screen.top(), y))
        self.show(); self.raise_(); self.activateWindow()

monitor_ctrl_panel  = MonitorControlPanel()

from PyQt5.QtGui import QFontMetrics

_SLIDER_ROWS = [
    # (label, attr_volume, attr_cancel, lo, hi)
    ("Vol",    "monitor_volume", 0, 150),
    ("Cancel", "monitor_cancel", 0, 200),
]

class TrayMenu(QMenu):
    """QMenu subclass that paints draggable slider rows for monitor controls."""
    _ROW_H   = 28
    _PAD_L   = 18
    _LBL_W   = 52
    _VAL_W   = 38
    _TRACK_H = 5
    _HANDLE  = 13
    _TEAL    = QColor(0, 255, 212)
    _BG      = QColor(0, 0, 0)
    _TRACK   = QColor(40, 40, 40)

    def __init__(self):
        super().__init__()
        self._sliders  = []   # list of dicts populated in set_sliders()
        self._dragging = None

    def set_sliders(self, specs):
        """specs: list of (label, get_fn, set_fn, lo, hi)"""
        self._sliders = [
            {"label": lbl, "get": gf, "set": sf, "lo": lo, "hi": hi, "rect": None}
            for lbl, gf, sf, lo, hi in specs
        ]

    def _slider_action_at(self, y):
        for i, act in enumerate(self.actions()):
            r = self.actionGeometry(act)
            if act.objectName() == f"__slider_{i}" and r.contains(0, y):
                return i, r
        return None, None

    def sizeHint(self):
        s = super().sizeHint()
        return s

    def paintEvent(self, event):
        super().paintEvent(event)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        fm = QFontMetrics(self.font())
        for act in self.actions():
            name = act.objectName()
            if not name.startswith("__slider_"):
                continue
            idx = int(name.split("_")[-1])
            sl  = self._sliders[idx]
            r   = self.actionGeometry(act)
            val = sl["get"]()
            lo, hi = sl["lo"], sl["hi"]

            # Background
            p.fillRect(r, self._BG)

            # Label
            p.setPen(self._TEAL)
            p.drawText(r.x() + self._PAD_L, r.y(), self._LBL_W, r.height(),
                       Qt.AlignVCenter | Qt.AlignLeft, sl["label"])

            # Track
            tx = r.x() + self._PAD_L + self._LBL_W
            tw = r.width() - self._PAD_L - self._LBL_W - self._VAL_W - 8
            ty = r.y() + (r.height() - self._TRACK_H) // 2
            p.setBrush(self._TRACK); p.setPen(Qt.NoPen)
            p.drawRoundedRect(tx, ty, tw, self._TRACK_H, 2, 2)

            # Fill
            ratio  = (val - lo) / max(1, hi - lo)
            fillw  = int(tw * ratio)
            p.setBrush(self._TEAL)
            p.drawRoundedRect(tx, ty, fillw, self._TRACK_H, 2, 2)

            # Handle
            hx = tx + fillw - self._HANDLE // 2
            hy = r.y() + (r.height() - self._HANDLE) // 2
            p.setBrush(self._TEAL); p.setPen(Qt.NoPen)
            p.drawEllipse(hx, hy, self._HANDLE, self._HANDLE)

            # Value label
            p.setPen(self._TEAL)
            p.drawText(tx + tw + 4, r.y(), self._VAL_W, r.height(),
                       Qt.AlignVCenter | Qt.AlignLeft, f"{val}%")

            sl["rect"] = (tx, ty, tw, r)

        p.end()

    def _val_from_x(self, idx, x):
        if self._sliders[idx]["rect"] is None:
            return None
        tx, _, tw, _ = self._sliders[idx]["rect"]
        sl = self._sliders[idx]
        ratio = max(0.0, min(1.0, (x - tx) / tw))
        return int(sl["lo"] + ratio * (sl["hi"] - sl["lo"]))

    def mousePressEvent(self, e):
        act = self.actionAt(e.pos())
        if act and act.objectName().startswith("__slider_"):
            idx = int(act.objectName().split("_")[-1])
            v   = self._val_from_x(idx, e.x())
            if v is not None:
                self._dragging = idx
                self._sliders[idx]["set"](v)
                self.update()
                return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._dragging is not None:
            v = self._val_from_x(self._dragging, e.x())
            if v is not None:
                self._sliders[self._dragging]["set"](v)
                self.update()
            return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        self._dragging = None
        super().mouseReleaseEvent(e)

menu = TrayMenu()
menu.set_sliders([
    ("Vol",    lambda: monitor_volume, set_monitor_volume,    0, 150),
    ("Cancel", lambda: monitor_cancel, set_monitor_cancel, 0, 200),
])

def _rebuild_menu():
    """Rebuild on every right-click so the Pending section reflects current state."""
    menu.clear()
    menu.addAction("Toggle Dictation").triggered.connect(on_toggle)
    fmt_menu = menu.addMenu("  ✦ AI Format")
    def _set_fmt(p):
        global FORMAT_PROVIDER
        FORMAT_PROVIDER = p
        _save_settings()
    for _lbl, _p in FORMAT_PROVIDERS:
        _a = fmt_menu.addAction(_lbl)
        _a.setCheckable(True); _a.setChecked(FORMAT_PROVIDER == _p)
        _a.triggered.connect(lambda checked=False, p=_p: _set_fmt(p))
    hal = menu.addAction("Filter Thank Yous")
    hal.setCheckable(True); hal.setChecked(filter_hallucinations)
    hal.triggered.connect(toggle_hallucination_filter)
    rf = menu.addAction("Restore Focus After Typing")
    rf.setCheckable(True); rf.setChecked(restore_focus_on)
    def _toggle_rf():
        global restore_focus_on
        restore_focus_on = not restore_focus_on
        _save_settings()
    rf.triggered.connect(_toggle_rf)
    mn = menu.addAction("Window Preview")
    mn.setCheckable(True); mn.setChecked(mini_enabled)
    def _toggle_mini():
        global mini_enabled
        mini_enabled = not mini_enabled
        if not mini_enabled:
            _mini_queue.clear()
            bridge.mini_hide.emit()
    mn.triggered.connect(_toggle_mini)
    ms = menu.addAction("Hide Preview on Desktop Switch")
    ms.setCheckable(True); ms.setChecked(hide_mini_on_switch)
    def _toggle_mini_switch():
        global hide_mini_on_switch
        hide_mini_on_switch = not hide_mini_on_switch
    ms.triggered.connect(_toggle_mini_switch)
    mon = menu.addAction("Mic Monitor")
    mon.setCheckable(True); mon.setChecked(monitor_active)
    mon.triggered.connect(toggle_monitor)
    acc = menu.addAction("  ⚡ Acceleration — silence only (no music)")
    acc.setCheckable(True); acc.setChecked(monitor_accel)
    def _toggle_accel():
        global monitor_accel
        was_active = monitor_active
        if was_active:
            _stop_monitor()
        monitor_accel = not monitor_accel
        _save_settings()
        if was_active:
            _start_monitor()
    acc.triggered.connect(_toggle_accel)
    for i in range(len(menu._sliders)):
        a = QAction(" " * 30, menu); a.setObjectName(f"__slider_{i}")
        a.setEnabled(False); menu.addAction(a)
    menu.setMinimumWidth(260)
    fx_menu = menu.addMenu("  ✦ Effects")
    def _make_fx_toggle(label, flag_name):
        a = fx_menu.addAction(label); a.setCheckable(True); a.setChecked(globals()[flag_name])
        a.triggered.connect(lambda: globals().update({flag_name: not globals()[flag_name]}))
    _make_fx_toggle("Chipmunk",   "fx_pitch_up")
    _make_fx_toggle("Deep Voice", "fx_pitch_down")
    _make_fx_toggle("Robot",      "fx_robot")
    _make_fx_toggle("Echo",       "fx_echo")
    menu.addSeparator()
    menu.addAction("Show History").triggered.connect(hist_panel.show_near_tray)
    menu.addAction("Stream Settings…").triggered.connect(stream_settings.show_near_tray)
    model_menu = menu.addMenu("  ◉ Whisper Model")
    def _set_model(model_id):
        global GROQ_MODEL
        GROQ_MODEL = model_id
        _save_settings()
    for label, model_id in GROQ_MODELS:
        a = model_menu.addAction(label)
        a.setCheckable(True)
        a.setChecked(GROQ_MODEL == model_id)
        a.triggered.connect(lambda checked=False, m=model_id: _set_model(m))
    lang_menu = menu.addMenu("  🌐 Language")
    def _set_language(code):
        global GROQ_LANGUAGE
        GROQ_LANGUAGE = code
        _save_settings()
    for label, code in GROQ_LANGUAGES:
        a = lang_menu.addAction(label)
        a.setCheckable(True)
        a.setChecked(GROQ_LANGUAGE == code)
        a.triggered.connect(lambda checked=False, c=code: _set_language(c))

    with _pending_recs_lock:
        now  = datetime.now().timestamp()
        recs = [r for r in _pending_recs if now - r.get("ts_epoch", now) >= 5.0]
    if recs:
        menu.addSeparator()
        hdr = menu.addAction("— Stuck (click to resend) —")
        hdr.setEnabled(False)
        for r in recs:
            seq = r["seq"]; wav = r["wav"]; t = r["time"]
            act = menu.addAction(f"  ↺ {t}  (seq {seq})")
            act.triggered.connect(lambda checked=False, s=seq, w=wav: _resend_pending(s, w))

    menu.addSeparator()
    dev = menu.addAction("Auto-reload")
    dev.setCheckable(True); dev.setChecked(_dev_reload)
    dev.triggered.connect(toggle_dev_reload)
    menu.addAction("Quit").triggered.connect(on_quit)

menu.aboutToShow.connect(_rebuild_menu)

# ══════════════════════════════════════════════════════════════════════════════
# ██████  DO NOT TOUCH — TRAY ACTIVATION (LINUX/KDE SNI FIX)  ████████████████
# ══════════════════════════════════════════════════════════════════════════════
# On Linux/KDE the tray menu is proxy-rendered by Plasma via D-Bus/libdbusmenu.
# setContextMenu() hands the menu to an external process — custom paintEvent
# overrides and QWidgetAction widgets NEVER appear (blank rows, no interaction).
#
# The fix: intercept right-click via tray.activated and call menu.popup() so
# Qt renders the menu IN THIS PROCESS. This is what makes the sliders visible
# and draggable. DO NOT revert to tray.setContextMenu(menu).
#
# See: ~/reusable_features/tray_widget_interact.md
# ══════════════════════════════════════════════════════════════════════════════
def _on_tray_activated(reason):
    if reason == QSystemTrayIcon.Context:
        from PyQt5.QtGui import QCursor
        menu.popup(QCursor.pos())
    elif reason == QSystemTrayIcon.Trigger:
        on_toggle()

tray.activated.connect(_on_tray_activated)
tray.show()

# Restore monitor state from last session
if monitor_active:
    monitor_active = False   # _start_monitor sets it back to True
    QTimer.singleShot(500, _start_monitor)

if os.path.exists(_DEV_FLAG):
    enable_dev_reload()

# ── Signals ───────────────────────────────────────────────────────────────────
def _on_sigusr1(signum, frame):
    QTimer.singleShot(0, on_toggle)

def _on_sigterm(signum, frame):
    QTimer.singleShot(0, on_quit)

signal.signal(signal.SIGUSR1, _on_sigusr1)
signal.signal(signal.SIGTERM, _on_sigterm)
signal.signal(signal.SIGINT,  _on_sigterm)

# Kill any loopback left over from a previous crashed session
def _cleanup_stale_loopback():
    try:
        r = subprocess.run(["pactl", "list", "modules", "short"], capture_output=True, text=True)
        for line in r.stdout.splitlines():
            if "module-loopback" in line:
                idx = line.split()[0]
                subprocess.run(["pactl", "unload-module", idx], capture_output=True)
    except Exception:
        pass

_cleanup_stale_loopback()

sys.exit(app.exec_())
