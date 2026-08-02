#!/usr/bin/env python3
"""Run a one-shot Claude Code prompt with current-turn COT enforcement."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shlex
import subprocess
import sys
import tempfile
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from cot_guard_hook import DEFAULT_ALLOWLIST
from live_state import LiveCotState


ROOT = Path(__file__).resolve().parent
RESERVED = {
    "-c",
    "-p",
    "-r",
    "--continue",
    "--include-partial-messages",
    "--input-format",
    "--output-format",
    "--print",
    "--resume",
    "--session-id",
    "--settings",
}


class StateServer(ThreadingHTTPServer):
    tracker: LiveCotState
    session_id: str
    state_path: str


class StateHandler(BaseHTTPRequestHandler):
    server: StateServer

    def do_GET(self) -> None:  # noqa: N802
        if self.path != self.server.state_path:
            self.send_error(404)
            return
        body = json.dumps(
            self.server.tracker.next_stop_snapshot(self.server.session_id)
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_: object) -> None:
        pass


def parse_args() -> tuple[argparse.Namespace, list[str]]:
    parser = argparse.ArgumentParser(
        description="Run Claude Code with current-turn thinking enforcement."
    )
    parser.add_argument(
        "--guard-claude-bin",
        default=os.getenv("CLAUDE_BIN", "claude"),
        help="Claude Code executable (default: claude)",
    )
    parser.add_argument(
        "--guard-min-thinking",
        type=int,
        default=200,
        help="minimum thinking characters for non-allowlisted models",
    )
    parser.add_argument(
        "--guard-max-blocks",
        type=int,
        default=2,
        help="maximum blocks for one user turn",
    )
    parser.add_argument(
        "--guard-allowlist",
        default=os.getenv("COT_GUARD_ALLOWLIST", DEFAULT_ALLOWLIST),
        help="comma-separated allowlisted model prefixes",
    )
    parser.add_argument(
        "--guard-raw-stream",
        action="store_true",
        help="print raw stream-json instead of rendered text",
    )
    options, claude_args = parser.parse_known_args()
    if options.guard_min_thinking < 0 or options.guard_max_blocks < 1:
        parser.error("guard thresholds must be non-negative, with max blocks at least 1")
    for arg in claude_args:
        name = arg.split("=", 1)[0]
        if name in RESERVED:
            parser.error(f"{name} is managed by the guard")
    if not claude_args:
        parser.error("pass a prompt and any Claude Code options after guard options")
    return options, claude_args


def write_settings(path: Path, server: StateServer, options: argparse.Namespace) -> None:
    hook_command = " ".join(
        (shlex.quote(sys.executable), shlex.quote(str(ROOT / "cot_guard_hook.py")))
    )
    settings = {
        "env": {
            "COT_GUARD_STATE_URL": (
                f"http://127.0.0.1:{server.server_port}{server.state_path}"
            ),
            "COT_GUARD_MIN_CHARS": str(options.guard_min_thinking),
            "COT_GUARD_MAX_BLOCKS": str(options.guard_max_blocks),
            "COT_GUARD_ALLOWLIST": options.guard_allowlist,
            "COT_GUARD_CACHE_DIR": str(path.parent / "cache"),
        },
        "hooks": {
            "Stop": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": hook_command,
                            "timeout": 6,
                        }
                    ]
                }
            ]
        },
    }
    path.write_text(json.dumps(settings), encoding="utf-8")
    path.chmod(0o600)


def handle_event(data: dict, state: LiveCotState) -> str:
    if data.get("type") == "stream_event":
        event = data.get("event") or {}
        event_type = event.get("type")
        if event_type == "message_start":
            state.begin_assistant((event.get("message") or {}).get("model", ""))
        elif event_type == "content_block_delta":
            delta = event.get("delta") or {}
            if delta.get("type") == "thinking_delta":
                state.observe_thinking(delta.get("thinking", ""))
            elif delta.get("type") == "text_delta":
                return str(delta.get("text") or "")
        elif event_type == "message_stop":
            state.complete_assistant()
    elif data.get("type") == "assistant":
        message = data.get("message") or {}
        state.observe_assistant(
            str(message.get("model") or ""),
            message.get("content") if isinstance(message.get("content"), list) else [],
        )
    return ""


def main() -> int:
    options, claude_args = parse_args()
    session_id = str(uuid.uuid4())
    tracker = LiveCotState()
    tracker.start()
    token = secrets.token_urlsafe(24)
    server = StateServer(("127.0.0.1", 0), StateHandler)
    server.tracker = tracker
    server.session_id = session_id
    server.state_path = f"/{token}/cot-state/{session_id}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        with tempfile.TemporaryDirectory(prefix="cot-guard-") as tempdir:
            settings_path = Path(tempdir) / "settings.json"
            write_settings(settings_path, server, options)
            command = [
                options.guard_claude_bin,
                "--print",
                "--output-format",
                "stream-json",
                "--verbose",
                "--include-partial-messages",
                "--session-id",
                session_id,
                "--settings",
                str(settings_path),
                *claude_args,
            ]
            process = subprocess.Popen(command, stdout=subprocess.PIPE, text=True)
            printed_delta = False
            assert process.stdout is not None
            for line in process.stdout:
                if options.guard_raw_stream:
                    print(line, end="")
                    continue
                try:
                    data = json.loads(line)
                except ValueError:
                    print(line, end="")
                    continue
                text = handle_event(data, tracker)
                if text:
                    print(text, end="", flush=True)
                    printed_delta = True
                elif data.get("type") == "result" and not printed_delta:
                    print(str(data.get("result") or ""), end="", flush=True)
            return process.wait()
    except KeyboardInterrupt:
        if "process" in locals():
            process.terminate()
        return 130
    finally:
        tracker.finish()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


if __name__ == "__main__":
    raise SystemExit(main())
