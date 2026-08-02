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
WRAPPER = ROOT / "guarded_claude.py"
FAKE_CLAUDE = ROOT / "tests" / "fake_claude.py"
sys.path.insert(0, str(ROOT))

from cot_guard_hook import DEFAULT_ALLOW_PREFIXES  # noqa: E402
from live_state import LiveCotState  # noqa: E402


class LiveCotStateTests(unittest.TestCase):
    def test_stream_completion_is_consumed_exactly_once(self) -> None:
        state = LiveCotState()
        state.start()
        state.begin_assistant("claude-opus-5")
        state.observe_thinking("x" * 200)
        state.complete_assistant()

        first = state.next_stop_snapshot("session", timeout=0.01)
        self.assertTrue(first["ready"])
        self.assertEqual(first["thinking_chars"], 200)

        state.observe_assistant(
            "claude-opus-5",
            [{"type": "thinking", "thinking": "duplicate" * 100}],
        )
        duplicate = state.next_stop_snapshot("session", timeout=0.01)
        self.assertFalse(duplicate["ready"])
        self.assertEqual(duplicate["thinking_chars"], 200)

    def test_full_assistant_is_a_fallback_when_deltas_are_missing(self) -> None:
        state = LiveCotState()
        state.start()
        state.observe_assistant(
            "claude-opus-5",
            [{"type": "thinking", "thinking": "x" * 250}],
        )
        snapshot = state.next_stop_snapshot("session", timeout=0.01)
        self.assertTrue(snapshot["ready"])
        self.assertEqual(snapshot["thinking_chars"], 250)

    def test_retry_waits_for_a_new_assistant_completion(self) -> None:
        state = LiveCotState()
        state.start()
        state.begin_assistant("claude-opus-5")
        state.complete_assistant()
        state.next_stop_snapshot("session", timeout=0.01)

        result = {}

        def wait_for_retry() -> None:
            result.update(state.next_stop_snapshot("session", timeout=1))

        thread = threading.Thread(target=wait_for_retry)
        thread.start()
        state.begin_assistant("claude-opus-5")
        state.observe_thinking("x" * 220)
        state.complete_assistant()
        thread.join(timeout=2)
        self.assertTrue(result["ready"])
        self.assertEqual(result["thinking_chars"], 220)


class StateHandler(BaseHTTPRequestHandler):
    state = {}

    def do_GET(self) -> None:  # noqa: N802
        body = json.dumps(type(self).state).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_: object) -> None:
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
        ready: bool = True,
        allowlist: str | None = None,
        prompts: dict[str, str] | None = None,
        live: bool = True,
    ) -> dict | None:
        StateHandler.state = {
            "session_id": self.session_id,
            "turn_id": turn_id,
            "serving_model": model,
            "thinking_chars": thinking_chars,
            "active": True,
            "ready": ready,
        }
        env = os.environ.copy()
        env.update(
            {
                "COT_GUARD_CACHE_DIR": str(Path(self.tempdir.name) / "cache"),
                "COT_GUARD_MIN_CHARS": "200",
                "COT_GUARD_MAX_BLOCKS": "2",
            }
        )
        if allowlist is None:
            env.pop("COT_GUARD_ALLOWLIST", None)
        else:
            env["COT_GUARD_ALLOWLIST"] = allowlist
        for name in ("COT_GUARD_ZERO_PROMPT", "COT_GUARD_THIN_PROMPT"):
            env.pop(name, None)
        env.update(prompts or {})
        if live:
            env["COT_GUARD_STATE_URL"] = (
                f"http://127.0.0.1:{self.server.server_port}/cot-state/{{session_id}}"
            )
        else:
            env.pop("COT_GUARD_STATE_URL", None)
        result = subprocess.run(
            [sys.executable, str(HOOK)],
            input=json.dumps(
                {
                    "session_id": self.session_id,
                    "transcript_path": str(Path(self.tempdir.name) / "ignored.jsonl"),
                }
            ),
            text=True,
            capture_output=True,
            env=env,
            check=True,
        )
        return json.loads(result.stdout) if result.stdout else None

    def test_default_allowlist(self) -> None:
        for index, prefix in enumerate(DEFAULT_ALLOW_PREFIXES):
            with self.subTest(prefix=prefix):
                self.assertIsNone(
                    self.run_hook(
                        model=f"{prefix}-example",
                        thinking_chars=0,
                        turn_id=f"allow-{index}",
                    )
                )

    def test_opus_5_uses_current_turn_threshold(self) -> None:
        zero = self.run_hook(
            model="claude-opus-5", thinking_chars=0, turn_id="zero"
        )
        thin = self.run_hook(
            model="claude-opus-5", thinking_chars=199, turn_id="thin"
        )
        self.assertEqual(zero["decision"], "block")
        self.assertIn("条件反射", zero["reason"])
        self.assertEqual(thin["decision"], "block")
        self.assertIn("199", thin["reason"])
        self.assertIn("被压扁了", thin["reason"])
        self.assertIsNone(
            self.run_hook(
                model="claude-opus-5", thinking_chars=200, turn_id="healthy"
            )
        )

    def test_unknown_or_incomplete_state_is_blocked(self) -> None:
        self.assertEqual(
            self.run_hook(model="", thinking_chars=0, turn_id="unknown")["decision"],
            "block",
        )
        self.assertEqual(
            self.run_hook(
                model="claude-opus-5",
                thinking_chars=300,
                turn_id="incomplete",
                ready=False,
            )["decision"],
            "block",
        )

    def test_allowlist_can_be_replaced_with_an_empty_list(self) -> None:
        result = self.run_hook(
            model="claude-sonnet-example",
            thinking_chars=0,
            turn_id="empty-list",
            allowlist="",
        )
        self.assertEqual(result["decision"], "block")

    def test_personalized_retry_prompts_can_be_replaced(self) -> None:
        zero = self.run_hook(
            model="claude-opus-5",
            thinking_chars=0,
            turn_id="custom-zero",
            prompts={"COT_GUARD_ZERO_PROMPT": "先别急着回答，认真想想她刚才说的话。"},
        )
        thin = self.run_hook(
            model="claude-opus-5",
            thinking_chars=123,
            turn_id="custom-thin",
            prompts={
                "COT_GUARD_THIN_PROMPT": "这次只有 {cur}/{min} 字，请按我们的约定重想。"
            },
        )
        self.assertEqual(zero["reason"], "先别急着回答，认真想想她刚才说的话。")
        self.assertEqual(thin["reason"], "这次只有 123/200 字，请按我们的约定重想。")

    def test_retry_is_checked_twice_then_capped(self) -> None:
        decisions = [
            self.run_hook(model="claude-opus-5", thinking_chars=0, turn_id="retry")
            for _ in range(3)
        ]
        self.assertEqual(decisions[0]["decision"], "block")
        self.assertEqual(decisions[1]["decision"], "block")
        self.assertIsNone(decisions[2])

    def test_transcript_is_never_used_as_a_delayed_fallback(self) -> None:
        transcript = Path(self.tempdir.name) / "ignored.jsonl"
        transcript.write_text("private transcript content", encoding="utf-8")
        self.assertIsNone(
            self.run_hook(
                model="claude-opus-5",
                thinking_chars=0,
                turn_id="no-live-state",
                live=False,
            )
        )


class WrapperTests(unittest.TestCase):
    def test_end_to_end_blocks_zero_then_accepts_current_retry(self) -> None:
        env = os.environ.copy()
        env["COT_GUARD_ZERO_PROMPT"] = "自定义的当轮人物化提醒"
        env["EXPECT_ZERO_PROMPT"] = env["COT_GUARD_ZERO_PROMPT"]
        result = subprocess.run(
            [
                sys.executable,
                str(WRAPPER),
                "--guard-claude-bin",
                str(FAKE_CLAUDE),
                "--model",
                "claude-opus-5",
                "test prompt",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            env=env,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "first answersecond answer")

    def test_stream_flags_are_owned_by_the_wrapper(self) -> None:
        result = subprocess.run(
            [sys.executable, str(WRAPPER), "--print", "test prompt"],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("managed by the guard", result.stderr)


if __name__ == "__main__":
    unittest.main()
