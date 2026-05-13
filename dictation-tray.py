#!/usr/bin/env python3
import sys, os, subprocess, threading, json, http.client, ssl, logging
import struct, math, wave, tempfile, uuid, queue, concurrent.futures, time
from datetime import datetime

# ── Recordings + log paths ────────────────────────────────────────────────────
DICT_DIR  = os.path.expanduser("~/.local/share/dictation")
AUDIO_DIR = os.path.join(DICT_DIR, "recordings")
LOG_PATH  = os.path.join(DICT_DIR, "dictation.log")
HIST_PATH = os.path.join(DICT_DIR, "history.json")
os.makedirs(AUDIO_DIR, exist_ok=True)
logging.basicConfig(
    filename=LOG_PATH, level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("dictation")
from PyQt5.QtWidgets import (QApplication, QSystemTrayIcon, QMenu, QAction,
                              QWidget, QVBoxLayout, QHBoxLayout, QLabel,
                              QScrollArea, QFrame, QPushButton, QSizePolicy)
from PyQt5.QtGui import QIcon, QPixmap, QPainter, QColor, QBrush, QPen, QFont
from PyQt5.QtCore import Qt, QObject, pyqtSignal, QRect, QTimer

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
NO_SPEECH_THRESHOLD = 0.6   # discard if Whisper's avg no_speech_prob exceeds this

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

ai_format             = False
filter_hallucinations = True
restore_focus_on      = True   # when True, view auto-jumps to the window receiving text
focus_flash_always    = False  # True = flash on focus change even outside dictation
dictation_active = False
recording        = False
_focus_watcher_stop  = threading.Event()
_last_focused_win    = None
def _load_history():
    try:
        with open(HIST_PATH) as f: return json.load(f)
    except Exception: return []

def _save_history():
    try:
        with open(HIST_PATH, "w") as f: json.dump(history, f)
    except Exception as e: log.error(f"history save error: {e}")

history          = _load_history()
_parec_proc      = None
_seq             = 0
_out_q           = queue.Queue()
_executor        = concurrent.futures.ThreadPoolExecutor(max_workers=4)
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
    colors = {"idle": QColor(90,90,90), "recording": QColor(220,50,50), "thinking": QColor(50,140,220)}
    px = QPixmap(64, 64)
    px.fill(Qt.transparent)
    p = QPainter(px)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QBrush(colors.get(state, colors["idle"])))
    p.setPen(Qt.NoPen)
    p.drawEllipse(4, 4, 56, 56)
    p.setBrush(QBrush(QColor("white")))
    p.drawRoundedRect(22, 12, 20, 28, 10, 10)
    p.setPen(QPen(QColor("white"), 3))
    p.setBrush(Qt.NoBrush)
    p.drawArc(QRect(16, 30, 32, 20), 0, 180 * 16)
    p.drawLine(32, 50, 32, 58)
    p.drawLine(24, 58, 40, 58)
    p.end()
    return px

# ── Bridge ────────────────────────────────────────────────────────────────────

class Bridge(QObject):
    update_ui       = pyqtSignal(str, str)
    update_subtitle = pyqtSignal(str)
    type_into_win   = pyqtSignal(str, str)
    add_history     = pyqtSignal(str, str)
    queue_update    = pyqtSignal(int)
    mini_show       = pyqtSignal(int, int, bytes) # desktop, num_desktops, png_bytes
    mini_countdown  = pyqtSignal(int)             # 3,2,1 — 0 means clear count
    mini_hide       = pyqtSignal()
    focus_flash     = pyqtSignal(int, int, int, int)  # x, y, w, h

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

def _format(raw):
    body = json.dumps({
        "model": _d(_M),
        "messages": [
            {"role": "system", "content":
                "You are a speech-to-text post-processor. Your only job is to add punctuation "
                "and fix capitalization. NEVER change, reorder, add, or remove any words — "
                "preserve exactly what was said, even if it sounds unusual or incomplete. "
                "Do not paraphrase, summarize, or correct meaning. "
                "Return ONLY the formatted text with no explanations, comments, or quotes."},
            {"role": "user", "content": raw}
        ],
        "max_tokens": 2048,
    }).encode()
    ctx  = ssl.create_default_context()
    conn = http.client.HTTPSConnection(_d(_H), context=ctx)
    conn.request("POST", _d(_P), body=body, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {_d(_AK)}",
    })
    resp = conn.getresponse()
    data = json.loads(resp.read())
    conn.close()
    return data["choices"][0]["message"]["content"].strip()

def transcribe_with_groq(wav_path):
    boundary = uuid.uuid4().hex
    with open(wav_path, "rb") as f:
        audio_data = f.read()
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="model"\r\n\r\n'
        f"{GROQ_MODEL}\r\n"
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="language"\r\n\r\nen\r\n'
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="response_format"\r\n\r\nverbose_json\r\n'
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="audio.wav"\r\n'
        f"Content-Type: audio/wav\r\n\r\n"
    ).encode() + audio_data + f"\r\n--{boundary}--\r\n".encode()
    ctx  = ssl.create_default_context()
    conn = http.client.HTTPSConnection("api.groq.com", context=ctx)
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

# ── Parallel dictation engine ─────────────────────────────────────────────────

def _record_one_chunk():
    """Records one speech segment. Returns (frames, win) or (None, None)."""
    global _parec_proc, _live_rms
    env = {**os.environ, "DISPLAY": os.environ.get("DISPLAY", ":0")}
    _parec_proc = subprocess.Popen(
        ["parec", f"--rate={SAMPLE_RATE}", "--channels=1",
         "--format=s16le", "--latency-msec=50"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env
    )
    win           = get_focused_window()
    frames        = []
    silent_chunks = 0
    has_speech    = False
    total_chunks  = 0
    peak_rms      = 0
    speech_chunks = 0
    mini_fired    = False   # ensure the screenshot fires only on FIRST speech

    while dictation_active:
        data = _parec_proc.stdout.read(CHUNK_BYTES)
        if not data:
            break
        frames.append(data)
        total_chunks += 1
        samples = struct.unpack(f"<{len(data)//2}h", data)
        rms = math.sqrt(sum(s * s for s in samples) / len(samples)) if samples else 0
        if rms > peak_rms:
            peak_rms = rms
        if rms > SILENCE_THRESHOLD:
            has_speech = True; silent_chunks = 0; speech_chunks += 1
            _live_rms = rms
            # Fire the miniature exactly once, the moment speech first registers.
            # All capture work happens in a background thread — desktop info +
            # screenshot are emitted together in a single mini_show signal so
            # queued miniatures can never race their own thumbnails.
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
                        desk = subprocess.run(
                            ["xdotool", "get_desktop_for_window", w],
                            capture_output=True, text=True, timeout=1).stdout.strip()
                        ndesk = subprocess.run(
                            ["xdotool", "get_num_desktops"],
                            capture_output=True, text=True, timeout=1).stdout.strip()
                        bridge.mini_show.emit(int(desk or 0), int(ndesk or 1), thumb)
                    except Exception as e:
                        log.error(f"mini setup error: {e}")
                threading.Thread(target=_cap, daemon=True).start()
        elif has_speech:
            silent_chunks += 1
            if silent_chunks >= SILENCE_CHUNKS:
                break
        if total_chunks >= MAX_CHUNKS:
            break

    _parec_proc.terminate(); _parec_proc.wait(); _parec_proc = None
    _live_rms = 0

    valid = has_speech and frames and peak_rms >= MIN_SPEECH_RMS and speech_chunks >= MIN_SPEECH_CHUNKS
    # If we fired mini_show but the chunk got rejected, release the miniature
    # so it doesn't sit on screen forever waiting for a type that never comes.
    if mini_fired and not valid:
        bridge.mini_hide.emit()
    return (frames, win) if valid else (None, None)

def _process_chunk(seq, frames, win):
    """Transcribe + format one chunk, post result to output queue."""
    ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
    wav_path = os.path.join(AUDIO_DIR, f"{ts}_{seq:04d}.wav")
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
        text = ""

    raw = text.strip()
    formatted = raw
    if raw and ai_format:
        try:
            formatted = _format(raw)
        except Exception:
            pass

    _out_q.put((seq, raw, formatted, win))

def _output_worker(start_seq):
    """Drains _out_q in submission order regardless of completion order."""
    pending  = {}
    next_seq = start_seq
    while dictation_active or not _out_q.empty() or pending:
        try:
            seq, raw, formatted, win = _out_q.get(timeout=0.2)
        except queue.Empty:
            continue
        pending[seq] = (raw, formatted, win)
        while next_seq in pending:
            raw, formatted, win = pending.pop(next_seq)
            if formatted:
                bridge.type_into_win.emit(formatted, win or "")
                bridge.add_history.emit(raw, formatted)
                _pending_remove(next_seq)  # success → drop from tray menu
            else:
                # Whisper returned empty — release the miniature so it doesn't
                # linger on screen waiting for a type that will never happen.
                bridge.mini_hide.emit()
            _dec_pending()
            next_seq += 1

def dictation_loop():
    global recording, dictation_active, _seq
    _seq = 0
    threading.Thread(target=_output_worker, args=(_seq,), daemon=True).start()

    while dictation_active:
        frames, win = _record_one_chunk()
        if frames:
            seq = _seq; _seq += 1
            _inc_pending()
            _executor.submit(_process_chunk, seq, frames, win)

    recording = False
    bridge.update_ui.emit("idle", "Dictation OFF — click to start")

def on_toggle():
    global recording, dictation_active, _parec_proc, _pending_count
    if dictation_active:
        dictation_active = False
        if _parec_proc:
            _parec_proc.terminate()
        with _pending_lock:
            _pending_count = 0
        bridge.queue_update.emit(0)
        bridge.update_ui.emit("idle", "Dictation OFF — click to start")
        bridge.update_subtitle.emit("")
        if not focus_flash_always:
            _stop_focus_watcher()
        return
    dictation_active = True
    if not recording:
        recording = True
        bridge.update_ui.emit("recording", "Listening…")
        threading.Thread(target=dictation_loop, daemon=True).start()
    if not focus_flash_always:
        _start_focus_watcher()

# ── UI handlers ───────────────────────────────────────────────────────────────

def handle_ui_update(state, tooltip):
    tray.setIcon(QIcon(make_pixmap(state)))
    tray.setToolTip(tooltip)

def handle_update_subtitle(text):
    pass  # level meter now driven by QTimer — signal kept for compat

def handle_type_into_win(text, win_id):
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
    # Step 2: 3-2-1 countdown on the miniature, then teleport (if toggled on).
    if restore_focus_on:
        from PyQt5.QtCore import QTimer
        def _tick(n):
            bridge.mini_countdown.emit(n)
            if n > 1:
                QTimer.singleShot(700, lambda: _tick(n - 1))
            else:
                QTimer.singleShot(700, _teleport)
        def _teleport():
            subprocess.run(["xdotool", "windowactivate", "--sync", win_id],
                           capture_output=True)
            bridge.mini_hide.emit()
        _tick(3)
    else:
        bridge.mini_hide.emit()

def handle_add_history(raw, formatted):
    ts = datetime.now().strftime("%H:%M:%S")
    history.append({"time": ts, "raw": raw, "formatted": formatted})
    _save_history()
    if hist_panel.isVisible():
        hist_panel.prepend_card(history[-1])

# ── Pending-recordings list (shown in tray menu, click to resend) ─────────────
# A chunk is added the moment its WAV is saved, and removed once its text
# successfully types out. Anything left in this list is "stuck" and resendable.
_pending_recs      = []     # list of {"seq": int, "wav": path, "time": "HH:MM:SS"}
_pending_recs_lock = threading.Lock()

def _pending_add(seq, wav_path):
    rec = {"seq": seq, "wav": wav_path, "time": datetime.now().strftime("%H:%M:%S")}
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
            if text and ai_format:
                try: formatted = _format(text)
                except Exception: pass
            if formatted:
                bridge.type_into_win.emit(formatted, win or "")
                bridge.add_history.emit(text, formatted)
                _pending_remove(seq)
        except Exception as e:
            log.error(f"resend seq={seq} error: {e}")
    _executor.submit(do)

def on_quit():
    global dictation_active, _parec_proc
    dictation_active = False
    if _parec_proc:
        _parec_proc.terminate()
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

        # ── audio level bar (red, bottom row) ────────────────────────────────
        bar_y = self._DOT_H + self._GAP
        p.setBrush(QBrush(QColor(0, 0, 0, 100)))
        p.drawRoundedRect(0, bar_y, self._W, self._BAR_H, 2, 2)
        if self._level:
            fill = int(self._W * self._level / self._MAX)
            p.setBrush(QBrush(QColor(220, 50, 50, 210)))
            p.drawRoundedRect(0, bar_y, fill, self._BAR_H, 2, 2)

        p.end()

# ── Focus Flash ───────────────────────────────────────────────────────────────

class FocusFlash(QWidget):
    _FPS      = 60
    _DURATION = 400   # ms total fade
    _STEPS    = _FPS * _DURATION // 1000
    _START_A  = 70    # starting alpha (~27% opacity)

    def __init__(self):
        super().__init__()
        self.setWindowFlags(
            Qt.FramelessWindowHint |
            Qt.WindowStaysOnTopHint |
            Qt.X11BypassWindowManagerHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._alpha = 0
        self._timer = QTimer(self)
        self._timer.setInterval(1000 // self._FPS)
        self._timer.timeout.connect(self._tick)
        self.hide()

    def flash(self, x, y, w, h):
        self.setGeometry(x, y, w, h)
        self._alpha = self._START_A
        self.show()
        self._timer.start()
        self.update()

    def _tick(self):
        self._alpha -= self._START_A / self._STEPS
        if self._alpha <= 0:
            self._alpha = 0
            self._timer.stop()
            self.hide()
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(100, 160, 255, int(self._alpha)))
        p.end()

def _focus_watcher():
    global _last_focused_win
    while not _focus_watcher_stop.is_set():
        try:
            r = subprocess.run(["xdotool", "getactivewindow"],
                               capture_output=True, text=True, timeout=1)
            win = r.stdout.strip()
            if win and win != _last_focused_win:
                _last_focused_win = win
                r2 = subprocess.run(
                    ["xdotool", "getwindowgeometry", "--shell", win],
                    capture_output=True, text=True, timeout=1)
                geo = {}
                for line in r2.stdout.splitlines():
                    if '=' in line:
                        k, v = line.split('=', 1)
                        try: geo[k] = int(v)
                        except ValueError: pass
                if all(k in geo for k in ('X', 'Y', 'WIDTH', 'HEIGHT')):
                    bridge.focus_flash.emit(geo['X'], geo['Y'], geo['WIDTH'], geo['HEIGHT'])
        except Exception:
            pass
        time.sleep(0.25)

def _start_focus_watcher():
    global _last_focused_win
    try:
        _last_focused_win = subprocess.run(
            ["xdotool", "getactivewindow"], capture_output=True, text=True, timeout=1
        ).stdout.strip()
    except Exception:
        _last_focused_win = None
    _focus_watcher_stop.clear()
    threading.Thread(target=_focus_watcher, daemon=True).start()

def _stop_focus_watcher():
    _focus_watcher_stop.set()

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

# ── App ───────────────────────────────────────────────────────────────────────

app = QApplication(sys.argv)
app.setQuitOnLastWindowClosed(False)

subtitle    = SubtitleOverlay()
hist_panel  = HistoryPanel()
mini        = WindowMiniature()
focus_flash = FocusFlash()

bridge.update_ui.connect(handle_ui_update)
bridge.update_subtitle.connect(handle_update_subtitle)
bridge.type_into_win.connect(handle_type_into_win)
bridge.add_history.connect(handle_add_history)
bridge.queue_update.connect(subtitle.set_pending)
# Mini queue: each chunk gets its own miniature, shown one at a time in order.
# A new chunk's miniature waits until the current one's text has been typed
# and its countdown+teleport finished (mini_hide fires). This way the arrow /
# screenshot always represents the NEXT move, never overwritten mid-flight.
_mini_queue  = []
_mini_active = False

def _handle_mini_show(desktop, num_desktops, thumb):
    global _mini_active
    if _mini_active:
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
bridge.focus_flash.connect(lambda x, y, w, h: focus_flash.flash(x, y, w, h))
bridge.mini_countdown.connect(mini.set_countdown)
bridge.mini_hide.connect(_handle_mini_hide)

from PyQt5.QtCore import QTimer
_level_timer = QTimer()
_level_timer.setInterval(80)
_level_timer.timeout.connect(lambda: subtitle.tick(_live_rms, _pending_count))
_level_timer.start()

tray = QSystemTrayIcon(QIcon(make_pixmap("idle")), app)
tray.setToolTip("Dictation OFF — click to start")

def toggle_ai():
    global ai_format
    ai_format = not ai_format

def toggle_hallucination_filter():
    global filter_hallucinations
    filter_hallucinations = not filter_hallucinations

def toggle_focus_flash_always():
    global focus_flash_always
    focus_flash_always = not focus_flash_always
    if focus_flash_always:
        _start_focus_watcher()
    elif not dictation_active:
        _stop_focus_watcher()

menu = QMenu()

def _rebuild_menu():
    """Rebuild on every right-click so the Pending section reflects current state."""
    menu.clear()
    menu.addAction("Toggle Dictation").triggered.connect(on_toggle)
    fmt = menu.addAction("AI Formatting")
    fmt.setCheckable(True); fmt.setChecked(ai_format)
    fmt.triggered.connect(toggle_ai)
    hal = menu.addAction("Filter Thank Yous")
    hal.setCheckable(True); hal.setChecked(filter_hallucinations)
    hal.triggered.connect(toggle_hallucination_filter)
    ffa = menu.addAction("Focus Flash Always")
    ffa.setCheckable(True); ffa.setChecked(focus_flash_always)
    ffa.triggered.connect(toggle_focus_flash_always)
    rf = menu.addAction("Restore Focus After Typing")
    rf.setCheckable(True); rf.setChecked(restore_focus_on)
    def _toggle_rf():
        global restore_focus_on
        restore_focus_on = not restore_focus_on
    rf.triggered.connect(_toggle_rf)
    menu.addAction("Show History").triggered.connect(hist_panel.show_near_tray)

    with _pending_recs_lock:
        recs = list(_pending_recs)
    if recs:
        menu.addSeparator()
        hdr = menu.addAction("— Pending (click to resend) —")
        hdr.setEnabled(False)
        for r in recs:
            seq = r["seq"]; wav = r["wav"]; t = r["time"]
            act = menu.addAction(f"  ↺ {t}  (seq {seq})")
            act.triggered.connect(lambda checked=False, s=seq, w=wav: _resend_pending(s, w))

    menu.addSeparator()
    menu.addAction("Quit").triggered.connect(on_quit)

menu.aboutToShow.connect(_rebuild_menu)
_rebuild_menu()  # initial build so the menu is valid before first show
tray.setContextMenu(menu)
tray.activated.connect(lambda r: on_toggle() if r == QSystemTrayIcon.Trigger else None)
tray.show()

sys.exit(app.exec_())
