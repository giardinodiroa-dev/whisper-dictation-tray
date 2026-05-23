#!/usr/bin/env python3
"""focus daemon — Super+Space focus capture system."""

import os
import sys
import socket
import subprocess

# Ensure sibling modules are importable regardless of cwd
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt6.QtWidgets import QApplication, QWidget, QLabel
from PyQt6.QtCore import Qt, QTimer, QSocketNotifier
from PyQt6.QtGui import QColor, QPainter, QPen, QFont

from const import TEAL, BG, FONT, SOCK_PATH
from fx import GlitchOverlay
from tasks import TaskStore
from strip import TaskStrip
from capture import CapturePopup


# ---------------------------------------------------------------------------
# Inline toast (avoids spawning a child process)
# ---------------------------------------------------------------------------

class _Toast(QWidget):
    def __init__(self, msg: str):
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.X11BypassWindowManagerHint |
            Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)

        lbl = QLabel(msg, self)
        lbl.setFont(QFont(FONT, 9))
        lbl.setStyleSheet(f"color: {TEAL}; background: transparent;")
        lbl.adjustSize()

        pad_x, pad_y = 16, 5
        w = lbl.width() + pad_x * 2
        h = lbl.height() + pad_y * 2
        self.setFixedSize(w, h)
        lbl.move(pad_x, pad_y)

        screen = QApplication.primaryScreen().availableGeometry()
        self.move(screen.x() + (screen.width() - w) // 2, screen.y() + 32)

        QTimer.singleShot(100, self._pin)
        QTimer.singleShot(1400, self.close)

    def _pin(self):
        try:
            wid = hex(int(self.winId()))
            subprocess.Popen(
                ["xprop", "-id", wid, "-f", "_NET_WM_DESKTOP", "32c",
                 "-set", "_NET_WM_DESKTOP", "0xFFFFFFFF"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setBrush(QColor(0, 0, 0, 220))
        p.setPen(QPen(QColor(0, 255, 212, 160), 1))
        p.drawRoundedRect(0, 0, self.width() - 1, self.height() - 1, 4, 4)
        p.end()


# ---------------------------------------------------------------------------
# Description preview popup (right-click on tag)
# ---------------------------------------------------------------------------

class _DescPopup(QWidget):
    def __init__(self, text: str):
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.X11BypassWindowManagerHint |
            Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)

        lbl = QLabel(text, self)
        lbl.setFont(QFont(FONT, 8))
        lbl.setWordWrap(True)
        lbl.setMaximumWidth(320)
        lbl.setStyleSheet(f"color: {TEAL}; background: transparent;")
        lbl.adjustSize()

        pad_x, pad_y = 14, 6
        w = min(lbl.width() + pad_x * 2, 340)
        lbl.setFixedWidth(w - pad_x * 2)
        lbl.adjustSize()
        h = lbl.height() + pad_y * 2
        self.setFixedSize(w, h)
        lbl.move(pad_x, pad_y)

        screen = QApplication.primaryScreen().availableGeometry()
        self.move(screen.x() + (screen.width() - w) // 2, screen.y() + 8)

        QTimer.singleShot(2200, self.close)

    def mousePressEvent(self, _):
        self.close()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setBrush(QColor(0, 0, 0, 230))
        p.setPen(QPen(QColor(0, 255, 212, 120), 1))
        p.drawRoundedRect(0, 0, self.width() - 1, self.height() - 1, 4, 4)
        p.end()


# ---------------------------------------------------------------------------
# Helpers — X11 window info
# ---------------------------------------------------------------------------

def _get_active_window():
    """Returns (win_id_hex, win_name, desktop_int)."""
    try:
        win_id = subprocess.check_output(
            ["xdotool", "getactivewindow"], text=True
        ).strip()
    except Exception:
        return "", "", 0

    try:
        win_name = subprocess.check_output(
            ["xdotool", "getwindowname", win_id], text=True
        ).strip()
    except Exception:
        win_name = ""

    try:
        desktop = int(subprocess.check_output(
            ["xdotool", "get_desktop"], text=True
        ).strip())
    except Exception:
        desktop = 0

    return win_id, win_name, desktop


def _focus_window(win_id: str, desktop: int):
    """Switch to desktop and raise window."""
    try:
        subprocess.Popen(["xdotool", "set_desktop", str(desktop)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.Popen(["xdotool", "windowactivate", "--sync", win_id],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Daemon
# ---------------------------------------------------------------------------

class FocusDaemon:
    def __init__(self, app: QApplication):
        self._app = app
        self._busy = False           # prevent re-entrant captures
        self._toast = None           # keep toast alive (ref)
        self._popup = None           # keep popup alive
        self._preview = None         # keep preview popup alive

        self._store = TaskStore()
        self._glitch = GlitchOverlay()
        self._strip = TaskStrip()
        self._strip.task_clicked.connect(self._on_task_clicked)
        self._strip.task_removed.connect(self._on_task_removed)
        self._strip.task_preview.connect(self._on_task_preview)
        self._strip.show()
        self._refresh_strip()

        self._setup_socket()

    # ── socket server ────────────────────────────────────────────────────

    def _setup_socket(self):
        if os.path.exists(SOCK_PATH):
            os.unlink(SOCK_PATH)
        self._srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._srv.bind(SOCK_PATH)
        self._srv.listen(5)
        self._srv.setblocking(False)
        self._notifier = QSocketNotifier(
            self._srv.fileno(), QSocketNotifier.Type.Read
        )
        self._notifier.activated.connect(self._on_socket_ready)

    def _on_socket_ready(self, _fd):
        try:
            conn, _ = self._srv.accept()
            data = conn.recv(256).decode().strip()
            conn.close()
        except Exception:
            return
        if data == "CAPTURE":
            self._begin_capture()

    # ── capture flow ─────────────────────────────────────────────────────

    def _begin_capture(self):
        if self._busy:
            # force-reset if stuck (e.g. previous popup killed externally)
            if self._popup is None:
                self._busy = False
            else:
                return
        self._busy = True
        # safety timeout — always unblock after 30s regardless of outcome
        QTimer.singleShot(30_000, self._force_unblock)

        win_id, win_name, desktop = _get_active_window()
        self._pending = (win_id, win_name, desktop)

        self._glitch.run_open(lambda: self._show_toast_and_popup(win_name))

    def _force_unblock(self):
        if self._busy and self._popup is None:
            self._busy = False

    def _show_toast_and_popup(self, win_name: str):
        # Toast
        if win_name:
            self._toast = _Toast(win_name)
            self._toast.show()

        # Input popup after a short delay so toast is visible first
        QTimer.singleShot(300, lambda: self._open_popup(win_name))

    def _open_popup(self, win_name: str):
        self._popup = CapturePopup(win_name)
        self._popup.task_saved.connect(self._on_task_saved)
        self._popup.destroyed.connect(self._on_popup_gone)
        self._popup.show()

    def _on_task_saved(self, label: str, description: str):
        win_id, win_name, desktop = self._pending
        self._store.add(label, description, win_id, win_name, desktop)
        self._refresh_strip()
        self._glitch.run_close(lambda: None)

    def _on_popup_gone(self):
        self._busy = False
        self._popup = None

    # ── strip interactions ────────────────────────────────────────────────

    def _on_task_clicked(self, win_id: str, desktop: int):
        if win_id:
            _focus_window(win_id, desktop)

    def _on_task_removed(self, task_id: int):
        self._store.remove(task_id)
        self._refresh_strip()

    def _on_task_preview(self, description: str, _task_id: int):
        if self._preview:
            self._preview.close()
        self._preview = _DescPopup(description)
        self._preview.show()

    def _refresh_strip(self):
        self._strip.refresh(self._store.all())


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    daemon = FocusDaemon(app)
    sys.exit(app.exec())
