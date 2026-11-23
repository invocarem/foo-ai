"""OpenAI-compatible chat client for the home model server."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Iterator
from typing import Any

from foo_ai.config import Provider


class ChatError(RuntimeError):
    pass


class Client:
    def __init__(self, provider: Provider):
        self.provider = provider

    def build_body(self, messages: list[dict], tools: list[dict]) -> dict:
        body: dict[str, Any] = {
            "model": self.provider.model,
            "messages": messages,
            "tools": tools,
            "stream": True,
        }
        body.update(self.provider.extra_body)
        return body

    def stream(self, messages: list[dict], tools: list[dict]) -> Iterator[dict]:
        body = json.dumps(self.build_body(messages, tools)).encode("utf-8")
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
        if self.provider.api_key:
            headers["Authorization"] = f"Bearer {self.provider.api_key}"
        request = urllib.request.Request(self.provider.chat_url, data=body, headers=headers, method="POST")
        try:
            response = urllib.request.urlopen(request, timeout=None)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:2000]
            raise ChatError(f"HTTP {exc.code} from {self.provider.chat_url}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise ChatError(f"cannot reach {self.provider.chat_url}: {exc.reason}") from exc

        with response:
            content_type = response.headers.get("Content-Type", "")
            if "application/json" in content_type and "text/event-stream" not in content_type:
                payload = json.loads(response.read().decode("utf-8"))
                yield {"type": "message", "message": _message_from_json(payload)}
                return
            yield from _read_sse(response)


def assistant_message(message: dict) -> dict:
    """Replay an assistant turn, including thinking text the server issued."""
    stored = {
        "role": "assistant",
        "content": message.get("content"),
        "reasoning_content": message.get("reasoning_content") or "",
    }
    calls = message.get("tool_calls") or []
    if calls:
        stored["tool_calls"] = calls
        if stored["content"] in ("", None):
            stored["content"] = None
    elif stored["content"] is None:
        stored["content"] = ""
    return stored


def _message_from_json(payload: dict) -> dict:
    try:
        message = payload["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ChatError(f"unexpected response: {json.dumps(payload)[:500]}") from exc
    return _finish_message(
        message.get("content") or "",
        message.get("reasoning_content") or message.get("reasoning") or "",
        message.get("tool_calls") or [],
    )


def _read_sse(response) -> Iterator[dict]:
    content: list[str] = []
    reasoning: list[str] = []
    calls: dict[int, dict] = {}
    order: list[int] = []
    started: set[int] = set()
    raw_lines: list[str] = []

    for raw in response:
        line = raw.decode("utf-8", errors="replace").strip()
        if not line or line.startswith(":"):
            continue
        raw_lines.append(line)
        if not line.startswith("data:"):
            continue
        data = line[len("data:") :].strip()
        if data == "[DONE]":
            break
        try:
            event = json.loads(data)
        except json.JSONDecodeError as exc:
            raise ChatError(f"invalid stream event: {data[:300]}") from exc
        if event.get("error"):
            detail = event["error"]
            message = detail.get("message", detail) if isinstance(detail, dict) else detail
            raise ChatError(str(message))
        choices = event.get("choices") or []
        if not choices:
            continue
        delta = choices[0].get("delta") or {}
        piece = delta.get("reasoning_content") or delta.get("reasoning") or ""
        if piece:
            reasoning.append(piece)
            yield {"type": "reasoning", "text": piece}
        text = delta.get("content") or ""
        if text:
            content.append(text)
            yield {"type": "content", "text": text}
        for call in delta.get("tool_calls") or []:
            index = int(call.get("index") or 0)
            current = calls.get(index)
            if current is None:
                current = {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
                calls[index] = current
                order.append(index)
            if call.get("id"):
                current["id"] = call["id"]
            function = call.get("function") or {}
            if function.get("name"):
                current["function"]["name"] += function["name"]
            if function.get("arguments"):
                piece = function["arguments"]
                current["function"]["arguments"] += piece if isinstance(piece, str) else json.dumps(piece)
            if index not in started and current["function"]["name"]:
                started.add(index)
                yield {"type": "tool_start", "name": current["function"]["name"]}

    if not content and not reasoning and not calls and raw_lines and not any(line.startswith("data:") for line in raw_lines):
        try:
            payload = json.loads("\n".join(raw_lines))
        except json.JSONDecodeError as exc:
            raise ChatError("server closed the stream without a completed response") from exc
        yield {"type": "message", "message": _message_from_json(payload)}
        return

    yield {
        "type": "message",
        "message": _finish_message("".join(content), "".join(reasoning), [calls[index] for index in order]),
    }


def _finish_message(content: str, reasoning: str, calls: list[dict]) -> dict:
    message: dict = {"role": "assistant", "content": content, "reasoning_content": reasoning}
    normalized = _normalize_calls(calls)
    if normalized:
        message["tool_calls"] = normalized
    return message


def _normalize_calls(calls: list[dict]) -> list[dict]:
    normalized = []
    for index, call in enumerate(calls):
        function = dict(call.get("function") or {})
        arguments = function.get("arguments", "")
        if not isinstance(arguments, str):
            arguments = json.dumps(arguments)
        normalized.append(
            {
                "id": call.get("id") or f"call_{index}",
                "type": "function",
                "function": {"name": function.get("name") or "", "arguments": arguments},
            }
        )
    return normalized
