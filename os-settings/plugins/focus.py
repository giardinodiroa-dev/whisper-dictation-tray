import re, subprocess
from pathlib import Path
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QSlider, QFrame
from core.style import *

LABEL = 'focus'
CONST_PY = Path.home() / 'focus/const.py'


def _read_strip_w():
    m = re.search(r'STRIP_W\s*=\s*(\d+)', CONST_PY.read_text())
    return int(m.group(1)) if m else 76


def _write_strip_w(val):
    text = CONST_PY.read_text()
    CONST_PY.write_text(re.sub(r'(STRIP_W\s*=\s*)\d+', rf'\g<1>{val}', text))


def _restart_daemon():
    subprocess.Popen(['systemctl', '--user', 'restart', 'focus-daemon'])


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

    # --- strip width ---
    row1 = _make_row(vbox)
    lbl_name = QLabel('strip width')
    lbl_name.setStyleSheet(label_style())
    px_val = _read_strip_w()
    lbl_px = QLabel(f'{px_val} px')
    lbl_px.setStyleSheet(label_style())
    slider = QSlider(Qt.Orientation.Horizontal)
    slider.setRange(40, 120)
    slider.setSingleStep(4)
    slider.setPageStep(4)
    slider.setValue(px_val)
    slider.setStyleSheet(
        f'QSlider::groove:horizontal {{ height: 4px; background: {BORDER}; border-radius: 2px; }}'
        f'QSlider::handle:horizontal {{ background: {TEAL}; width: 12px; height: 12px;'
        f' margin: -4px 0; border-radius: 6px; }}'
        f'QSlider::sub-page:horizontal {{ background: {TEAL_DIM}; border-radius: 2px; }}'
    )

    def _on_release():
        v = (slider.value() // 4) * 4  # snap to step
        slider.setValue(v)
        lbl_px.setText(f'{v} px')
        _write_strip_w(v)
        _restart_daemon()

    def _on_move(v):
        lbl_px.setText(f'{(v // 4) * 4} px')

    slider.sliderReleased.connect(_on_release)
    slider.valueChanged.connect(_on_move)
    row1.addWidget(lbl_name)
    row1.addStretch()
    row1.addWidget(lbl_px)
    row1.addWidget(slider)

    # --- clear tasks ---
    row2 = _make_row(vbox)
    lbl_clear = QLabel('tasks')
    lbl_clear.setStyleSheet(label_style())
    btn_clear = QPushButton('clear tasks')
    btn_clear.setStyleSheet(button_style())
    btn_clear.setCursor(Qt.CursorShape.PointingHandCursor)
    btn_clear.clicked.connect(lambda: subprocess.Popen([
        'python3', '-c',
        "import sys; sys.path.insert(0,__import__('os').path.expanduser('~/focus'));"
        " from tasks import TaskStore; TaskStore().clear()"
    ]))
    row2.addWidget(lbl_clear)
    row2.addStretch()
    row2.addWidget(btn_clear)

    # --- restart daemon ---
    row3 = _make_row(vbox)
    lbl_daemon = QLabel('daemon')
    lbl_daemon.setStyleSheet(label_style())
    btn_restart = QPushButton('restart daemon')
    btn_restart.setStyleSheet(button_style())
    btn_restart.setCursor(Qt.CursorShape.PointingHandCursor)
    btn_restart.clicked.connect(_restart_daemon)
    row3.addWidget(lbl_daemon)
    row3.addStretch()
    row3.addWidget(btn_restart)

    vbox.addStretch()
    return panel
