"""Compatibility import; implementation lives in dsbridge.dependencies.manager."""
import importlib as _importlib
import sys as _sys
_implementation = _importlib.import_module("dsbridge.dependencies.manager")
if __name__ == "__main__":
    raise SystemExit(_implementation.main())
else:
    _sys.modules[__name__] = _implementation
