"""Thread-safe current-turn state shared by a stream reader and Stop hook."""

from __future__ import annotations

import threading
import time
import uuid


class LiveCotState:
    def __init__(self) -> None:
        self._changed = threading.Condition()
        self.turn_id = ""
        self.serving_model = ""
        self.thinking_chars = 0
        self.active = False
        self._assistant_open = False
        self._awaiting_full_assistant = False
        self._saw_thinking_delta = False
        self._assistant_seq = 0
        self._served_seq = 0

    def start(self) -> None:
        with self._changed:
            self.turn_id = str(uuid.uuid4())
            self.serving_model = ""
            self.thinking_chars = 0
            self.active = True
            self._assistant_open = False
            self._awaiting_full_assistant = False
            self._saw_thinking_delta = False
            self._assistant_seq = 0
            self._served_seq = 0

    def begin_assistant(self, model: str) -> None:
        with self._changed:
            if not self.active:
                return
            self._assistant_open = True
            self._awaiting_full_assistant = False
            self._saw_thinking_delta = False
            self._set_model(model)

    def observe_thinking(self, text: str) -> None:
        with self._changed:
            if self.active and text:
                self.thinking_chars += len(text)
                self._saw_thinking_delta = True

    def complete_assistant(self) -> None:
        with self._changed:
            if not self.active or not self._assistant_open:
                return
            self._assistant_open = False
            self._awaiting_full_assistant = True
            self._assistant_seq += 1
            self._changed.notify_all()

    def observe_assistant(self, model: str, blocks: list) -> None:
        """Fallback for hosts that receive full assistant objects, not deltas."""
        with self._changed:
            if not self.active:
                return
            self._set_model(model)
            if self._awaiting_full_assistant:
                self._awaiting_full_assistant = False
                return
            if not self._assistant_open:
                self._assistant_open = True
            if not self._saw_thinking_delta:
                self.thinking_chars += sum(
                    len(block.get("thinking") or "")
                    for block in blocks or []
                    if isinstance(block, dict) and block.get("type") == "thinking"
                )
            self._assistant_open = False
            self._assistant_seq += 1
            self._changed.notify_all()

    def finish(self) -> None:
        with self._changed:
            self.active = False
            self._changed.notify_all()

    def next_stop_snapshot(self, session_id: str, timeout: float = 3) -> dict:
        """Wait for an unconsumed assistant completion, then consume it."""
        deadline = time.monotonic() + timeout
        with self._changed:
            while self.active and self._assistant_seq <= self._served_seq:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._changed.wait(remaining)
            ready = self._assistant_seq > self._served_seq
            if ready:
                self._served_seq = self._assistant_seq
            return self._snapshot(session_id, ready)

    def _set_model(self, model: str) -> None:
        if model and not model.startswith("<"):
            self.serving_model = model

    def _snapshot(self, session_id: str, ready: bool) -> dict:
        return {
            "session_id": session_id,
            "turn_id": self.turn_id,
            "serving_model": self.serving_model,
            "thinking_chars": self.thinking_chars,
            "active": self.active,
            "ready": ready,
        }
