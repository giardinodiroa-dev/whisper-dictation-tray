import subprocess
from pathlib import Path

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QComboBox, QCheckBox, QPushButton, QFrame,
)
from core.style import (
    BG, BG2, TEAL, TEAL_DIM, FONT, FONT_SZ,
    label_style, button_style, toggle_style, row_style,
)

LABEL = 'dictation'


def _read_env():
    p = Path.home() / 'bin' / '.env'
    try:
        return dict(l.split('=', 1) for l in p.read_text().splitlines()
                    if '=' in l and not l.strip().startswith('#'))
    except Exception:
        return {}


def _write_env(key, val):
    p = Path.home() / 'bin' / '.env'
    try:
        lines = p.read_text().splitlines() if p.exists() else []
        found = False
        for i, l in enumerate(lines):
            if l.strip().startswith(key + '='):
                lines[i] = f'{key}={val}'; found = True; break
        if not found:
            lines.append(f'{key}={val}')
        p.write_text('\n'.join(lines) + '\n')
    except Exception:
        pass


def _reload():
    subprocess.Popen(['pkill', '-USR1', '-f', 'dictation-tray-stream'],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _make_row(parent_layout):
    row = QFrame()
    row.setStyleSheet(f'background: {BG2}; border: none; border-radius: 3px;')
    h = QHBoxLayout(row)
    h.setContentsMargins(6, 4, 6, 4)
    h.setSpacing(8)
    parent_layout.addWidget(row)
    return h


def create_panel(ctx) -> QWidget:
    env = _read_env()

    panel = QWidget()
    panel.setStyleSheet(f'background: {BG};')
    vbox = QVBoxLayout(panel)
    vbox.setContentsMargins(0, 0, 0, 0)
    vbox.setSpacing(4)

    # --- AI Provider ---
    h1 = _make_row(vbox)
    lbl_provider = QLabel('AI provider')
    lbl_provider.setStyleSheet(label_style())
    box = QComboBox()
    box.addItems(['groq', 'ollama', 'built-in', 'off'])
    current = env.get('DICTATION_PROVIDER', 'built-in')
    idx = box.findText(current)
    if idx >= 0:
        box.setCurrentIndex(idx)
    box.setStyleSheet(
        f"QComboBox {{ background: {BG}; color: {TEAL}; font-family: '{FONT}'; font-size: {FONT_SZ}px;"
        f" border: 1px solid {TEAL_DIM}; border-radius: 3px; padding: 2px 6px; }}"
        f"QComboBox::drop-down {{ border: none; }}"
        f"QComboBox QAbstractItemView {{ background: {BG}; color: {TEAL};"
        f" selection-background-color: rgba(0,255,212,0.15); }}"
    )
    box.currentTextChanged.connect(lambda v: (_write_env('DICTATION_PROVIDER', v), _reload()))
    h1.addWidget(lbl_provider)
    h1.addStretch()
    h1.addWidget(box)

    # --- Restore focus toggle ---
    h2 = _make_row(vbox)
    chk = QCheckBox('restore focus after dictation')
    chk.setStyleSheet(toggle_style())
    chk.setChecked(env.get('DICTATION_RESTORE_FOCUS', 'true').lower() == 'true')
    chk.stateChanged.connect(
        lambda s: (_write_env('DICTATION_RESTORE_FOCUS', 'true' if s else 'false'), _reload())
    )
    h2.addWidget(chk)
    h2.addStretch()

    # --- Restart button ---
    h3 = _make_row(vbox)
    btn = QPushButton('restart dictation')
    btn.setStyleSheet(button_style())
    btn.clicked.connect(_restart)
    h3.addStretch()
    h3.addWidget(btn)

    vbox.addStretch()
    return panel


def _restart():
    subprocess.Popen(['pkill', '-f', 'dictation-tray-stream.py'],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.Popen(
        ['nohup', 'python', str(Path.home() / 'bin' / 'dictation-tray-stream.py')],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
