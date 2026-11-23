"""Tool loop: sample the model, run calls, and append paired tool results."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from foo_ai.client import ChatError, Client, assistant_message
from foo_ai.context import (
    DEFAULT_TOKENS_PER_CHAR,
    SUMMARY_PROMPT,
    accept_token_ratio,
    discard_to_trigger,
    estimate_tokens,
    message_size,
    prune_tool_results,
    summary_prefix,
    transcript_for_summary,
    trigger_tokens,
    with_summary,
)
from foo_ai.mcp import McpHost
from foo_ai.prompt import SYSTEM_PROMPT
from foo_ai.session import archive_session, close_open_tool_calls, load_session, save_session
from foo_ai.tools import ApproveShell, run_tool, tool_schemas
from foo_ai.usage import DEFAULT_CONTEXT_WINDOW, format_usage_line

EventCallback = Callable[[dict], None]


class Agent:
    def __init__(
        self,
        client: Client,
        workspace: Path,
        max_steps: int = 16,
        confirm_shell: ApproveShell | None = None,
        mcp: McpHost | None = None,
    ):
        self.client = client
        self.workspace = workspace
        self.max_steps = max_steps
        self.confirm_shell = confirm_shell
        self.mcp = mcp
        provider = getattr(client, "provider", None)
        self.context_window = getattr(provider, "context_window", DEFAULT_CONTEXT_WINDOW)
        self.tokens_per_char = DEFAULT_TOKENS_PER_CHAR
        self._outbound_chars = 0
        self.messages, self.session_note = load_session(workspace, SYSTEM_PROMPT)

    def reset(self) -> str:
        archived = archive_session(self.workspace)
        self.messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        self.tokens_per_char = DEFAULT_TOKENS_PER_CHAR
        self.session_note = ""
        if archived is None:
            return "Conversation cleared."
        relative = archived.relative_to(self.workspace).as_posix()
        return f"Conversation cleared. Archived {relative}."

    def ask(self, user_text: str, on_event: EventCallback | None = None) -> str:
        text = user_text.strip()
        if not text:
            return ""
        self._repair_transcript()
        self.messages.append({"role": "user", "content": text})
        self._save()
        tools = self._tool_schemas()
        for _ in range(self.max_steps):
            message = self._sample(tools, on_event)
            self.messages.append(assistant_message(message))
            self._save()
            calls = message.get("tool_calls") or []
            if not calls:
                return message.get("content") or ""
            for call in calls:
                function = call.get("function") or {}
                name = function.get("name") or ""
                result = self._call_tool(name, function.get("arguments") or "{}")
                self.messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.get("id") or "",
                        "name": name,
                        "content": result,
                    }
                )
                self._save()
                if on_event:
                    on_event({"type": "tool_result", "name": name, "content": result})
        notice = "Stopped: tool-call limit reached before a final answer."
        if on_event:
            on_event({"type": "content", "text": notice})
        return notice

    def _tool_schemas(self) -> list[dict]:
        tools = tool_schemas()
        if self.mcp is not None:
            tools.extend(self.mcp.schemas())
        return tools

    def _call_tool(self, name: str, arguments: str | dict) -> str:
        if self.mcp is not None and self.mcp.owns(name):
            return self.mcp.call(name, arguments)
        return run_tool(name, arguments, self.workspace, approve_shell=self.confirm_shell)

    def _sample(self, tools: list[dict], on_event: EventCallback | None) -> dict:
        outbound = self._fit_context(on_event)
        self._outbound_chars = message_size(outbound)
        return self._consume(self.client.stream(outbound, tools), on_event, report_usage=True)

    def _fit_context(self, on_event: EventCallback | None) -> list[dict]:
        window = self.context_window
        ratio = self.tokens_per_char
        if trigger_tokens(window) <= 0 or estimate_tokens(self.messages, tokens_per_char=ratio) < trigger_tokens(window):
            return self.messages
        compressed = prune_tool_results(self.messages)
        if compressed is not self.messages:
            self.messages = compressed
            self._save()
            self._status(on_event, "Compressed old tool results.")
        if estimate_tokens(self.messages, tokens_per_char=ratio) < trigger_tokens(window):
            return self.messages
        prefix = summary_prefix(self.messages, context_window=window, tokens_per_char=ratio)
        if prefix is not None:
            self._status(on_event, "Summarizing earlier turns.")
            try:
                summary = self._summarize(prefix)
            except ChatError as exc:
                self._status(on_event, f"Summary skipped: {exc}")
            else:
                self.messages = with_summary(self.messages, summary, prefix)
                self._save()
        discarded = discard_to_trigger(self.messages, context_window=window, tokens_per_char=ratio)
        if discarded is not self.messages:
            self.messages = discarded
            self._save()
            self._status(on_event, "Discarded earlier turns.")
        return self.messages

    def _status(self, on_event: EventCallback | None, text: str) -> None:
        if on_event:
            on_event({"type": "status", "text": text})

    def _summarize(self, prefix: list[dict]) -> str:
        request = [
            {"role": "system", "content": SUMMARY_PROMPT},
            {"role": "user", "content": transcript_for_summary(prefix)},
        ]
        message = self._consume(self.client.stream(request, None), None, report_usage=False)
        return message.get("content") or ""

    def _consume(self, events, on_event: EventCallback | None, *, report_usage: bool) -> dict:
        message = None
        usage = None
        for event in events:
            if event["type"] == "message":
                message = event["message"]
            elif event["type"] == "usage":
                usage = event["usage"]
            elif on_event:
                on_event(event)
        if message is None:
            raise ChatError("server closed the stream without a completed response")
        if report_usage and usage:
            learned = accept_token_ratio(usage.get("prompt_tokens", 0), self._outbound_chars)
            if learned is not None:
                self.tokens_per_char = learned
            if on_event:
                on_event({"type": "usage", "text": format_usage_line(usage, self.context_window)})
        return message

    def _repair_transcript(self) -> None:
        repaired = close_open_tool_calls(self.messages)
        if repaired != self.messages:
            self.messages = repaired
            self._save()

    def _save(self) -> None:
        save_session(self.workspace, self.messages)
