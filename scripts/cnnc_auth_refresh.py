# Legacy import/CLI compatibility; implementation is owned by app.integrations.
from pathlib import Path as _Path
import sys as _sys
import importlib as _importlib
_root = str(_Path(__file__).resolve().parents[1])
if _root not in _sys.path:
    _sys.path.insert(0, _root)
_impl = _importlib.import_module('app.integrations.cnnc_auth_refresh')
if __name__ == "__main__":
    raise SystemExit(_impl.main())
globals().update({k:v for k,v in vars(_impl).items() if not k.startswith("__")})
_sys.modules[__name__] = _impl
