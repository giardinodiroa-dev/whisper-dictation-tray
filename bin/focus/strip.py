"""strip.py — floating vertical task tags on right screen edge."""

import subprocess

from PyQt6.QtCore import Qt, pyqtSignal, QRect, QTimer
from PyQt6.QtGui import QColor, QPainter, QPen, QFont, QFontMetrics, QTransform
from PyQt6.QtWidgets import QApplication, QWidget

from const import TEAL, BG, STRIP_W, FONT

_TAG_H    = 28    # height of each tag pill
_TAG_PAD  = 4     # gap between tags
_TAG_W    = STRIP_W
_FONT_SZ  = 9
_MAX_CHARS = 14   # truncate label


class TaskStrip(QWidget):
    task_clicked  = pyqtSignal(str, int)   # win_id, desktop
    task_removed  = pyqtSignal(int)        # task_id
    task_preview  = pyqtSignal(str, int)   # description, task_id

    def __init__(self, parent=None):
        super().__init__(parent)
        self._tasks   = []
        self._hovered = -1    # index of hovered task, -1 = none

        scr = QApplication.primaryScreen().geometry()
        self._sw = scr.width()
        self._sh = scr.height()

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.X11BypassWindowManagerHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)

        self.setGeometry(
            scr.x() + scr.width() - _TAG_W,
            scr.y(),
            _TAG_W,
            scr.height(),
        )
        self.setMouseTracking(True)

    # ── pin to screen ─────────────────────────────────────────────────────

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(200, self._pin)

    def _pin(self):
        try:
            wid = hex(int(self.winId()))
            subprocess.Popen(
                ["xprop", "-id", wid,
                 "-f", "_NET_WM_DESKTOP", "32c", "-set", "_NET_WM_DESKTOP", "0xFFFFFFFF"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            subprocess.Popen(
                ["xprop", "-id", wid,
                 "-f", "_NET_WM_STATE", "32a",
                 "-set", "_NET_WM_STATE", "_NET_WM_STATE_STICKY,_NET_WM_STATE_ABOVE"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass

    # ── data ──────────────────────────────────────────────────────────────

    def refresh(self, tasks: list):
        self._tasks = tasks
        self.update()

    def _tag_rect(self, idx: int) -> QRect:
        """Bounding rect for tag pill at index idx (in widget coords)."""
        y = idx * (_TAG_H + _TAG_PAD) + _TAG_PAD
        return QRect(0, y, _TAG_W, _TAG_H)

    def _task_at(self, y: int) -> int:
        """Return task index under y, or -1."""
        for i in range(len(self._tasks)):
            if self._tag_rect(i).contains(0, y):
                return i
        return -1

    # ── painting ──────────────────────────────────────────────────────────

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        font = QFont(FONT, _FONT_SZ)
        font.setWeight(QFont.Weight.Medium)
        p.setFont(font)
        fm = QFontMetrics(font)

        tr, tg, tb = 0, 255, 212   # TEAL_RGB

        for i, task in enumerate(self._tasks):
            rect   = self._tag_rect(i)
            hovered = (i == self._hovered)
            label  = task.get("label", "")
            if len(label) > _MAX_CHARS:
                label = label[:_MAX_CHARS]

            # pill background
            bg_alpha = 180 if hovered else 110
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(0, 0, 0, bg_alpha))
            p.drawRoundedRect(rect, 3, 3)

            # teal left border
            p.fillRect(rect.x(), rect.y(), 2, rect.height(),
                       QColor(tr, tg, tb, 220))

            # horizontal tiny text, centered vertically in pill
            text_color = QColor(tr, tg, tb, 255 if hovered else 180)
            p.setPen(text_color)
            text_rect = QRect(rect.x() + 4, rect.y(), rect.width() - 16, rect.height())
            p.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, label)

            # × always visible, brighter on hover
            x_rect = QRect(rect.right() - 12, rect.top() + 2, 10, 10)
            p.setPen(QColor(tr, tg, tb, 220 if hovered else 80))
            p.setFont(QFont(FONT, 7))
            p.drawText(x_rect, Qt.AlignmentFlag.AlignCenter, "×")

        p.end()

    # ── mouse ─────────────────────────────────────────────────────────────

    def mouseMoveEvent(self, event):
        idx = self._task_at(event.pos().y())
        if idx != self._hovered:
            self._hovered = idx
            self.update()

    def leaveEvent(self, event):
        self._hovered = -1
        self.update()

    def mousePressEvent(self, event):
        idx = self._task_at(event.pos().y())
        if idx < 0 or idx >= len(self._tasks):
            return
        task = self._tasks[idx]

        if event.button() == Qt.MouseButton.RightButton:
            desc = task.get("description") or task.get("label", "")
            self.task_preview.emit(desc, task["id"])
            return

        if event.button() != Qt.MouseButton.LeftButton:
            return

        if event.pos().x() >= _TAG_W - 14:
            self.task_removed.emit(task["id"])
        else:
            self.task_clicked.emit(str(task.get("win_id", "")),
                                   int(task.get("desktop", 0)))
