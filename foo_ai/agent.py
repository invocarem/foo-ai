"""The step loop: fit the transcript, stream one completion, run tool calls in order."""

from __future__ import annotations

from foo_ai.client import ChatError
from foo_ai.context import Context
from foo_ai.skill import render_inline, run_subagent
from foo_ai.tools import ASK_DISMISSED, ASK_NO_USER, ASK_SKIPPED
from foo_ai.usage import format_usage_line, parse_usage

_STOP = "Stopped: tool-call limit reached before a final answer."
_NUDGE_LIMIT = 2


def todo_nudge(items: list[dict]) -> str:
    lines = "\n".join(f"- [{item['status']}] {item['content']}" for item in items)
    return (
        "[todo] Your task list still has open items. Continue the work, update the list with todo_write, "
        "and do not give the final answer until every item is completed or you have removed it. "
        "If an open item is a decision only the user can make, call ask instead of assuming an answer.\n"
        + lines
    )


class Agent:
    def __init__(self, provider, session, toolbox, client, sink, max_steps: int):
        self.provider = provider
        self.session = session
        self.toolbox = toolbox
        self.client = client
        self.sink = sink
        self.max_steps = max_steps
        self.context = Context()
        self._todo_nudges = 0
        self._asked = False
        self.toolbox.skill_runner = self._execute_skill

    def reset(self) -> str:
        self.context.ratio = 0.25
        self._begin_request()
        note = self.session.reset()
        self.toolbox.decisions.clear()
        return note

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
        self._begin_request()
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
        self._begin_request()
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
                if self._continue_for_todos():
                    continue
                return completion.content
            dismissed = False
            for call in completion.tool_calls:
                name = call["function"]["name"]
                if dismissed:
                    self._record_tool(call["id"], name, ASK_SKIPPED)
                    continue
                try:
                    result = self.toolbox.run(name, call["function"]["arguments"])
                except KeyboardInterrupt:
                    raise
                self._record_tool(call["id"], name, result)
                if name == "todo_write" and not result.startswith("error:"):
                    self._publish_todos()
                if name == "ask" and result not in {ASK_DISMISSED, ASK_NO_USER} and not result.startswith("error:"):
                    self._asked = True
                if result == ASK_DISMISSED:
                    dismissed = True
            if dismissed:
                return ASK_DISMISSED
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

    def _record_tool(self, call_id: str, name: str, result: str) -> None:
        self.session.add_tool(call_id, name, result)
        self.session.save()
        if self.sink is not None:
            self.sink.tool_result(name, result)

    def _begin_request(self) -> None:
        self._todo_nudges = 0
        self._asked = False
        had = bool(self.toolbox.todos)
        self.toolbox.clear_todos()
        if had:
            self._publish_todos()

    def _publish_todos(self) -> None:
        if self.sink is None or not hasattr(self.sink, "todos"):
            return
        self.sink.todos([dict(item) for item in self.toolbox.todos])

    def _continue_for_todos(self) -> bool:
        if self._asked:
            return False
        open_items = [item for item in self.toolbox.todos if item["status"] != "completed"]
        if not open_items or self._todo_nudges >= _NUDGE_LIMIT:
            return False
        self._todo_nudges += 1
        self.session.add_user(todo_nudge(open_items))
        self.session.save()
        return True

    def _report_usage(self, raw: object) -> None:
        if not isinstance(raw, dict) or self.sink is None:
            return
        line = format_usage_line(parse_usage(raw), self.provider.context_window)
        if line:
            self.sink.status("\n" + line + "\n")
