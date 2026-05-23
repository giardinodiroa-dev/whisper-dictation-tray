import subprocess
from pathlib import Path
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QCheckBox, QFrame
from core.style import *

LABEL = 'voidmode'
_FLAG = Path.home() / '.cache/voidmode'


def _is_active():
    return _FLAG.exists()


def _signal_tray():
    subprocess.Popen(['pkill', '-USR1', '-f', 'voidmode-tray'], stderr=subprocess.DEVNULL)


def _make_row(parent_layout):
    frame = QFrame()
    frame.setStyleSheet(f'background: {BG2}; border: none; border-radius: 3px; padding: 2px 6px;')
    row = QHBoxLayout(frame)
    row.setContentsMargins(6, 4, 6, 4)
    row.setSpacing(10)
    parent_layout.addWidget(frame)
    return row


def create_panel(ctx) -> QWidget:
    panel = QWidget()
    panel.setStyleSheet(f'background: {BG};')
    vbox = QVBoxLayout(panel)
    vbox.setContentsMargins(8, 8, 8, 8)
    vbox.setSpacing(4)

    # --- voidblack toggle ---
    row1 = _make_row(vbox)
    chk = QCheckBox('voidblack mode')
    chk.setStyleSheet(toggle_style())
    chk.setCursor(Qt.CursorShape.PointingHandCursor)
    chk.setChecked(_is_active())

    status = QLabel('● active' if _is_active() else '○ inactive')
    status.setStyleSheet(f"color: {TEAL}; font-family: '{FONT}'; font-size: {FONT_SZ}px;"
                         f" background: transparent; border: none;")

    def _update_status(active):
        if active:
            status.setText('● active')
            status.setStyleSheet(f"color: {TEAL}; font-family: '{FONT}'; font-size: {FONT_SZ}px;"
                                 f" background: transparent; border: none;")
        else:
            status.setText('○ inactive')
            status.setStyleSheet(f"color: {TEAL_DIM}; font-family: '{FONT}'; font-size: {FONT_SZ}px;"
                                 f" background: transparent; border: none;")

    def _on_toggle(checked):
        if checked:
            _FLAG.touch()
        else:
            _FLAG.unlink(missing_ok=True)
        _signal_tray()
        _update_status(checked)

    chk.toggled.connect(_on_toggle)
    row1.addWidget(chk)
    row1.addStretch()
    row1.addWidget(status)

    # --- restart tray ---
    row2 = _make_row(vbox)
    lbl_tray = QLabel('tray')
    lbl_tray.setStyleSheet(label_style())
    btn_restart = QPushButton('restart tray')
    btn_restart.setStyleSheet(button_style())
    btn_restart.setCursor(Qt.CursorShape.PointingHandCursor)

    def _restart_tray():
        subprocess.Popen(['pkill', '-f', 'voidmode-tray.py'], stderr=subprocess.DEVNULL)
        subprocess.Popen(
            ['nohup', 'python', str(Path.home() / 'bin/voidmode-tray.py')],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    btn_restart.clicked.connect(_restart_tray)
    row2.addWidget(lbl_tray)
    row2.addStretch()
    row2.addWidget(btn_restart)

    vbox.addStretch()
    return panel
