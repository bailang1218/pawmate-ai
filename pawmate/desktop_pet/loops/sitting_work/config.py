from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


DESKTOP_PET_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SOURCE_ROOT = DESKTOP_PET_ROOT / "assets" / "sitting_work"
STANDING_CONFUSED_REFERENCE_FRAME = (
    DESKTOP_PET_ROOT / "assets" / "standing" / "01_confused" / "confused_01.png"
)
DEFAULT_NEUTRAL_FRAME = Path("10_sitting_idle") / "frame_000.png"
IDLE_ACTION_ID = "10_sitting_idle"
WORKING_IDLE_ACTION_ID = "13_use_computer"
SITTING_TALKING_ACTION_ID = "20_sitting_talking_raw"
STAND_UP_ACTION_ID = "19_stand_up"
INITIAL_CUES = ("09_sit_down", "10_sitting_idle")
IDLE_CLICK_EVENT = "stand_up"
IDLE_CLICK_EXIT_FRAMES = ("frame_007.png",)
SLEEP_EVENT = "sleep"


@dataclass(frozen=True)
class ActionPlan:
    action_id: str
    entry_frame: str
    mode: str
    fps: int
    cycles: int = 1
    event_cycles: int = 1
    hold_start: int = 0
    hold_end: int = 0


@dataclass(frozen=True)
class CueSpec:
    action_id: str
    entry_frame: str
    mode: str
    fps: int
    cycles: int = 1
    hold_start: int = 0
    hold_end: int = 0
    end_frame: str | None = None
    frames: tuple[str, ...] | None = None


@dataclass(frozen=True)
class CrossfadeFrame:
    from_path: Path
    to_path: Path
    alpha: float


@dataclass(frozen=True)
class FrameComparison:
    score: float
    mask_iou: float
    center_delta: float
    alpha_rms: float
    rgb_rms: float


ACTION_PLANS: tuple[ActionPlan, ...] = (
    ActionPlan("09_sit_down", "sit_down_01.png", "once", fps=12),
    ActionPlan("10_sitting_idle", "frame_000.png", "cyclic", fps=9, cycles=3, event_cycles=3),
    ActionPlan("11_take_computer", "000.png", "once", fps=12),
    ActionPlan("12_wear_glasses", "000.png", "once", fps=12),
    ActionPlan("13_use_computer", "000.png", "return", fps=10, cycles=4, event_cycles=4),
    ActionPlan("14_frustrated", "000.png", "return", fps=10, event_cycles=1),
    ActionPlan("15_put_away", "000.png", "once", fps=12),
    ActionPlan("19_stand_up", "frame_00.png", "once", fps=12),
    ActionPlan(SITTING_TALKING_ACTION_ID, "frame_00.png", "cyclic", fps=14, cycles=2, event_cycles=2),
)


CUE_SPECS: dict[str, CueSpec] = {
    "sitting_thinking_short": CueSpec(
        action_id="14_frustrated",
        entry_frame="000.png",
        mode="return",
        fps=10,
        cycles=1,
    ),
}


EVENT_CUES = {
    "idle": INITIAL_CUES,
    "sit_down": ("09_sit_down", "10_sitting_idle"),
    "sitting_idle": ("10_sitting_idle",),
    "task_start": ("11_take_computer", "12_wear_glasses", "13_use_computer"),
    "start_working": ("09_sit_down", "11_take_computer", "12_wear_glasses", "13_use_computer"),
    "sitting_working": ("11_take_computer", "12_wear_glasses", "13_use_computer"),
    "busy": ("14_frustrated", "13_use_computer"),
    "thinking": ("13_use_computer",),
    "processing": ("13_use_computer",),
    "outputting": ("15_put_away", SITTING_TALKING_ACTION_ID),
    "chat_normal": (SITTING_TALKING_ACTION_ID,),
    "chat_complete": (IDLE_ACTION_ID,),
    "short_task": ("13_use_computer",),
    "memory_task": ("13_use_computer",),
    "user_input_received": (IDLE_ACTION_ID,),
    "complete": ("15_put_away", IDLE_ACTION_ID),
    "work_complete": ("15_put_away", IDLE_ACTION_ID),
    "cancel": ("15_put_away",),
    "back_to_idle": ("15_put_away", "19_stand_up"),
    "stand_up": ("19_stand_up",),
    "full_loop": (
        "09_sit_down",
        "10_sitting_idle",
        "11_take_computer",
        "12_wear_glasses",
        "13_use_computer",
        "14_frustrated",
        "13_use_computer",
        "15_put_away",
        "19_stand_up",
    ),
}


CROSS_LOOP_EVENTS = {
    SLEEP_EVENT: {"loop": "sleeping", "event": "sleep"},
}

AUTO_IDLE_EVENT = SLEEP_EVENT
AUTO_IDLE_MIN_SECONDS = 10 * 60
AUTO_IDLE_MAX_SECONDS = 30 * 60


EVENT_IDLE_ACTIONS = {
    "idle": IDLE_ACTION_ID,
    "sit_down": IDLE_ACTION_ID,
    "sitting_idle": IDLE_ACTION_ID,
    "task_start": WORKING_IDLE_ACTION_ID,
    "start_working": WORKING_IDLE_ACTION_ID,
    "sitting_working": WORKING_IDLE_ACTION_ID,
    "busy": WORKING_IDLE_ACTION_ID,
    "thinking": WORKING_IDLE_ACTION_ID,
    "processing": WORKING_IDLE_ACTION_ID,
    "outputting": IDLE_ACTION_ID,
    "chat_normal": IDLE_ACTION_ID,
    "chat_complete": IDLE_ACTION_ID,
    "short_task": WORKING_IDLE_ACTION_ID,
    "memory_task": WORKING_IDLE_ACTION_ID,
    "user_input_received": IDLE_ACTION_ID,
    "complete": IDLE_ACTION_ID,
    "work_complete": IDLE_ACTION_ID,
    "cancel": IDLE_ACTION_ID,
    "back_to_idle": IDLE_ACTION_ID,
    "stand_up": IDLE_ACTION_ID,
    "full_loop": WORKING_IDLE_ACTION_ID,
}

IMMEDIATE_EVENTS = tuple(EVENT_CUES)
TERMINAL_ACTION_IDS = (STAND_UP_ACTION_ID,)
TERMINAL_HANDOFFS = {
    STAND_UP_ACTION_ID: {"loop": "standing", "event": "wake"},
}


def cue_action_id(cue_id: str) -> str:
    spec = CUE_SPECS.get(cue_id)
    return spec.action_id if spec else cue_id


EVENT_ACTIONS = {
    event_name: tuple(cue_action_id(cue_id) for cue_id in cue_ids)
    for event_name, cue_ids in EVENT_CUES.items()
}
