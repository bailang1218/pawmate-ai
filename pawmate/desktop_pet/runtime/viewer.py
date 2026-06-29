from __future__ import annotations

import argparse
import importlib
import random
import sys
import time
from pathlib import Path
from types import ModuleType
from typing import Any

INTERRUPTIBLE_CRITICAL_EVENTS = {"cancel"}


def load_loop_modules(loop_name: str) -> tuple[ModuleType, ModuleType, type]:
    package = f"pawmate.desktop_pet.loops.{loop_name}"
    config = importlib.import_module(f"{package}.config")
    sequences = importlib.import_module(f"{package}.sequences")
    transition = importlib.import_module(f"{package}.transition")
    return config, sequences, transition.TransitionPlanner


class DesktopCarousel:
    def __init__(self, args: argparse.Namespace) -> None:
        from pawmate.qt_compat import (
            QApplication,
            QLabel,
            QGuiApplication,
            QPainter,
            QPixmap,
            QSize,
            QTimer,
            QWidget,
            Qt,
        )

        self.QSize = QSize
        self.Qt = Qt
        self.QPixmap = QPixmap
        self.QPainter = QPainter
        self.QGuiApplication = QGuiApplication
        self.app = QApplication.instance() or QApplication(sys.argv)
        try:
            from pawmate.ui.app_icon import apply_app_icon

            apply_app_icon()
        except Exception:
            pass

        self.loop_name = getattr(args, "loop", "standing")
        self.loop_config, self.loop_sequences, transition_planner_class = load_loop_modules(self.loop_name)
        self.CrossfadeFrame = self.loop_config.CrossfadeFrame
        self.DEFAULT_NEUTRAL_FRAME = self.loop_config.DEFAULT_NEUTRAL_FRAME
        self.EVENT_CUES = self.loop_config.EVENT_CUES
        self.cue_action_id = self.loop_config.cue_action_id
        self.default_idle_action_id = self.loop_config.IDLE_ACTION_ID
        self.idle_action_id = self.default_idle_action_id
        self.event_idle_actions = getattr(self.loop_config, "EVENT_IDLE_ACTIONS", {})
        self.cross_loop_events = getattr(self.loop_config, "CROSS_LOOP_EVENTS", {})
        self.immediate_events = set(getattr(self.loop_config, "IMMEDIATE_EVENTS", ()))
        self.initial_cues = (
            ()
            if getattr(args, "skip_initial_cues", False)
            else tuple(getattr(self.loop_config, "INITIAL_CUES", ()))
        )
        self.idle_click_event = getattr(self.loop_config, "IDLE_CLICK_EVENT", None)
        self.click_event_weights = tuple(getattr(self.loop_config, "CLICK_EVENT_WEIGHTS", ()))
        self.hover_event = getattr(self.loop_config, "HOVER_EVENT", None)
        self.idle_click_exit_frames = set(getattr(self.loop_config, "IDLE_CLICK_EXIT_FRAMES", ()))
        self.terminal_action_ids = set(getattr(self.loop_config, "TERMINAL_ACTION_IDS", ()))
        self.terminal_handoffs = getattr(self.loop_config, "TERMINAL_HANDOFFS", {})
        self.auto_idle_event = getattr(self.loop_config, "AUTO_IDLE_EVENT", None)
        self.auto_idle_min_seconds = getattr(self.loop_config, "AUTO_IDLE_MIN_SECONDS", None)
        self.auto_idle_max_seconds = getattr(self.loop_config, "AUTO_IDLE_MAX_SECONDS", None)

        self.source_root = args.source_root.resolve()
        self.mode = args.mode
        self.cycles_override = args.cycles
        self.crossfade_frames = max(0, args.crossfade_frames)
        self.transition_planner = transition_planner_class(
            neutral_path=self.source_root / self.DEFAULT_NEUTRAL_FRAME,
            bridge_hold=args.bridge_hold,
            crossfade_frames=self.crossfade_frames,
        )
        self.click_lookaround_after = args.click_lookaround_after
        self.last_attention_at = time.monotonic()
        self.plans = self.loop_sequences.selected_plans(args.action)
        self.timeline = self.loop_sequences.build_timeline(self.source_root, self.plans, args.bridge_hold, args.cycles)
        if not self.timeline:
            raise RuntimeError("No frames to display")

        self.frame_index = 0
        self.paused = False
        self.drag_start_pos = None
        self.drag_window_pos = None
        self.dragging = False
        self._on_window_moved = None
        self.pending_actions: list[str] = []
        self.deferred_event_cues: list[str] = []
        self.pending_event: str | None = None
        self.pending_idle_click_event: str | None = None
        self.action_started_callback = None
        self._last_notified_action_id: str | None = None
        if self.mode == "pet":
            self.current_sequence = self.build_initial_pet_sequence(args.cycles)
            first_plan = self.current_sequence[0][1]
            self.current_action_id = first_plan.action_id
        else:
            self.current_action_id = self.timeline[0][1].action_id
            self.current_sequence = self.timeline
        self.pixmap_cache: dict[object, Any] = {}

        self.target_size = self._target_size(args.width_cm, args.height_cm)
        self.window = QWidget()
        try:
            from pawmate.ui.app_icon import apply_app_icon

            apply_app_icon(self.window)
        except Exception:
            pass
        flags = Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool
        self.window.setWindowFlags(flags)
        self.window.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.window.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.window.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.window.setAutoFillBackground(False)
        self.window.setStyleSheet("background: transparent;")
        if args.click_through:
            self.window.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        self.label = QLabel(self.window)
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label.setFixedSize(self.target_size)
        self.label.setMouseTracking(True)
        self.label.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.label.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.label.setAutoFillBackground(False)
        self.label.setStyleSheet("background: transparent;")
        self.window.setFixedSize(self.target_size)
        self.window.setMouseTracking(True)

        self.timer = QTimer(self.window)
        self.timer.timeout.connect(self.next_frame)
        self.auto_idle_timer = QTimer(self.window)
        self.auto_idle_timer.setSingleShot(True)
        self.auto_idle_timer.timeout.connect(self.maybe_fire_auto_idle)
        if args.quit_after:
            self.quit_timer = QTimer(self.window)
            self.quit_timer.setSingleShot(True)
            self.quit_timer.timeout.connect(self.app.quit)
            self.quit_timer.start(round(args.quit_after * 1000))
        else:
            self.quit_timer = None

        self.window.keyPressEvent = self.key_press_event  # type: ignore[method-assign]
        self.window.mousePressEvent = self.mouse_press_event  # type: ignore[method-assign]
        self.window.mouseMoveEvent = self.mouse_move_event  # type: ignore[method-assign]
        self.window.mouseReleaseEvent = self.mouse_release_event  # type: ignore[method-assign]
        self.window.mouseDoubleClickEvent = self.mouse_double_click_event  # type: ignore[method-assign]
        self.window.enterEvent = self.mouse_enter_event  # type: ignore[method-assign]
        self.label.enterEvent = self.mouse_enter_event  # type: ignore[method-assign]
        self.position_bottom_right(args.margin)
        self.show_frame()
        self.window.show()
        self.timer.start(self.interval_for_current_frame())
        self.reset_auto_idle_timer()

    def _target_size(self, width_cm: float, height_cm: float):
        screen = self.app.primaryScreen()
        dpi = screen.logicalDotsPerInch() if screen else 96.0
        width_px = max(24, round(width_cm / 2.54 * dpi))
        height_px = max(24, round(height_cm / 2.54 * dpi))
        source = self.QPixmap(str(self.source_root / self.DEFAULT_NEUTRAL_FRAME))
        if not source.isNull() and source.height() > 0:
            width_px = max(width_px, round(height_px * source.width() / source.height()))
        return self.QSize(width_px, height_px)

    def build_initial_pet_sequence(self, cycles_override: int | None):
        if self.initial_cues:
            sequence = []
            for cue_id in self.initial_cues:
                sequence.extend(self.loop_sequences.build_cue_sequence(self.source_root, cue_id, 0, cycles_override))
            return sequence
        return self.loop_sequences.build_action_sequence(
            self.source_root,
            self.loop_sequences.find_plan(self.idle_action_id),
            0,
            cycles_override,
        )

    def build_event_sequence(self, cue_ids: tuple[str, ...] | list[str]):
        sequence = []
        for cue_id in cue_ids:
            cycles = (
                self.cycles_override
                if self.cycles_override is not None and self.cue_action_id(cue_id) != self.idle_action_id
                else None
            )
            sequence.extend(self.loop_sequences.build_cue_sequence(self.source_root, cue_id, 0, cycles))
        return sequence

    def position_bottom_right(self, margin: int) -> None:
        screen = self.app.primaryScreen()
        if screen is None:
            self.window.move(margin, margin)
            self._notify_window_moved()
            return
        rect = screen.availableGeometry()
        x = rect.x() + rect.width() - self.window.width() - margin
        y = rect.y() + rect.height() - self.window.height() - margin
        self.window.move(max(rect.x(), x), max(rect.y(), y))
        self._notify_window_moved()

    def set_window_moved_callback(self, callback) -> None:
        self._on_window_moved = callback

    def _notify_window_moved(self) -> None:
        if self._on_window_moved is not None:
            self._on_window_moved()

    def scaled_pixmap(self, frame):
        if frame in self.pixmap_cache:
            return self.pixmap_cache[frame]
        if isinstance(frame, self.CrossfadeFrame):
            result = self.blended_pixmap(frame)
            self.pixmap_cache[frame] = result
            return result

        source = self.QPixmap(str(frame))
        if source.isNull():
            raise RuntimeError(f"Could not load frame: {frame}")
        scaled = source.scaled(
            self.target_size,
            self.Qt.AspectRatioMode.KeepAspectRatio,
            self.Qt.TransformationMode.SmoothTransformation,
        )
        canvas = self.QPixmap(self.target_size)
        canvas.fill(self.Qt.GlobalColor.transparent)
        painter = self.QPainter(canvas)
        x = round((self.target_size.width() - scaled.width()) / 2)
        y = round((self.target_size.height() - scaled.height()) / 2)
        painter.drawPixmap(x, y, scaled)
        painter.end()
        self.pixmap_cache[frame] = canvas
        return canvas

    def blended_pixmap(self, frame):
        base = self.scaled_pixmap(frame.from_path)
        overlay = self.scaled_pixmap(frame.to_path)
        pixmap = self.QPixmap(self.target_size)
        pixmap.fill(self.Qt.GlobalColor.transparent)
        painter = self.QPainter(pixmap)
        painter.setOpacity(1.0)
        painter.drawPixmap(0, 0, base)
        painter.setOpacity(frame.alpha)
        painter.drawPixmap(0, 0, overlay)
        painter.end()
        return pixmap

    def interval_for_current_frame(self) -> int:
        _, plan = self.active_sequence()[self.frame_index]
        return max(16, round(1000 / plan.fps))

    def active_sequence(self):
        return self.current_sequence if self.mode == "pet" else self.timeline

    def show_frame(self) -> None:
        frame, plan = self.active_sequence()[self.frame_index]
        self.current_action_id = plan.action_id
        if plan.action_id != self._last_notified_action_id:
            self._last_notified_action_id = plan.action_id
            if self.action_started_callback is not None:
                self.action_started_callback(plan.action_id)
        self.label.setPixmap(self.scaled_pixmap(frame))

    def next_frame(self) -> None:
        if self.paused:
            return
        if self.should_start_idle_click_event():
            self.start_immediate_event(self.pending_idle_click_event)
            self.pending_idle_click_event = None
            self.show_frame()
            self.timer.setInterval(self.interval_for_current_frame())
            return
        sequence = self.active_sequence()
        self.frame_index += 1
        if self.frame_index >= len(sequence):
            if self.mode == "pet" and self.active_sequence_ends_terminal():
                if self.try_terminal_handoff():
                    self.show_frame()
                    self.timer.setInterval(self.interval_for_current_frame())
                    return
                self.frame_index = max(0, len(sequence) - 1)
                self.show_frame()
                self.timer.setInterval(self.interval_for_current_frame())
                return
            if self.mode == "pet":
                if self.consume_pending_event():
                    self.show_frame()
                    self.timer.setInterval(self.interval_for_current_frame())
                    return
                self.advance_pet_action()
            else:
                self.frame_index = 0
        elif self.mode == "pet" and self.should_consume_pending_after_atomic_action(sequence):
            if self.consume_pending_event():
                self.show_frame()
                self.timer.setInterval(self.interval_for_current_frame())
                return
        elif self.mode == "pet" and self.should_start_deferred_transition():
            self.advance_pet_action()
        self.show_frame()
        self.timer.setInterval(self.interval_for_current_frame())

    def active_sequence_ends_terminal(self) -> bool:
        if not self.current_sequence:
            return False
        return self.current_sequence[-1][1].action_id in self.terminal_action_ids

    def try_terminal_handoff(self) -> bool:
        if not self.current_sequence:
            return False
        action_id = self.current_sequence[-1][1].action_id
        handoff = self.terminal_handoffs.get(action_id)
        if not handoff:
            return False
        return self.start_cross_loop_event(handoff)

    def current_frame_and_plan(self):
        sequence = self.active_sequence()
        index = min(self.frame_index, len(sequence) - 1)
        return sequence[index]

    def current_plan_action_id(self) -> str | None:
        if not self.active_sequence():
            return None
        _, plan = self.current_frame_and_plan()
        return plan.action_id

    def current_action_is_atomic(self) -> bool:
        if self.loop_name != "sitting_work" or self.mode != "pet" or not self.current_sequence:
            return False
        _, plan = self.current_frame_and_plan()
        return plan.mode == "once" and self.frame_index < len(self.current_sequence) - 1

    def consume_pending_event(self) -> bool:
        if not self.pending_event:
            return False
        event_name = self.pending_event
        self.pending_event = None
        self.trigger_event(event_name)
        return True

    def should_consume_pending_after_atomic_action(self, sequence) -> bool:
        if self.loop_name != "sitting_work" or not self.pending_event:
            return False
        if self.frame_index <= 0 or self.frame_index >= len(sequence):
            return False
        _, previous_plan = sequence[self.frame_index - 1]
        _, current_plan = sequence[self.frame_index]
        return previous_plan.mode == "once" and previous_plan.action_id != current_plan.action_id

    def should_start_idle_click_event(self) -> bool:
        if not self.pending_idle_click_event:
            return False
        frame, plan = self.current_frame_and_plan()
        if plan.action_id != self.default_idle_action_id:
            return False
        if not isinstance(frame, Path):
            return False
        return frame.name in self.idle_click_exit_frames

    def should_start_deferred_transition(self) -> bool:
        if not self.deferred_event_cues:
            return False
        current_path = self.current_display_path()
        if current_path is None:
            return False
        target_sequence = self.loop_sequences.build_cue_sequence(
            self.source_root,
            self.deferred_event_cues[0],
            0,
            self.cycles_override,
        )
        if not target_sequence:
            return False
        entry_index = self.transition_planner.choose_entry_index(current_path, target_sequence)
        target_path, _ = target_sequence[entry_index]
        return self.transition_planner.can_exit_toward(current_path, target_path)

    def advance_pet_action(self) -> None:
        if self.deferred_event_cues:
            first, *rest = self.deferred_event_cues
            self.deferred_event_cues = []
            self.pending_actions = list(rest)
            self.play_action(first)
            return
        if self.pending_actions:
            self.play_action(self.pending_actions.pop(0), bridge=False)
            return
        self.play_action(self.idle_action_id, bridge=False)

    def play_action(self, action_id: str, bridge: bool = True) -> None:
        old_path = self.current_display_path()
        self.current_action_id = action_id
        cycles = (
            self.cycles_override
            if self.cycles_override is not None and self.cue_action_id(action_id) != self.idle_action_id
            else None
        )
        sequence = self.loop_sequences.build_cue_sequence(self.source_root, action_id, 0, cycles)
        self.current_sequence = self.transition_planner.build_transition(
            old_path,
            sequence,
            allow_neutral_fallback=bridge,
        )
        self.frame_index = 0

    def current_display_path(self) -> Path | None:
        if not self.current_sequence:
            return None
        index = min(self.frame_index, len(self.current_sequence) - 1)
        frame, _ = self.current_sequence[index]
        return frame.to_path if isinstance(frame, self.CrossfadeFrame) else frame

    def trigger_event(self, event_name: str) -> None:
        if event_name in INTERRUPTIBLE_CRITICAL_EVENTS:
            self.pending_event = None
        elif self.mode == "pet" and self.current_action_is_atomic():
            self.pending_event = event_name
            return
        cross_loop_event = self.cross_loop_events.get(event_name)
        if cross_loop_event and self.mode == "pet":
            self.start_cross_loop_event(cross_loop_event)
            return
        cue_ids = self.EVENT_CUES.get(event_name)
        if not cue_ids:
            return
        self.idle_action_id = self.event_idle_actions.get(event_name, self.idle_action_id)
        self.last_attention_at = time.monotonic()
        self.reset_auto_idle_timer()
        if self.mode == "pet":
            if event_name in self.immediate_events:
                self.pending_idle_click_event = None
                self.start_immediate_event(event_name)
                return
            self.deferred_event_cues = list(cue_ids)
            self.pending_actions = []
            return
        first, *rest = cue_ids
        self.pending_actions = list(rest)
        self.play_action(first)

    def switch_loop(self, loop_name: str) -> None:
        self.loop_name = loop_name
        self.loop_config, self.loop_sequences, transition_planner_class = load_loop_modules(loop_name)
        self.CrossfadeFrame = self.loop_config.CrossfadeFrame
        self.DEFAULT_NEUTRAL_FRAME = self.loop_config.DEFAULT_NEUTRAL_FRAME
        self.EVENT_CUES = self.loop_config.EVENT_CUES
        self.cue_action_id = self.loop_config.cue_action_id
        self.default_idle_action_id = self.loop_config.IDLE_ACTION_ID
        self.idle_action_id = self.default_idle_action_id
        self.event_idle_actions = getattr(self.loop_config, "EVENT_IDLE_ACTIONS", {})
        self.cross_loop_events = getattr(self.loop_config, "CROSS_LOOP_EVENTS", {})
        self.immediate_events = set(getattr(self.loop_config, "IMMEDIATE_EVENTS", ()))
        self.initial_cues = ()
        self.idle_click_event = getattr(self.loop_config, "IDLE_CLICK_EVENT", None)
        self.click_event_weights = tuple(getattr(self.loop_config, "CLICK_EVENT_WEIGHTS", ()))
        self.hover_event = getattr(self.loop_config, "HOVER_EVENT", None)
        self.idle_click_exit_frames = set(getattr(self.loop_config, "IDLE_CLICK_EXIT_FRAMES", ()))
        self.terminal_action_ids = set(getattr(self.loop_config, "TERMINAL_ACTION_IDS", ()))
        self.terminal_handoffs = getattr(self.loop_config, "TERMINAL_HANDOFFS", {})
        self.auto_idle_event = getattr(self.loop_config, "AUTO_IDLE_EVENT", None)
        self.auto_idle_min_seconds = getattr(self.loop_config, "AUTO_IDLE_MIN_SECONDS", None)
        self.auto_idle_max_seconds = getattr(self.loop_config, "AUTO_IDLE_MAX_SECONDS", None)
        self.source_root = self.loop_config.DEFAULT_SOURCE_ROOT.resolve()
        self.transition_planner = transition_planner_class(
            neutral_path=self.source_root / self.DEFAULT_NEUTRAL_FRAME,
            bridge_hold=self.transition_planner.bridge_hold,
            crossfade_frames=self.crossfade_frames,
        )
        self.plans = self.loop_sequences.selected_plans(None)
        self.timeline = self.loop_sequences.build_timeline(
            self.source_root,
            self.plans,
            self.transition_planner.bridge_hold,
            self.cycles_override,
        )
        self.pixmap_cache.clear()

    def start_cross_loop_event(self, spec: dict[str, str]) -> bool:
        loop_name = spec.get("loop")
        event_name = spec.get("event")
        if not loop_name or not event_name:
            return False
        self.switch_loop(loop_name)
        return self.start_immediate_event(event_name)

    def start_immediate_event(self, event_name: str | None) -> bool:
        if not event_name:
            return False
        cue_ids = self.EVENT_CUES.get(event_name)
        if not cue_ids:
            return False
        self.idle_action_id = self.event_idle_actions.get(event_name, self.idle_action_id)
        self.deferred_event_cues = []
        self.pending_actions = []
        self.current_sequence = self.build_event_sequence(cue_ids)
        self.current_action_id = self.current_sequence[0][1].action_id
        self.frame_index = 0
        self.last_attention_at = time.monotonic()
        self.reset_auto_idle_timer()
        return True

    def trigger_click_event(self) -> None:
        if self.try_queue_idle_click_event():
            return
        weighted_event = self.choose_weighted_click_event()
        if weighted_event:
            self.trigger_event(weighted_event)
            return
        quiet_seconds = time.monotonic() - self.last_attention_at
        event_name = "user_touch" if quiet_seconds >= self.click_lookaround_after else "wake"
        self.trigger_event(event_name)

    def choose_weighted_click_event(self, roll: float | None = None) -> str | None:
        weighted_events = [
            (event_name, float(weight))
            for event_name, weight in self.click_event_weights
            if event_name in self.EVENT_CUES and float(weight) > 0
        ]
        total = sum(weight for _, weight in weighted_events)
        if total <= 0:
            return None
        value = random.uniform(0.0, total) if roll is None else roll
        value = max(0.0, min(value, total))
        cumulative = 0.0
        for event_name, weight in weighted_events:
            cumulative += weight
            if value < cumulative:
                return event_name
        return weighted_events[-1][0]

    def trigger_hover_event(self) -> None:
        if self.mode != "pet" or not self.hover_event:
            return
        if self.current_plan_action_id() != self.default_idle_action_id:
            return
        if self.deferred_event_cues or self.pending_actions:
            return
        self.trigger_event(self.hover_event)

    def try_queue_idle_click_event(self) -> bool:
        if self.mode != "pet" or not self.idle_click_event:
            return False
        if self.current_plan_action_id() != self.default_idle_action_id:
            return True
        self.pending_idle_click_event = self.idle_click_event
        if self.should_start_idle_click_event():
            self.start_immediate_event(self.pending_idle_click_event)
            self.pending_idle_click_event = None
            self.show_frame()
            self.timer.setInterval(self.interval_for_current_frame())
        return True

    def reset_auto_idle_timer(self) -> None:
        timer = getattr(self, "auto_idle_timer", None)
        if timer is None:
            return
        timer.stop()
        if not self.auto_idle_event or self.auto_idle_min_seconds is None or self.auto_idle_max_seconds is None:
            return
        delay = random.uniform(float(self.auto_idle_min_seconds), float(self.auto_idle_max_seconds))
        timer.start(round(delay * 1000))

    def maybe_fire_auto_idle(self) -> None:
        if (
            self.mode == "pet"
            and self.auto_idle_event
            and not self.deferred_event_cues
            and not self.pending_actions
            and self.current_plan_action_id() == self.default_idle_action_id
        ):
            self.trigger_event(self.auto_idle_event)
            return
        self.reset_auto_idle_timer()

    def previous_action(self) -> None:
        current_plan = self.active_sequence()[self.frame_index][1]
        plan_ids = [plan.action_id for plan in self.plans]
        self.jump_to_action(plan_ids[plan_ids.index(current_plan.action_id) - 1])

    def next_action(self) -> None:
        current_plan = self.active_sequence()[self.frame_index][1]
        plan_ids = [plan.action_id for plan in self.plans]
        self.jump_to_action(plan_ids[(plan_ids.index(current_plan.action_id) + 1) % len(plan_ids)])

    def jump_to_action(self, action_id: str) -> None:
        if self.mode == "pet":
            self.play_action(action_id)
            return
        for index, (_, plan) in enumerate(self.timeline):
            if plan.action_id == action_id:
                self.frame_index = index
                self.show_frame()
                self.timer.setInterval(self.interval_for_current_frame())
                return

    def key_press_event(self, event) -> None:
        key = event.key()
        if key == self.Qt.Key.Key_Escape:
            self.app.quit()
        elif key == self.Qt.Key.Key_Space:
            self.paused = not self.paused
        elif key == self.Qt.Key.Key_Right:
            self.next_action()
        elif key == self.Qt.Key.Key_Left:
            self.previous_action()
        elif key == self.Qt.Key.Key_1:
            self.trigger_event("chat_normal")
        elif key == self.Qt.Key.Key_2:
            self.trigger_event("chat_explain")
        elif key == self.Qt.Key.Key_3:
            self.trigger_event("chat_hard")
        elif key == self.Qt.Key.Key_4:
            self.trigger_event("repeat_question")
        elif key == self.Qt.Key.Key_5:
            self.trigger_event("shock")
        elif key == self.Qt.Key.Key_6:
            self.trigger_event("user_scold")
        elif key == self.Qt.Key.Key_7:
            self.trigger_event("user_praise")
        elif key == self.Qt.Key.Key_8:
            self.trigger_event("strong_praise")

    def mouse_press_event(self, event) -> None:
        button = event.button()
        if button == self.Qt.MouseButton.LeftButton:
            self.window.activateWindow()
            self.window.raise_()
            try:
                self.window.grabMouse()
            except Exception:
                pass
            self.drag_start_pos = event.globalPosition().toPoint()
            self.drag_window_pos = self.window.pos()
            self.dragging = False
        elif button == self.Qt.MouseButton.RightButton:
            self.next_action()

    def mouse_move_event(self, event) -> None:
        if self.drag_start_pos is None or self.drag_window_pos is None:
            return
        if not event.buttons() & self.Qt.MouseButton.LeftButton:
            return
        delta = event.globalPosition().toPoint() - self.drag_start_pos
        if delta.manhattanLength() >= 6:
            self.dragging = True
            self.window.move(self.drag_window_pos + delta)
            self._notify_window_moved()

    def mouse_release_event(self, event) -> None:
        if event.button() != self.Qt.MouseButton.LeftButton:
            return
        try:
            self.window.releaseMouse()
        except Exception:
            pass
        was_dragging = self.dragging
        self.drag_start_pos = None
        self.drag_window_pos = None
        self.dragging = False
        if was_dragging:
            return
        if self.mode == "pet":
            self.trigger_click_event()
        else:
            self.paused = not self.paused

    def mouse_double_click_event(self, event) -> None:
        if self.mode != "pet":
            self.app.quit()

    def mouse_enter_event(self, _event) -> None:
        self.trigger_hover_event()

    def run(self) -> int:
        return self.app.exec()
