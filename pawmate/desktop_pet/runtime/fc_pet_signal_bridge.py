from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from pawmate.desktop_pet.runtime.pet_event_aggregator import PetDataSignal, PetSignalType


class FunctionCallSignalType(str, Enum):
    TURN_STARTED = "turn_started"
    TEXT_DELTA = "text_delta"
    TOOL_USE = "tool_use"
    TOOL_RESULT = "tool_result"
    TOOL_ERROR = "tool_error"
    FINISHED = "finished"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class FunctionCallSignal:
    signal_type: FunctionCallSignalType
    turn_id: int | None = None
    tool_use_id: str | None = None
    tool_name: str | None = None
    tool_input: dict[str, Any] | None = None
    payload: dict[str, Any] | None = None


class FCPetSignalBridge:
    """Convert normalized function-calling telemetry into pet data signals.

    This bridge is deliberately side-channel only: it extracts lightweight
    features from function-calling events, and never mutates tool input or
    controls tool execution.
    """

    def to_pet_signals(self, signal: FunctionCallSignal) -> list[PetDataSignal]:
        if signal.signal_type == FunctionCallSignalType.TURN_STARTED:
            return [PetDataSignal(PetSignalType.TURN_STARTED, signal.turn_id)]
        if signal.signal_type == FunctionCallSignalType.TEXT_DELTA:
            return [PetDataSignal(PetSignalType.TEXT_DELTA, signal.turn_id, signal.payload)]
        if signal.signal_type == FunctionCallSignalType.TOOL_USE:
            category = categorize_tool(signal.tool_name or "")
            if category in HEAVY_TOOL_CATEGORIES:
                signal_type = PetSignalType.TOOL_START
            elif category == "memory":
                signal_type = PetSignalType.MEMORY_TASK
            else:
                signal_type = PetSignalType.SHORT_TASK
            return [
                PetDataSignal(
                    signal_type,
                    signal.turn_id,
                    {
                        "tool_use_id": signal.tool_use_id,
                        "tool_name": signal.tool_name,
                        "category": category,
                    },
                )
            ]
        if signal.signal_type == FunctionCallSignalType.TOOL_RESULT:
            return [
                PetDataSignal(
                    PetSignalType.TOOL_DONE,
                    signal.turn_id,
                    {
                        "tool_use_id": signal.tool_use_id,
                        "tool_name": signal.tool_name,
                        "category": categorize_tool(signal.tool_name or ""),
                    },
                )
            ]
        if signal.signal_type == FunctionCallSignalType.TOOL_ERROR:
            return [
                PetDataSignal(
                    PetSignalType.TOOL_ERROR,
                    signal.turn_id,
                    {
                        "tool_use_id": signal.tool_use_id,
                        "tool_name": signal.tool_name,
                        "category": categorize_tool(signal.tool_name or ""),
                    },
                )
            ]
        if signal.signal_type == FunctionCallSignalType.FINISHED:
            return [PetDataSignal(PetSignalType.FINISHED, signal.turn_id)]
        if signal.signal_type == FunctionCallSignalType.CANCELLED:
            return [PetDataSignal(PetSignalType.CANCELLED, signal.turn_id)]
        return []


def categorize_tool(tool_name: str) -> str:
    name = tool_name.lower()
    if name == "native_web_search":
        return "web_search"
    if name.startswith("browser_") or name in {"download_file", "download_with_metadata"}:
        return "external_io"
    if name == "run_powershell_query":
        return "local_read"
    if name in {"run_script", "run_shell_command", "write_file", "patch_file"}:
        return "side_effect"
    if name == "search_file":
        return "local_scan"
    if name in {"read_file", "read_text_file", "list_directory", "read_symbol"}:
        return "local_read"
    if name in {
        "remember_memory", "consider_memory", "forget_memory", "search_memory", "search_history",
        "open_history_context", "core_remember", "recall", "list_memories",
    }:
        return "memory"
    return "generic"


HEAVY_TOOL_CATEGORIES = frozenset({"external_io", "side_effect", "local_scan"})
