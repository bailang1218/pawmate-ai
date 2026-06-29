from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pawmate.desktop_pet.runtime.viewer import DesktopCarousel, load_loop_modules


LOOP_CHOICES = ("standing", "sitting_work", "sleeping")


def apply_loop_defaults(args: argparse.Namespace) -> argparse.Namespace:
    config, _, _ = load_loop_modules(args.loop)
    if args.source_root is None:
        args.source_root = config.DEFAULT_SOURCE_ROOT
    return args


def dry_run(args: argparse.Namespace) -> int:
    config, sequences, _ = load_loop_modules(args.loop)
    source_root = args.source_root.resolve()
    plans = sequences.selected_plans(args.action)
    timeline = sequences.build_timeline(source_root, plans, args.bridge_hold, args.cycles)
    print(f"loop={args.loop}")
    print(f"source_root={source_root}")
    print(f"neutral_frame={source_root / config.DEFAULT_NEUTRAL_FRAME}")
    print(f"timeline_frames={len(timeline)}")
    for plan in plans:
        frames = sequences.build_action_frames(source_root, plan)
        cycles = args.cycles or plan.cycles
        print(f"{plan.action_id}: mode={plan.mode}, entry={plan.entry_frame}, frames={len(frames)}, fps={plan.fps}, cycles={cycles}")
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Preview desktop-pet action loops.")
    parser.add_argument("--loop", choices=LOOP_CHOICES, default="standing")
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--action", action="append", help="Only preview this action id; can be repeated.")
    parser.add_argument("--mode", choices=("pet", "carousel"), default="pet")
    parser.add_argument("--width-cm", type=float, default=4.0)
    parser.add_argument("--height-cm", type=float, default=6.0)
    parser.add_argument("--margin", type=int, default=24)
    parser.add_argument("--bridge-hold", type=int, default=5)
    parser.add_argument("--crossfade-frames", type=int, default=2)
    parser.add_argument("--cycles", type=int, help="Override cycles per action.")
    parser.add_argument("--click-through", action="store_true")
    parser.add_argument(
        "--click-lookaround-after",
        type=float,
        default=600.0,
        help="Seconds of no attention before a click plays looking_around; otherwise it greets only.",
    )
    parser.add_argument("--compute-crop", action="store_true", help="Kept for old scripts; no longer used.")
    parser.add_argument("--quit-after", type=float, help="Quit automatically after N seconds.")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = apply_loop_defaults(parse_args(argv or sys.argv[1:]))
    if args.dry_run:
        return dry_run(args)
    return DesktopCarousel(args).run()


if __name__ == "__main__":
    raise SystemExit(main())
