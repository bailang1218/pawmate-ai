"""Aggregated desktop-pet loop exports."""

from __future__ import annotations

from pawmate.desktop_pet.loops.standing.config import (
    ACTION_PLANS as STANDING_ACTION_PLANS,
    EVENT_ACTIONS as STANDING_EVENT_ACTIONS,
    EVENT_CUES as STANDING_EVENT_CUES,
)
from pawmate.desktop_pet.loops.standing.sequences import build_cue_sequence as build_standing_cue_sequence
from pawmate.desktop_pet.loops.sitting_work.config import (
    ACTION_PLANS as SITTING_WORK_ACTION_PLANS,
    EVENT_ACTIONS as SITTING_WORK_EVENT_ACTIONS,
    EVENT_CUES as SITTING_WORK_EVENT_CUES,
    EVENT_IDLE_ACTIONS as SITTING_WORK_EVENT_IDLE_ACTIONS,
)
from pawmate.desktop_pet.loops.sitting_work.sequences import build_cue_sequence as build_sitting_work_cue_sequence
from pawmate.desktop_pet.loops.sleeping.config import (
    ACTION_PLANS as SLEEPING_ACTION_PLANS,
    EVENT_ACTIONS as SLEEPING_EVENT_ACTIONS,
    EVENT_CUES as SLEEPING_EVENT_CUES,
    EVENT_IDLE_ACTIONS as SLEEPING_EVENT_IDLE_ACTIONS,
)
from pawmate.desktop_pet.loops.sleeping.sequences import build_cue_sequence as build_sleeping_cue_sequence

__all__ = [
    "SLEEPING_ACTION_PLANS",
    "SLEEPING_EVENT_ACTIONS",
    "SLEEPING_EVENT_CUES",
    "SLEEPING_EVENT_IDLE_ACTIONS",
    "SITTING_WORK_ACTION_PLANS",
    "SITTING_WORK_EVENT_ACTIONS",
    "SITTING_WORK_EVENT_CUES",
    "SITTING_WORK_EVENT_IDLE_ACTIONS",
    "STANDING_ACTION_PLANS",
    "STANDING_EVENT_ACTIONS",
    "STANDING_EVENT_CUES",
    "build_sleeping_cue_sequence",
    "build_sitting_work_cue_sequence",
    "build_standing_cue_sequence",
]
