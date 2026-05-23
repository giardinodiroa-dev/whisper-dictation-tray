"""fx.py — digital corruption / Tron-break animation, centered on screen."""

import random
import math

from PyQt6.QtCore import Qt, QTimer, QRect
from PyQt6.QtGui import QColor, QPainter, QPen
from PyQt6.QtWidgets import QApplication, QWidget

from const import TEAL_RGB, BG

_T  = QColor(*TEAL_RGB)
_T2 = QColor(TEAL_RGB[0], TEAL_RGB[1], TEAL_RGB[2], 80)
_W  = QColor(255, 255, 255)


def _teal(alpha=255):
    r, g, b = TEAL_RGB
    return QColor(r, g, b, max(0, min(255, alpha)))


def _white(alpha=255):
    return QColor(255, 255, 255, max(0, min(255, alpha)))


# ---------------------------------------------------------------------------
# Corruption band: a horizontal strip displaced sideways
# ---------------------------------------------------------------------------

def _gen_bands(cy, sw, sh):
    """Horizontal corruption bands concentrated around cy."""
    bands = []
    zone = int(sh * 0.42)         # corruption zone ±42% from center
    y = cy - zone
    while y < cy + zone:
        h = random.randint(1, 5)
        dist = abs(y - cy)
        weight = max(0.0, 1.0 - dist / zone)
        shift = int(random.choice([-1, 1]) * random.randint(4, 48) * weight)
        alpha = int(random.uniform(0.4, 1.0) * weight * 255)
        bands.append((y, h, shift, alpha))
        y += h + random.randint(0, 3)
    return bands


def _gen_glitch_cols(cx, sw, sh, count=18):
    """Vertical teal columns near the split point — data leak lines."""
    cols = []
    for _ in range(count):
        x = cx + random.randint(-6, 6)
        h = random.randint(sh // 6, sh // 2)
        y = random.randint(0, sh - h)
        a = random.randint(60, 220)
        w = random.randint(1, 2)
        cols.append((x, y, w, h, a))
    return cols


def _gen_artifacts(cx, cy, count=40):
    """Small pixel blocks scattered from center — memory corruption."""
    arts = []
    for _ in range(count):
        angle = random.uniform(0, math.pi * 2)
        dist  = random.uniform(20, 340)
        x = int(cx + math.cos(angle) * dist)
        y = int(cy + math.sin(angle) * dist)
        w = random.randint(2, 18)
        h = random.randint(1, 4)
        a = random.randint(80, 255)
        arts.append([x, y, w, h, a])
    return arts


# ---------------------------------------------------------------------------
# Overlay widget
# ---------------------------------------------------------------------------

class GlitchOverlay(QWidget):
    def __init__(self):
        super().__init__()
        scr = QApplication.primaryScreen().geometry()
        self.setGeometry(scr)
        self.setWindowFlags(
            Qt.WindowType.Tool |
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        self._bands    = []
        self._cols     = []
        self._arts     = []
        self._split_w  = 0       # width of center vertical split
        self._split_a  = 0       # alpha of split
        self._phase    = 0       # 0=off 1=bands 2=tear 3=fade
        self._timers   = []
        self.hide()

    # ── helpers ──────────────────────────────────────────────────────────

    def _cx_cy(self):
        s = QApplication.primaryScreen().geometry()
        return s.x() + s.width() // 2, s.y() + s.height() // 2

    def _sw_sh(self):
        s = QApplication.primaryScreen().geometry()
        return s.width(), s.height()

    def _delay(self, ms, fn):
        t = QTimer(self)
        t.setSingleShot(True)
        t.timeout.connect(fn)
        t.start(ms)
        self._timers.append(t)
        return t

    def _repeat(self, ms, fn):
        t = QTimer(self)
        t.setInterval(ms)
        t.timeout.connect(fn)
        t.start()
        self._timers.append(t)
        return t

    def _stop_all(self):
        for t in self._timers:
            t.stop()
        self._timers.clear()

    # ── paint ─────────────────────────────────────────────────────────────

    def paintEvent(self, _):
        if self._phase == 0:
            return
        p = QPainter(self)
        cx, cy = self._cx_cy()
        sw, sh = self._sw_sh()

        # — corruption bands (displaced horizontal strips) —
        for (y, h, shift, alpha) in self._bands:
            # paint a teal line where the band edge is
            c = _teal(alpha)
            p.fillRect(shift, y, sw, 1, c)
            # faint dim fill for the displaced band itself
            c2 = _teal(alpha // 5)
            p.fillRect(shift, y, sw, h, c2)

        # — vertical data-leak columns —
        for (x, y, w, h, a) in self._cols:
            p.fillRect(x, y, w, h, _teal(a))

        # — center vertical split / tear —
        if self._split_w > 0 and self._split_a > 0:
            # white hot core line
            p.fillRect(cx - 1, 0, 2, sh, _white(self._split_a))
            # teal glow around it
            for gw, ga in [(6, 40), (14, 20), (28, 10)]:
                p.fillRect(cx - gw, 0, gw * 2, sh, _teal(ga * self._split_a // 255))

        # — pixel artifacts —
        for art in self._arts:
            x, y, w, h, a = art
            if a > 0:
                p.fillRect(x, y, w, h, _teal(a))

        # — horizontal scanlines across the full screen (subtle) —
        for i in range(0, sh, 4):
            p.fillRect(0, i, sw, 1, QColor(0, 0, 0, 18))

        p.end()

    # ── open ──────────────────────────────────────────────────────────────

    def run_open(self, on_done):
        self._stop_all()
        cx, cy = self._cx_cy()
        sw, sh = self._sw_sh()
        self._bands   = []
        self._cols    = []
        self._arts    = []
        self._split_w = 0
        self._split_a = 0
        self._phase   = 1
        self.show()
        self.raise_()

        # Phase 1 (0–120ms): corruption bands shake in
        _b = [0]
        def band_tick():
            _b[0] += 1
            self._bands = _gen_bands(cy, sw, sh)
            self.update()
        t_bands = self._repeat(14, band_tick)
        self._delay(120, t_bands.stop)

        # Phase 2 (60ms): data-leak columns appear
        def add_cols():
            self._cols = _gen_glitch_cols(cx, sw, sh)
            self.update()
        self._delay(60, add_cols)

        # Phase 3 (120ms): center tear rips open
        def start_tear():
            self._arts = _gen_artifacts(cx, cy)
            _t = [0]
            def tear_tick():
                _t[0] += 1
                self._split_a = min(255, _t[0] * 28)
                self._split_w = min(30, _t[0] * 4)
                # refresh bands for flicker
                self._bands = _gen_bands(cy, sw, sh) if _t[0] % 2 == 0 else self._bands
                self.update()
            t_tear = self._repeat(12, tear_tick)
            self._delay(160, t_tear.stop)
        self._delay(120, start_tear)

        # Phase 4 (280ms): all fades out
        def start_fade():
            self._cols  = []
            self._bands = []
            _f = [0]
            def fade_tick():
                _f[0] += 1
                self._split_a = max(0, self._split_a - 22)
                for art in self._arts:
                    art[4] = max(0, art[4] - 18)
                self.update()
            t_fade = self._repeat(14, fade_tick)
            self._delay(160, t_fade.stop)
        self._delay(280, start_fade)

        # Done
        def finish():
            self._phase = 0
            self._stop_all()
            self.hide()
            on_done()
        self._delay(460, finish)

    # ── close ─────────────────────────────────────────────────────────────

    def run_close(self, on_done):
        self._stop_all()
        cx, cy = self._cx_cy()
        sw, sh = self._sw_sh()
        self._phase   = 1
        self._split_a = 200
        self._split_w = 20
        self._arts    = _gen_artifacts(cx, cy, count=20)
        self._bands   = _gen_bands(cy, sw, sh)
        self._cols    = []
        self.show()
        self.update()

        _f = [0]
        def fade_tick():
            _f[0] += 1
            self._split_a = max(0, self._split_a - 26)
            self._bands   = _gen_bands(cy, sw, sh) if _f[0] % 2 == 0 else []
            for art in self._arts:
                art[4] = max(0, art[4] - 22)
            self.update()
        t = self._repeat(14, fade_tick)
        self._delay(200, t.stop)

        def finish():
            self._phase = 0
            self._stop_all()
            self.hide()
            on_done()
        self._delay(260, finish)
