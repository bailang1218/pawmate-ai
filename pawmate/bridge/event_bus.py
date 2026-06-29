"""Thin typed event bus for cross-subsystem app events."""
from __future__ import annotations

import asyncio
import inspect
import logging
from collections import defaultdict
from collections.abc import Callable
from typing import Any

from pawmate.bridge.contracts import (
    AppEvent,
    EventKind,
)
from pawmate.qt_compat import QObject, Signal

Handler = Callable[[AppEvent], Any]


class EventBus(QObject):
    """Transport cross-subsystem events without owning subsystem behavior.

    Producers publish typed contracts from ``pawmate.bridge.contracts``.
    Consumers can subscribe in-process with ``subscribe`` or cross the Qt
    thread boundary through the single typed ``event`` signal.
    """

    event = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._subscribers: dict[type[AppEvent], list[Handler]] = defaultdict(list)
        self._kind_subscribers: dict[str, list[Handler]] = defaultdict(list)
        # Strong refs to in-flight async handler tasks. The event loop only keeps
        # a weak reference, so without this set a pending task can be garbage
        # collected mid-execution and the handler silently never runs.
        self._pending_tasks: set[asyncio.Task[Any]] = set()

    def publish(self, event: AppEvent, *args: Any, **kwargs: Any) -> None:
        """Publish one typed event to Python subscribers and Qt listeners."""
        if not isinstance(event, AppEvent) or args or kwargs:
            raise TypeError("EventBus.publish requires an AppEvent instance")
        self._dispatch(event)
        self.event.emit(event)

    def subscribe(self, event_type: type[AppEvent] | EventKind | str, handler: Handler) -> None:
        if isinstance(event_type, type):
            bucket = self._subscribers[event_type]
        else:
            bucket = self._kind_subscribers[_kind_value(event_type)]
        if handler not in bucket:
            bucket.append(handler)

    def subscribe_once(self, event_type: type[AppEvent] | EventKind | str, handler: Handler) -> None:
        def wrapper(event: AppEvent) -> Any:
            self.unsubscribe(event_type, wrapper)
            return handler(event)

        self.subscribe(event_type, wrapper)

    once = subscribe_once

    def unsubscribe(self, event_type: type[AppEvent] | EventKind | str, handler: Handler) -> None:
        if isinstance(event_type, type):
            bucket = self._subscribers.get(event_type, [])
        else:
            bucket = self._kind_subscribers.get(_kind_value(event_type), [])
        try:
            bucket.remove(handler)
        except ValueError:
            pass

    def clear(self) -> None:
        """Clear typed Python subscribers.

        Qt signal connections are owned by Qt and remain connected.
        """
        self._subscribers.clear()
        self._kind_subscribers.clear()

    def _dispatch(self, event: AppEvent) -> None:
        handlers: list[Handler] = []
        handlers.extend(self._subscribers.get(AppEvent, ()))
        handlers.extend(self._subscribers.get(type(event), ()))
        handlers.extend(self._kind_subscribers.get(event.kind.value, ()))
        for handler in tuple(handlers):
            try:
                result = handler(event)
                if inspect.isawaitable(result):
                    self._schedule_awaitable(result)
            except Exception:
                logging.getLogger("pawmate").exception(
                    "[EventBus] handler failed for %s", event.kind.value
                )

    def _schedule_awaitable(self, awaitable: Any) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logging.getLogger("pawmate").warning(
                "[EventBus] async handler returned awaitable without a running loop"
            )
            return
        task = loop.create_task(awaitable)
        self._pending_tasks.add(task)
        task.add_done_callback(self._pending_tasks.discard)


def _kind_value(event_type: EventKind | str) -> str:
    return event_type.value if isinstance(event_type, EventKind) else str(event_type)


event_bus = EventBus()
