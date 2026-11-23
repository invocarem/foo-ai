"""Workspace session transcript at .foo-ai/session.jsonl."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

SESSION_DIR = ".foo-ai"
SESSION_NAME = "session.jsonl"


def session_path(workspace: Path) -> Path:
    return workspace / SESSION_DIR / SESSION_NAME


def load_session(workspace: Path, system_prompt: str) -> tuple[list[dict], str]:
    """Return messages and a note for the user. A broken file is renamed aside."""
    fresh = [{"role": "system", "content": system_prompt}]
    path = session_path(workspace)
    if not path.is_file():
        return fresh, ""
    try:
        messages = _read_jsonl(path)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        broken = path.with_name("session.jsonl.broken")
        if broken.exists():
            broken.unlink()
        path.replace(broken)
        return fresh, "Session file was unreadable and was renamed to .foo-ai/session.jsonl.broken."
    if not messages:
        path.unlink()
        return fresh, ""
    if messages[0].get("role") != "system":
        messages.insert(0, fresh[0])
    else:
        messages[0] = dict(fresh[0])
    messages = close_open_tool_calls(messages)
    count = len(messages) - 1
    return messages, f"Resumed .foo-ai/session.jsonl ({count} messages)."


def save_session(workspace: Path, messages: list[dict]) -> None:
    path = session_path(workspace)
    if len(messages) <= 1:
        if path.is_file():
            path.unlink()
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = "\n".join(json.dumps(message, ensure_ascii=False) for message in messages) + "\n"
    temporary = path.with_suffix(".jsonl.tmp")
    temporary.write_text(payload, encoding="utf-8", newline="\n")
    temporary.replace(path)


def archive_session(workspace: Path) -> Path | None:
    """Move the active session aside. Returns the archive path, if there was one."""
    path = session_path(workspace)
    if not path.is_file():
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    destination = path.with_name(f"session-{stamp}.jsonl")
    counter = 2
    while destination.exists():
        destination = path.with_name(f"session-{stamp}-{counter}.jsonl")
        counter += 1
    path.replace(destination)
    return destination


def close_open_tool_calls(messages: list[dict]) -> list[dict]:
    """Append a result for every tool call that was saved without one.

    A cancelled shell prompt or a crash can leave an assistant tool call at the
    end of the file. The next request has to pair it before a new user turn.
    """
    repaired: list[dict] = []
    pending: list[str] = []
    for message in messages:
        role = message.get("role")
        if pending and role != "tool":
            repaired.extend(_interrupted(pending))
            pending = []
        repaired.append(message)
        if role == "assistant":
            pending = [str(call.get("id") or "") for call in message.get("tool_calls") or []]
        elif role == "tool" and pending:
            call_id = str(message.get("tool_call_id") or "")
            if call_id in pending:
                pending.remove(call_id)
            else:
                pending.pop(0)
    if pending:
        repaired.extend(_interrupted(pending))
    return repaired


def _interrupted(call_ids: list[str]) -> list[dict]:
    return [
        {
            "role": "tool",
            "tool_call_id": call_id,
            "name": "",
            "content": "error: interrupted before the tool result was saved",
        }
        for call_id in call_ids
    ]


def _read_jsonl(path: Path) -> list[dict]:
    messages = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        item = json.loads(line)
        if not isinstance(item, dict) or not isinstance(item.get("role"), str):
            raise ValueError(f"line {line_number} is not a message")
        messages.append(item)
    return messages
