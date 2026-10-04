"""Compatibility import; implementation lives in dsbridge.controllers.flydigi.apex6.session."""
import importlib as _importlib
import sys as _sys
_implementation = _importlib.import_module("dsbridge.controllers.flydigi.apex6.session")
if __name__ == "__main__":
    raise SystemExit(_implementation.main())
else:
    _sys.modules[__name__] = _implementation
