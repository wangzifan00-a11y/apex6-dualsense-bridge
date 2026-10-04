"""Compatibility import; implementation lives in dsbridge.virtual.dualsense.transport."""
import importlib as _importlib
import sys as _sys
_implementation = _importlib.import_module("dsbridge.virtual.dualsense.transport")
if __name__ == "__main__":
    raise SystemExit(_implementation.main())
else:
    _sys.modules[__name__] = _implementation
