# Compatibility alias. Canonical implementation lives in app.integrations.
from pathlib import Path as _Path
import sys as _sys
import importlib as _importlib
_root = str(_Path(__file__).resolve().parents[1])
if _root not in _sys.path:
    _sys.path.insert(0, _root)
_impl = _importlib.import_module('app.integrations.zhaoshang_mall_mapping')
_sys.modules[__name__] = _impl
