# Tray Menu Custom Widgets / Custom Painting on Linux

## The Problem

On Linux (KDE Plasma, SNI/StatusNotifierItem), `QSystemTrayIcon.setContextMenu(menu)`
hands the menu off to an **external process** (Plasma shell via D-Bus / libdbusmenu).
That external process renders the menu — your Qt process's `paintEvent` overrides and
`QWidgetAction` embedded widgets never appear. You get blank/empty rows no matter what
you paint or embed.

## Why It's Invisible

- `QWidgetAction` with custom widgets → serialized to D-Bus, widget can't cross process → blank space
- `QMenu.paintEvent` override → runs in your process, external renderer overwrites it → blank space
- `QAction("")` with empty text → zero-height row → `actionGeometry()` returns height=0 → nothing draws

All three fail for the same root reason: **rendering happens outside your process**.

## The Fix

Do **not** use `tray.setContextMenu(menu)`.
Instead intercept right-click in `tray.activated` and call `menu.popup()` directly.
This forces Qt to show the menu **in your process** — custom painting and drag interaction work.

```python
def _on_tray_activated(reason):
    if reason == QSystemTrayIcon.Context:
        from PyQt5.QtGui import QCursor
        menu.popup(QCursor.pos())
    elif reason == QSystemTrayIcon.Trigger:
        on_toggle()  # or whatever left-click does

tray.activated.connect(_on_tray_activated)
# Do NOT call tray.setContextMenu(menu)
```

`menu.aboutToShow` still fires when `menu.popup()` is called, so rebuild hooks work normally.

## Custom Painting in the Menu

With the fix above, `paintEvent` overrides work. For interactive rows (e.g. sliders):

1. Add a `QAction` with non-empty text (spaces are fine) so Qt allocates proper row height:
   ```python
   a = QAction(" " * 30, menu)
   a.setObjectName("__slider_0")
   a.setEnabled(False)
   menu.addAction(a)
   ```

2. Override `paintEvent` in your `QMenu` subclass — call `super().paintEvent(event)` first,
   then create a new `QPainter(self)` and draw over the action's rect:
   ```python
   def paintEvent(self, event):
       super().paintEvent(event)
       p = QPainter(self)
       for act in self.actions():
           if not act.objectName().startswith("__slider_"):
               continue
           r = self.actionGeometry(act)
           # r is now a valid non-zero rect — draw freely
           p.fillRect(r, QColor(0, 0, 0))
           p.setPen(QColor(0, 255, 212))
           p.drawText(r, Qt.AlignCenter, "your content")
       p.end()
   ```

3. Override `mousePressEvent` / `mouseMoveEvent` for drag interaction:
   ```python
   def mousePressEvent(self, e):
       act = self.actionAt(e.pos())
       if act and act.objectName().startswith("__slider_"):
           # handle drag — do NOT call super() so menu stays open
           self._dragging = int(act.objectName().split("_")[-1])
           self.update()
           return
       super().mousePressEvent(e)
   ```

## Key Rules

| Approach | Works on Linux SNI? |
|---|---|
| `tray.setContextMenu(menu)` + custom painting | NO |
| `tray.setContextMenu(menu)` + `QWidgetAction` | NO |
| `menu.popup(QCursor.pos())` + custom painting | YES |
| `menu.popup(QCursor.pos())` + `QWidgetAction` | YES |

## Tested On

- Arch Linux 6.19, KDE Plasma, Qt5 / PyQt5
