from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from pawmate.desktop_pet.runtime.fc_pet_signal_bridge import (  # noqa: E402
    FCPetSignalBridge,
    FunctionCallSignal,
    FunctionCallSignalType,
)
from pawmate.desktop_pet.runtime.motion_graph import MotionRuntimeSimulator, MotionState  # noqa: E402
from pawmate.desktop_pet.runtime.pet_event_aggregator import PetEventAggregator  # noqa: E402


@dataclass(frozen=True)
class Scenario:
    name: str
    initial_state: MotionState
    signals: tuple[FunctionCallSignal, ...]


def default_scenarios() -> tuple[Scenario, ...]:
    return (
        Scenario(
            name="short task: one or two light tools",
            initial_state=MotionState.STANDING_IDLE,
            signals=(
                FunctionCallSignal(FunctionCallSignalType.TURN_STARTED, turn_id=100),
                FunctionCallSignal(
                    FunctionCallSignalType.TOOL_USE,
                    turn_id=100,
                    tool_use_id="fc_0a",
                    tool_name="read_text_file",
                    tool_input={"path": "notes.txt"},
                ),
                FunctionCallSignal(
                    FunctionCallSignalType.TOOL_RESULT,
                    turn_id=100,
                    tool_use_id="fc_0a",
                    tool_name="read_text_file",
                ),
                FunctionCallSignal(
                    FunctionCallSignalType.TOOL_USE,
                    turn_id=100,
                    tool_use_id="fc_0b",
                    tool_name="search_memory",
                    tool_input={"query": "last context"},
                ),
                FunctionCallSignal(
                    FunctionCallSignalType.TOOL_RESULT,
                    turn_id=100,
                    tool_use_id="fc_0b",
                    tool_name="search_memory",
                ),
                FunctionCallSignal(FunctionCallSignalType.TEXT_DELTA, turn_id=100, payload={"chars": 12}),
                FunctionCallSignal(FunctionCallSignalType.FINISHED, turn_id=100),
            ),
        ),
        Scenario(
            name="tool chain: search -> read -> answer",
            initial_state=MotionState.STANDING_IDLE,
            signals=(
                FunctionCallSignal(FunctionCallSignalType.TURN_STARTED, turn_id=101),
                FunctionCallSignal(
                    FunctionCallSignalType.TOOL_USE,
                    turn_id=101,
                    tool_use_id="fc_1",
                    tool_name="browser_search",
                    tool_input={"query": "weather today"},
                ),
                FunctionCallSignal(
                    FunctionCallSignalType.TOOL_RESULT,
                    turn_id=101,
                    tool_use_id="fc_1",
                    tool_name="browser_search",
                ),
                FunctionCallSignal(
                    FunctionCallSignalType.TOOL_USE,
                    turn_id=101,
                    tool_use_id="fc_2",
                    tool_name="read_text_file",
                    tool_input={"path": "notes.txt"},
                ),
                FunctionCallSignal(
                    FunctionCallSignalType.TOOL_RESULT,
                    turn_id=101,
                    tool_use_id="fc_2",
                    tool_name="read_text_file",
                ),
                FunctionCallSignal(FunctionCallSignalType.TEXT_DELTA, turn_id=101, payload={"chars": 18}),
                FunctionCallSignal(FunctionCallSignalType.FINISHED, turn_id=101),
            ),
        ),
        Scenario(
            name="big jump: sleeping idle -> tool use -> answer",
            initial_state=MotionState.SLEEPING_IDLE,
            signals=(
                FunctionCallSignal(FunctionCallSignalType.TURN_STARTED, turn_id=102),
                FunctionCallSignal(
                    FunctionCallSignalType.TOOL_USE,
                    turn_id=102,
                    tool_use_id="fc_3",
                    tool_name="run_script",
                    tool_input={"code": "print('work')"},
                ),
                FunctionCallSignal(
                    FunctionCallSignalType.TOOL_RESULT,
                    turn_id=102,
                    tool_use_id="fc_3",
                    tool_name="run_script",
                ),
                FunctionCallSignal(FunctionCallSignalType.TEXT_DELTA, turn_id=102, payload={"chars": 24}),
                FunctionCallSignal(FunctionCallSignalType.FINISHED, turn_id=102),
            ),
        ),
        Scenario(
            name="cancel race: old tool result arrives late",
            initial_state=MotionState.STANDING_IDLE,
            signals=(
                FunctionCallSignal(FunctionCallSignalType.TURN_STARTED, turn_id=103),
                FunctionCallSignal(
                    FunctionCallSignalType.TOOL_USE,
                    turn_id=103,
                    tool_use_id="fc_4",
                    tool_name="search_file",
                    tool_input={"pattern": "api_key"},
                ),
                FunctionCallSignal(FunctionCallSignalType.CANCELLED, turn_id=103),
                FunctionCallSignal(
                    FunctionCallSignalType.TOOL_RESULT,
                    turn_id=103,
                    tool_use_id="fc_4",
                    tool_name="search_file",
                ),
                FunctionCallSignal(
                    FunctionCallSignalType.TOOL_ERROR,
                    turn_id=103,
                    tool_use_id="fc_4",
                    tool_name="search_file",
                    payload={"error": "late stale result"},
                ),
                FunctionCallSignal(FunctionCallSignalType.FINISHED, turn_id=103),
            ),
        ),
    )


def run_scenario(scenario: Scenario) -> list[dict[str, str]]:
    bridge = FCPetSignalBridge()
    aggregator = PetEventAggregator()
    runtime = MotionRuntimeSimulator(initial_state=scenario.initial_state)
    rows: list[dict[str, str]] = []

    for fc_signal in scenario.signals:
        pet_signals = bridge.to_pet_signals(fc_signal)
        if not pet_signals:
            rows.append(_row(fc_signal, "(none)", "(none)", "", runtime.current_state))
            continue

        for pet_signal in pet_signals:
            emissions = aggregator.handle(pet_signal)
            if not emissions:
                rows.append(_row(fc_signal, pet_signal.signal_type.value, "(none)", "", runtime.current_state))
                continue

            for emission in emissions:
                runtime.receive(emission.event_name, turn_id=emission.turn_id)
                clips = runtime.run_until_settled()
                rows.append(
                    _row(
                        fc_signal,
                        pet_signal.signal_type.value,
                        emission.event_name,
                        " -> ".join(clips),
                        runtime.current_state,
                    )
                )

    return rows


def _row(
    fc_signal: FunctionCallSignal,
    pet_signal: str,
    pet_event: str,
    clips: str,
    state: MotionState,
) -> dict[str, str]:
    tool = fc_signal.tool_name or ""
    if fc_signal.tool_use_id:
        tool = f"{tool}#{fc_signal.tool_use_id}"
    return {
        "fc_signal": fc_signal.signal_type.value,
        "tool": tool,
        "pet_signal": pet_signal,
        "pet_event": pet_event,
        "clips": clips,
        "state": state.value,
    }


def print_rows(name: str, rows: list[dict[str, str]]) -> None:
    print(f"\n=== {name} ===")
    headers = ("fc_signal", "tool", "pet_signal", "pet_event", "clips", "state")
    widths = {header: len(header) for header in headers}
    for row in rows:
        for header in headers:
            widths[header] = max(widths[header], len(row[header]))

    print(" | ".join(header.ljust(widths[header]) for header in headers))
    print("-+-".join("-" * widths[header] for header in headers))
    for row in rows:
        print(" | ".join(row[header].ljust(widths[header]) for header in headers))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", choices=["all", "short", "tool", "sleep", "cancel"], default="all")
    args = parser.parse_args()

    scenarios = default_scenarios()
    selected = {
        "all": scenarios,
        "short": scenarios[:1],
        "tool": scenarios[1:2],
        "sleep": scenarios[2:3],
        "cancel": scenarios[3:],
    }[args.scenario]

    for scenario in selected:
        print_rows(scenario.name, run_scenario(scenario))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
