"""Tool loop: sample the model, run calls, and append paired tool results."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from foo_ai.client import Client, assistant_message
from foo_ai.prompt import SYSTEM_PROMPT
from foo_ai.tools import run_tool, tool_schemas

EventCallback = Callable[[dict], None]


class Agent:
    def __init__(self, client: Client, workspace: Path, max_steps: int = 16):
        self.client = client
        self.workspace = workspace
        self.max_steps = max_steps
        self.messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]

    def reset(self) -> None:
        self.messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    def ask(self, user_text: str, on_event: EventCallback | None = None) -> str:
        text = user_text.strip()
        if not text:
            return ""
        self.messages.append({"role": "user", "content": text})
        tools = tool_schemas()
        final = ""
        for _ in range(self.max_steps):
            message = self._sample(tools, on_event)
            self.messages.append(assistant_message(message))
            calls = message.get("tool_calls") or []
            if not calls:
                final = message.get("content") or ""
                return final
            for call in calls:
                function = call.get("function") or {}
                name = function.get("name") or ""
                result = run_tool(name, function.get("arguments") or "{}", self.workspace)
                self.messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.get("id") or "",
                        "name": name,
                        "content": result,
                    }
                )
                if on_event:
                    on_event({"type": "tool_result", "name": name, "content": result})
        notice = "Stopped: tool-call limit reached before a final answer."
        if on_event:
            on_event({"type": "content", "text": notice})
        return notice

    def _sample(self, tools: list[dict], on_event: EventCallback | None) -> dict:
        message = None
        for event in self.client.stream(self.messages, tools):
            if event["type"] == "message":
                message = event["message"]
            elif on_event:
                on_event(event)
        if message is None:
            raise ChatError("server closed the stream without a completed response")
        return message
