import http.client
import json
import os
import random
import ssl
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor

from PyQt6.QtCore import Qt, QPropertyAnimation, QEasingCurve, QEvent, QTimer, pyqtSignal, QRect

MATRIX_CHARS = 'アイウエオカキクケコ01ΩΨΦ∆∑√∞≠≤≥'
from PyQt6.QtGui import QPainter, QColor, QPen
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QLineEdit, QLabel, QApplication

from const import TEAL, BG, FONT


_KILO_MODELS = [
    "deepseek/deepseek-v4-flash:free",
    "kilo-auto/free",
    "stepfun/step-3.5-flash:free",
    "x-ai/grok-code-fast-1:optimized:free",
    "poolside/laguna-xs.2:free",
    "poolside/laguna-m.1:free",
    "openrouter/free",
    "openrouter/owl-alpha",
]


_KILO_HOST = "api.kilo.ai"
_KILO_KEY  = "anonymous"
_env = os.path.expanduser("~/bin/.env")
try:
    for _l in open(_env).read().splitlines():
        _l = _l.strip()
        if "=" not in _l or _l.startswith("#"): continue
        _k, _v = _l.split("=", 1)
        if _k.strip() == "KILO_HOST": _KILO_HOST = _v.strip()
        elif _k.strip() in ("KILO_KEY", "KILO_API_KEY"): _KILO_KEY = _v.strip()
except Exception:
    pass


def _kilo_call(model, text):
    payload = json.dumps({
        "model": model,
        "max_tokens": 4096,
        "messages": [
            {"role": "system", "content": "Respond in 1 word."},
            {"role": "user",   "content": text},
        ],
    }).encode()
    print(f"[focus] trying model={model}", flush=True)
    ctx  = ssl.create_default_context()
    conn = http.client.HTTPSConnection(_KILO_HOST, context=ctx, timeout=10)
    conn.request("POST", "/api/openrouter/chat/completions", body=payload,
                 headers={"Authorization": f"Bearer {_KILO_KEY}",
                          "Content-Type": "application/json"})
    resp = conn.getresponse()
    body = resp.read()
    print(f"[focus] status={resp.status} body={body[:200]}", flush=True)
    if resp.status != 200:
        raise RuntimeError(f"{resp.status}")
    data = json.loads(body)
    msg  = data["choices"][0]["message"]
    return (msg.get("content") or msg.get("reasoning") or "").strip()


_executor = ThreadPoolExecutor(max_workers=2)


def _distill(text):
    return _executor.submit(_run_distill, text)


def _run_distill(text):
    fallback = text.split()[0].lower()[:12]
    print(f"[focus] distilling: {text!r}", flush=True)
    for model in _KILO_MODELS:
        try:
            raw  = _kilo_call(model, text)
            word = raw.split()[0].lower().strip(".,!?;:'\"")[:32]
            print(f"[focus] got word={word!r}", flush=True)
            if word:
                return word
        except Exception as e:
            print(f"[focus] model {model} failed: {e}", flush=True)
            continue
    print(f"[focus] all failed, using fallback={fallback}", flush=True)
    return fallback

_W = 500
_H = 90


class CapturePopup(QWidget):
    task_saved = pyqtSignal(str, str)   # label, description

    def __init__(self, win_name: str, parent=None):
        super().__init__(parent)
        self._open = False

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.X11BypassWindowManagerHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFixedSize(_W, _H)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(4)

        self._lbl = QLabel(win_name)
        self._lbl.setStyleSheet(
            f"color: rgba(0,255,212,0.45); font-family: '{FONT}'; font-size: 9px;"
            f" background: transparent; border: none;"
        )
        self._lbl.setAlignment(Qt.AlignmentFlag.AlignLeft)

        self._edit = QLineEdit()
        self._edit.setPlaceholderText("what are you working on?")
        self._edit.setStyleSheet(
            f"QLineEdit {{ background: {BG}; color: {TEAL}; font-family: '{FONT}';"
            f" font-size: 10px; border: none;"
            f" selection-background-color: rgba(0,255,212,0.25); }}"
            f"QLineEdit::placeholder {{ color: rgba(0,255,212,0.30); }}"
        )
        self._edit.returnPressed.connect(self._on_return)
        # per pin_to_screen_input.md: filter the widget itself (QLineEdit has no viewport)
        self._edit.installEventFilter(self)

        layout.addWidget(self._lbl)
        layout.addWidget(self._edit)

        self.setWindowOpacity(0.0)
        self._anim = QPropertyAnimation(self, b"windowOpacity")
        self._anim.setDuration(120)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.setEasingCurve(QEasingCurve.Type.InOutQuad)

        self._center_on_screen()

    # ── pin-to-screen focus (from pin_to_screen_input.md) ────────────────

    def _x11_force_focus(self):
        try:
            subprocess.Popen(
                ['xdotool', 'windowfocus', '--sync', '--clearmodifiers',
                 str(int(self.winId()))],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass

    def _regrab_focus(self):
        if not self._open:
            return
        self.raise_()
        self.activateWindow()
        self._edit.setFocus(Qt.FocusReason.OtherFocusReason)
        self._x11_force_focus()

    def eventFilter(self, obj, event):
        if obj is self._edit and event.type() == QEvent.Type.MouseButtonPress:
            QTimer.singleShot(120, self._regrab_focus)
        return super().eventFilter(obj, event)

    def mousePressEvent(self, event):
        super().mousePressEvent(event)
        QTimer.singleShot(120, self._regrab_focus)

    # ── positioning ───────────────────────────────────────────────────────

    def _center_on_screen(self):
        geo = QApplication.primaryScreen().availableGeometry()
        self.move(geo.x() + (geo.width() - _W) // 2,
                  geo.y() + (geo.height() - _H) // 2)

    # ── painting ──────────────────────────────────────────────────────────

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setBrush(QColor(BG))
        pen = QPen(QColor(TEAL))
        pen.setWidth(1)
        p.setPen(pen)
        p.drawRoundedRect(QRect(0, 0, self.width() - 1, self.height() - 1), 4, 4)

    # ── key handling ──────────────────────────────────────────────────────

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self._open = False
            self.close()
        else:
            super().keyPressEvent(event)

    # ── slots ─────────────────────────────────────────────────────────────

    def _on_return(self):
        text = self._edit.text().strip()
        if not text:
            self._open = False
            self.close()
            return
        self._description = text
        self._edit.setEnabled(False)
        future = _distill(text)
        self._poll_distill(future, len(text))

    def _poll_distill(self, future, text_len):
        tick_count = [0]

        poll  = QTimer(self)
        think = QTimer(self)
        poll.setInterval(80)
        think.setInterval(180)

        dots = ['.', '..', '...']

        def _think_tick():
            tick_count[0] += 1
            self._lbl.setText(f"thinking{dots[tick_count[0] % 3]}")
            self._edit.setText(''.join(
                random.choice(MATRIX_CHARS) for _ in range(text_len)
            ))

        think.timeout.connect(_think_tick)
        think.start()

        def check():
            if future.done():
                poll.stop()
                think.stop()
                self._lbl.setText("")
                self._edit.clear()
                self._edit.setEnabled(True)
                try:
                    word = future.result()
                    if word:
                        self._morph_to_word(word)
                except Exception:
                    self._open = False
                    self.close()

        poll.timeout.connect(check)
        poll.start()

    def _morph_to_word(self, target):
        original_len = max(len(self._edit.text()), len(target))
        buf   = list(self._edit.text().ljust(original_len))
        tgt   = list(target.ljust(original_len))
        phase = [1]
        idx   = [0]
        timer = QTimer(self)

        def tick():
            if phase[0] == 1:
                if idx[0] < len(buf):
                    buf[idx[0]] = random.choice(MATRIX_CHARS)
                    self._edit.setText(''.join(buf))
                    idx[0] += 1
                    timer.setInterval(15)
                else:
                    phase[0] = 2
                    idx[0]   = 0
                    timer.setInterval(12)
            else:
                if idx[0] < len(tgt):
                    buf[idx[0]] = tgt[idx[0]]
                    self._edit.setText(''.join(buf).rstrip())
                    idx[0] += 1
                else:
                    timer.stop()
                    self._edit.setText(target)
                    self._open = False
                    self.task_saved.emit(target, getattr(self, '_description', ''))
                    QTimer.singleShot(400, self.close)

        timer.timeout.connect(tick)
        timer.start(15)

    # ── lifecycle ─────────────────────────────────────────────────────────

    def showEvent(self, event):
        super().showEvent(event)
        self._open = True
        self._anim.start()
        # deferred regrab per pin_to_screen_input.md — WM must finish first
        QTimer.singleShot(120, self._regrab_focus)
