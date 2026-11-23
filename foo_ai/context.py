"""Compress, summarize, or discard a transcript that no longer fits."""

from __future__ import annotations

from foo_ai.client import ChatError
from foo_ai.prompt import SUMMARY_PROMPT
from foo_ai.usage import parse_usage

_PRUNED = "[... tool result middle pruned ...]"
_DISCARD = "Earlier turns were removed to fit the context window."


def transcript_chars(messages: list[dict]) -> int:
    total = 0
    for message in messages:
        for key in ("content", "reasoning_content", "name"):
            value = message.get(key)
            if isinstance(value, str):
                total += len(value)
        for call in message.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            function = call.get("function")
            if not isinstance(function, dict):
                continue
            name = function.get("name")
            arguments = function.get("arguments")
            if isinstance(name, str):
                total += len(name)
            if isinstance(arguments, str):
                total += len(arguments)
    return total


class Context:
    def __init__(self) -> None:
        self.ratio = 0.25

    def tokens(self, messages: list[dict]) -> int:
        return int(transcript_chars(messages) * self.ratio)

    def observe(self, messages: list[dict], raw_usage: object) -> None:
        if not isinstance(raw_usage, dict):
            return
        usage = parse_usage(raw_usage)
        chars = transcript_chars(messages)
        if chars <= 0 or usage.prompt <= 0:
            return
        ratio = usage.prompt / chars
        if 0.05 < ratio < 2:
            self.ratio = ratio

    def fit(self, session, window: int, client, sink) -> None:
        if window <= 0:
            return
        trigger = window * 80 // 100 or 1
        hard = max(1, window - 256)
        tail_budget = max(1, window * 16 // 100)
        if self.tokens(session.messages) < trigger:
            return
        if _compress(session.messages):
            session.save()
            _status(sink, "Compressed old tool results.")
        if self.tokens(session.messages) < trigger:
            return
        self._summarize(session, tail_budget, client, sink)
        self._discard(session, trigger, hard, sink)

    def _summarize(self, session, tail_budget: int, client, sink) -> None:
        prefix, tail = self._prefix_and_tail(session.messages, tail_budget)
        if not prefix or self.tokens(prefix) < 400:
            return
        _status(sink, "Summarizing earlier turns.")
        summary_messages = [
            {"role": "system", "content": SUMMARY_PROMPT},
            {"role": "user", "content": _summary_source(prefix)},
        ]
        try:
            reply = client.complete(summary_messages, None, None)
        except ChatError as exc:
            _status(sink, f"Summary skipped: {exc}")
            return
        body = (reply.content or "").strip()
        if not body:
            body = _DISCARD
        elif len(body) > 8000:
            body = body[:8000] + "\n…[summary truncated]"
        summary = {"role": "user", "content": "Earlier conversation, summarized:\n" + body}
        session.messages = [session.messages[0], summary, *tail]
        session.save()

    def _prefix_and_tail(self, messages: list[dict], tail_budget: int) -> tuple[list[dict], list[dict]]:
        last_user = None
        for index, message in enumerate(messages):
            if message.get("role") == "user":
                last_user = index
        if last_user is None:
            return [], []
        start = last_user
        while start > 0 and self.tokens(messages[start - 1 :]) <= tail_budget:
            start -= 1
        while start > 1 and messages[start].get("role") == "tool":
            start -= 1
        if start <= 0:
            return [], []
        return messages[1:start], messages[start:]

    def _discard(self, session, trigger: int, hard: int, sink) -> None:
        if self.tokens(session.messages) < hard:
            return
        messages = session.messages
        last_user = None
        for index, message in enumerate(messages):
            if message.get("role") == "user":
                last_user = index
        if last_user is None:
            return
        system = messages[0]
        suffix = messages[last_user:]
        if self.tokens([system, *suffix]) >= trigger:
            return
        groups = _groups(messages[1:last_user])
        dropped = False
        while groups and self.tokens(_assemble(system, groups, suffix, dropped)) >= trigger:
            groups.pop(0)
            dropped = True
        if not dropped:
            return
        session.messages = _assemble(system, groups, suffix, True)
        session.save()
        _status(sink, "Discarded earlier turns.")


def _compress(messages: list[dict]) -> bool:
    changed = False
    for message in messages:
        if message.get("role") != "tool":
            continue
        content = message.get("content")
        if not isinstance(content, str) or len(content) <= 8192:
            continue
        message["content"] = content[:4096] + _PRUNED + content[-1024:]
        changed = True
    return changed


def _groups(messages: list[dict]) -> list[list[dict]]:
    groups: list[list[dict]] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        if message.get("role") == "assistant" and message.get("tool_calls"):
            group = [message]
            index += 1
            while index < len(messages) and messages[index].get("role") == "tool":
                group.append(messages[index])
                index += 1
            groups.append(group)
        else:
            groups.append([message])
            index += 1
    return groups


def _assemble(system: dict, groups: list[list[dict]], suffix: list[dict], dropped: bool) -> list[dict]:
    messages = [system]
    if dropped:
        messages.append({"role": "user", "content": _DISCARD})
    for group in groups:
        messages.extend(group)
    messages.extend(suffix)
    return messages


def _summary_source(prefix: list[dict]) -> str:
    text = "\n".join(_render(message) for message in prefix)
    if len(text) > 40_000:
        return text[:40_000] + "\n…[truncated]"
    return text


def _render(message: dict) -> str:
    role = message.get("role")
    content = message.get("content")
    if not isinstance(content, str):
        content = ""
    if role == "tool":
        if len(content) > 300:
            total = len(content)
            content = content[:300] + f"\n…[snipped, {total} chars total]"
        name = message.get("name")
        if not isinstance(name, str):
            name = ""
        return f"[tool {name}] {content}"
    if role == "assistant":
        names: list[str] = []
        for call in message.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            function = call.get("function")
            if not isinstance(function, dict):
                continue
            name = function.get("name")
            if isinstance(name, str) and name:
                names.append(name)
        called = ", ".join(names)
        if content and called:
            return f"[assistant] {content}\n[called {called}]"
        if called:
            return f"[assistant] [called {called}]"
        return f"[assistant] {content}"
    role_name = role if isinstance(role, str) else ""
    return f"[{role_name}] {content}"


def _status(sink, text: str) -> None:
    if sink is not None:
        sink.status("\n" + text + "\n")
