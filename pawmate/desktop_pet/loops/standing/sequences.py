from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

from .config import (
    ACTION_PLANS,
    CUE_SPECS,
    DEFAULT_NEUTRAL_FRAME,
    ActionPlan,
)


def natural_key(path: Path) -> list[object]:
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", path.name)]


def iter_pngs(folder: Path) -> list[Path]:
    return sorted(folder.glob("*.png"), key=natural_key)


def rotate_to_entry(files: list[Path], entry_frame: str) -> list[Path]:
    try:
        entry_index = next(i for i, path in enumerate(files) if path.name == entry_frame)
    except StopIteration as exc:
        raise FileNotFoundError(f"Missing entry frame {entry_frame!r} in {files[0].parent}") from exc
    return files[entry_index:] + files[: entry_index + 1]


def return_to_entry(files: list[Path], entry_frame: str) -> list[Path]:
    try:
        entry_index = next(i for i, path in enumerate(files) if path.name == entry_frame)
    except StopIteration as exc:
        raise FileNotFoundError(f"Missing entry frame {entry_frame!r} in {files[0].parent}") from exc
    forward = files[entry_index:]
    return forward + list(reversed(forward[:-1]))


def find_plan(action_id: str) -> ActionPlan:
    for plan in ACTION_PLANS:
        if plan.action_id == action_id:
            return plan
    raise ValueError(f"Unknown action id: {action_id}")


def build_action_frames(source_root: Path, plan: ActionPlan) -> list[Path]:
    folder = source_root / plan.action_id
    files = iter_pngs(folder)
    if not files:
        raise FileNotFoundError(f"No PNG frames found in {folder}")
    if plan.mode == "cyclic":
        return rotate_to_entry(files, plan.entry_frame)
    if plan.mode == "return":
        return return_to_entry(files, plan.entry_frame)
    raise ValueError(f"Unknown loop mode: {plan.mode}")


def files_between(files: list[Path], start_frame: str, end_frame: str | None) -> list[Path]:
    try:
        start_index = next(i for i, path in enumerate(files) if path.name == start_frame)
    except StopIteration as exc:
        raise FileNotFoundError(f"Missing start frame {start_frame!r} in {files[0].parent}") from exc
    if end_frame is None:
        return files[start_index:]
    try:
        end_index = next(i for i, path in enumerate(files) if path.name == end_frame)
    except StopIteration as exc:
        raise FileNotFoundError(f"Missing end frame {end_frame!r} in {files[0].parent}") from exc
    if end_index < start_index:
        raise ValueError(f"End frame {end_frame!r} comes before {start_frame!r}")
    return files[start_index : end_index + 1]


def build_cue_frames(source_root: Path, cue_id: str) -> tuple[list[Path], ActionPlan]:
    spec = CUE_SPECS.get(cue_id)
    if spec is None:
        plan = find_plan(cue_id)
        return build_action_frames(source_root, plan), plan

    folder = source_root / spec.action_id
    files = iter_pngs(folder)
    if not files:
        raise FileNotFoundError(f"No PNG frames found in {folder}")
    by_name = {path.name: path for path in files}
    if spec.frames:
        missing = [name for name in spec.frames if name not in by_name]
        if missing:
            raise FileNotFoundError(f"Missing cue frame(s) in {folder}: {', '.join(missing)}")
        frames = [by_name[name] for name in spec.frames]
    else:
        frames = files_between(files, spec.entry_frame, spec.end_frame)
    if spec.mode == "return":
        frames = frames + list(reversed(frames[:-1]))
    elif spec.mode == "cyclic":
        frames = frames + frames[:1]
    elif spec.mode != "explicit":
        raise ValueError(f"Unknown cue loop mode: {spec.mode}")
    plan = ActionPlan(
        action_id=cue_id,
        entry_frame=spec.entry_frame,
        mode=spec.mode,
        fps=spec.fps,
        cycles=spec.cycles,
        event_cycles=spec.cycles,
        hold_start=spec.hold_start,
        hold_end=spec.hold_end,
    )
    return frames, plan


def build_timeline(
    source_root: Path,
    plans: Iterable[ActionPlan],
    bridge_hold: int,
    cycles_override: int | None = None,
) -> list[tuple[Path, ActionPlan]]:
    neutral = source_root / DEFAULT_NEUTRAL_FRAME
    timeline: list[tuple[Path, ActionPlan]] = []
    for plan in plans:
        frames = build_action_frames(source_root, plan)
        cycles = cycles_override if cycles_override is not None else plan.cycles
        timeline.extend((neutral, plan) for _ in range(bridge_hold))
        for _ in range(max(1, cycles)):
            timeline.extend((frames[0], plan) for _ in range(plan.hold_start))
            timeline.extend((frame, plan) for frame in frames)
            timeline.extend((frames[-1], plan) for _ in range(plan.hold_end))
    return timeline


def build_action_sequence(
    source_root: Path,
    plan: ActionPlan,
    bridge_hold: int,
    cycles_override: int | None = None,
) -> list[tuple[Path, ActionPlan]]:
    neutral = source_root / DEFAULT_NEUTRAL_FRAME
    frames = build_action_frames(source_root, plan)
    cycles = cycles_override if cycles_override is not None else plan.cycles
    sequence: list[tuple[Path, ActionPlan]] = []
    sequence.extend((neutral, plan) for _ in range(bridge_hold))
    for _ in range(max(1, cycles)):
        sequence.extend((frames[0], plan) for _ in range(plan.hold_start))
        sequence.extend((frame, plan) for frame in frames)
        sequence.extend((frames[-1], plan) for _ in range(plan.hold_end))
    return sequence


def build_cue_sequence(
    source_root: Path,
    cue_id: str,
    bridge_hold: int,
    cycles_override: int | None = None,
) -> list[tuple[Path, ActionPlan]]:
    neutral = source_root / DEFAULT_NEUTRAL_FRAME
    frames, plan = build_cue_frames(source_root, cue_id)
    cycles = cycles_override if cycles_override is not None else plan.event_cycles
    sequence: list[tuple[Path, ActionPlan]] = []
    sequence.extend((neutral, plan) for _ in range(bridge_hold))
    for _ in range(max(1, cycles)):
        sequence.extend((frames[0], plan) for _ in range(plan.hold_start))
        sequence.extend((frame, plan) for frame in frames)
        sequence.extend((frames[-1], plan) for _ in range(plan.hold_end))
    return sequence


def selected_plans(action_filter: list[str] | None) -> list[ActionPlan]:
    if not action_filter:
        return list(ACTION_PLANS)
    wanted = set(action_filter)
    plans = [plan for plan in ACTION_PLANS if plan.action_id in wanted]
    missing = wanted - {plan.action_id for plan in plans}
    if missing:
        raise ValueError(f"Unknown action id(s): {', '.join(sorted(missing))}")
    return plans

