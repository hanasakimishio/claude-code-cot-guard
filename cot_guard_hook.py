#!/usr/bin/env python3
"""Stop hook for enforcing a minimum current-turn thinking length."""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path


DEFAULT_ALLOW_PREFIXES = (
    "claude-opus-4-5",
    "claude-opus-4-6",
    "claude-opus-4-7",
    "claude-opus-4-8",
    "claude-sonnet-",
    "claude-haiku-",
    "claude-fable-5",
)
DEFAULT_ALLOWLIST = ",".join(DEFAULT_ALLOW_PREFIXES)


def env_int(name: str, default: int, minimum: int = 0) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except ValueError:
        return default


def allow_prefixes() -> tuple[str, ...]:
    raw = os.getenv("COT_GUARD_ALLOWLIST")
    values = DEFAULT_ALLOW_PREFIXES if raw is None else raw.split(",")
    return tuple(value.strip().lower() for value in values if value.strip())


MIN_THINKING_CHARS = env_int("COT_GUARD_MIN_CHARS", 200)
MAX_BLOCKS = env_int("COT_GUARD_MAX_BLOCKS", 2, 1)
ALLOW_PREFIXES = allow_prefixes()
CACHE_DIR = Path(
    os.path.expanduser(os.getenv("COT_GUARD_CACHE_DIR", "~/.claude/cache"))
)
LEDGER_PATH = CACHE_DIR / "cot-guard-state.json"
LOG_PATH = CACHE_DIR / "cot-guard.log"


def model_is_allowlisted(model: str) -> bool:
    normalized = (model or "").strip().lower()
    return any(normalized.startswith(prefix) for prefix in ALLOW_PREFIXES)


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
                "last": str(data.get("last") or ""),
                "count": int(data.get("count") or 0),
            }
    except (OSError, ValueError, TypeError):
        pass
    return {"last": "", "count": 0}


def save_ledger(ledger: dict) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        LEDGER_PATH.write_text(json.dumps(ledger), encoding="utf-8")
    except OSError:
        pass


def block_reason(thinking_chars: int, ready: bool) -> str:
    if not ready:
        return (
            "Current-turn thinking telemetry was incomplete. "
            "Re-evaluate the request before answering again."
        )
    if thinking_chars == 0:
        return (
            "No thinking content was observed for this turn. "
            "Re-evaluate the request carefully before answering again."
        )
    return (
        f"Only {thinking_chars} thinking characters were observed for this turn. "
        "Re-evaluate the request more carefully before answering again."
    )


def block_once(key: str, thinking_chars: int, ready: bool, ledger: dict) -> None:
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
        ready=ready,
        attempt=ledger["count"],
    )
    json.dump(
        {"decision": "block", "reason": block_reason(thinking_chars, ready)},
        sys.stdout,
    )


def read_live_state(session_id: str) -> dict | None:
    template = os.getenv("COT_GUARD_STATE_URL", "").strip()
    if not template or not session_id:
        return None
    try:
        url = template.format(session_id=urllib.parse.quote(session_id, safe=""))
        parsed = urllib.parse.urlparse(url)
    except (KeyError, ValueError):
        return None
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1"}:
        trace("state_url_rejected")
        return None

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=4) as response:
            data = json.load(response)
        if not isinstance(data, dict) or data.get("session_id") != session_id:
            return None
        thinking_chars = max(0, int(data.get("thinking_chars") or 0))
    except (OSError, ValueError, TypeError, AttributeError):
        return None
    if not data.get("active") or not data.get("turn_id"):
        return None
    return {
        "turn_id": str(data["turn_id"]),
        "serving_model": str(data.get("serving_model") or ""),
        "thinking_chars": thinking_chars,
        "ready": bool(data.get("ready")),
    }


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (ValueError, TypeError):
        return

    session_id = str(payload.get("session_id") or "")
    live = read_live_state(session_id)
    if live is None:
        trace("live_state_unavailable")
        return

    model = live["serving_model"]
    thinking_chars = live["thinking_chars"]
    ready = live["ready"]
    if model_is_allowlisted(model) or (ready and thinking_chars >= MIN_THINKING_CHARS):
        trace(
            "live_check_passed",
            model=model,
            thinking_chars=thinking_chars,
            ready=ready,
        )
        return
    block_once(
        f"live:{session_id}:{live['turn_id']}",
        thinking_chars,
        ready,
        load_ledger(),
    )


if __name__ == "__main__":
    main()
