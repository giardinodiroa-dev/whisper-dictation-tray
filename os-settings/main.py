#!/usr/bin/env python3
"""os-settings — voidblack system tray settings hub."""

import os
import sys

os.environ.setdefault('DISPLAY', ':0')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt6.QtCore import Qt, QPoint, QRect, QTimer, QPointF, QRectF
from PyQt6.QtGui import QColor, QPainter, QPen, QFont, QIcon, QPixmap, QCursor, QPolygonF
from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QFrame, QScrollArea, QSystemTrayIcon, QMenu
)

from core.context import AppContext
from core.hub import PluginHub
from core.style import (
    TEAL, TEAL_DIM, BG, BG2, FONT, FONT_SZ, WIDTH,
    section_title_style, label_style, button_style, window_style
)


# ── tray icon ─────────────────────────────────────────────────────────────────

def _make_tray_icon(size: int = 64) -> QIcon:
    import math
    TEAL_C = QColor("#00FFD4")
    px = QPixmap(size, size)
    px.fill(Qt.GlobalColor.transparent)
    p = QPainter(px)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(TEAL_C)
    p.setPen(Qt.PenStyle.NoPen)

    cx, cy   = size / 2, size / 2
    r_outer  = size * 0.46   # tip of teeth
    r_inner  = size * 0.32   # base of teeth
    r_hole   = size * 0.16   # center hole
    teeth    = 8
    pts      = []
    for i in range(teeth):
        for frac, r in [(0.0, r_inner), (0.3, r_outer), (0.7, r_outer), (1.0, r_inner)]:
            angle = 2 * math.pi * (i + frac) / teeth - math.pi / 2
            pts.append(QPointF(cx + r * math.cos(angle), cy + r * math.sin(angle)))

    p.drawPolygon(QPolygonF(pts))
    # punch hole in center
    p.setBrush(QColor(0, 0, 0, 0))
    p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
    p.drawEllipse(QPointF(cx, cy), r_hole, r_hole)
    p.end()
    return QIcon(px)


# ── section widget ─────────────────────────────────────────────────────────────

class SectionWidget(QWidget):
    def __init__(self, label: str, panel: QWidget, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        # header row
        hdr = QLabel(label.upper())
        hdr.setStyleSheet(section_title_style())
        hdr.setContentsMargins(4, 6, 4, 2)
        layout.addWidget(hdr)

        # divider
        line = QFrame()
        line.setFixedHeight(1)
        line.setStyleSheet(f"background: {TEAL_DIM}; border: none;")
        layout.addWidget(line)

        layout.addWidget(panel)


# ── settings window ───────────────────────────────────────────────────────────

class SettingsWindow(QWidget):
    def __init__(self, hub: PluginHub):
        super().__init__()
        self._hub = hub

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFixedWidth(WIDTH)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(1, 1, 1, 1)
        outer.setSpacing(0)

        self._inner = QWidget()
        self._inner.setObjectName("inner")
        self._inner.setStyleSheet(f"#inner {{ {window_style()} }}")

        inner_layout = QVBoxLayout(self._inner)
        inner_layout.setContentsMargins(10, 10, 10, 10)
        inner_layout.setSpacing(10)

        # title bar
        title = QLabel("⬡  OS SETTINGS")
        title.setStyleSheet(
            f"color: {TEAL}; font-family: '{FONT}'; font-size: 10px;"
            f" letter-spacing: 3px; background: transparent;"
        )
        inner_layout.addWidget(title)

        top_line = QFrame()
        top_line.setFixedHeight(1)
        top_line.setStyleSheet(f"background: {TEAL_DIM}; border: none;")
        inner_layout.addWidget(top_line)

        # scrollable plugin panels
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet(
            "QScrollArea { border: none; background: transparent; }"
            f"QScrollBar:vertical {{ background: {BG}; width: 4px; border: none; }}"
            f"QScrollBar::handle:vertical {{ background: {TEAL_DIM}; border-radius: 2px; }}"
            f"QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0px; }}"
        )

        panels_widget = QWidget()
        panels_widget.setStyleSheet(f"background: transparent;")
        panels_layout = QVBoxLayout(panels_widget)
        panels_layout.setContentsMargins(0, 0, 0, 0)
        panels_layout.setSpacing(14)

        for label, panel in hub.panels():
            panels_layout.addWidget(SectionWidget(label, panel))

        panels_layout.addStretch()
        scroll.setWidget(panels_widget)
        inner_layout.addWidget(scroll)

        outer.addWidget(self._inner)
        self.adjustSize()

    def show_near_tray(self, tray: QSystemTrayIcon):
        geo = tray.geometry()
        screen = QApplication.primaryScreen().availableGeometry()
        w, h = self.width(), min(self.sizeHint().height(), screen.height() - 80)
        self._inner.setFixedHeight(h)
        self.setFixedHeight(h + 2)
        # position above tray icon, right-aligned to screen
        x = screen.right() - w - 4
        y = screen.bottom() - h - 4
        self.move(x, y)

    def paintEvent(self, _):
        pass

    def focusOutEvent(self, _):
        self.hide()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.hide()


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    ctx = AppContext(app=app)

    plugins_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'plugins')
    hub = PluginHub(plugins_dir, ctx)

    window = SettingsWindow(hub)
    ctx.window = window

    tray = QSystemTrayIcon(_make_tray_icon(), app)
    tray.setToolTip("OS Settings")

    def _toggle():
        if window.isVisible():
            window.hide()
        else:
            window.show_near_tray(tray)
            window.show()
            window.raise_()
            window.activateWindow()

    tray.activated.connect(lambda reason: (
        _toggle() if reason == QSystemTrayIcon.ActivationReason.Trigger else None
    ))

    menu = QMenu()
    menu.setStyleSheet(
        f"QMenu {{ background: {BG}; color: {TEAL}; font-family: '{FONT}';"
        f" font-size: {FONT_SZ}px; border: 1px solid {TEAL_DIM}; }}"
        f"QMenu::item:selected {{ background: rgba(0,255,212,0.1); }}"
    )
    menu.addAction("Settings").triggered.connect(_toggle)
    menu.addSeparator()
    menu.addAction("Quit").triggered.connect(app.quit)
    tray.setContextMenu(menu)

    tray.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
