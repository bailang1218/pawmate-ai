from pawmate.core.model import llm_factory as _impl
import sys as _sys

_sys.modules[__name__] = _impl
