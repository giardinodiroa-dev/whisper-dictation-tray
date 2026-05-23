import json
from dataclasses import dataclass, field
from pathlib import Path

_CFG = Path.home() / '.config' / 'os-settings' / 'settings.json'


def _load() -> dict:
    try:
        return json.loads(_CFG.read_text())
    except Exception:
        return {}


def _save(d: dict):
    try:
        _CFG.parent.mkdir(parents=True, exist_ok=True)
        existing = _load()
        existing.update(d)
        _CFG.write_text(json.dumps(existing, indent=2))
    except Exception:
        pass


@dataclass
class AppContext:
    app:    object = None   # QApplication
    window: object = None   # SettingsWindow
    config: dict   = field(default_factory=_load)
    save:   callable = staticmethod(_save)
