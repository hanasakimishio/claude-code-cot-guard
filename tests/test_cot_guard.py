import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "cot_guard_hook.py"
sys.path.insert(0, str(ROOT))

from live_state import LiveCotState  # noqa: E402


class LiveCotStateTests(unittest.TestCase):
    def test_deltas_are_not_double_counted_by_full_blocks(self) -> None:
        state = LiveCotState()
        state.start()
        state.observe_model("claude-opus-example")
        state.observe_thinking("x" * 200)
        state.observe_assistant(
            "claude-opus-example",
            [{"type": "thinking", "thinking": "duplicate" * 100}],
        )
        self.assertEqual(state.thinking_chars, 200)
        self.assertEqual(state.serving_model, "claude-opus-example")

    def test_full_blocks_are_used_when_deltas_are_missing(self) -> None:
        state = LiveCotState()
        state.start()
        state.observe_assistant(
            "claude-opus-example",
            [{"type": "thinking", "thinking": "x" * 250}],
        )
        self.assertEqual(state.thinking_chars, 250)
        state.finish()
        self.assertFalse(state.active)


class StateHandler(BaseHTTPRequestHandler):
    state = {}

    def do_GET(self):  # noqa: N802
        body = json.dumps(type(self).state).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


class HookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), StateHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.session_id = "11111111-1111-4111-8111-111111111111"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.tempdir.cleanup()

    def run_hook(
        self,
        *,
        model: str,
        thinking_chars: int,
        turn_id: str,
        live: bool = True,
    ):
        StateHandler.state = {
            "session_id": self.session_id,
            "turn_id": turn_id,
            "serving_model": model,
            "thinking_chars": thinking_chars,
            "active": True,
        }
        env = os.environ.copy()
        env.update(
            {
                "HOME": self.tempdir.name,
                "COT_GUARD_CACHE_DIR": str(Path(self.tempdir.name) / "cache"),
                "COT_GUARD_MIN_CHARS": "200",
                "COT_GUARD_MAX_BLOCKS": "2",
                "COT_GUARD_ALLOWLIST": "claude-fast-",
            }
        )
        if live:
            env["COT_GUARD_STATE_URL"] = (
                f"http://127.0.0.1:{self.server.server_port}/cot-state/{{session_id}}"
            )
        else:
            env.pop("COT_GUARD_STATE_URL", None)
        payload = {
            "session_id": self.session_id,
            "transcript_path": str(Path(self.tempdir.name) / "missing.jsonl"),
            "stop_hook_active": False,
        }
        result = subprocess.run(
            [sys.executable, str(HOOK)],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            env=env,
            check=True,
        )
        return json.loads(result.stdout) if result.stdout else None

    def test_live_threshold_allowlist_and_unknown_model(self) -> None:
        self.assertEqual(
            self.run_hook(
                model="claude-opus-example", thinking_chars=0, turn_id="zero"
            )["decision"],
            "block",
        )
        self.assertEqual(
            self.run_hook(
                model="claude-opus-example", thinking_chars=199, turn_id="thin"
            )["decision"],
            "block",
        )
        self.assertIsNone(
            self.run_hook(
                model="claude-opus-example", thinking_chars=200, turn_id="healthy"
            )
        )
        self.assertIsNone(
            self.run_hook(model="claude-fast-v1", thinking_chars=0, turn_id="fast")
        )
        self.assertEqual(
            self.run_hook(model="", thinking_chars=0, turn_id="unknown")["decision"],
            "block",
        )

    def test_retry_is_checked_twice_then_capped(self) -> None:
        first = self.run_hook(
            model="claude-opus-example", thinking_chars=0, turn_id="retry"
        )
        second = self.run_hook(
            model="claude-opus-example", thinking_chars=0, turn_id="retry"
        )
        third = self.run_hook(
            model="claude-opus-example", thinking_chars=0, turn_id="retry"
        )
        self.assertEqual(first["decision"], "block")
        self.assertEqual(second["decision"], "block")
        self.assertIsNone(third)

    def test_transcript_fallback_checks_completed_turns(self) -> None:
        transcript = Path(self.tempdir.name) / "session.jsonl"
        rows = [
            {
                "type": "user",
                "uuid": "user-turn-1",
                "message": {"content": "question"},
            },
            {
                "type": "assistant",
                "message": {
                    "model": "claude-opus-example",
                    "content": [{"type": "text", "text": "answer"}],
                },
            },
        ]
        transcript.write_text(
            "\n".join(json.dumps(row) for row in rows) + "\n",
            encoding="utf-8",
        )
        env = os.environ.copy()
        env.update(
            {
                "HOME": self.tempdir.name,
                "COT_GUARD_CACHE_DIR": str(Path(self.tempdir.name) / "cache"),
                "COT_GUARD_MIN_CHARS": "200",
                "COT_GUARD_ALLOWLIST": "",
            }
        )
        env.pop("COT_GUARD_STATE_URL", None)
        result = subprocess.run(
            [sys.executable, str(HOOK)],
            input=json.dumps(
                {"session_id": self.session_id, "transcript_path": str(transcript)}
            ),
            text=True,
            capture_output=True,
            env=env,
            check=True,
        )
        self.assertEqual(json.loads(result.stdout)["decision"], "block")


if __name__ == "__main__":
    unittest.main()
