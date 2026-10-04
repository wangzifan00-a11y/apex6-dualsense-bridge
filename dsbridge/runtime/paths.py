"""Immutable resources and per-user runtime state have separate lifetimes."""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import sys


@dataclass(frozen=True)
class RuntimePaths:
    assets: Path
    state: Path

    def __post_init__(self):
        object.__setattr__(self, "assets", Path(self.assets).resolve())
        object.__setattr__(self, "state", Path(self.state).resolve())

    def prepare(self):
        self.state.mkdir(parents=True, exist_ok=True)
        return self

    def __fspath__(self):
        return str(self.state)

    def __truediv__(self, name):
        return self.state / name

    def command(self, *arguments, executable=None):
        args = [str(executable or sys.executable)]
        if executable is None and not getattr(sys, "frozen", False):
            args.append(str(self.assets / "app.py"))
        return args + ["--data-dir", str(self.state), *map(str, arguments)]

    def config(self):
        path = self.state / "viiper-config.json"
        if not path.is_file():
            path = self.assets / "viiper-config.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}

    @classmethod
    def discover(cls, state=None):
        assets = (Path(sys.executable).resolve().parent if getattr(sys, "frozen", False)
                  else Path(__file__).resolve().parents[2])
        local = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
        return cls(assets, Path(state) if state else local / "DSFeedbackBridge")


_current = None


def configure(paths):
    global _current
    _current = paths.prepare()
    return _current


def current_paths():
    return _current or RuntimePaths.discover()


def as_paths(value):
    """Legacy tools may supply one folder; production passes RuntimePaths."""
    if isinstance(value, RuntimePaths):
        return value
    base = Path(value).resolve()
    if _current and base == _current.state:
        return _current
    return RuntimePaths(base, base)
