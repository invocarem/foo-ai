"""The step loop: fit the transcript, stream one completion, run tool calls in order."""

from __future__ import annotations

from foo_ai.context import Context
from foo_ai.usage import format_usage_line, parse_usage

_STOP = "Stopped: tool-call limit reached before a final answer."


class Agent:
    def __init__(self, provider, session, toolbox, client, sink, max_steps: int):
        self.provider = provider
        self.session = session
        self.toolbox = toolbox
        self.client = client
        self.sink = sink
        self.max_steps = max_steps
        self.context = Context()

    def reset(self) -> str:
        self.context.ratio = 0.25
        return self.session.reset()

    def ask(self, text: str) -> str:
        text = text.strip()
        if not text:
            return ""
        if self.session.repair():
            self.session.save()
        self.session.add_user(text)
        self.session.save()
        schemas = self.toolbox.schemas()
        for _ in range(self.max_steps):
            self.context.fit(
                self.session,
                self.provider.context_window,
                self.client,
                self.sink,
            )
            sent = list(self.session.messages)
            completion = self.client.complete(self.session.messages, schemas, self.sink)
            self.context.observe(sent, completion.usage)
            self._report_usage(completion.usage)
            self.session.add_assistant(
                completion.content,
                completion.reasoning_content,
                completion.tool_calls,
            )
            self.session.save()
            if not completion.tool_calls:
                return completion.content
            for call in completion.tool_calls:
                name = call["function"]["name"]
                try:
                    result = self.toolbox.run(name, call["function"]["arguments"])
                except KeyboardInterrupt:
                    raise
                self.session.add_tool(call["id"], name, result)
                self.session.save()
                if self.sink is not None:
                    self.sink.tool_result(name, result)
        if self.sink is not None:
            self.sink.content(_STOP)
        return _STOP

    def _report_usage(self, raw: object) -> None:
        if not isinstance(raw, dict) or self.sink is None:
            return
        line = format_usage_line(parse_usage(raw), self.provider.context_window)
        if line:
            self.sink.status("\n" + line + "\n")
