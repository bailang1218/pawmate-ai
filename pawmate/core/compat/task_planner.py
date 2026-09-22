from pawmate.core.planning import task_planner as _impl
import sys as _sys

_sys.modules[__name__] = _impl
