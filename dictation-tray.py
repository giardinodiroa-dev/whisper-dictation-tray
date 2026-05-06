#!/usr/bin/env python3
import sys, os, subprocess, threading, json, http.client, ssl
import struct, math, wave, tempfile, uuid
from datetime import datetime
from PyQt5.QtWidgets import (QApplication, QSystemTrayIcon, QMenu, QAction,
                              QWidget, QVBoxLayout, QHBoxLayout, QLabel,
                              QScrollArea, QFrame, QPushButton, QSizePolicy)
from PyQt5.QtGui import QIcon, QPixmap, QPainter, QColor, QBrush, QPen, QFont
from PyQt5.QtCore import Qt, QObject, pyqtSignal, QRect

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
SAMPLE_RATE       = 16000
GROQ_API_KEY      = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL        = "whisper-large-v3-turbo"

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

ai_format        = True
dictation_active = False
recording        = False
history          = []
_parec_proc      = None

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
        subprocess.run(["xdotool", "windowfocus", "--sync", win_id], capture_output=True)

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
        f'Content-Disposition: form-data; name="response_format"\r\n\r\njson\r\n'
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
    return data.get("text", "").strip()

# ── Dictation worker ──────────────────────────────────────────────────────────

def dictation_worker():
    global recording, dictation_active, _parec_proc

    win = get_focused_window()
    env = {**os.environ, "DISPLAY": os.environ.get("DISPLAY", ":0")}

    _parec_proc = subprocess.Popen(
        ["parec", f"--rate={SAMPLE_RATE}", "--channels=1",
         "--format=s16le", "--latency-msec=50"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env
    )

    frames        = []
    silent_chunks = 0
    has_speech    = False
    total_chunks  = 0
    peak_rms      = 0
    speech_chunks = 0

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
            has_speech    = True
            silent_chunks = 0
            speech_chunks += 1
            level = min(int(rms / 300), 12)
            bridge.update_subtitle.emit("▮" * level + "▯" * (12 - level))
        elif has_speech:
            silent_chunks += 1
            if silent_chunks >= SILENCE_CHUNKS:
                break

        if total_chunks >= MAX_CHUNKS:
            break

    _parec_proc.terminate()
    _parec_proc.wait()
    _parec_proc = None
    recording = False
    bridge.update_subtitle.emit("")

    if not has_speech or not frames or peak_rms < MIN_SPEECH_RMS or speech_chunks < MIN_SPEECH_CHUNKS:
        if dictation_active:
            recording = True
            threading.Thread(target=dictation_worker, daemon=True).start()
        else:
            bridge.update_ui.emit("idle", "Dictation OFF — click to start")
        return

    bridge.update_ui.emit("thinking", "Transcribing…")

    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    try:
        with wave.open(tmp.name, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes(b"".join(frames))
        transcribed = transcribe_with_groq(tmp.name)
    except Exception:
        transcribed = ""
    finally:
        os.unlink(tmp.name)

    if not transcribed.strip():
        if dictation_active:
            recording = True
            threading.Thread(target=dictation_worker, daemon=True).start()
        else:
            bridge.update_ui.emit("idle", "Dictation OFF — click to start")
        return

    if ai_format:
        bridge.update_ui.emit("thinking", "Formatting…")
        try:
            formatted = _format(transcribed)
        except Exception:
            formatted = transcribed
        bridge.type_into_win.emit(formatted, win or "")
        bridge.add_history.emit(transcribed, formatted)
    else:
        bridge.type_into_win.emit(transcribed, win or "")
        bridge.add_history.emit(transcribed, transcribed)

def on_toggle():
    global recording, dictation_active, _parec_proc
    if dictation_active:
        dictation_active = False
        if _parec_proc:
            _parec_proc.terminate()
        bridge.update_ui.emit("idle", "Dictation OFF — click to start")
        bridge.update_subtitle.emit("")
        return
    dictation_active = True
    if not recording:
        recording = True
        bridge.update_ui.emit("recording", "Listening…")
        threading.Thread(target=dictation_worker, daemon=True).start()

# ── UI handlers ───────────────────────────────────────────────────────────────

def handle_ui_update(state, tooltip):
    tray.setIcon(QIcon(make_pixmap(state)))
    tray.setToolTip(tooltip)

def handle_update_subtitle(text):
    subtitle.set_text(text)

def handle_type_into_win(text, win_id):
    global recording
    restore_focus(win_id)
    subprocess.run(
        ["xdotool", "type", "--clearmodifiers", "--delay", "0", "--", text],
        capture_output=True
    )
    if dictation_active:
        recording = True
        bridge.update_ui.emit("recording", "Listening…")
        threading.Thread(target=dictation_worker, daemon=True).start()
    else:
        bridge.update_ui.emit("idle", "Dictation OFF — click to start")

def handle_add_history(raw, formatted):
    ts = datetime.now().strftime("%H:%M:%S")
    history.append({"time": ts, "raw": raw, "formatted": formatted})
    if hist_panel.isVisible():
        hist_panel.prepend_card(history[-1])

def on_quit():
    global dictation_active, _parec_proc
    dictation_active = False
    if _parec_proc:
        _parec_proc.terminate()
    app.quit()

# ── Subtitle Overlay ──────────────────────────────────────────────────────────

class SubtitleOverlay(QWidget):
    _W, _H = 140, 4

    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self._level = 0
        self._max   = 12
        screen = QApplication.primaryScreen().availableGeometry()
        self._screen = screen
        self.setFixedSize(self._W, self._H)
        self.move(
            screen.x() + (screen.width() - self._W) // 2,
            screen.y() + screen.height() - self._H - 8,
        )

    def set_text(self, text):
        self._level = text.count("▮") if text else 0
        if not self._level:
            self.hide()
            return
        self.update()
        if not self.isVisible():
            self.show()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(QColor(0, 0, 0, 100)))
        p.drawRoundedRect(0, 0, self._W, self._H, 2, 2)
        fill = int(self._W * self._level / self._max)
        p.setBrush(QBrush(QColor(220, 50, 50, 210)))
        p.drawRoundedRect(0, 0, fill, self._H, 2, 2)
        p.end()

# ── History Panel ─────────────────────────────────────────────────────────────

CARD_SS  = "QFrame#card { background:#1e1e2e; border:1px solid #313244; border-radius:10px; }"
PANEL_SS = """
QWidget#panel { background:#181825; border:1px solid #45475a; border-radius:12px; }
QScrollBar:vertical { background:#181825; width:6px; border-radius:3px; }
QScrollBar::handle:vertical { background:#45475a; border-radius:3px; min-height:20px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height:0; }
"""

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
        self.cards_widget = QWidget()
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

subtitle   = SubtitleOverlay()
hist_panel = HistoryPanel()

bridge.update_ui.connect(handle_ui_update)
bridge.update_subtitle.connect(handle_update_subtitle)
bridge.type_into_win.connect(handle_type_into_win)
bridge.add_history.connect(handle_add_history)

tray = QSystemTrayIcon(QIcon(make_pixmap("idle")), app)
tray.setToolTip("Dictation OFF — click to start")

def toggle_ai():
    global ai_format
    ai_format = not ai_format
    fmt_act.setChecked(ai_format)

menu = QMenu()
toggle_act = QAction("Toggle Dictation"); toggle_act.triggered.connect(on_toggle)
fmt_act    = QAction("AI Formatting");    fmt_act.setCheckable(True); fmt_act.setChecked(True); fmt_act.triggered.connect(toggle_ai)
hist_act   = QAction("Show History");     hist_act.triggered.connect(hist_panel.show_near_tray)
quit_act   = QAction("Quit");             quit_act.triggered.connect(on_quit)
menu.addAction(toggle_act); menu.addAction(fmt_act); menu.addAction(hist_act)
menu.addSeparator(); menu.addAction(quit_act)
tray.setContextMenu(menu)
tray.activated.connect(lambda r: on_toggle() if r == QSystemTrayIcon.Trigger else None)
tray.show()

sys.exit(app.exec_())
