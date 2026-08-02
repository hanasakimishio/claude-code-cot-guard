"""Framework-neutral example for wiring LiveCotState to stream-json events.

Your host should expose ``state.next_stop_snapshot(session_id)`` from a
loopback-only GET endpoint and set COT_GUARD_STATE_URL for the Claude child.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from live_state import LiveCotState


state = LiveCotState()


def before_prompt() -> None:
    """Call immediately before writing a real user prompt to Claude's stdin."""
    state.start()


def on_stream_json(data: dict) -> None:
    """Call for every object read from Claude Code's stream-json stdout."""
    event_type = data.get("type")
    if event_type == "stream_event":
        event = data.get("event") or {}
        if event.get("type") == "message_start":
            state.begin_assistant((event.get("message") or {}).get("model", ""))
        elif event.get("type") == "content_block_delta":
            delta = event.get("delta") or {}
            if delta.get("type") == "thinking_delta":
                state.observe_thinking(delta.get("thinking", ""))
        elif event.get("type") == "message_stop":
            state.complete_assistant()
    elif event_type == "assistant":
        message = data.get("message") or {}
        state.observe_assistant(
            message.get("model", ""),
            message.get("content") if isinstance(message.get("content"), list) else [],
        )
    elif event_type == "result":
        state.finish()


def loopback_response(session_id: str) -> dict:
    """Return this JSON from GET /cot-state/{session_id} on 127.0.0.1."""
    return state.next_stop_snapshot(session_id)
