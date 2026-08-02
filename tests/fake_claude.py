#!/usr/bin/env python3
"""Fake stream-json process used by the wrapper end-to-end test."""

import json
import os
import stat
import subprocess
import sys
from pathlib import Path


def option(name: str) -> str:
    index = sys.argv.index(name)
    return sys.argv[index + 1]


def emit(data: dict) -> None:
    print(json.dumps(data), flush=True)


def stream_event(data: dict) -> None:
    emit({"type": "stream_event", "event": data})


def run_stop_hook(command: str, env: dict, session_id: str) -> dict | None:
    result = subprocess.run(
        command,
        shell=True,
        input=json.dumps({"session_id": session_id, "stop_hook_active": False}),
        text=True,
        capture_output=True,
        env=env,
        check=True,
    )
    return json.loads(result.stdout) if result.stdout else None


def response(text: str, thinking: str = "") -> None:
    model = "claude-opus-5"
    stream_event({"type": "message_start", "message": {"model": model}})
    if thinking:
        stream_event(
            {
                "type": "content_block_delta",
                "delta": {"type": "thinking_delta", "thinking": thinking},
            }
        )
    stream_event(
        {
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": text},
        }
    )
    stream_event({"type": "message_stop"})
    content = ([{"type": "thinking", "thinking": thinking}] if thinking else []) + [
        {"type": "text", "text": text}
    ]
    emit({"type": "assistant", "message": {"model": model, "content": content}})


def main() -> int:
    settings_path = Path(option("--settings"))
    if stat.S_IMODE(settings_path.stat().st_mode) != 0o600:
        return 10
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    expected_prompt = os.getenv("EXPECT_ZERO_PROMPT")
    if (
        expected_prompt
        and settings["env"].get("COT_GUARD_ZERO_PROMPT") != expected_prompt
    ):
        return 13
    hook = settings["hooks"]["Stop"][0]["hooks"][0]["command"]
    env = os.environ.copy()
    env.update(settings["env"])
    session_id = option("--session-id")

    response("first answer")
    first = run_stop_hook(hook, env, session_id)
    if not first or first.get("decision") != "block":
        return 11

    response("second answer", "x" * 220)
    second = run_stop_hook(hook, env, session_id)
    if second is not None:
        return 12

    emit({"type": "result", "result": "first answersecond answer"})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
