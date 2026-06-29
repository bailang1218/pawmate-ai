"""Sitting and working desktop-pet loop definitions."""

from __future__ import annotations

from .config import (
    ACTION_PLANS,
    DEFAULT_SOURCE_ROOT,
    EVENT_ACTIONS,
    EVENT_CUES,
    EVENT_IDLE_ACTIONS,
    IDLE_ACTION_ID,
    IDLE_CLICK_EVENT,
    IDLE_CLICK_EXIT_FRAMES,
    IMMEDIATE_EVENTS,
    INITIAL_CUES,
    STAND_UP_ACTION_ID,
    TERMINAL_ACTION_IDS,
    WORKING_IDLE_ACTION_ID,
)
from .sequences import build_cue_sequence

__all__ = [
    "ACTION_PLANS",
    "DEFAULT_SOURCE_ROOT",
    "EVENT_ACTIONS",
    "EVENT_CUES",
    "EVENT_IDLE_ACTIONS",
    "IDLE_ACTION_ID",
    "IDLE_CLICK_EVENT",
    "IDLE_CLICK_EXIT_FRAMES",
    "IMMEDIATE_EVENTS",
    "INITIAL_CUES",
    "STAND_UP_ACTION_ID",
    "TERMINAL_ACTION_IDS",
    "WORKING_IDLE_ACTION_ID",
    "build_cue_sequence",
]
