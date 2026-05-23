# PyQt6 System Tray Icon — Pure QPainter (No Image Files)

Pattern for creating a programmatically drawn `QSystemTrayIcon` on KDE Plasma. No PNG/SVG needed — draw any shape with QPainter onto a QPixmap.

**THIS FILE CONTAINS ONE SPECIFIC EXAMPLE (all-seeing eye). Do not reuse the shape — draw whatever icon fits your app. The pattern (QPixmap → QPainter → QIcon → QSystemTrayIcon) is what matters.**

---

## The Pattern

```python
import os
os.environ.setdefault('DISPLAY', ':0')   # required when launched from non-display shell

from PyQt6.QtGui import QColor, QIcon, QPainter, QPixmap
from PyQt6.QtCore import Qt

def make_tray_icon(size: int = 64) -> QIcon:
    px = QPixmap(size, size)
    px.fill(Qt.GlobalColor.transparent)
    p = QPainter(px)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor("#00FFD4"))
    p.setPen(Qt.PenStyle.NoPen)

    # ── draw your shape here ──────────────────────────────────────────────
    # cogwheel, circle, triangle, hamburger lines, letter — anything goes
    # use p.drawPolygon(), p.drawEllipse(), p.drawRoundedRect(), etc.
    # ─────────────────────────────────────────────────────────────────────

    p.end()
    return QIcon(px)   # ← KDE Plasma requires QIcon, NOT raw QPixmap
```

### Wire it up

```python
from PyQt6.QtWidgets import QApplication, QSystemTrayIcon, QMenu
import sys

app = QApplication(sys.argv)
app.setQuitOnLastWindowClosed(False)

tray = QSystemTrayIcon(make_tray_icon(), app)
tray.setToolTip("My App")

menu = QMenu()
menu.addAction("Quit").triggered.connect(app.quit)
tray.setContextMenu(menu)
tray.show()

sys.exit(app.exec())
```

---

## Example shapes

### Cogwheel (settings app)
```python
import math
from PyQt6.QtCore import QPointF
from PyQt6.QtGui import QPolygonF

def make_tray_icon(size=64):
    px = QPixmap(size, size)
    px.fill(Qt.GlobalColor.transparent)
    p = QPainter(px)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor("#00FFD4"))
    p.setPen(Qt.PenStyle.NoPen)
    cx, cy = size/2, size/2
    r_outer, r_inner, r_hole, teeth = size*0.46, size*0.32, size*0.16, 8
    pts = []
    for i in range(teeth):
        for frac, r in [(0.0,r_inner),(0.3,r_outer),(0.7,r_outer),(1.0,r_inner)]:
            a = 2*math.pi*(i+frac)/teeth - math.pi/2
            pts.append(QPointF(cx+r*math.cos(a), cy+r*math.sin(a)))
    p.drawPolygon(QPolygonF(pts))
    p.setBrush(QColor(0,0,0,0))
    p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
    p.drawEllipse(QPointF(cx,cy), r_hole, r_hole)
    p.end()
    return QIcon(px)
```

### All-seeing eye (supervisor/monitor app) — the original example in this file
```python
from PyQt6.QtCore import QPointF, QRectF
from PyQt6.QtGui import QPolygonF

def make_tray_icon(size=64):
    TEAL, BLACK = QColor("#00FFD4"), QColor("#000000")
    px = QPixmap(size, size)
    px.fill(Qt.GlobalColor.transparent)
    p = QPainter(px)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    m, cx = 2, size/2
    tri = QPolygonF([QPointF(cx,m), QPointF(size-m,size-m), QPointF(m,size-m)])
    p.setBrush(TEAL); p.setPen(Qt.PenStyle.NoPen); p.drawPolygon(tri)
    ey, ew, eh = size*0.58, size*0.44, size*0.24
    p.setBrush(BLACK); p.drawEllipse(QRectF(cx-ew/2, ey-eh/2, ew, eh))
    p.setBrush(TEAL);  p.drawEllipse(QPointF(cx,ey), size*0.085, size*0.085)
    p.setBrush(BLACK); p.drawEllipse(QPointF(cx,ey), size*0.04,  size*0.04)
    p.end()
    return QIcon(px)
```

---

## Gotchas

- **KDE Plasma requires `QIcon(px)` not raw `QPixmap`** — `setIcon(pixmap)` raises `TypeError`
- **`DISPLAY=:0` must be set** when launching from a non-display shell (systemd service, cron, SSH)
- **`size=64` minimum** — smaller sizes lose detail at high-DPI
- **KDE hides new tray icons** under the `^` overflow arrow — click it to reveal after first launch
- **Never mix PyQt5 and PyQt6** in the same process — causes immediate segfault with no error output
- `app.setQuitOnLastWindowClosed(False)` is required or the app exits when all windows close

## When to use

- Any PyQt6 daemon or background app that needs a system tray presence
- VoidBlack theme: draw in `#00FFD4` teal on transparent background

## When not to use

- Wayland sessions (use `StatusNotifierItem` protocol instead)
- PyQt5 (adjust enum syntax: `Qt.transparent`, not `Qt.GlobalColor.transparent`)
