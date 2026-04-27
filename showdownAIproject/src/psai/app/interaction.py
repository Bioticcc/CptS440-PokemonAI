"""Manual runtime interaction ports.

This module decouples run_battle I/O from terminal-only input/output so
manual runtime can be driven by CLI or HTTP-backed desktop UI flows.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import select
import sys
import threading
import time
from typing import Any, Protocol


class ManualInteractionPort(Protocol):
    def emit(self, line: str) -> None:
        """Emit one runtime log line to the active interaction channel."""

    def prompt(
        self,
        *,
        kind: str,
        message: str,
        options: list[dict[str, str]] | None = None,
        timeout_seconds: float | None = None,
        allow_text: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> str | None:
        """Request user input for a prompt and return the selected value."""


class ConsoleInteractionPort:
    """Terminal-backed interaction port preserving current manual UX."""

    def emit(self, line: str) -> None:
        print(line)

    def prompt(
        self,
        *,
        kind: str,
        message: str,
        options: list[dict[str, str]] | None = None,
        timeout_seconds: float | None = None,
        allow_text: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> str | None:
        del kind, allow_text, metadata

        if options:
            for option in options:
                option_id = str(option.get("id", "")).strip()
                option_label = str(option.get("label", option_id)).strip()
                if option_id and option_label:
                    print(f"{option_id}. {option_label}")
                elif option_label:
                    print(option_label)

        if timeout_seconds is None:
            try:
                return input(message).strip()
            except EOFError:
                return None

        return _timed_console_input(message, timeout_seconds)


@dataclass(slots=True)
class _PromptState:
    prompt_id: int
    kind: str
    message: str
    options: list[dict[str, str]]
    allow_text: bool
    timeout_seconds: float | None
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at_monotonic: float = field(default_factory=time.monotonic)


class HttpBridgeInteractionPort:
    """Thread-safe prompt/log bridge for GUI polling over HTTP."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._next_prompt_id = 1
        self._active_prompt: _PromptState | None = None
        self._active_response: str | None = None

        self._next_log_id = 1
        self._logs: list[dict[str, Any]] = []
        self._max_logs = 5000

    def emit(self, line: str) -> None:
        text = str(line)
        with self._condition:
            log_item = {
                "id": int(self._next_log_id),
                "line": text,
                "timestamp": time.time(),
            }
            self._next_log_id += 1
            self._logs.append(log_item)
            if len(self._logs) > self._max_logs:
                self._logs = self._logs[-self._max_logs :]

    def prompt(
        self,
        *,
        kind: str,
        message: str,
        options: list[dict[str, str]] | None = None,
        timeout_seconds: float | None = None,
        allow_text: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> str | None:
        timeout_value = None if timeout_seconds is None else max(0.0, float(timeout_seconds))
        options_value = list(options or [])
        metadata_value = dict(metadata or {})

        with self._condition:
            prompt_id = int(self._next_prompt_id)
            self._next_prompt_id += 1
            self._active_prompt = _PromptState(
                prompt_id=prompt_id,
                kind=str(kind),
                message=str(message),
                options=options_value,
                allow_text=bool(allow_text),
                timeout_seconds=timeout_value,
                metadata=metadata_value,
            )
            self._active_response = None
            self._condition.notify_all()

            deadline = None if timeout_value is None else (time.monotonic() + timeout_value)
            while self._active_response is None:
                if deadline is None:
                    self._condition.wait()
                    continue

                remaining = deadline - time.monotonic()
                if remaining <= 0.0:
                    self._active_prompt = None
                    self._condition.notify_all()
                    return None
                self._condition.wait(timeout=remaining)

            response = self._active_response
            self._active_response = None
            self._active_prompt = None
            self._condition.notify_all()
            return response

    def get_prompt_snapshot(self) -> dict[str, Any] | None:
        with self._condition:
            prompt = self._active_prompt
            if prompt is None:
                return None

            elapsed = max(0.0, time.monotonic() - prompt.created_at_monotonic)
            if prompt.timeout_seconds is None:
                remaining_seconds = None
            else:
                remaining_seconds = max(0.0, float(prompt.timeout_seconds) - elapsed)

            return {
                "prompt_id": int(prompt.prompt_id),
                "kind": str(prompt.kind),
                "message": str(prompt.message),
                "options": [dict(option) for option in prompt.options],
                "allow_text": bool(prompt.allow_text),
                "timeout_seconds": prompt.timeout_seconds,
                "remaining_seconds": remaining_seconds,
                "metadata": dict(prompt.metadata),
            }

    def submit_prompt_response(
        self,
        *,
        prompt_id: int,
        choice_id: str | None,
        value: str | None,
    ) -> tuple[bool, str]:
        with self._condition:
            prompt = self._active_prompt
            if prompt is None:
                return False, "no_active_prompt"
            if int(prompt.prompt_id) != int(prompt_id):
                return False, "stale_prompt"

            normalized_choice = None if choice_id is None else str(choice_id).strip()
            normalized_value = None if value is None else str(value).strip()

            option_ids = {
                str(option.get("id", "")).strip()
                for option in prompt.options
                if str(option.get("id", "")).strip()
            }

            resolved: str | None = None
            if normalized_choice:
                if option_ids and normalized_choice not in option_ids:
                    return False, "invalid_choice"
                resolved = normalized_choice
            elif normalized_value:
                if not prompt.allow_text:
                    return False, "text_not_allowed"
                resolved = normalized_value
            elif prompt.allow_text:
                return False, "missing_text"
            else:
                return False, "missing_choice"

            self._active_response = resolved
            self._condition.notify_all()
            return True, "accepted"

    def get_logs_since(self, since: int = 0) -> dict[str, Any]:
        with self._condition:
            cursor = max(0, int(since))
            items = [dict(item) for item in self._logs if int(item["id"]) > cursor]
            next_cursor = int(items[-1]["id"]) if items else cursor
            return {
                "cursor": next_cursor,
                "items": items,
            }


def _timed_console_input(prompt: str, timeout_seconds: float) -> str | None:
    timeout = max(0.0, float(timeout_seconds))
    print(prompt, end="", flush=True)

    try:
        ready, _, _ = select.select([sys.stdin], [], [], timeout)
    except Exception:
        try:
            value = input()
        except EOFError:
            return None
        return value.strip()

    if not ready:
        print("")
        return None

    line = sys.stdin.readline()
    if line == "":
        return None
    return line.strip()
