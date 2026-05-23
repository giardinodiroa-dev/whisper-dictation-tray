# Qt Worker Thread → Main Thread Result (Future + Poll)

Correct pattern for running a blocking call (AI, subprocess, network) on a
`ThreadPoolExecutor` worker thread and getting the result back to a PyQt6
widget on the **main thread** so it can update the UI.

---

## The Problem

`QTimer.singleShot(0, callback)` called from inside a worker thread **never
fires** — worker threads have no Qt event loop. The callback is silently lost.
Any attempt to touch Qt widgets directly from a worker thread causes crashes or
undefined behaviour.

---

## The Solution

Return a `concurrent.futures.Future` from the thread submission. Poll it from
the main thread using a `QTimer` that checks `future.done()` every 80 ms.

```python
from concurrent.futures import ThreadPoolExecutor
from PyQt6.QtCore import QTimer

_executor = ThreadPoolExecutor(max_workers=2)


def submit_work(arg) -> "Future":
    """Submit blocking call; returns Future immediately (non-blocking)."""
    return _executor.submit(_do_work, arg)


def _do_work(arg):
    # runs on worker thread — no Qt calls allowed here
    result = some_blocking_call(arg)
    return result


class MyWidget(QWidget):

    def start(self, arg):
        future = submit_work(arg)
        self._poll_result(future)

    def _poll_result(self, future):
        timer = QTimer(self)
        timer.setInterval(80)

        def check():
            if future.done():
                timer.stop()
                try:
                    result = future.result()
                    self._on_result(result)   # safe — runs on main thread
                except Exception as e:
                    self._on_error(e)

        timer.timeout.connect(check)
        timer.start()

    def _on_result(self, result):
        # update UI here — main thread, safe
        ...
```

---

## Optional: Thinking Animation While Waiting

Run a second `QTimer` alongside the poll timer to animate the UI during the
wait. Stop both when the future completes.

```python
    def _poll_result(self, future):
        poll  = QTimer(self)
        think = QTimer(self)
        poll.setInterval(80)
        think.setInterval(120)

        dots = ['.', '..', '...']
        tick = [0]

        def _think():
            tick[0] = (tick[0] + 1) % 3
            self._lbl.setText(f"thinking{dots[tick[0]]}")

        def check():
            if future.done():
                poll.stop()
                think.stop()
                try:
                    result = future.result()
                    self._on_result(result)
                except Exception as e:
                    self._on_error(e)

        think.timeout.connect(_think)
        poll.timeout.connect(check)
        think.start()
        poll.start()
```

---

## Real-World Usage

- `~/bin/focus/capture.py` — `_poll_distill()` polls Kilo AI word-distill future
- `~/glitch/features/bar/ai.py` — `_poll_translation()` polls AI command translation

---

## When to Use

- Any PyQt6 widget that needs a result from a blocking call without freezing the UI
- Replaces: callbacks passed into thread workers, `QThread` subclasses for simple tasks

## When NOT to Use

- Streaming responses (need `QThread` + signals for chunk-by-chunk delivery)
- Work that must complete before the widget is shown (just block in `__init__` or use `QSplashScreen`)
