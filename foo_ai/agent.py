"""The step loop: fit the transcript, stream one completion, run tool calls in order."""

from __future__ import annotations

from foo_ai.client import ChatError
from foo_ai.context import Context
from foo_ai.skill import render_inline, run_subagent
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
        self.toolbox.skill_runner = self._execute_skill

    def reset(self) -> str:
        self.context.ratio = 0.25
        return self.session.reset()

    def run_slash(self, command: str, name: str, arguments: str) -> str | None:
        skill = self.toolbox.skills.get(name)
        if skill is None:
            return None
        if skill.run == "inline":
            return self.ask(render_inline(skill.body, arguments))
        if arguments == "":
            arguments = skill.task
        if arguments == "":
            if self.sink is not None:
                self.sink.status(f"error: arguments are required for subagent skill {skill.name!r}\n")
            return None
        if self.session.repair():
            self.session.save()
        self.session.add_user(command)
        self.session.save()
        result = run_subagent(
            skill,
            arguments,
            provider=self.provider,
            client=self.client,
            sink=self.sink,
            workspace=self.toolbox.workspace,
            confirm=self.toolbox.confirm,
            max_steps=self.max_steps,
        )
        self.session.add_assistant(result, "", [])
        self.session.save()
        if self.sink is not None:
            self.sink.content(result)
        return result

    def _execute_skill(self, skill, arguments: str) -> str:
        if skill.run == "inline":
            return render_inline(skill.body, arguments)
        try:
            return run_subagent(
                skill,
                arguments,
                provider=self.provider,
                client=self.client,
                sink=self.sink,
                workspace=self.toolbox.workspace,
                confirm=self.toolbox.confirm,
                max_steps=self.max_steps,
            )
        except ChatError as exc:
            return f"error: {exc}"

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
        if self.max_steps > 0:
            return self._closing_answer()
        if self.sink is not None:
            self.sink.content(_STOP)
        return _STOP

    def _closing_answer(self) -> str:
        self.context.fit(
            self.session,
            self.provider.context_window,
            self.client,
            self.sink,
        )
        sent = list(self.session.messages)
        completion = self.client.complete(self.session.messages, None, self.sink)
        self.context.observe(sent, completion.usage)
        self._report_usage(completion.usage)
        if completion.tool_calls and not completion.content.strip():
            if self.sink is not None:
                self.sink.content(_STOP)
            return _STOP
        self.session.add_assistant(completion.content, completion.reasoning_content, [])
        self.session.save()
        return completion.content

    def _report_usage(self, raw: object) -> None:
        if not isinstance(raw, dict) or self.sink is None:
            return
        line = format_usage_line(parse_usage(raw), self.provider.context_window)
        if line:
            self.sink.status("\n" + line + "\n")
