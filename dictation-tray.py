#!/usr/bin/env python3
import sys, os, subprocess, threading, json, http.client, ssl
import struct, math, wave, tempfile, uuid, queue, concurrent.futures
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
_seq             = 0
_out_q           = queue.Queue()
_executor        = concurrent.futures.ThreadPoolExecutor(max_workers=4)
_pending_count   = 0
_pending_lock    = threading.Lock()

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

# ── Parallel dictation engine ─────────────────────────────────────────────────

def _record_one_chunk():
    """Records one speech segment. Returns (frames, win) or (None, None)."""
    global _parec_proc
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
            bridge.update_subtitle.emit("▮" * min(int(rms / 300), 12) + "▯" * (12 - min(int(rms / 300), 12)))
        elif has_speech:
            silent_chunks += 1
            if silent_chunks >= SILENCE_CHUNKS:
                break
        if total_chunks >= MAX_CHUNKS:
            break

    _parec_proc.terminate(); _parec_proc.wait(); _parec_proc = None
    bridge.update_subtitle.emit("")

    valid = has_speech and frames and peak_rms >= MIN_SPEECH_RMS and speech_chunks >= MIN_SPEECH_CHUNKS
    return (frames, win) if valid else (None, None)

def _process_chunk(seq, frames, win):
    """Transcribe + format one chunk, post result to output queue."""
    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    try:
        with wave.open(tmp.name, "wb") as wf:
            wf.setnchannels(1); wf.setsampwidth(2)
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes(b"".join(frames))
        text = transcribe_with_groq(tmp.name)
    except Exception:
        text = ""
    finally:
        os.unlink(tmp.name)

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
    while True:
        try:
            seq, raw, formatted, win = _out_q.get(timeout=0.2)
        except queue.Empty:
            # Exit cleanly when dictation stopped and nothing left to drain
            if not dictation_active:
                break
            continue
        pending[seq] = (raw, formatted, win)
        while next_seq in pending:
            raw, formatted, win = pending.pop(next_seq)
            if formatted:
                bridge.type_into_win.emit(formatted, win or "")
                bridge.add_history.emit(raw, formatted)
            _dec_pending()
            next_seq += 1

def dictation_loop():
    global recording, dictation_active, _seq
    _seq = 0
    out_thread = threading.Thread(target=_output_worker, args=(_seq,), daemon=True)
    out_thread.start()

    while dictation_active:
        frames, win = _record_one_chunk()
        if frames:
            seq = _seq; _seq += 1
            _inc_pending()
            _executor.submit(_process_chunk, seq, frames, win)

    out_thread.join(timeout=15)
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
        return
    dictation_active = True
    if not recording:
        recording = True
        bridge.update_ui.emit("recording", "Listening…")
        threading.Thread(target=dictation_loop, daemon=True).start()

# ── UI handlers ───────────────────────────────────────────────────────────────

def handle_ui_update(state, tooltip):
    tray.setIcon(QIcon(make_pixmap(state)))
    tray.setToolTip(tooltip)

def handle_update_subtitle(text):
    subtitle.set_text(text)

def handle_type_into_win(text, win_id):
    restore_focus(win_id)
    subprocess.run(
        ["xdotool", "type", "--clearmodifiers", "--delay", "0", "--", text],
        capture_output=True
    )

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
    _executor.shutdown(wait=False)
    app.quit()

# ── Subtitle Overlay ──────────────────────────────────────────────────────────

class SubtitleOverlay(QWidget):
    _W     = 140
    _BAR_H = 4
    _DOT_H = 6
    _GAP   = 4
    _H     = _DOT_H + _GAP + _BAR_H   # 14px total
    _DOT_W = 10
    _DOT_G = 3

    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self._level   = 0
        self._pending = 0
        self._max     = 12
        screen = QApplication.primaryScreen().availableGeometry()
        self.setFixedSize(self._W, self._H)
        self.move(
            screen.x() + (screen.width() - self._W) // 2,
            screen.y() + screen.height() - self._H - 8,
        )

    def set_text(self, text):
        self._level = text.count("▮") if text else 0
        self._refresh()

    def set_pending(self, count):
        self._pending = count
        self._refresh()

    def _refresh(self):
        if not self._level and not self._pending:
            self.hide()
            return
        self.update()
        if not self.isVisible():
            self.show()

    def paintEvent(self, event):
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
            fill = int(self._W * self._level / self._max)
            p.setBrush(QBrush(QColor(220, 50, 50, 210)))
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
bridge.queue_update.connect(lambda n: subtitle.set_pending(n))

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
