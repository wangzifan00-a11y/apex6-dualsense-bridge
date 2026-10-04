"""Compatibility import; implementation lives in dsbridge.diagnostics.sony_writer."""
import importlib as _importlib
import sys as _sys
_implementation = _importlib.import_module("dsbridge.diagnostics.sony_writer")
if __name__ == "__main__":
    raise SystemExit(_implementation.main())
else:
    _sys.modules[__name__] = _implementation
