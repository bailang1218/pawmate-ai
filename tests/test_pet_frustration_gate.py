from pawmate.desktop_pet.runtime.motion_graph import MotionRuntimeSimulator, MotionState
from pawmate.desktop_pet.integration import _looks_hard
from pawmate.desktop_pet.runtime.fc_pet_signal_bridge import (
    FCPetSignalBridge,
    FunctionCallSignal,
    FunctionCallSignalType,
    categorize_tool,
)
from pawmate.desktop_pet.runtime.pet_event_aggregator import (
    PetDataSignal,
    PetEventAggregator,
    PetSignalType,
)


def _events(emissions):
    return [item.event_name for item in emissions]


def test_turn_start_does_not_emit_frustrated_thinking():
    aggregator = PetEventAggregator()

    emissions = aggregator.handle(PetDataSignal(PetSignalType.TURN_STARTED, turn_id=1))

    assert emissions == []


def test_plain_question_mark_does_not_trigger_confused_hard_question():
    assert not _looks_hard("这个本地搜索有没有做其他格式的 UI 框来展示嘞？")
    assert _looks_hard("为什么 DeepSeek 中断的根源是什么")


def test_native_web_search_is_lightweight_pet_task():
    bridge = FCPetSignalBridge()
    signals = bridge.to_pet_signals(
        FunctionCallSignal(
            FunctionCallSignalType.TOOL_USE,
            turn_id=1,
            tool_name="native_web_search",
            tool_input={"query": "杭州天气"},
        )
    )

    assert categorize_tool("native_web_search") == "web_search"
    assert len(signals) == 1
    assert signals[0].signal_type == PetSignalType.SHORT_TASK
    assert signals[0].payload["category"] == "web_search"


def test_frustrated_is_gated_to_long_running_work():
    now = [0.0]
    aggregator = PetEventAggregator(clock=lambda: now[0])

    aggregator.handle(PetDataSignal(PetSignalType.TURN_STARTED, turn_id=1))
    assert _events(aggregator.handle(PetDataSignal(PetSignalType.TOOL_START, turn_id=1))) == ["task_start"]

    now[0] = 5.0
    assert _events(aggregator.handle(PetDataSignal(PetSignalType.PROGRESS_UPDATE, turn_id=1))) == ["processing"]

    now[0] = 16.0
    assert _events(aggregator.handle(PetDataSignal(PetSignalType.PROGRESS_UPDATE, turn_id=1))) == ["busy"]

    now[0] = 17.0
    assert _events(aggregator.handle(PetDataSignal(PetSignalType.PROGRESS_UPDATE, turn_id=1))) == ["processing"]


def test_working_thinking_keeps_using_computer_not_frustrated():
    sim = MotionRuntimeSimulator(initial_state=MotionState.STANDING_IDLE)
    sim.receive("task_start", turn_id=1)
    sim.run_until_settled()
    assert sim.current_state == MotionState.WORKING_IDLE

    sim.receive("thinking", turn_id=1)
    played = sim.run_until_settled()

    assert "14_frustrated" not in played
    assert sim.current_state == MotionState.WORKING_IDLE


def test_working_busy_still_allows_frustrated():
    sim = MotionRuntimeSimulator(initial_state=MotionState.STANDING_IDLE)
    sim.receive("task_start", turn_id=1)
    sim.run_until_settled()
    assert sim.current_state == MotionState.WORKING_IDLE

    sim.receive("busy", turn_id=1)
    played = sim.run_until_settled()

    assert "14_frustrated" in played
    assert sim.current_state == MotionState.WORKING_IDLE
