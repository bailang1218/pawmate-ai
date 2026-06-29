r"""Play sitting/work animations from pseudo state-machine signals.

Examples:
    python -m pawmate.desktop_pet.demos.sitting_work_demo --signals all
    python -m pawmate.desktop_pet.demos.sitting_work_demo --signals full_loop
    python -m pawmate.desktop_pet.demos.sitting_work_demo --signals sit_down,sitting_working,back_to_idle
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
    DEFAULT_SOURCE_ROOT,
    EVENT_CUES,
    IDLE_ACTION_ID,
    STAND_UP_ACTION_ID,
    WORKING_IDLE_ACTION_ID,
    cue_action_id,
)
from pawmate.desktop_pet.loops.sitting_work.sequences import (  # noqa: E402
    build_action_frames,
    build_cue_sequence,
    find_plan,
)
from pawmate.desktop_pet.runtime.viewer import DesktopCarousel  # noqa: E402


@dataclass(frozen=True)
class PseudoSignal:
    event: str
    label: str
    sample_trigger: str


PSEUDO_SIGNALS: tuple[PseudoSignal, ...] = (
    PseudoSignal("idle", "stand/confused -> sit down -> sitting idle", "Enter the seated idle posture."),
    PseudoSignal("sit_down", "stand/confused -> sit down -> sitting idle", "Start a sitting session."),
    PseudoSignal("sitting_idle", "sitting idle loop", "Waiting while seated."),
    PseudoSignal("sitting_working", "take computer -> wear glasses -> use computer", "A task starts running."),
    PseudoSignal("busy", "frustrated -> use computer", "The task enters a busy state."),
    PseudoSignal("thinking", "frustrated -> use computer", "The task is hard or needs thought."),
    PseudoSignal("processing", "use computer return loop", "The task is actively processing."),
    PseudoSignal("outputting", "use computer return loop", "The assistant is producing output."),
    PseudoSignal("complete", "put away computer -> stand up", "The task is finished."),
    PseudoSignal("work_complete", "put away computer -> stand up", "The task is finished."),
    PseudoSignal("back_to_idle", "put away computer -> stand up", "Compatibility alias for completion."),
    PseudoSignal("stand_up", "stand up", "Leave the sitting posture."),
    PseudoSignal("full_loop", "sit down -> work -> busy -> put away -> stand up", "A complete sitting-work cycle."),
)

DEFAULT_DEMO_EVENTS = ("idle", "sitting_working", "busy", "complete")


def cue_label(cue_id: str) -> str:
    action_id = cue_action_id(cue_id)
    return cue_id if cue_id == action_id else f"{cue_id}({action_id})"


def signal_by_event() -> dict[str, PseudoSignal]:
    return {signal.event: signal for signal in PSEUDO_SIGNALS}


def selected_events(value: str) -> list[str]:
    if value in {"all", "remaining"}:
        return list(DEFAULT_DEMO_EVENTS)
    return [event.strip() for event in value.split(",") if event.strip()]


def event_duration_ms(source_root: Path, event: str, bridge_hold: int, crossfade_frames: int) -> int:
    cue_ids = EVENT_CUES[event]
    duration = 0.0
    for cue_id in cue_ids:
        sequence = build_cue_sequence(source_root, cue_id, bridge_hold)
        duration += sum(1000.0 / plan.fps for _, plan in sequence)
        duration += crossfade_frames * 2 * (1000.0 / 10.0)
    return round(duration + 700)


def print_schedule(
    events: list[str],
    source_root: Path,
    bridge_hold: int,
    fixed_interval: float | None,
    crossfade_frames: int,
) -> int:
    elapsed = 0
    for index, event in enumerate(events, start=1):
        cues = " -> ".join(cue_label(cue_id) for cue_id in EVENT_CUES[event])
        signal = signal_by_event().get(event)
        label = signal.label if signal else event
        trigger = signal.sample_trigger if signal else ""
        duration = (
            round(fixed_interval * 1000)
            if fixed_interval
            else event_duration_ms(source_root, event, bridge_hold, crossfade_frames)
        )
        print(f"{index:02d}. ~+{elapsed / 1000:5.1f}s  {event:16} {cues:86} {label}")
        if trigger:
            print(f"      pseudo: {trigger}")
        elapsed += duration
    print(f"estimated_total~={elapsed / 1000:.1f}s (actual run is paced by the pet reaching its active idle)")
    return elapsed


def prime_pose_for_first_event(carousel: DesktopCarousel, source_root: Path, events: list[str]) -> None:
    if not events:
        return
    first = events[0]
    if first in {"idle", "sit_down", "full_loop"}:
        plan = find_plan("09_sit_down")
        frame = build_action_frames(source_root, plan)[0]
        carousel.idle_action_id = IDLE_ACTION_ID
        carousel.current_sequence = [(frame, plan)] * 20
        carousel.current_action_id = carousel.idle_action_id
    elif first in {"back_to_idle", "complete", "work_complete", "stand_up"}:
        plan = find_plan(WORKING_IDLE_ACTION_ID)
        frame = build_action_frames(source_root, plan)[0]
        carousel.idle_action_id = WORKING_IDLE_ACTION_ID
        carousel.current_sequence = [(frame, plan)] * 20
        carousel.current_action_id = carousel.idle_action_id
    else:
        return
    carousel.frame_index = 0
    carousel.show_frame()


def run_visual(args: argparse.Namespace) -> int:
    from pawmate.qt_compat import QTimer

    source_root = args.source_root.resolve()
    events = selected_events(args.signals)
    missing = [event for event in events if event not in EVENT_CUES]
    if missing:
        raise ValueError(f"Unknown event(s): {', '.join(missing)}")
    if not events:
        raise ValueError("No events selected.")

    print_schedule(events, source_root, args.bridge_hold, args.fixed_interval, args.crossfade_frames)

    carousel_args = SimpleNamespace(
        loop="sitting_work",
        source_root=source_root,
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
    carousel = DesktopCarousel(carousel_args)
    prime_pose_for_first_event(carousel, source_root, events)

    def pet_is_active_idle() -> bool:
        sequence = carousel.active_sequence()
        if sequence and sequence[-1][1].action_id == STAND_UP_ACTION_ID:
            return (
                not carousel.deferred_event_cues
                and not carousel.pending_actions
                and carousel.frame_index >= len(sequence) - 1
            )
        return (
            carousel.current_action_id == carousel.idle_action_id
            and not carousel.deferred_event_cues
            and not carousel.pending_actions
        )

    min_dwell = args.fixed_interval if args.fixed_interval else 0.4
    tail_seconds = 2.0
    state = SimpleNamespace(
        index=0,
        next_allowed=time.monotonic() + args.initial_delay_ms / 1000.0,
        quitting=False,
    )

    pump = QTimer(carousel.window)
    pump.setInterval(120)

    def step() -> None:
        now = time.monotonic()
        if not pet_is_active_idle() or now < state.next_allowed:
            return
        if state.index < len(events):
            event = events[state.index]
            state.index += 1
            print(f"-> trigger {state.index:02d}/{len(events)}: {event}")
            carousel.trigger_event(event)
            state.next_allowed = now + min_dwell
            return
        if not state.quitting:
            state.quitting = True
            pump.stop()
            QTimer.singleShot(round(tail_seconds * 1000), carousel.app.quit)

    pump.timeout.connect(step)
    pump.start()

    if args.quit_after is not None:
        QTimer.singleShot(round(args.quit_after * 1000), carousel.app.quit)

    return carousel.run()


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--signals", default="all", help="'all', 'remaining', or comma-separated events.")
    parser.add_argument("--print-only", action="store_true", help="Print the pseudo-signal schedule without opening UI.")
    parser.add_argument("--source-root", type=Path, default=DEFAULT_SOURCE_ROOT)
    parser.add_argument("--width-cm", type=float, default=4.0)
    parser.add_argument("--height-cm", type=float, default=6.0)
    parser.add_argument("--margin", type=int, default=24)
    parser.add_argument("--bridge-hold", type=int, default=5)
    parser.add_argument("--crossfade-frames", type=int, default=0)
    parser.add_argument(
        "--fixed-interval",
        type=float,
        help="Minimum seconds to dwell between actions (still waits for the active idle).",
    )
    parser.add_argument("--initial-delay-ms", type=int, default=700)
    parser.add_argument("--quit-after", type=float, help="Hard cap: quit after N seconds no matter what.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    events = selected_events(args.signals)
    missing = [event for event in events if event not in EVENT_CUES]
    if missing:
        raise ValueError(f"Unknown event(s): {', '.join(missing)}")
    if args.print_only:
        print_schedule(
            events,
            args.source_root.resolve(),
            args.bridge_hold,
            args.fixed_interval,
            args.crossfade_frames,
        )
        return 0
    return run_visual(args)


if __name__ == "__main__":
    raise SystemExit(main())
