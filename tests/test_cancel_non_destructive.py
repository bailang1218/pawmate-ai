import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pawmate.core.runtime.engine import AgentEngine, EngineState


class FakeHistory:
    def __init__(self):
        self.rollback_called = False

    def rollback_to(self, seq):
        self.rollback_called = True
        return 999


class FakeConfirmGate:
    def __init__(self):
        self.resolved = None

    def resolve(self, allowed):
        self.resolved = allowed


def ck(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + ("  " + info if info else ""))
    if not cond:
        raise AssertionError(name)


engine = AgentEngine.__new__(AgentEngine)
engine._state = EngineState(tool_call_history=[])
engine._state.is_running = True
engine._history = FakeHistory()
engine._confirm_gate = FakeConfirmGate()
engine._turn_snapshot_seq = 123

deleted = engine.cancel_current_turn()

ck("cancel returns zero deleted messages", deleted == 0, str(deleted))
ck("cancel does not rollback history", not engine._history.rollback_called)
ck("cancel stops running state", engine._state.is_running is False)
ck("cancel denies pending confirmations", engine._confirm_gate.resolved is False)
