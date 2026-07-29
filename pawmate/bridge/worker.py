"""Agent worker thread.

Runs one asyncio loop in a QThread and accepts multiple concurrent turn
futures. Each submitted turn must carry its own turn_id so UI events can be
demultiplexed safely.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import logging
from collections.abc import Coroutine
from typing import Any, Dict, Optional

from pawmate.qt_compat import QThread, Signal

from pawmate.bridge.contracts import ErrorEvent, FinishedEvent, TurnCancelledEvent
from pawmate.bridge.event_bus import event_bus
from pawmate.core.runtime.engine import AgentEngine


class AgentWorker(QThread):
    ready = Signal()

    def __init__(self, engine: AgentEngine):
        super().__init__()
        self._engine = engine
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._current_task: Optional[concurrent.futures.Future] = None
        self._current_turn_id: int = 0
        self._tasks: Dict[int, concurrent.futures.Future] = {}
        self._should_stop = False

    def is_ready(self) -> bool:
        return bool(self._loop is not None and self._loop.is_running())

    def submit_coroutine(self, coroutine: Coroutine[Any, Any, Any]) -> concurrent.futures.Future:
        """Run non-chat async work on the worker's single event loop.

        Browser automation uses this entry point so Playwright objects, locks,
        and cancellation events never cross asyncio event loops or run on the
        Qt GUI thread.
        """
        loop = self._loop
        if loop is None or not loop.is_running():
            coroutine.close()
            future: concurrent.futures.Future = concurrent.futures.Future()
            future.set_exception(RuntimeError("Worker loop is not running"))
            return future
        return asyncio.run_coroutine_threadsafe(coroutine, loop)

    def run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._engine.start_heartbeat(self._loop)
            self._loop.call_soon(self.ready.emit)
            self._loop.run_forever()
        except Exception as exc:
            logging.getLogger("pawmate").exception("[Worker] loop crashed")
            try:
                event_bus.publish(ErrorEvent(f"Worker loop crashed: {exc}"))
            except Exception:
                pass
        finally:
            self._engine.stop_heartbeat()
            pending = asyncio.all_tasks(self._loop)
            for task in pending:
                task.cancel()
            self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            self._loop.close()
            self._loop = None

    def send_message(self, user_input: str, turn_id: int = 0) -> None:
        if not self._loop or not self._loop.is_running():
            event_bus.publish(ErrorEvent("Worker loop is not running", turn_id=turn_id))
            return

        current_turn_id = int(turn_id or 0)
        self._current_turn_id = current_turn_id
        try:
            chat_coro = self._engine.chat(user_input, turn_id=current_turn_id)
        except TypeError:
            chat_coro = self._engine.chat(user_input)

        future = asyncio.run_coroutine_threadsafe(chat_coro, self._loop)
        self._current_task = future
        self._tasks[current_turn_id] = future

        def _on_done(fut: concurrent.futures.Future) -> None:
            try:
                fut.result()
            except (asyncio.CancelledError, concurrent.futures.CancelledError):
                logging.getLogger("pawmate").info("[Worker] engine.chat() cancelled turn=%s", current_turn_id)
            except Exception as exc:
                logging.getLogger("pawmate").exception(
                    "[Worker] engine.chat() failed turn=%s: %s: %s",
                    current_turn_id,
                    type(exc).__name__,
                    exc,
                )
                try:
                    event_bus.publish(ErrorEvent(f"{type(exc).__name__}: {exc}", turn_id=current_turn_id))
                except Exception:
                    pass
                try:
                    event_bus.publish(FinishedEvent(turn_id=current_turn_id))
                except Exception:
                    pass
            finally:
                if self._current_task is fut:
                    self._current_task = None
                if self._tasks.get(current_turn_id) is fut:
                    self._tasks.pop(current_turn_id, None)

        future.add_done_callback(_on_done)

    def request_cancel(self, turn_id: int = 0) -> None:
        if self._engine is None or self._loop is None:
            return

        def _do_cancel() -> None:
            target_turn_id = int(turn_id or self._current_turn_id)
            future = self._tasks.get(target_turn_id)
            if future is not None and not future.done():
                future.cancel()
            deleted = 0
            try:
                deleted = self._engine.cancel_current_turn(target_turn_id)
            except Exception:
                logging.getLogger("pawmate").exception("[Worker] cancel rollback failed")
            event_bus.publish(TurnCancelledEvent(deleted, turn_id=target_turn_id))

        self._loop.call_soon_threadsafe(_do_cancel)

    def stop(self) -> None:
        self._should_stop = True
        if self._loop:
            try:
                fut = asyncio.run_coroutine_threadsafe(self._engine.shutdown(), self._loop)
                fut.result(timeout=3)
            except Exception:
                pass
            self._loop.call_soon_threadsafe(self._loop.stop)
        self.wait()
