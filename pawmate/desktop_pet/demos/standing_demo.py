r"""Play standing-pet animations from pseudo state-machine signals.

This is a visual sandbox for deciding future trigger logic before the real
PawMate data flow is wired in.

Examples:
    python tools\pet_pseudo_signal_demo.py
    python tools\pet_pseudo_signal_demo.py --signals all
    python tools\pet_pseudo_signal_demo.py --signals chat_normal,chat_hard,user_praise
    python tools\pet_pseudo_signal_demo.py --signals all --min-dwell 1.5

Pacing note:
    In pet mode a triggered event only starts at a safe exit point, so this demo
    fires the next signal only once the pet has actually returned to idle (not
    on an estimated timer). That guarantees every action plays in full and the
    window stays open until the last one finishes.
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

from pawmate.desktop_pet.loops.standing.config import (  # noqa: E402
    DEFAULT_SOURCE_ROOT,
    EVENT_CUES,
    IDLE_ACTION_ID,
    cue_action_id,
)
from pawmate.desktop_pet.loops.standing.sequences import build_cue_sequence  # noqa: E402
from pawmate.desktop_pet.runtime.viewer import DesktopCarousel  # noqa: E402


@dataclass(frozen=True)
class PseudoSignal:
    event: str
    label: str
    sample_trigger: str


PSEUDO_SIGNALS: tuple[PseudoSignal, ...] = (
    PseudoSignal("long_idle", "long idle / daydream", "No user activity for a long time."),
    PseudoSignal("user_touch", "long-quiet click", "User clicks the pet after 10+ minutes."),
    PseudoSignal("chat_normal", "normal chat", "Short ordinary answer."),
    PseudoSignal("chat_explain", "explain answer", "Teaching or explaining a normal topic."),
    PseudoSignal("chat_hard", "hard answer", "Hard question or long reasoning."),
    PseudoSignal("repeat_question", "repeat question", "User asks the same thing again."),
    PseudoSignal("shock", "shock / clarify", "User says something surprising or confusing."),
    PseudoSignal("sad", "aggrieved", "Soft negative mood, not full crying."),
    PseudoSignal("user_scold", "aggrieved -> crying -> aggrieved", "Rare severe scold after an assistant mistake."),
    PseudoSignal("user_praise", "happy", "User praises a good answer."),
    PseudoSignal("strong_praise", "happy + shy", "User praises it a lot."),
)


def cue_label(cue_id: str) -> str:
    action_id = cue_action_id(cue_id)
    return cue_id if cue_id == action_id else f"{cue_id}({action_id})"


def signal_by_event() -> dict[str, PseudoSignal]:
    return {signal.event: signal for signal in PSEUDO_SIGNALS}


def selected_events(value: str) -> list[str]:
    if value == "remaining":
        return [signal.event for signal in PSEUDO_SIGNALS]
    if value == "all":
        return list(EVENT_CUES)
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
        print(f"{index:02d}. ~+{elapsed / 1000:5.1f}s  {event:16} {cues:42} {label}")
        if trigger:
            print(f"      pseudo: {trigger}")
        elapsed += duration
    print(f"estimated_total~={elapsed / 1000:.1f}s (actual run is paced by the pet returning to idle)")
    return elapsed


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
        source_root=source_root,
        action=None,
        mode="pet",
        width_cm=args.width_cm,
        height_cm=args.height_cm,
        margin=args.margin,
        bridge_hold=args.bridge_hold,
        crossfade_frames=args.crossfade_frames,
        cycles=None,
        click_through=False,
        click_lookaround_after=600.0,
        compute_crop=False,
        quit_after=None,
    )
    carousel = DesktopCarousel(carousel_args)

    # Pace the schedule off the pet's ACTUAL state, not estimated durations.
    # A triggered event only starts at a safe exit point, and re-triggering while
    # it is still playing would clobber the queued cues, so we wait until the pet
    # is back to idle (and a small dwell has passed) before firing the next one.
    def pet_is_idle() -> bool:
        return (
            carousel.current_action_id == IDLE_ACTION_ID
            and not carousel.deferred_event_cues
            and not carousel.pending_actions
        )

    # --fixed-interval (if given) acts as a minimum dwell between actions.
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
        if not pet_is_idle() or now < state.next_allowed:
            return
        if state.index < len(events):
            event = events[state.index]
            state.index += 1
            print(f"-> trigger {state.index:02d}/{len(events)}: {event}")
            carousel.trigger_event(event)
            state.next_allowed = now + min_dwell
            return
        # All signals fired and the pet is idle again -> wind down.
        if not state.quitting:
            state.quitting = True
            pump.stop()
            QTimer.singleShot(round(tail_seconds * 1000), carousel.app.quit)

    pump.timeout.connect(step)
    pump.start()

    # Optional hard cap regardless of progress.
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
    parser.add_argument("--crossfade-frames", type=int, default=2)
    parser.add_argument(
        "--fixed-interval",
        type=float,
        help="Minimum seconds to dwell between actions (still waits for idle).",
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
