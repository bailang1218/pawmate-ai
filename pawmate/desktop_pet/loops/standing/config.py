from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


DEFAULT_SOURCE_ROOT = Path(__file__).resolve().parents[2] / "assets" / "standing"
DEFAULT_NEUTRAL_FRAME = Path("03_idle_standby") / "idle_standby_01.png"
IDLE_ACTION_ID = "03_idle_standby"
HOVER_EVENT = "long_idle"
CLICK_EVENT_WEIGHTS = (
    ("wake", 70),
    ("user_upload_received", 20),
    ("user_touch", 10),
)


@dataclass(frozen=True)
class ActionPlan:
    action_id: str
    entry_frame: str
    mode: str
    fps: int
    cycles: int = 1
    event_cycles: int = 1
    hold_start: int = 3
    hold_end: int = 4


@dataclass(frozen=True)
class CueSpec:
    action_id: str
    entry_frame: str
    mode: str
    fps: int
    cycles: int = 1
    hold_start: int = 1
    hold_end: int = 1
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
    ActionPlan("01_confused", "confused_03.png", "cyclic", fps=10, cycles=2, event_cycles=2),
    ActionPlan("02_greeting", "greeting_01.png", "return", fps=12),
    ActionPlan("03_idle_standby", "idle_standby_01.png", "cyclic", fps=9, cycles=2),
    ActionPlan("04_daydream", "daydream_07.png", "cyclic", fps=9, cycles=2),
    ActionPlan("05_looking_around", "attention_04.png", "cyclic", fps=10, cycles=2, event_cycles=1),
    ActionPlan("06_stand_clarify", "001.png", "return", fps=11),
    ActionPlan("07_talking", "native_56.png", "cyclic", fps=14),
    ActionPlan("08_talking_v2", "028.png", "cyclic", fps=14),
    ActionPlan("09_talking_norm", "frame_024.png", "cyclic", fps=14, cycles=2, event_cycles=2),
    ActionPlan("10_aggrieved", "frame_003.png", "return", fps=10),
    ActionPlan("11_crying", "frame_002.png", "return", fps=11),
    ActionPlan("12_happy_jumping", "frame_002.png", "cyclic", fps=14),
    ActionPlan("13_shy", "frame_002.png", "cyclic", fps=10, cycles=2, event_cycles=1),
)


CUE_SPECS = {
    "02_greeting_micro": CueSpec(
        action_id="02_greeting",
        entry_frame="greeting_01.png",
        mode="explicit",
        fps=12,
        frames=(
            "greeting_01.png",
            "greeting_02.png",
            "greeting_03.png",
            "greeting_04.png",
            "greeting_03.png",
            "greeting_02.png",
            "greeting_01.png",
        ),
    ),
    "02_greeting_short": CueSpec(
        action_id="02_greeting",
        entry_frame="greeting_01.png",
        mode="explicit",
        fps=12,
        frames=(
            "greeting_01.png",
            "greeting_02.png",
            "greeting_03.png",
            "greeting_04.png",
            "greeting_05.png",
            "greeting_06.png",
            "greeting_07.png",
            "greeting_08.png",
            "greeting_09.png",
            "greeting_10.png",
            "greeting_11.png",
            "greeting_12.png",
            "greeting_13.png",
            "greeting_14.png",
            "greeting_15.png",
            "greeting_14.png",
            "greeting_13.png",
            "greeting_12.png",
            "greeting_11.png",
            "greeting_10.png",
            "greeting_09.png",
            "greeting_08.png",
            "greeting_07.png",
            "greeting_06.png",
            "greeting_05.png",
            "greeting_04.png",
            "greeting_03.png",
            "greeting_02.png",
            "greeting_01.png",
        ),
    ),
    "02_greeting_long": CueSpec(
        action_id="02_greeting",
        entry_frame="greeting_01.png",
        mode="return",
        fps=14,
        hold_start=2,
        hold_end=2,
    ),
}


EVENT_CUES = {
    "idle": ("03_idle_standby",),
    "long_idle": ("04_daydream",),
    "thinking": ("01_confused",),
    "user_input_received": ("04_daydream",),
    "user_upload_received": ("05_looking_around",),
    "user_asks_hard": ("01_confused",),
    "short_task": ("08_talking_v2",),
    "memory_task": ("01_confused",),
    "user_touch": ("05_looking_around", "02_greeting_long"),
    "wake": ("02_greeting_short",),
    "wake_up": ("02_greeting_short",),
    "chat_normal": ("09_talking_norm",),
    "chat_complete": ("03_idle_standby",),
    "chat_explain": ("07_talking",),
    "chat_hard": ("08_talking_v2",),
    "repeat_question": ("01_confused",),
    "shock": ("06_stand_clarify",),
    "sad": ("10_aggrieved",),
    "user_scold": ("10_aggrieved", "11_crying", "10_aggrieved"),
    "user_praise": ("12_happy_jumping",),
    "strong_praise": ("12_happy_jumping", "13_shy"),
    "shy": ("13_shy",),
}

IMMEDIATE_EVENTS = ("chat_complete",)


CROSS_LOOP_EVENTS = {
    "task_start": {"loop": "sitting_work", "event": "start_working"},
    "sitting_working": {"loop": "sitting_work", "event": "start_working"},
    "auto_sit_idle": {"loop": "sitting_work", "event": "idle"},
    "sit_idle": {"loop": "sitting_work", "event": "idle"},
    "idle_sitting": {"loop": "sitting_work", "event": "idle"},
}

AUTO_IDLE_EVENT = "auto_sit_idle"
AUTO_IDLE_MIN_SECONDS = 60
AUTO_IDLE_MAX_SECONDS = 150


def cue_action_id(cue_id: str) -> str:
    spec = CUE_SPECS.get(cue_id)
    return spec.action_id if spec else cue_id


EVENT_ACTIONS = {
    event_name: tuple(cue_action_id(cue_id) for cue_id in cue_ids)
    for event_name, cue_ids in EVENT_CUES.items()
}
