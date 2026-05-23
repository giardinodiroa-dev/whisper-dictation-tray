TEAL       = '#00FFD4'
TEAL_DIM   = '#007A64'
BG         = '#000000'
BG2        = '#0a0a0a'
BORDER     = '#1a1a1a'
FG         = '#00FFD4'
FG_DIM     = 'rgba(0,255,212,0.35)'
FONT       = 'Courier New'
FONT_SZ    = 9
WIDTH      = 300

def row_style():
    return (
        f"background: {BG2}; border: none; border-radius: 3px; padding: 2px 6px;"
    )

def label_style(dim=False):
    color = FG_DIM if dim else FG
    return f"color: {color}; font-family: '{FONT}'; font-size: {FONT_SZ}px; background: transparent; border: none;"

def section_title_style():
    return (
        f"color: {TEAL}; font-family: '{FONT}'; font-size: 8px; letter-spacing: 2px;"
        f" background: transparent; border: none; text-transform: uppercase;"
    )

def button_style():
    return (
        f"QPushButton {{ background: transparent; color: {TEAL}; font-family: '{FONT}';"
        f" font-size: {FONT_SZ}px; border: 1px solid {TEAL_DIM}; border-radius: 3px;"
        f" padding: 2px 10px; }}"
        f"QPushButton:hover {{ background: rgba(0,255,212,0.08); }}"
        f"QPushButton:pressed {{ background: rgba(0,255,212,0.15); }}"
    )

def toggle_style():
    return (
        f"QCheckBox {{ color: {TEAL}; font-family: '{FONT}'; font-size: {FONT_SZ}px;"
        f" background: transparent; spacing: 6px; }}"
        f"QCheckBox::indicator {{ width: 28px; height: 14px; border-radius: 7px;"
        f" border: 1px solid {TEAL_DIM}; background: {BG}; }}"
        f"QCheckBox::indicator:checked {{ background: {TEAL}; border-color: {TEAL}; }}"
    )

def window_style():
    return (
        f"background: {BG}; border: 1px solid {TEAL_DIM}; border-radius: 6px;"
    )
