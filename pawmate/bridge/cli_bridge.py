"""Persistent local CLI process exposed to the Web workspace via QWebChannel."""
from __future__ import annotations

import base64
import codecs
import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any, Callable

from pawmate.qt_compat import QObject, QProcess, QTimer, Signal, Slot
from pawmate.storage.app_paths import get_project_root


logger = logging.getLogger("pawmate.cli")
MAX_COMMAND_CHARS = 32 * 1024
MAX_AGENT_INPUT_CHARS = 32 * 1024


def _json(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False)


def resolve_cli_shell() -> tuple[str, list[str], str]:
    """Return an interactive shell program, arguments, and display name."""
    if os.name == "nt":
        program = shutil.which("pwsh.exe") or shutil.which("powershell.exe") or "powershell.exe"
        return program, ["-NoLogo", "-NoProfile", "-NoExit", "-Command", "-"], "PowerShell"
    program = os.environ.get("SHELL") or shutil.which("bash") or "/bin/sh"
    return program, ["-i"], Path(program).name


class CliBridge(QObject):
    """Expose the shared PawMate agent plus an explicit persistent shell."""

    outputReady = Signal(str)
    stateChanged = Signal(str)
    agentOutputReady = Signal(str)
    agentStateChanged = Signal(str)
    agentToolEvent = Signal(str)

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        working_directory: str | Path | None = None,
        process_factory: Callable[[QObject], QProcess] | None = None,
    ) -> None:
        super().__init__(parent)
        self._working_directory = Path(working_directory or get_project_root()).resolve()
        self._program, self._arguments, self._shell_name = resolve_cli_shell()
        self._uses_legacy_windows_powershell = (
            os.name == "nt"
            and Path(self._program).name.lower() == "powershell.exe"
        )
        self._input_encoding = (
            "ascii" if self._uses_legacy_windows_powershell else "utf-8"
        )
        self._process = (process_factory or QProcess)(self)
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._pending_lines: list[str] = []
        self._restart_after_stop = False
        self._closing = False
        self._chat_service: Any | None = None
        self._agent_turn_id = 0
        self._agent_state = "unavailable"

        self._process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self._process.readyReadStandardOutput.connect(self._drain_output)
        self._process.started.connect(self._on_started)
        self._process.finished.connect(self._on_finished)
        self._process.errorOccurred.connect(self._on_error)

    def set_chat_service(self, chat_service: Any) -> None:
        """Attach the already-running shared ChatService without creating another engine."""
        if chat_service is self._chat_service:
            return
        self._disconnect_chat_service()
        self._chat_service = chat_service
        chat_service.external_text_delta.connect(self._on_agent_delta)
        chat_service.external_finished.connect(self._on_agent_finished)
        chat_service.external_stream_end.connect(self._on_agent_stream_end)
        chat_service.external_error.connect(self._on_agent_error)
        chat_service.external_tool_event.connect(self._on_agent_tool_event)
        chat_service.external_cancelled.connect(self._on_agent_cancelled)
        self._agent_state = "ready"
        self._emit_agent_state()

    def _disconnect_chat_service(self) -> None:
        service = self._chat_service
        if service is None:
            return
        connections = (
            ("external_text_delta", self._on_agent_delta),
            ("external_finished", self._on_agent_finished),
            ("external_stream_end", self._on_agent_stream_end),
            ("external_error", self._on_agent_error),
            ("external_tool_event", self._on_agent_tool_event),
            ("external_cancelled", self._on_agent_cancelled),
        )
        for signal_name, slot in connections:
            signal = getattr(service, signal_name, None)
            if signal is None:
                continue
            try:
                signal.disconnect(slot)
            except (RuntimeError, TypeError, ValueError):
                pass
        self._chat_service = None

    def _agent_status_payload(self, *, ok: bool = True, error: str = "") -> dict:
        payload = {
            "ok": ok,
            "state": self._agent_state,
            "turnId": self._agent_turn_id,
            "available": self._chat_service is not None and not self._closing,
        }
        if error:
            payload["error"] = error
        return payload

    def _emit_agent_state(self, *, ok: bool = True, error: str = "") -> None:
        self.agentStateChanged.emit(
            _json(self._agent_status_payload(ok=ok, error=error))
        )

    @Slot(result=str)
    def getAgentStatus(self) -> str:
        return _json(self._agent_status_payload())

    @Slot(str, result=str)
    def submitAgent(self, text: str) -> str:
        message = str(text or "").strip()
        if not message:
            return _json(
                self._agent_status_payload(ok=False, error="Message is empty")
            )
        if len(message) > MAX_AGENT_INPUT_CHARS:
            return _json(
                self._agent_status_payload(ok=False, error="Message is too long")
            )
        if self._closing:
            return _json(
                self._agent_status_payload(ok=False, error="CLI is shutting down")
            )
        service = self._chat_service
        if service is None:
            return _json(
                self._agent_status_payload(
                    ok=False, error="PawMate Agent is not ready"
                )
            )
        if self._agent_turn_id:
            return _json(
                self._agent_status_payload(
                    ok=False, error="The Agent CLI already has a running task"
                )
            )
        if bool(getattr(service, "is_busy", lambda: False)()):
            return _json(
                self._agent_status_payload(
                    ok=False, error="PawMate is busy in another workspace"
                )
            )

        self._agent_state = "thinking"
        self._emit_agent_state()
        turn_id = int(
            service.submit_external_message(message, source="cli") or 0
        )
        if turn_id:
            active = bool(service.is_turn_active(turn_id))
            if active:
                self._agent_turn_id = turn_id
                self._agent_state = "thinking"
                self._emit_agent_state()
            return _json({
                **self._agent_status_payload(),
                "accepted": True,
                "completed": not active,
            })

        # A synchronous backend failure can emit its error before the call
        # returns. Preserve that state rather than claiming acceptance.
        if self._agent_state == "thinking":
            self._agent_state = "ready"
        self._emit_agent_state(ok=False, error="PawMate did not accept the task")
        return _json(
            self._agent_status_payload(
                ok=False, error="PawMate did not accept the task"
            )
        )

    @Slot(result=str)
    def cancelAgent(self) -> str:
        service = self._chat_service
        turn_id = self._agent_turn_id
        if service is None or not turn_id:
            return _json(
                {**self._agent_status_payload(), "cancelled": False}
            )
        cancelled = bool(service.cancel_turn(turn_id))
        return _json(
            {**self._agent_status_payload(), "cancelled": cancelled}
        )

    def _state_name(self) -> str:
        state = self._process.state()
        if state == QProcess.ProcessState.Running:
            return "running"
        if state == QProcess.ProcessState.Starting:
            return "starting"
        return "stopped"

    def _status_payload(self, *, ok: bool = True, error: str = "") -> dict:
        payload = {
            "ok": ok,
            "state": self._state_name(),
            "shell": self._shell_name,
            "program": self._program,
            "cwd": str(self._working_directory),
        }
        if error:
            payload["error"] = error
        return payload

    def _emit_state(self, *, ok: bool = True, error: str = "") -> None:
        self.stateChanged.emit(_json(self._status_payload(ok=ok, error=error)))

    @Slot(result=str)
    def getStatus(self) -> str:
        return _json(self._status_payload())

    @Slot(result=str)
    def start(self) -> str:
        if self._closing:
            return _json(self._status_payload(ok=False, error="CLI is shutting down"))
        if self._process.state() != QProcess.ProcessState.NotRunning:
            return _json(self._status_payload())
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._process.setWorkingDirectory(str(self._working_directory))
        self._process.setProgram(self._program)
        self._process.setArguments(self._arguments)
        self._process.start()
        self._emit_state()
        return _json(self._status_payload())

    @Slot(str, result=str)
    def writeLine(self, command: str) -> str:
        line = str(command or "").replace("\r", "").rstrip("\n")
        if "\x00" in line:
            return _json(self._status_payload(ok=False, error="Command contains a NUL byte"))
        if len(line) > MAX_COMMAND_CHARS:
            return _json(self._status_payload(ok=False, error="Command is too long"))
        self._pending_lines.append(line)
        if self._process.state() == QProcess.ProcessState.NotRunning:
            self.start()
        elif self._process.state() == QProcess.ProcessState.Running:
            self._flush_pending()
        return _json({**self._status_payload(), "accepted": True})

    @Slot(result=str)
    def interrupt(self) -> str:
        if self._process.state() == QProcess.ProcessState.NotRunning:
            return _json(self._status_payload())
        self._restart_after_stop = True
        self.outputReady.emit("\r\n^C\r\n[CLI] 正在中断当前命令并重启 PowerShell…\r\n")
        self._process.terminate()
        QTimer.singleShot(1200, self._kill_if_running)
        return _json({**self._status_payload(), "interrupting": True})

    @Slot(result=str)
    def restart(self) -> str:
        self._pending_lines.clear()
        if self._process.state() == QProcess.ProcessState.NotRunning:
            return self.start()
        self._restart_after_stop = True
        self._process.terminate()
        QTimer.singleShot(1200, self._kill_if_running)
        return _json({**self._status_payload(), "restarting": True})

    @Slot(result=str)
    def stop(self) -> str:
        self._restart_after_stop = False
        self._pending_lines.clear()
        if self._process.state() != QProcess.ProcessState.NotRunning:
            self._process.terminate()
            QTimer.singleShot(1200, self._kill_if_running)
        return _json(self._status_payload())

    def shutdown(self) -> None:
        self._closing = True
        if self._chat_service is not None and self._agent_turn_id:
            try:
                self._chat_service.cancel_turn(self._agent_turn_id)
            except Exception:
                logger.exception("Failed to cancel Agent CLI task during shutdown")
        self._disconnect_chat_service()
        self._restart_after_stop = False
        self._pending_lines.clear()
        if self._process.state() != QProcess.ProcessState.NotRunning:
            self._process.kill()

    def _on_started(self) -> None:
        if os.name == "nt":
            init = (
                "[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false); "
                "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
                "$OutputEncoding = [Console]::OutputEncoding\r\n"
            )
            self._process.write(init.encode("ascii"))
        self.outputReady.emit(
            f"[CLI] {self._shell_name} 已启动 · {self._working_directory}\r\n"
        )
        self._flush_pending()
        self._emit_state()

    def _flush_pending(self) -> None:
        if self._process.state() != QProcess.ProcessState.Running:
            return
        while self._pending_lines:
            line = self._pending_lines.pop(0)
            if self._uses_legacy_windows_powershell:
                payload = base64.b64encode(line.encode("utf-8")).decode("ascii")
                line = (
                    "Invoke-Expression "
                    "([Text.Encoding]::UTF8.GetString("
                    f"[Convert]::FromBase64String('{payload}')))"
                )
            self._process.write(
                (line + "\r\n").encode(self._input_encoding, errors="replace")
            )

    def _drain_output(self) -> None:
        raw = bytes(self._process.readAllStandardOutput())
        if raw:
            self.outputReady.emit(self._decoder.decode(raw))

    def _on_finished(self, *_args) -> None:
        tail = self._decoder.decode(b"", final=True)
        if tail:
            self.outputReady.emit(tail)
        should_restart = self._restart_after_stop and not self._closing
        self._restart_after_stop = False
        self._emit_state()
        if should_restart:
            QTimer.singleShot(0, self.start)

    def _on_error(self, error) -> None:
        message = str(self._process.errorString() or error)
        logger.warning("CLI process error: %s", message)
        self.outputReady.emit(f"\r\n[CLI error] {message}\r\n")
        self._emit_state(ok=False, error=message)

    def _kill_if_running(self) -> None:
        if self._process.state() != QProcess.ProcessState.NotRunning:
            self._process.kill()

    def _matches_agent_turn(self, source: str, turn_id: int) -> bool:
        if str(source or "") != "cli":
            return False
        return not self._agent_turn_id or int(turn_id or 0) == self._agent_turn_id

    def _on_agent_delta(self, source: str, turn_id: int, text: str) -> None:
        if not self._matches_agent_turn(source, turn_id):
            return
        if not self._agent_turn_id:
            self._agent_turn_id = int(turn_id or 0)
        self._agent_state = "answering"
        self.agentOutputReady.emit(str(text or ""))
        self._emit_agent_state()

    def _on_agent_stream_end(self, source: str, turn_id: int) -> None:
        if self._matches_agent_turn(source, turn_id):
            self._agent_state = "working"
            self._emit_agent_state()

    def _on_agent_finished(self, source: str, turn_id: int) -> None:
        if not self._matches_agent_turn(source, turn_id):
            return
        self._agent_turn_id = 0
        self._agent_state = "ready"
        self._emit_agent_state()

    def _on_agent_error(self, source: str, turn_id: int, message: str) -> None:
        if str(source or "") != "cli":
            return
        if turn_id and not self._matches_agent_turn(source, turn_id):
            return
        self.agentOutputReady.emit(f"\r\n[Agent error] {message}\r\n")
        if not turn_id or int(turn_id) == self._agent_turn_id:
            self._agent_state = "error"
            self._emit_agent_state(ok=False, error=str(message or "Agent error"))

    def _on_agent_tool_event(self, source: str, payload: dict) -> None:
        turn_id = int((payload or {}).get("turn_id") or 0)
        if not self._matches_agent_turn(source, turn_id):
            return
        self._agent_state = "working"
        self.agentToolEvent.emit(_json(dict(payload or {})))
        self._emit_agent_state()

    def _on_agent_cancelled(self, source: str, turn_id: int) -> None:
        if not self._matches_agent_turn(source, turn_id):
            return
        self.agentOutputReady.emit("\r\n[Agent] Task cancelled.\r\n")
        self._agent_state = "cancelling"
        self._emit_agent_state()


__all__ = [
    "CliBridge",
    "MAX_AGENT_INPUT_CHARS",
    "MAX_COMMAND_CHARS",
    "resolve_cli_shell",
]
