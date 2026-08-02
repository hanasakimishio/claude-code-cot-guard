"""Minimal current-turn thinking counter for stream-json hosts."""

import uuid
from dataclasses import dataclass


@dataclass
class LiveCotState:
    """Track the actual model and thinking length for one Claude process."""

    turn_id: str = ""
    serving_model: str = ""
    thinking_chars: int = 0
    active: bool = False
    saw_thinking_delta: bool = False

    def start(self) -> None:
        self.turn_id = str(uuid.uuid4())
        self.serving_model = ""
        self.thinking_chars = 0
        self.active = True
        self.saw_thinking_delta = False

    def observe_model(self, model: str) -> None:
        if self.active and model and not model.startswith("<"):
            self.serving_model = model

    def observe_thinking(self, text: str) -> None:
        if self.active and text:
            self.thinking_chars += len(text)
            self.saw_thinking_delta = True

    def observe_assistant(self, model: str, blocks: list) -> None:
        if not self.active:
            return
        self.observe_model(model)
        if not self.saw_thinking_delta:
            for block in blocks or []:
                if isinstance(block, dict) and block.get("type") == "thinking":
                    self.thinking_chars += len(block.get("thinking") or "")
        self.saw_thinking_delta = False

    def finish(self) -> None:
        self.active = False

    def snapshot(self, session_id: str) -> dict:
        return {
            "session_id": session_id,
            "turn_id": self.turn_id,
            "serving_model": self.serving_model,
            "thinking_chars": self.thinking_chars,
            "active": self.active,
        }
