"""Hardcoded pet-event trigger sandbox.

This file models the future data-flow boundary:

    user text + context flags -> pet_event -> standing animation action(s)

Run from the repository root:
    python tools\test_standing_pet_events.py
    python tools\test_standing_pet_events.py --check
    python tools\test_standing_pet_events.py --visual --events all
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace


REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from pawmate.desktop_pet.loops.standing.config import (  # noqa: E402
    ACTION_PLANS,
    DEFAULT_SOURCE_ROOT,
    EVENT_ACTIONS,
    EVENT_CUES,
    cue_action_id,
)
from pawmate.desktop_pet.runtime.viewer import DesktopCarousel  # noqa: E402


@dataclass(frozen=True)
class PetTriggerContext:
    repeated_question: bool = False
    answer_difficulty: str = "normal"  # normal | explain | hard
    assistant_made_mistake: bool = False
    idle_seconds: float = 0.0
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class PetEventCase:
    name: str
    text: str
    context: PetTriggerContext
    expected_event: str


def u(value: str) -> str:
    return value.encode("ascii").decode("unicode_escape")


STRONG_PRAISE_WORDS = (
    u(r"\u592a\u68d2"),
    u(r"\u592a\u5389\u5bb3"),
    u(r"\u53ef\u7231\u6b7b"),
    u(r"\u7edd\u4e86"),
    u(r"\u597d\u4e56"),
    u(r"\u6ee1\u5206"),
)
PRAISE_WORDS = (
    u(r"\u8c22\u8c22"),
    u(r"\u4e0d\u9519"),
    u(r"\u505a\u5f97\u597d"),
    u(r"\u7b54\u5f97\u597d"),
    u(r"\u5938"),
    u(r"\u559c\u6b22"),
)
SCOLD_WORDS = (
    u(r"\u9519\u4e86"),
    u(r"\u4e0d\u5bf9"),
    u(r"\u4e0d\u662f\u8fd9\u6837"),
    u(r"\u4f60\u641e\u9519"),
)
CRY_WORDS = (
    u(r"\u7b28"),
    u(r"\u6c14\u6b7b"),
    u(r"\u592a\u8fc7\u5206"),
    u(r"\u6211\u771f\u7684\u751f\u6c14"),
)
SHOCK_WORDS = (
    u(r"\u9707\u60ca"),
    u(r"\u79bb\u8c31"),
    u(r"\u4f60\u8ba4\u771f\u7684\u5417"),
    u(r"\u600e\u4e48\u4f1a\u8fd9\u6837"),
    u(r"\u5b8c\u5168\u4e0d\u61c2"),
    u(r"\u770b\u4e0d\u61c2"),
)
REPEAT_WORDS = (
    u(r"\u7b2c\u4e8c\u6b21"),
    u(r"\u53c8\u95ee"),
    u(r"\u518d\u8bf4\u4e00\u904d"),
    u(r"\u521a\u624d\u90a3\u4e2a"),
    u(r"\u8fd8\u662f\u6ca1\u61c2"),
)
WAKE_WORDS = (
    u(r"\u9192\u9192"),
    u(r"\u5728\u5417"),
    u(r"\u6765\u4e00\u4e0b"),
    "hello",
    "hi",
)


def contains_any(text: str, words: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(word.lower() in lowered for word in words)


def detect_pet_event(text: str, context: PetTriggerContext | None = None) -> str:
    """A first-pass rule router; replace pieces as the real signal grows."""
    context = context or PetTriggerContext()
    severity = context.metadata.get("severity", "").lower()
    if context.idle_seconds >= 900:
        return "long_idle"
    if contains_any(text, WAKE_WORDS):
        return "wake"
    if severity in {"high", "severe", "cry"} or contains_any(text, CRY_WORDS):
        return "user_scold"
    if context.assistant_made_mistake or contains_any(text, SCOLD_WORDS):
        return "sad"
    if contains_any(text, STRONG_PRAISE_WORDS):
        return "strong_praise"
    if contains_any(text, PRAISE_WORDS):
        return "user_praise"
    if context.repeated_question or contains_any(text, REPEAT_WORDS):
        return "repeat_question"
    if contains_any(text, SHOCK_WORDS):
        return "shock"
    if context.answer_difficulty == "hard":
        return "chat_hard"
    if context.answer_difficulty == "explain":
        return "chat_explain"
    return "chat_normal"


CASES: tuple[PetEventCase, ...] = (
    PetEventCase("long idle", "", PetTriggerContext(idle_seconds=901), "long_idle"),
    PetEventCase("wake", u(r"\u9192\u9192\uff0c\u5728\u5417"), PetTriggerContext(), "wake"),
    PetEventCase("normal chat", u(r"\u5e2e\u6211\u5199\u4e00\u4e2a\u5c0f\u51fd\u6570"), PetTriggerContext(), "chat_normal"),
    PetEventCase("explain chat", u(r"\u7ed9\u6211\u8bb2\u4e00\u4e0b\u8fd9\u4e2a\u67b6\u6784"), PetTriggerContext(answer_difficulty="explain"), "chat_explain"),
    PetEventCase("hard chat", u(r"\u8fd9\u4e2a\u591a\u4ee3\u7406\u8c03\u5ea6\u600e\u4e48\u8bbe\u8ba1\u6bd4\u8f83\u7a33"), PetTriggerContext(answer_difficulty="hard"), "chat_hard"),
    PetEventCase("repeat", u(r"\u6211\u7b2c\u4e8c\u6b21\u95ee\u4f60\u8fd9\u4e2a\u95ee\u9898\u4e86"), PetTriggerContext(), "repeat_question"),
    PetEventCase("repeat flag", u(r"\u8fd8\u662f\u8fd9\u4e2a\u95ee\u9898"), PetTriggerContext(repeated_question=True), "repeat_question"),
    PetEventCase("shock", u(r"\u4f60\u8ba4\u771f\u7684\u5417\uff0c\u8fd9\u4e2a\u7ed3\u679c\u4e5f\u592a\u79bb\u8c31\u4e86"), PetTriggerContext(), "shock"),
    PetEventCase("soft scold", u(r"\u4e0d\u5bf9\uff0c\u4f60\u521a\u521a\u641e\u9519\u4e86"), PetTriggerContext(), "sad"),
    PetEventCase("mistake flag", u(r"\u8fd9\u4e2a\u5730\u65b9\u9700\u8981\u91cd\u6765"), PetTriggerContext(assistant_made_mistake=True), "sad"),
    PetEventCase("rare crying", u(r"\u6211\u771f\u7684\u751f\u6c14\uff0c\u4f60\u8fd9\u91cc\u9519\u5f97\u592a\u8fc7\u5206\u4e86"), PetTriggerContext(metadata={"severity": "high"}), "user_scold"),
    PetEventCase("praise", u(r"\u4e0d\u9519\uff0c\u7b54\u5f97\u597d"), PetTriggerContext(), "user_praise"),
    PetEventCase("strong praise", u(r"\u592a\u68d2\u4e86\uff0c\u6ee1\u5206\uff0c\u771f\u7684\u597d\u4e56"), PetTriggerContext(), "strong_praise"),
)


ALL_VISUAL_EVENTS = (
    "idle",
    "long_idle",
    "wake",
    "user_touch",
    "chat_normal",
    "chat_explain",
    "chat_hard",
    "repeat_question",
    "shock",
    "sad",
    "user_scold",
    "user_praise",
    "strong_praise",
)


def print_cases(cases: tuple[PetEventCase, ...]) -> None:
    for case in cases:
        event = detect_pet_event(case.text, case.context)
        actions = " -> ".join(cue_label(cue_id) for cue_id in EVENT_CUES[event])
        status = "OK" if event == case.expected_event else f"EXPECTED {case.expected_event}"
        print(f"{case.name:14} {event:16} {actions:38} {status}")


def check_cases(cases: tuple[PetEventCase, ...]) -> int:
    failures = []
    for case in cases:
        event = detect_pet_event(case.text, case.context)
        if event != case.expected_event:
            failures.append((case, event))

    reachable_actions = {action for actions in EVENT_ACTIONS.values() for action in actions}
    planned_actions = {plan.action_id for plan in ACTION_PLANS}
    missing_actions = sorted(planned_actions - reachable_actions)

    if failures:
        for case, actual in failures:
            print(f"FAIL {case.name}: expected={case.expected_event}, actual={actual}, text={case.text}")
    if missing_actions:
        print(f"FAIL missing event coverage for action(s): {', '.join(missing_actions)}")

    if failures or missing_actions:
        return 1

    print(f"All {len(cases)} hardcoded pet-event cases passed.")
    print(f"All {len(planned_actions)} standing actions are reachable from EVENT_ACTIONS.")
    return 0


def selected_events(args: argparse.Namespace) -> list[str]:
    if args.events == "cases":
        return [detect_pet_event(case.text, case.context) for case in CASES]
    if args.events == "all":
        return list(ALL_VISUAL_EVENTS)
    return [event.strip() for event in args.events.split(",") if event.strip()]


def cue_label(cue_id: str) -> str:
    action_id = cue_action_id(cue_id)
    return cue_id if cue_id == action_id else f"{cue_id}({action_id})"


def run_visual_demo(args: argparse.Namespace) -> int:
    from pawmate.qt_compat import QTimer

    carousel_args = SimpleNamespace(
        source_root=args.source_root,
        action=None,
        mode="pet",
        width_cm=args.width_cm,
        height_cm=args.height_cm,
        margin=args.margin,
        bridge_hold=5,
        crossfade_frames=2,
        cycles=None,
        click_through=False,
        click_lookaround_after=600.0,
        compute_crop=False,
        quit_after=None,
    )
    carousel = DesktopCarousel(carousel_args)
    events = selected_events(args)
    missing = [event for event in events if event not in EVENT_ACTIONS]
    if missing:
        raise ValueError(f"Unknown event(s): {', '.join(missing)}")

    interval_ms = round(args.interval * 1000)
    for index, event_name in enumerate(events):
        print(f"{index + 1:02d}. {event_name}: {' -> '.join(cue_label(cue) for cue in EVENT_CUES[event_name])}")
        QTimer.singleShot(index * interval_ms, lambda name=event_name: carousel.trigger_event(name))
    if args.quit_after:
        QTimer.singleShot(round(args.quit_after * 1000), carousel.app.quit)
    return carousel.run()


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Assert hardcoded trigger cases and action coverage.")
    parser.add_argument("--visual", action="store_true", help="Play detected events in the desktop pet.")
    parser.add_argument("--events", default="cases", help="'cases', 'all', or comma-separated event names.")
    parser.add_argument("--source-root", type=Path, default=DEFAULT_SOURCE_ROOT)
    parser.add_argument("--width-cm", type=float, default=4.0)
    parser.add_argument("--height-cm", type=float, default=6.0)
    parser.add_argument("--margin", type=int, default=24)
    parser.add_argument("--interval", type=float, default=4.0, help="Seconds between visual demo events.")
    parser.add_argument("--quit-after", type=float, default=60.0, help="Auto-quit visual demo after N seconds.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    if args.check:
        return check_cases(CASES)
    if args.visual:
        return run_visual_demo(args)
    print_cases(CASES)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
