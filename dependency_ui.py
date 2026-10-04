"""Compatibility import; implementation lives in dsbridge.ui.dependencies."""
import importlib as _importlib
import sys as _sys
_implementation = _importlib.import_module("dsbridge.ui.dependencies")
if __name__ == "__main__":
    raise SystemExit(_implementation.main())
else:
    _sys.modules[__name__] = _implementation
