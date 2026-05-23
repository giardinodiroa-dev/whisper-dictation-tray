import json, subprocess
from pathlib import Path
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QCheckBox, QFrame
from core.style import *

LABEL = 'glitch'


def _read_gs():
    p = Path.home()/'glitchv.2'/'features'/'bar'/'.glitch_settings.json'
    try: return json.loads(p.read_text())
    except: return {}


def _write_gs(key, val):
    p = Path.home()/'glitchv.2'/'features'/'bar'/'.glitch_settings.json'
    d = _read_gs(); d[key] = val
    try: p.write_text(json.dumps(d, indent=2))
    except: pass


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

    # restart glitch
    row1 = _make_row(vbox)
    lbl_restart = QLabel('process')
    lbl_restart.setStyleSheet(label_style())
    btn_restart = QPushButton('restart glitch')
    btn_restart.setStyleSheet(button_style())
    btn_restart.setCursor(Qt.CursorShape.PointingHandCursor)
    def _restart_glitch():
        subprocess.Popen(['pkill', '-f', 'python.*glitchv.2/engine.py'])
        QTimer.singleShot(800, lambda: subprocess.Popen(
            ['nohup', 'python', str(Path.home()/'glitchv.2'/'engine.py')],
            cwd=str(Path.home()/'glitchv.2'),
            start_new_session=True,
        ))
    btn_restart.clicked.connect(_restart_glitch)
    row1.addWidget(lbl_restart)
    row1.addStretch()
    row1.addWidget(btn_restart)

    # AI enabled
    row2 = _make_row(vbox)
    lbl_ai = QLabel('AI')
    lbl_ai.setStyleSheet(label_style())
    chk_ai = QCheckBox('AI enabled')
    chk_ai.setStyleSheet(
        f'QCheckBox {{ color: {FG}; font-family: "Courier New"; font-size: 12px; }}'
        f'QCheckBox::indicator {{ width: 14px; height: 14px; border: 1px solid {TEAL}; border-radius: 2px; background: {BG}; }}'
        f'QCheckBox::indicator:checked {{ background: {TEAL}; }}'
    )
    chk_ai.setChecked(bool(_read_gs().get('ai_enabled', False)))
    chk_ai.stateChanged.connect(lambda state: _write_gs('ai_enabled', bool(state)))
    row2.addWidget(lbl_ai)
    row2.addStretch()
    row2.addWidget(chk_ai)

    # open settings panel
    row3 = _make_row(vbox)
    lbl_settings = QLabel('settings')
    lbl_settings.setStyleSheet(label_style())
    btn_settings = QPushButton('open glitch settings')
    btn_settings.setStyleSheet(button_style())
    btn_settings.setCursor(Qt.CursorShape.PointingHandCursor)
    _err_label = QLabel('')
    _err_label.setStyleSheet(f'color: {TEAL_DIM}; font-family: "Courier New"; font-size: 11px;')
    _err_label.hide()
    def _open_settings():
        sock = '/tmp/glitch.sock'
        try:
            subprocess.run(
                ['python3', '-c',
                 f"import socket; s=socket.socket(socket.AF_UNIX); s.connect('{sock}');"
                 " s.sendall(b'>>settings'); s.close()"],
                timeout=2, check=True
            )
        except Exception:
            _err_label.setText('glitch not running')
            _err_label.show()
            QTimer.singleShot(3000, _err_label.hide)
    btn_settings.clicked.connect(_open_settings)
    row3.addWidget(lbl_settings)
    row3.addStretch()
    row3.addWidget(_err_label)
    row3.addWidget(btn_settings)

    vbox.addStretch()
    return panel
