#!/usr/bin/env python3
"""Claude Code Stop hook that blocks zero or unusually thin thinking.

Current-turn checks use an optional loopback state provider. Without one, the
hook falls back to completed turns in Claude Code's local JSONL transcript.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path


def env_int(name: str, default: int, minimum: int = 0) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except ValueError:
        return default


MIN_THINKING_CHARS = env_int("COT_GUARD_MIN_CHARS", 200)
MAX_BLOCKS = env_int("COT_GUARD_MAX_BLOCKS", 2, 1)
ALLOW_PREFIXES = tuple(
    value.strip().lower()
    for value in os.getenv("COT_GUARD_ALLOWLIST", "").split(",")
    if value.strip()
)
MAX_TAIL_BYTES = 512 * 1024
MAX_CHECKED_TURNS = 200

CACHE_DIR = Path(
    os.path.expanduser(os.getenv("COT_GUARD_CACHE_DIR", "~/.claude/cache"))
)
LEDGER_PATH = CACHE_DIR / "cot-guard-state.json"
LOG_PATH = CACHE_DIR / "cot-guard.log"


def model_is_allowlisted(model: str) -> bool:
    normalized = (model or "").strip().lower()
    return any(normalized.startswith(prefix) for prefix in ALLOW_PREFIXES)


def block_reason(thinking_chars: int) -> str:
    if thinking_chars == 0:
        return (
            "No thinking content was observed for this turn. "
            "Re-evaluate the request carefully before answering again."
        )
    return (
        f"Only {thinking_chars} thinking characters were observed for this turn. "
        "Re-evaluate the request more carefully before answering again."
    )


def trace(event: str, **fields: object) -> None:
    record = {
        "at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "event": event,
        **fields,
    }
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


def load_ledger() -> dict:
    try:
        data = json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {
                "checked": list(data.get("checked") or []),
                "last": str(data.get("last") or ""),
                "count": int(data.get("count") or 0),
            }
    except (OSError, ValueError, TypeError):
        pass
    return {"checked": [], "last": "", "count": 0}


def save_ledger(ledger: dict) -> None:
    ledger["checked"] = ledger["checked"][-MAX_CHECKED_TURNS:]
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        LEDGER_PATH.write_text(json.dumps(ledger), encoding="utf-8")
    except OSError:
        pass


def emit_block(reason: str) -> None:
    json.dump({"decision": "block", "reason": reason}, sys.stdout)


def block_once(key: str, thinking_chars: int, ledger: dict) -> None:
    if ledger["last"] == key and ledger["count"] >= MAX_BLOCKS:
        trace("block_cap_reached", turn=key, thinking_chars=thinking_chars)
        return
    ledger["count"] = ledger["count"] + 1 if ledger["last"] == key else 1
    ledger["last"] = key
    save_ledger(ledger)
    trace(
        "blocked",
        turn=key,
        thinking_chars=thinking_chars,
        attempt=ledger["count"],
    )
    emit_block(block_reason(thinking_chars))


def read_live_state(session_id: str) -> dict | None:
    template = os.getenv("COT_GUARD_STATE_URL", "").strip()
    if not template or not session_id:
        return None
    try:
        url = template.format(
            session_id=urllib.parse.quote(session_id, safe="")
        )
        parsed = urllib.parse.urlparse(url)
    except (KeyError, ValueError):
        return None
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1"}:
        trace("state_url_rejected")
        return None

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=0.75) as response:
            data = json.load(response)
        thinking_chars = max(0, int(data.get("thinking_chars") or 0))
    except (OSError, ValueError, AttributeError):
        return None
    if not isinstance(data, dict) or data.get("session_id") != session_id:
        return None
    if not data.get("active") or not data.get("turn_id"):
        return None
    return {
        "session_id": session_id,
        "turn_id": str(data["turn_id"]),
        "serving_model": str(data.get("serving_model") or ""),
        "thinking_chars": thinking_chars,
    }


def tail_jsonl(path: Path):
    with path.open("rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        fh.seek(max(0, size - MAX_TAIL_BYTES))
        chunk = fh.read()
    if size > MAX_TAIL_BYTES:
        chunk = chunk.split(b"\n", 1)[-1]
    for raw in chunk.splitlines():
        try:
            yield json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            continue


def human_turn_heads(entries: list[dict]) -> list[int]:
    heads = []
    for index, entry in enumerate(entries):
        if entry.get("type") != "user" or entry.get("isMeta"):
            continue
        content = (entry.get("message") or {}).get("content")
        is_human = isinstance(content, str) or (
            isinstance(content, list)
            and not any(
                isinstance(block, dict) and block.get("type") == "tool_result"
                for block in content
            )
        )
        if is_human:
            heads.append(index)
    return heads


def completed_turns(entries: list[dict]) -> list[tuple[int, int, str]]:
    heads = human_turn_heads(entries)
    turns = []
    for index, start in enumerate(heads):
        end = heads[index + 1] if index + 1 < len(heads) else len(entries)
        if any(row.get("type") == "assistant" for row in entries[start + 1 : end]):
            turns.append((start, end, entries[start].get("uuid") or str(start)))
    return turns


def inspect_turn(entries: list[dict], start: int, end: int) -> tuple[str, int]:
    model = ""
    thinking_chars = 0
    for entry in entries[start + 1 : end]:
        if entry.get("type") != "assistant":
            continue
        message = entry.get("message") or {}
        model = message.get("model") or model
        for block in message.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "thinking":
                thinking_chars += len(block.get("thinking") or "")
    return model, thinking_chars


def inspect_transcript(path: Path, ledger: dict) -> None:
    try:
        entries = list(tail_jsonl(path))
    except OSError:
        trace("transcript_read_failed")
        return

    turns = completed_turns(entries)
    checked = set(ledger["checked"])
    pending = [turn for turn in turns if turn[2] not in checked]
    if not pending:
        trace("no_unchecked_transcript_turn")
        return

    finding = None
    for start, end, key in pending:
        model, thinking_chars = inspect_turn(entries, start, end)
        if not model_is_allowlisted(model) and thinking_chars < MIN_THINKING_CHARS:
            finding = (key, thinking_chars)
    ledger["checked"].extend(turn[2] for turn in pending)
    if finding is None:
        save_ledger(ledger)
        return
    block_once(f"transcript:{finding[0]}", finding[1], ledger)


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (ValueError, TypeError):
        return

    ledger = load_ledger()
    session_id = str(payload.get("session_id") or "")
    live = read_live_state(session_id)
    if live is not None:
        model = live["serving_model"]
        thinking_chars = live["thinking_chars"]
        if model_is_allowlisted(model) or thinking_chars >= MIN_THINKING_CHARS:
            trace("live_check_passed", model=model, thinking_chars=thinking_chars)
            return
        block_once(
            f"live:{session_id}:{live['turn_id']}",
            thinking_chars,
            ledger,
        )
        return

    trace("live_state_unavailable")
    transcript = payload.get("transcript_path")
    if transcript:
        path = Path(os.path.expanduser(str(transcript)))
        if path.is_file():
            inspect_transcript(path, ledger)


if __name__ == "__main__":
    main()
