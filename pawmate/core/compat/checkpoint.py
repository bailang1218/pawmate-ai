import importlib as _importlib
import sys as _sys

_impl = _importlib.import_module("pawmate.core.checkpoint.checkpoint")
_sys.modules[__name__] = _impl
