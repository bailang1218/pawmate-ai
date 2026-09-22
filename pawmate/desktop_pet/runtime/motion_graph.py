from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from heapq import heappop, heappush
from itertools import count
from typing import Iterable, Literal


class MotionState(str, Enum):
    STANDING_IDLE = "standing_idle"
    SITTING_IDLE = "sitting_idle"
    WORK_ENTERING = "work_entering"
    WORKING_IDLE = "working_idle"
    SLEEPING_IDLE = "sleeping_idle"


STABLE_STATES = frozenset(
    {
        MotionState.STANDING_IDLE,
        MotionState.SITTING_IDLE,
        MotionState.WORKING_IDLE,
        MotionState.SLEEPING_IDLE,
    }
)


class PetIntentType(str, Enum):
    USER_INPUT_RECEIVED = "user_input_received"
    USER_UPLOAD_RECEIVED = "user_upload_received"
    USER_ASKS_HARD = "user_asks_hard"
    REPEAT_QUESTION = "repeat_question"
    CHAT_NORMAL = "chat_normal"
    CHAT_COMPLETE = "chat_complete"
    SHORT_TASK = "short_task"
    MEMORY_TASK = "memory_task"
    USER_PRAISE = "user_praise"
    TASK_START = "task_start"
    THINKING = "thinking"
    PROCESSING = "processing"
    BUSY = "busy"
    OUTPUTTING = "outputting"
    WORK_COMPLETE = "work_complete"
    BACK_TO_IDLE = "back_to_idle"
    SLEEP = "sleep"
    WAKE_UP = "wake_up"
    CANCEL = "cancel"
    SAD = "sad"


@dataclass(frozen=True)
class ClipEdge:
    clip_id: str
    source: MotionState
    target: MotionState
    cost: float
    tags: frozenset[str] = frozenset()
    loop: bool = False
    oneshot: bool = False
    interruptible: bool = False


@dataclass(frozen=True)
class PetIntent:
    intent_type: PetIntentType
    target: MotionState
    priority: int
    turn_id: int | None = None
    preferred_tag: str | None = None


@dataclass(frozen=True)
class MotionCall:
    op: Literal["play_clip", "loop_clip", "reset"]
    clip_id: str
    reason: str = ""


class MotionGraph:
    def __init__(self, edges: Iterable[ClipEdge], stable_states: Iterable[MotionState] = STABLE_STATES) -> None:
        self.edges = tuple(edges)
        self.stable_states = frozenset(stable_states)
        self._by_source: dict[MotionState, list[ClipEdge]] = {}
        for edge in self.edges:
            self._by_source.setdefault(edge.source, []).append(edge)

    def outgoing(self, state: MotionState) -> tuple[ClipEdge, ...]:
        return tuple(self._by_source.get(state, ()))

    def self_loop_for(self, state: MotionState, tag: str) -> ClipEdge | None:
        candidates = [
            edge
            for edge in self._by_source.get(state, ())
            if edge.source == edge.target and tag in edge.tags
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda edge: edge.cost)

    def shortest_path(self, source: MotionState, target: MotionState) -> list[ClipEdge] | None:
        if source == target:
            return []

        sequence = count()
        queue: list[tuple[float, int, MotionState, list[ClipEdge]]] = []
        heappush(queue, (0.0, next(sequence), source, []))
        best_cost: dict[MotionState, float] = {source: 0.0}

        while queue:
            cost, _, state, path = heappop(queue)
            if state == target:
                return path
            if cost > best_cost.get(state, float("inf")):
                continue
            for edge in self._by_source.get(state, ()):
                if edge.source == edge.target:
                    continue
                next_cost = cost + edge.cost
                if next_cost >= best_cost.get(edge.target, float("inf")):
                    continue
                best_cost[edge.target] = next_cost
                heappush(queue, (next_cost, next(sequence), edge.target, path + [edge]))
        return None

    def validate_reachability(self) -> None:
        missing: list[tuple[MotionState, MotionState]] = []
        for source in self.stable_states:
            for target in self.stable_states:
                if source == target:
                    continue
                if self.shortest_path(source, target) is None:
                    missing.append((source, target))
        if missing:
            rendered = ", ".join(f"{source.value}->{target.value}" for source, target in missing)
            raise ValueError(f"Motion graph is not strongly connected across stable states: {rendered}")


class MotionPlanner:
    def __init__(self, graph: MotionGraph) -> None:
        self.graph = graph

    def plan(self, source: MotionState, intent: PetIntent) -> list[ClipEdge]:
        path = self.graph.shortest_path(source, intent.target)
        if path is None:
            raise ValueError(f"No motion path from {source.value} to {intent.target.value}")
        if intent.preferred_tag is None:
            return path

        tag_state = intent.target if path else source
        loop = self.graph.self_loop_for(tag_state, intent.preferred_tag)
        if loop is None:
            return path
        return path + [loop]

    def plan_calls(self, source: MotionState, intent: PetIntent) -> list[MotionCall]:
        calls: list[MotionCall] = []
        for edge in self.plan(source, intent):
            op: Literal["play_clip", "loop_clip"] = "loop_clip" if edge.loop else "play_clip"
            calls.append(MotionCall(op=op, clip_id=edge.clip_id, reason=intent.intent_type.value))
        return calls


def default_motion_graph() -> MotionGraph:
    graph = MotionGraph(
        (
            ClipEdge("09_sit_down", MotionState.STANDING_IDLE, MotionState.SITTING_IDLE, cost=1.2, tags=frozenset({"sit"})),
            ClipEdge(
                "11_take_computer",
                MotionState.SITTING_IDLE,
                MotionState.WORK_ENTERING,
                cost=2.0,
                tags=frozenset({"work_enter"}),
            ),
            ClipEdge(
                "12_wear_glasses",
                MotionState.WORK_ENTERING,
                MotionState.WORKING_IDLE,
                cost=0.8,
                tags=frozenset({"work_enter"}),
            ),
            ClipEdge(
                "15_put_away",
                MotionState.WORKING_IDLE,
                MotionState.SITTING_IDLE,
                cost=2.6,
                tags=frozenset({"work_exit"}),
            ),
            ClipEdge("19_stand_up", MotionState.SITTING_IDLE, MotionState.STANDING_IDLE, cost=2.0, tags=frozenset({"stand"})),
            ClipEdge(
                "20_sitting_talking_raw",
                MotionState.SITTING_IDLE,
                MotionState.SITTING_IDLE,
                cost=2.0,
                tags=frozenset({"sitting_talking", "outputting", "emote"}),
                oneshot=True,
            ),
            ClipEdge(
                "16_fall_asleep",
                MotionState.SITTING_IDLE,
                MotionState.SLEEPING_IDLE,
                cost=3.4,
                tags=frozenset({"sleep_enter"}),
            ),
            ClipEdge(
                "18_waking_up",
                MotionState.SLEEPING_IDLE,
                MotionState.SITTING_IDLE,
                cost=1.5,
                tags=frozenset({"sleep_exit", "wake"}),
            ),
            ClipEdge(
                "02_greeting",
                MotionState.STANDING_IDLE,
                MotionState.STANDING_IDLE,
                cost=1.6,
                tags=frozenset({"greeting", "emote"}),
                oneshot=True,
            ),
            ClipEdge(
                "05_looking_around",
                MotionState.STANDING_IDLE,
                MotionState.STANDING_IDLE,
                cost=1.8,
                tags=frozenset({"user_upload", "attention", "emote"}),
                oneshot=True,
            ),
            ClipEdge(
                "01_confused",
                MotionState.STANDING_IDLE,
                MotionState.STANDING_IDLE,
                cost=1.5,
                tags=frozenset({"confused", "thinking", "emote"}),
                oneshot=True,
            ),
            ClipEdge(
                "04_daydream",
                MotionState.STANDING_IDLE,
                MotionState.STANDING_IDLE,
                cost=1.7,
                tags=frozenset({"daydream", "hard_question", "emote"}),
                oneshot=True,
            ),
            ClipEdge(
                "09_talking_norm",
                MotionState.STANDING_IDLE,
                MotionState.STANDING_IDLE,
                cost=2.0,
                tags=frozenset({"chat_normal", "emote"}),
                oneshot=True,
            ),
            ClipEdge(
                "08_talking_v2",
                MotionState.STANDING_IDLE,
                MotionState.STANDING_IDLE,
                cost=1.8,
                tags=frozenset({"short_task", "tool_brief", "emote"}),
                oneshot=True,
            ),
            ClipEdge(
                "12_happy_jumping",
                MotionState.STANDING_IDLE,
                MotionState.STANDING_IDLE,
                cost=1.8,
                tags=frozenset({"user_praise", "emote"}),
                oneshot=True,
            ),
            ClipEdge(
                "10_aggrieved",
                MotionState.STANDING_IDLE,
                MotionState.STANDING_IDLE,
                cost=1.7,
                tags=frozenset({"sad", "emote"}),
                oneshot=True,
            ),
            ClipEdge(
                "14_frustrated",
                MotionState.WORKING_IDLE,
                MotionState.WORKING_IDLE,
                cost=1.4,
                tags=frozenset({"thinking", "busy", "emote"}),
                oneshot=True,
            ),
            ClipEdge(
                "13_use_computer",
                MotionState.WORKING_IDLE,
                MotionState.WORKING_IDLE,
                cost=0.0,
                tags=frozenset({"working_idle"}),
                loop=True,
            ),
        )
    )
    graph.validate_reachability()
    return graph


_DO_NOT_INTERRUPT_STATES = frozenset(
    {
        MotionState.WORKING_IDLE,
        MotionState.WORK_ENTERING,
        MotionState.SLEEPING_IDLE,
    }
)

_STANDING_EMOTE_INTENTS = frozenset(
    {
        PetIntentType.CHAT_NORMAL,
        PetIntentType.SHORT_TASK,
        PetIntentType.MEMORY_TASK,
        PetIntentType.USER_INPUT_RECEIVED,
        PetIntentType.USER_UPLOAD_RECEIVED,
        PetIntentType.USER_ASKS_HARD,
        PetIntentType.REPEAT_QUESTION,
        PetIntentType.USER_PRAISE,
        PetIntentType.SAD,
    }
)


class IntentRouter:
    def __init__(self) -> None:
        self._cancelled_turns: set[int] = set()

    def route(
        self,
        event_name: str,
        *,
        current_state: MotionState,
        turn_id: int | None = None,
    ) -> PetIntent | None:
        try:
            intent_type = PetIntentType(event_name)
        except ValueError:
            return None

        if turn_id is not None and turn_id in self._cancelled_turns and intent_type in {
            PetIntentType.SAD,
            PetIntentType.WORK_COMPLETE,
            PetIntentType.TASK_START,
            PetIntentType.THINKING,
            PetIntentType.PROCESSING,
            PetIntentType.BUSY,
            PetIntentType.OUTPUTTING,
        }:
            return None

        if intent_type == PetIntentType.CANCEL:
            if turn_id is not None:
                self._cancelled_turns.add(turn_id)
            return PetIntent(intent_type, self._cancel_target(current_state), priority=100, turn_id=turn_id)
        if intent_type == PetIntentType.WAKE_UP:
            return PetIntent(intent_type, MotionState.SITTING_IDLE, priority=90, turn_id=turn_id)
        if intent_type in {PetIntentType.TASK_START, PetIntentType.PROCESSING}:
            return PetIntent(intent_type, MotionState.WORKING_IDLE, priority=70, turn_id=turn_id)
        if intent_type == PetIntentType.OUTPUTTING:
            return PetIntent(
                intent_type,
                MotionState.SITTING_IDLE,
                priority=70,
                turn_id=turn_id,
                preferred_tag="outputting",
            )
        if intent_type == PetIntentType.BUSY:
            return PetIntent(
                intent_type,
                MotionState.WORKING_IDLE,
                priority=65,
                turn_id=turn_id,
                preferred_tag="busy",
            )
        if intent_type == PetIntentType.THINKING:
            if current_state == MotionState.SLEEPING_IDLE:
                return PetIntent(intent_type, MotionState.SLEEPING_IDLE, priority=20, turn_id=turn_id)
            if current_state == MotionState.WORKING_IDLE:
                return PetIntent(
                    intent_type,
                    MotionState.WORKING_IDLE,
                    priority=45,
                    turn_id=turn_id,
                    preferred_tag="working_idle",
                )
            return PetIntent(
                intent_type,
                MotionState.STANDING_IDLE,
                priority=45,
                turn_id=turn_id,
                preferred_tag="confused",
            )
        if intent_type in {PetIntentType.WORK_COMPLETE, PetIntentType.BACK_TO_IDLE}:
            return PetIntent(intent_type, MotionState.SITTING_IDLE, priority=60, turn_id=turn_id)
        if intent_type == PetIntentType.SLEEP:
            return PetIntent(intent_type, MotionState.SLEEPING_IDLE, priority=40, turn_id=turn_id)
        # Low-priority "emote" intents below all target STANDING_IDLE. Letting them
        # run while the pet is working or sleeping forces a put_away/stand_up (or a
        # wake_up), i.e. the "pet stands up unexpectedly mid-work" bug. Those loops
        # have their own in-place feedback channels (outputting/thinking/busy), so
        # here we simply stay put instead of yanking the pet out of them.
        if intent_type in _STANDING_EMOTE_INTENTS and current_state in _DO_NOT_INTERRUPT_STATES:
            return None
        if intent_type == PetIntentType.CHAT_NORMAL:
            return PetIntent(intent_type, MotionState.STANDING_IDLE, priority=35, turn_id=turn_id, preferred_tag="chat_normal")
        if intent_type == PetIntentType.CHAT_COMPLETE:
            target = current_state if current_state in STABLE_STATES else MotionState.SITTING_IDLE
            return PetIntent(intent_type, target, priority=55, turn_id=turn_id)
        if intent_type == PetIntentType.SHORT_TASK:
            return PetIntent(intent_type, MotionState.STANDING_IDLE, priority=38, turn_id=turn_id, preferred_tag="short_task")
        if intent_type == PetIntentType.MEMORY_TASK:
            return PetIntent(intent_type, MotionState.STANDING_IDLE, priority=39, turn_id=turn_id, preferred_tag="confused")
        if intent_type == PetIntentType.USER_INPUT_RECEIVED:
            return PetIntent(intent_type, MotionState.STANDING_IDLE, priority=30, turn_id=turn_id, preferred_tag="daydream")
        if intent_type == PetIntentType.USER_UPLOAD_RECEIVED:
            return PetIntent(intent_type, MotionState.STANDING_IDLE, priority=32, turn_id=turn_id, preferred_tag="user_upload")
        if intent_type == PetIntentType.USER_ASKS_HARD:
            return PetIntent(intent_type, MotionState.STANDING_IDLE, priority=34, turn_id=turn_id, preferred_tag="confused")
        if intent_type == PetIntentType.REPEAT_QUESTION:
            return PetIntent(intent_type, MotionState.STANDING_IDLE, priority=34, turn_id=turn_id, preferred_tag="confused")
        if intent_type == PetIntentType.USER_PRAISE:
            return PetIntent(intent_type, MotionState.STANDING_IDLE, priority=35, turn_id=turn_id, preferred_tag="user_praise")
        if intent_type == PetIntentType.SAD:
            return PetIntent(intent_type, MotionState.STANDING_IDLE, priority=50, turn_id=turn_id, preferred_tag="sad")
        return None

    @staticmethod
    def _cancel_target(current_state: MotionState) -> MotionState:
        if current_state in {MotionState.WORKING_IDLE, MotionState.WORK_ENTERING, MotionState.SITTING_IDLE}:
            return MotionState.SITTING_IDLE
        if current_state == MotionState.SLEEPING_IDLE:
            return MotionState.SLEEPING_IDLE
        return MotionState.STANDING_IDLE


class MotionRuntimeSimulator:
    """Boundary-replanning simulator for testing runtime policy before Qt wiring."""

    def __init__(
        self,
        graph: MotionGraph | None = None,
        initial_state: MotionState = MotionState.STANDING_IDLE,
    ) -> None:
        self.graph = graph or default_motion_graph()
        self.planner = MotionPlanner(self.graph)
        self.router = IntentRouter()
        self.current_state = initial_state
        self.active_edge: ClipEdge | None = None
        self.desired_intent: PetIntent | None = None
        self.played_clips: list[str] = []

    def receive(self, event_name: str, *, turn_id: int | None = None) -> PetIntent | None:
        intent = self.router.route(event_name, current_state=self.current_state, turn_id=turn_id)
        if intent is None:
            return None
        if self._should_replace_desired(intent):
            self.desired_intent = intent
        return intent

    def _should_replace_desired(self, intent: PetIntent) -> bool:
        existing = self.desired_intent
        if existing is None:
            return True
        if intent.intent_type == PetIntentType.CANCEL:
            return True
        if existing.intent_type == PetIntentType.CANCEL:
            return False

        same_turn = (
            intent.turn_id is not None
            and existing.turn_id is not None
            and intent.turn_id == existing.turn_id
        )
        if intent.intent_type in {PetIntentType.WORK_COMPLETE, PetIntentType.BACK_TO_IDLE}:
            return same_turn or intent.priority >= existing.priority
        if intent.intent_type in {PetIntentType.TASK_START, PetIntentType.PROCESSING, PetIntentType.OUTPUTTING}:
            return existing.intent_type in {
                PetIntentType.WAKE_UP,
                PetIntentType.SLEEP,
                PetIntentType.TASK_START,
                PetIntentType.PROCESSING,
                PetIntentType.OUTPUTTING,
            } or intent.priority >= existing.priority
        if intent.intent_type == PetIntentType.WAKE_UP:
            return existing.intent_type in {PetIntentType.SLEEP, PetIntentType.WAKE_UP} or intent.priority >= existing.priority
        if intent.intent_type == PetIntentType.SLEEP:
            return intent.priority >= existing.priority
        if intent.preferred_tag is not None:
            return intent.priority >= existing.priority
        return intent.priority >= existing.priority

    def start_next_clip(self) -> ClipEdge | None:
        if self.active_edge is not None or self.desired_intent is None:
            return None
        path = self.planner.plan(self.current_state, self.desired_intent)
        if not path:
            self.desired_intent = None
            return None
        self.active_edge = path[0]
        return self.active_edge

    def finish_active_clip(self) -> ClipEdge | None:
        if self.active_edge is None:
            return None
        edge = self.active_edge
        self.active_edge = None
        self.current_state = edge.target
        self.played_clips.append(edge.clip_id)
        if self.desired_intent is not None and self.current_state == self.desired_intent.target:
            loop = None
            if self.desired_intent.preferred_tag is not None and edge.source != edge.target:
                loop = self.graph.self_loop_for(self.current_state, self.desired_intent.preferred_tag)
            if loop is None:
                self.desired_intent = None
        return edge

    def run_until_settled(self, max_clips: int = 20) -> list[str]:
        start_index = len(self.played_clips)
        for _ in range(max_clips):
            edge = self.start_next_clip()
            if edge is None:
                break
            self.finish_active_clip()
        else:
            raise RuntimeError("Motion runtime did not settle")
        return self.played_clips[start_index:]
