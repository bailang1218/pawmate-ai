r"""Play the full desktop-pet pseudo data flow across standing, work, and sleep.

Run from the repository root:
    python -m pawmate.desktop_pet.demos.full_flow_demo
    python -m pawmate.desktop_pet.demos.full_flow_demo --print-only
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

from pawmate.desktop_pet.loops.sitting_work.config import EVENT_CUES as SITTING_WORK_EVENT_CUES  # noqa: E402
from pawmate.desktop_pet.loops.sleeping.config import EVENT_CUES as SLEEPING_EVENT_CUES  # noqa: E402
from pawmate.desktop_pet.loops.standing.config import DEFAULT_SOURCE_ROOT, EVENT_CUES as STANDING_EVENT_CUES  # noqa: E402
from pawmate.desktop_pet.runtime.viewer import DesktopCarousel  # noqa: E402


@dataclass(frozen=True)
class FlowStep:
    event: str
    label: str


FLOW_STEPS: tuple[FlowStep, ...] = (
    FlowStep("wake", "standing: wake / greeting"),
    FlowStep("long_idle", "standing: long idle / daydream"),
    FlowStep("user_touch", "standing: long-quiet user touch"),
    FlowStep("chat_normal", "standing: normal chat"),
    FlowStep("chat_explain", "standing: explanation chat"),
    FlowStep("chat_hard", "standing: hard chat"),
    FlowStep("repeat_question", "standing: repeated question"),
    FlowStep("shock", "standing: shock / clarify"),
    FlowStep("sad", "standing: soft negative mood"),
    FlowStep("user_scold", "standing: scold / crying chain"),
    FlowStep("user_praise", "standing: praise"),
    FlowStep("strong_praise", "standing: strong praise / shy"),
    FlowStep("task_start", "handoff: standing -> sitting_work"),
    FlowStep("busy", "sitting_work: frustrated -> working idle"),
    FlowStep("thinking", "sitting_work: thinking"),
    FlowStep("processing", "sitting_work: working idle"),
    FlowStep("outputting", "sitting_work: outputting"),
    FlowStep("work_complete", "handoff: put away -> stand up -> standing"),
    FlowStep("auto_sit_idle", "handoff: standing -> sitting idle"),
    FlowStep("sleep", "handoff: sitting idle -> sleep 16/17"),
    FlowStep("wake_up", "sleeping: wake up 18 -> sitting idle"),
    FlowStep("stand_up", "handoff: sitting idle -> standing"),
)


EVENT_TABLES = (STANDING_EVENT_CUES, SITTING_WORK_EVENT_CUES, SLEEPING_EVENT_CUES)


def cue_text(event: str) -> str:
    for table in EVENT_TABLES:
        cues = table.get(event)
        if cues:
            return " -> ".join(cues)
    return "cross-loop trigger"


def print_schedule(steps: tuple[FlowStep, ...]) -> None:
    for index, step in enumerate(steps, start=1):
        print(f"{index:02d}. {step.event:16} {cue_text(step.event):62} {step.label}")


def run_visual(args: argparse.Namespace) -> int:
    from pawmate.qt_compat import QTimer

    print_schedule(FLOW_STEPS)

    carousel_args = SimpleNamespace(
        loop="standing",
        source_root=DEFAULT_SOURCE_ROOT.resolve(),
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

    def pet_is_ready() -> bool:
        return (
            carousel.current_action_id == carousel.idle_action_id
            and not carousel.deferred_event_cues
            and not carousel.pending_actions
            and not carousel.pending_idle_click_event
        )

    state = SimpleNamespace(
        index=0,
        next_allowed=time.monotonic() + args.initial_delay_ms / 1000.0,
        quitting=False,
    )

    pump = QTimer(carousel.window)
    pump.setInterval(120)

    def step() -> None:
        now = time.monotonic()
        if not pet_is_ready() or now < state.next_allowed:
            return
        if state.index < len(FLOW_STEPS):
            flow_step = FLOW_STEPS[state.index]
            state.index += 1
            print(f"-> trigger {state.index:02d}/{len(FLOW_STEPS)}: {flow_step.event} ({flow_step.label})")
            carousel.trigger_event(flow_step.event)
            state.next_allowed = now + args.min_dwell
            return
        if not state.quitting:
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
    parser.add_argument("--min-dwell", type=float, default=0.5)
    parser.add_argument("--initial-delay-ms", type=int, default=700)
    parser.add_argument("--tail-seconds", type=float, default=2.0)
    parser.add_argument("--quit-after", type=float, help="Hard cap in seconds.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    if args.print_only:
        print_schedule(FLOW_STEPS)
        return 0
    return run_visual(args)


if __name__ == "__main__":
    raise SystemExit(main())
