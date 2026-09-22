from pawmate.core.prompts import prompt_assembler as _impl
import sys as _sys

_sys.modules[__name__] = _impl
