"""Transcript file at .foo-ai/session.jsonl."""

from __future__ import annotations

import json
import time
from pathlib import Path

from foo_ai.prompt import SYSTEM_PROMPT

_INTERRUPTED = "error: interrupted before the tool result was saved"


def system_message() -> dict:
    return {"role": "system", "content": SYSTEM_PROMPT}


def repair_messages(messages: list[dict]) -> bool:
    """Insert results for tool calls that never received one. Returns True when the list changes."""
    result: list[dict] = []
    pending: list[str] = []
    changed = False

    def flush() -> None:
        nonlocal changed
        if not pending:
            return
        for call_id in pending:
            result.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "name": "",
                    "content": _INTERRUPTED,
                }
            )
        pending.clear()
        changed = True

    for message in messages:
        role = message.get("role")
        if role == "tool":
            call_id = message.get("tool_call_id")
            if call_id in pending:
                pending.remove(call_id)
            elif pending:
                pending.pop(0)
            result.append(message)
            continue
        flush()
        result.append(message)
        if role == "assistant" and message.get("tool_calls"):
            pending = []
            for call in message["tool_calls"]:
                if isinstance(call, dict):
                    call_id = call.get("id")
                    pending.append(call_id if isinstance(call_id, str) else "")
                else:
                    pending.append("")
    flush()
    messages.clear()
    messages.extend(result)
    return changed


class Session:
    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.directory = workspace / ".foo-ai"
        self.path = self.directory / "session.jsonl"
        self.messages: list[dict] = [system_message()]
        self.note: str | None = None

    def load(self) -> str | None:
        self.messages = [system_message()]
        self.note = None
        if not self.path.exists():
            return None
        try:
            text = self.path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            self.note = self._mark_broken()
            return self.note
        if text.strip() == "":
            self.path.unlink()
            return None
        parsed: list[dict] = []
        for line in text.splitlines():
            if line.strip() == "":
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                self.note = self._mark_broken()
                return self.note
            if not isinstance(value, dict) or not isinstance(value.get("role"), str):
                self.note = self._mark_broken()
                return self.note
            parsed.append(value)
        if not parsed:
            self.path.unlink()
            return None
        if parsed[0].get("role") == "system":
            parsed[0] = system_message()
        else:
            parsed.insert(0, system_message())
        repair_messages(parsed)
        self.messages = parsed
        count = len(parsed) - 1
        self.note = f"Resumed .foo-ai/session.jsonl ({count} messages)."
        return self.note

    def _mark_broken(self) -> str:
        broken = self.directory / "session.jsonl.broken"
        if broken.exists():
            broken.unlink()
        self.path.replace(broken)
        self.messages = [system_message()]
        return "Session file was unreadable and was renamed to .foo-ai/session.jsonl.broken."

    def save(self) -> None:
        if len(self.messages) <= 1:
            if self.path.exists():
                self.path.unlink()
            return
        self.directory.mkdir(parents=True, exist_ok=True)
        temporary = self.directory / "session.jsonl.tmp"
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            for message in self.messages:
                handle.write(json.dumps(message, ensure_ascii=False))
                handle.write("\n")
        temporary.replace(self.path)

    def add_user(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})

    def add_assistant(self, content: str, reasoning: str, tool_calls: list[dict]) -> None:
        record: dict = {
            "role": "assistant",
            "content": content,
            "reasoning_content": reasoning,
        }
        if tool_calls:
            if content == "":
                record["content"] = None
            record["tool_calls"] = tool_calls
        self.messages.append(record)

    def add_tool(self, call_id: str, name: str, content: str) -> None:
        self.messages.append(
            {
                "role": "tool",
                "tool_call_id": call_id,
                "name": name,
                "content": content,
            }
        )

    def repair(self) -> bool:
        return repair_messages(self.messages)

    def reset(self) -> str:
        self.messages = [system_message()]
        if not self.path.exists():
            return "Conversation cleared."
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
        destination = self.directory / f"session-{stamp}.jsonl"
        suffix = 2
        while destination.exists():
            destination = self.directory / f"session-{stamp}-{suffix}.jsonl"
            suffix += 1
        self.path.replace(destination)
        relative = destination.relative_to(self.workspace).as_posix()
        return f"Conversation cleared. Archived {relative}."
