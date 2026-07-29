from pawmate.core.observability import app_logs as _impl
import sys as _sys

_sys.modules[__name__] = _impl
