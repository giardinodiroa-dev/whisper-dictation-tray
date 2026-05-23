import importlib.util
from pathlib import Path


class PluginHub:
    def __init__(self, plugins_dir: str, ctx):
        self._ctx = ctx
        self._plugins: list = []
        self._load_all(plugins_dir)

    def _load_all(self, plugins_dir: str):
        for path in sorted(Path(plugins_dir).glob('*.py')):
            if path.stem.startswith('_'):
                continue
            try:
                spec = importlib.util.spec_from_file_location(path.stem, path)
                mod  = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                self._plugins.append(mod)
            except Exception as e:
                print(f'[os-settings] failed to load plugin {path.name}: {e}')

    def panels(self) -> list:
        """Return list of (label, QWidget) for each loaded plugin."""
        result = []
        for mod in self._plugins:
            label = getattr(mod, 'LABEL', mod.__name__)
            try:
                panel = mod.create_panel(self._ctx)
                if panel is not None:
                    result.append((label, panel))
            except Exception as e:
                print(f'[os-settings] plugin "{label}" panel error: {e}')
        return result
