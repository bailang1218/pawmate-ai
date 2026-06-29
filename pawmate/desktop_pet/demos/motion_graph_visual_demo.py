r"""Preview motion-graph planned desktop-pet transitions visually.

This demo intentionally drives the existing viewer with MotionPlanner output:
pet_event -> intent -> clip path -> play_clip calls.
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace


REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from pawmate.desktop_pet.loops.sitting_work.config import (  # noqa: E402
    IDLE_ACTION_ID as SITTING_IDLE_ACTION_ID,
    WORKING_IDLE_ACTION_ID,
)
from pawmate.desktop_pet.runtime.motion_graph import (  # noqa: E402
    MotionRuntimeSimulator,
    MotionState,
)
from pawmate.desktop_pet.runtime.viewer import DesktopCarousel  # noqa: E402


CLIP_LOOP = {
    "09_talking_norm": "standing",
    "12_happy_jumping": "standing",
    "10_aggrieved": "standing",
    "09_sit_down": "sitting_work",
    "11_take_computer": "sitting_work",
    "12_wear_glasses": "sitting_work",
    "13_use_computer": "sitting_work",
    "14_frustrated": "sitting_work",
    "15_put_away": "sitting_work",
    "19_stand_up": "sitting_work",
    "16_fall_asleep": "sleeping",
    "17_sleeping_idle": "sleeping",
    "18_waking_up": "sleeping",
}

WORK_ENTRY_CLIPS = {"11_take_computer", "12_wear_glasses", "13_use_computer", "14_frustrated"}
WORK_EXIT_CLIPS = {"15_put_away"}


@dataclass(frozen=True)
class PlannedClip:
    label: str
    event: str
    clip_id: str
    final_state: MotionState


@dataclass(frozen=True)
class PlannedGroup:
    label: str
    event: str
    loop_name: str
    clip_ids: tuple[str, ...]
    final_state: MotionState


FLOW = (
    ("continuous / standing chat", "chat_normal", 10),
    ("continuous / enter work", "task_start", 11),
    ("continuous / processing pulse", "processing", 11),
    ("continuous / outputting pulse", "outputting", 11),
    ("continuous / complete work", "work_complete", 11),
    ("large jump / standing to sleep", "sleep", None),
    ("large jump / wake to sitting", "wake_up", None),
    ("large jump / sitting to work", "task_start", 12),
    ("race / cancel from work", "cancel", 12),
    ("race / late sad after cancel", "sad", 12),
    ("continuous / praise from sitting", "user_praise", 13),
)


def build_plan() -> list[PlannedClip]:
    runtime = MotionRuntimeSimulator(initial_state=MotionState.STANDING_IDLE)
    plan: list[PlannedClip] = []
    print("Motion graph visual demo plan:")
    for label, event, turn_id in FLOW:
        intent = runtime.receive(event, turn_id=turn_id)
        clips = runtime.run_until_settled()
        intent_text = "ignored" if intent is None else f"{intent.intent_type.value}->{intent.target.value}"
        clip_text = " -> ".join(clips) if clips else "(no clip)"
        print(f"- {label}: {event} [{intent_text}] => {clip_text}; final={runtime.current_state.value}")
        for clip_id in clips:
            plan.append(PlannedClip(label=label, event=event, clip_id=clip_id, final_state=runtime.current_state))
    return plan


def group_plan(plan: list[PlannedClip]) -> list[PlannedGroup]:
    groups: list[PlannedGroup] = []
    current: list[PlannedClip] = []
    current_loop: str | None = None
    for item in plan:
        loop_name = CLIP_LOOP[item.clip_id]
        if current and loop_name != current_loop:
            groups.append(
                PlannedGroup(
                    label=current[0].label,
                    event=current[0].event,
                    loop_name=current_loop or loop_name,
                    clip_ids=tuple(clip.clip_id for clip in current),
                    final_state=current[-1].final_state,
                )
            )
            current = []
        current.append(item)
        current_loop = loop_name
    if current:
        loop_name = current_loop or CLIP_LOOP[current[0].clip_id]
        groups.append(
            PlannedGroup(
                label=current[0].label,
                event=current[0].event,
                loop_name=loop_name,
                clip_ids=tuple(clip.clip_id for clip in current),
                final_state=current[-1].final_state,
            )
        )
    return groups


def run_visual(args: argparse.Namespace) -> int:
    from pawmate.qt_compat import QTimer

    plan = build_plan()
    groups = group_plan(plan)
    if not groups:
        raise RuntimeError("No clips planned")

    carousel_args = SimpleNamespace(
        loop="standing",
        source_root=None,
        action=None,
        mode="pet",
        width_cm=args.width_cm,
        height_cm=args.height_cm,
        margin=args.margin,
        bridge_hold=args.bridge_hold,
        crossfade_frames=args.crossfade_frames,
        cycles=None,
        skip_initial_cues=True,
        click_through=False,
        click_lookaround_after=600.0,
        compute_crop=False,
        quit_after=None,
    )
    from pawmate.desktop_pet.app import apply_loop_defaults

    carousel = DesktopCarousel(apply_loop_defaults(carousel_args))

    state = SimpleNamespace(
        index=0,
        waiting_group=None,
        group_started_at=0.0,
        next_allowed=time.monotonic() + args.initial_delay_ms / 1000.0,
        quitting=False,
    )

    pump = QTimer(carousel.window)
    pump.setInterval(120)

    def configure_idle_for_clip(clip_id: str) -> None:
        if carousel.loop_name == "sitting_work":
            if clip_id in WORK_ENTRY_CLIPS:
                carousel.idle_action_id = WORKING_IDLE_ACTION_ID
            elif clip_id in WORK_EXIT_CLIPS:
                carousel.idle_action_id = SITTING_IDLE_ACTION_ID

    def configure_loop_for_group(group: PlannedGroup) -> None:
        if carousel.loop_name != group.loop_name:
            carousel.switch_loop(group.loop_name)
        if carousel.loop_name == "sitting_work":
            carousel.terminal_handoffs = {
                **carousel.terminal_handoffs,
                "19_stand_up": {"loop": "standing", "event": "idle"},
            }
        for clip_id in group.clip_ids:
            configure_idle_for_clip(clip_id)

    def build_group_sequence(group: PlannedGroup):
        sequence = []
        for clip_id in group.clip_ids:
            sequence.extend(carousel.loop_sequences.build_cue_sequence(carousel.source_root, clip_id, 0, None))
        return sequence

    def start_group(group: PlannedGroup) -> None:
        configure_loop_for_group(group)
        print(
            f"-> {state.index + 1:02d}/{len(groups)} {group.label}: "
            f"{' -> '.join(group.clip_ids)}"
        )
        carousel.current_sequence = build_group_sequence(group)
        carousel.frame_index = 0
        carousel.current_action_id = carousel.current_sequence[0][1].action_id
        carousel.show_frame()
        carousel.timer.setInterval(carousel.interval_for_current_frame())
        state.waiting_group = group
        state.group_started_at = time.monotonic()

    def waiting_group_finished() -> bool:
        group = state.waiting_group
        if group is None:
            return True
        if time.monotonic() - state.group_started_at < args.min_clip_seconds:
            return False
        return carousel.current_action_id not in set(group.clip_ids)

    def step() -> None:
        now = time.monotonic()
        if state.quitting:
            return
        if state.waiting_group is not None:
            if not waiting_group_finished():
                return
            state.waiting_group = None
            state.next_allowed = now + args.dwell
            return
        if now < state.next_allowed:
            return
        if state.index < len(groups):
            group = groups[state.index]
            state.index += 1
            start_group(group)
            return
        state.quitting = True
        pump.stop()
        QTimer.singleShot(round(args.tail_seconds * 1000), carousel.app.quit)

    pump.timeout.connect(step)
    pump.start()
    if args.quit_after is not None:
        QTimer.singleShot(round(args.quit_after * 1000), carousel.app.quit)
    return carousel.run()


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--print-only", action="store_true")
    parser.add_argument("--width-cm", type=float, default=4.0)
    parser.add_argument("--height-cm", type=float, default=6.0)
    parser.add_argument("--margin", type=int, default=24)
    parser.add_argument("--bridge-hold", type=int, default=5)
    parser.add_argument("--crossfade-frames", type=int, default=2)
    parser.add_argument("--dwell", type=float, default=0.45)
    parser.add_argument("--min-clip-seconds", type=float, default=0.25)
    parser.add_argument("--initial-delay-ms", type=int, default=700)
    parser.add_argument("--tail-seconds", type=float, default=2.0)
    parser.add_argument("--quit-after", type=float, default=180.0)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    if args.print_only:
        build_plan()
        return 0
    return run_visual(args)


if __name__ == "__main__":
    raise SystemExit(main())
