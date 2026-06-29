from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


DESKTOP_PET_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SOURCE_ROOT = DESKTOP_PET_ROOT / "assets" / "sleeping"
DEFAULT_NEUTRAL_FRAME = Path("17_sleeping_idle") / "frame_000.png"
IDLE_ACTION_ID = "17_sleeping_idle"
FALL_ASLEEP_ACTION_ID = "16_fall_asleep"
WAKE_UP_ACTION_ID = "18_waking_up"
INITIAL_CUES = (FALL_ASLEEP_ACTION_ID, IDLE_ACTION_ID)
IDLE_CLICK_EVENT = "wake_up"
IDLE_CLICK_EXIT_FRAMES = ("frame_000.png",)


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
    ActionPlan(FALL_ASLEEP_ACTION_ID, "fall_asleep_01.png", "once", fps=12),
    ActionPlan(IDLE_ACTION_ID, "frame_000.png", "cyclic", fps=8, cycles=8, event_cycles=8, hold_start=2, hold_end=2),
    ActionPlan(WAKE_UP_ACTION_ID, "frame_000.png", "once", fps=12),
)


CUE_SPECS: dict[str, CueSpec] = {}


EVENT_CUES = {
    "idle": (IDLE_ACTION_ID,),
    "sleep": (FALL_ASLEEP_ACTION_ID, IDLE_ACTION_ID),
    "sleeping_idle": (IDLE_ACTION_ID,),
    "wake_up": (WAKE_UP_ACTION_ID,),
    "wake": (WAKE_UP_ACTION_ID,),
    "user_touch": (WAKE_UP_ACTION_ID,),
}


EVENT_IDLE_ACTIONS = {
    "idle": IDLE_ACTION_ID,
    "sleep": IDLE_ACTION_ID,
    "sleeping_idle": IDLE_ACTION_ID,
    "wake_up": IDLE_ACTION_ID,
    "wake": IDLE_ACTION_ID,
    "user_touch": IDLE_ACTION_ID,
}

IMMEDIATE_EVENTS = tuple(EVENT_CUES)
TERMINAL_ACTION_IDS = (WAKE_UP_ACTION_ID,)
TERMINAL_HANDOFFS = {
    WAKE_UP_ACTION_ID: {"loop": "sitting_work", "event": "sitting_idle"},
}

AUTO_IDLE_EVENT = "wake_up"
AUTO_IDLE_MIN_SECONDS = 5 * 60
AUTO_IDLE_MAX_SECONDS = 15 * 60


def cue_action_id(cue_id: str) -> str:
    spec = CUE_SPECS.get(cue_id)
    return spec.action_id if spec else cue_id


EVENT_ACTIONS = {
    event_name: tuple(cue_action_id(cue_id) for cue_id in cue_ids)
    for event_name, cue_ids in EVENT_CUES.items()
}
