"""HTTP chat client: one streaming completion, no retries."""

from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from foo_ai.config import Provider, completions_url


class ChatError(Exception):
    """The provider rejected the call or the stream could not be read."""


@dataclass
class Completion:
    content: str = ""
    reasoning_content: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    usage: dict | None = None


class Client:
    def __init__(self, provider: Provider, timeout: float | None = None):
        self.provider = provider
        self.timeout = timeout

    def complete(self, messages: list[dict], tools: list[dict] | None, sink) -> Completion:
        url = completions_url(self.provider.base_url)
        payload: dict = {
            "model": self.provider.model,
            "messages": messages,
            "stream": True,
        }
        if tools is not None:
            payload["tools"] = tools
        for key, value in self.provider.extra_body.items():
            payload[key] = value
        if "stream_options" not in payload:
            payload["stream_options"] = {"include_usage": True}
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        if self.provider.api_key:
            headers["Authorization"] = f"Bearer {self.provider.api_key}"
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            response = urllib.request.urlopen(request, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            text = raw.decode("utf-8", "replace")[:2000]
            raise ChatError(f"HTTP {exc.code} from {url}: {text}") from None
        except urllib.error.URLError as exc:
            raise ChatError(f"cannot reach {url}: {exc.reason}") from None
        except OSError as exc:
            raise ChatError(f"cannot reach {url}: {exc}") from None
        try:
            content_type = response.headers.get("Content-Type", "")
            lowered = content_type.lower()
            json_body = "application/json" in lowered and "text/event-stream" not in lowered
            if json_body:
                text = response.read().decode("utf-8", "replace")
                completion = _completion_from_json(text)
            else:
                completion = _read_sse(response, sink)
        except (OSError, http.client.HTTPException) as exc:
            raise ChatError(f"cannot reach {url}: {exc}") from None
        finally:
            response.close()
        if json_body:
            _replay(completion, sink)
        return completion


def _completion_from_json(text: str) -> Completion:
    try:
        body = json.loads(text)
    except json.JSONDecodeError:
        raise ChatError(f"unexpected response: {text[:500]}") from None
    return _completion_from_body(body, text)


def _completion_from_body(body: object, text: str) -> Completion:
    if not isinstance(body, dict):
        raise ChatError(f"unexpected response: {text[:500]}")
    usage = body.get("usage") if isinstance(body.get("usage"), dict) else None
    try:
        message = body["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        raise ChatError(f"unexpected response: {text[:500]}") from None
    if not isinstance(message, dict):
        raise ChatError(f"unexpected response: {text[:500]}")
    content = message.get("content")
    if not isinstance(content, str):
        content = ""
    if "reasoning_content" in message and message["reasoning_content"] is not None:
        reasoning = message["reasoning_content"]
    elif message.get("reasoning") is not None:
        reasoning = message["reasoning"]
    else:
        reasoning = ""
    if not isinstance(reasoning, str):
        reasoning = ""
    tool_calls = _normalize_calls(message.get("tool_calls") or [])
    return Completion(
        content=content,
        reasoning_content=reasoning,
        tool_calls=tool_calls,
        usage=usage,
    )


def _normalize_calls(calls: object) -> list[dict]:
    if not isinstance(calls, list):
        return []
    result: list[dict] = []
    for call in calls:
        if not isinstance(call, dict):
            continue
        function = call.get("function") if isinstance(call.get("function"), dict) else {}
        name = function.get("name")
        if not isinstance(name, str):
            name = "" if name is None else str(name)
        arguments = function.get("arguments")
        if isinstance(arguments, str):
            argument_text = arguments
        elif arguments is None:
            argument_text = ""
        else:
            argument_text = json.dumps(arguments)
        call_id = call.get("id")
        if not isinstance(call_id, str) or call_id == "":
            call_id = f"call_{len(result)}"
        result.append(
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": argument_text},
            }
        )
    return result


def _replay(completion: Completion, sink) -> None:
    """Print a finished body that was not streamed, as the streamed chunks would have been."""
    if sink is None:
        return
    if completion.reasoning_content:
        sink.reasoning(completion.reasoning_content)
    if completion.content:
        sink.content(completion.content)
    for call in completion.tool_calls:
        name = call["function"]["name"]
        if name:
            sink.tool_name(name)


def _read_sse(response, sink) -> Completion:
    content: list[str] = []
    reasoning: list[str] = []
    slots: dict[int, dict] = {}
    order: list[int] = []
    usage = None
    raw_lines: list[str] = []
    saw_data = False

    while True:
        raw = response.readline()
        if not raw:
            break
        line = raw.decode("utf-8", "replace").strip()
        raw_lines.append(line)
        if line == "" or line.startswith(":"):
            continue
        if not line.startswith("data:"):
            continue
        saw_data = True
        payload = line[5:].strip()
        if payload == "[DONE]":
            break
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            raise ChatError(f"invalid stream event: {payload[:300]}") from None
        if not isinstance(event, dict):
            raise ChatError(f"invalid stream event: {payload[:300]}")
        if "error" in event:
            raise ChatError(_error_text(event["error"]))
        if isinstance(event.get("usage"), dict):
            usage = event["usage"]
        choices = event.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            continue
        delta = choices[0].get("delta")
        if not isinstance(delta, dict):
            continue
        piece = _delta_text(delta, "reasoning_content", "reasoning")
        if piece:
            reasoning.append(piece)
            if sink is not None:
                sink.reasoning(piece)
        piece = delta.get("content")
        if isinstance(piece, str) and piece:
            content.append(piece)
            if sink is not None:
                sink.content(piece)
        fragments = delta.get("tool_calls")
        if isinstance(fragments, list):
            for fragment in fragments:
                if isinstance(fragment, dict):
                    _add_fragment(slots, order, fragment, sink)

    if not content and not reasoning and not slots and raw_lines and not saw_data:
        blob = "\n".join(raw_lines)
        try:
            body = json.loads(blob)
        except json.JSONDecodeError:
            raise ChatError("server closed the stream without a completed response") from None
        completion = _completion_from_body(body, blob)
        _replay(completion, sink)
        return completion

    tool_calls = []
    for position, index in enumerate(order):
        slot = slots[index]
        call_id = slot["id"] or f"call_{position}"
        tool_calls.append(
            {
                "id": call_id,
                "type": "function",
                "function": {"name": slot["name"], "arguments": slot["arguments"]},
            }
        )
    return Completion(
        content="".join(content),
        reasoning_content="".join(reasoning),
        tool_calls=tool_calls,
        usage=usage,
    )


def _delta_text(delta: dict, primary: str, fallback: str) -> str:
    if primary in delta and delta[primary] is not None:
        value = delta[primary]
    elif delta.get(fallback) is not None:
        value = delta[fallback]
    else:
        return ""
    return value if isinstance(value, str) else ""


def _add_fragment(slots: dict[int, dict], order: list[int], fragment: dict, sink) -> None:
    index = fragment.get("index", 0)
    if not isinstance(index, int) or isinstance(index, bool):
        index = 0
    slot = slots.get(index)
    if slot is None:
        slot = {"id": "", "name": "", "arguments": "", "announced": False}
        slots[index] = slot
        order.append(index)
    call_id = fragment.get("id")
    if isinstance(call_id, str) and call_id:
        slot["id"] = call_id
    function = fragment.get("function")
    if not isinstance(function, dict):
        return
    name = function.get("name")
    if isinstance(name, str) and name:
        slot["name"] += name
        if not slot["announced"] and slot["name"]:
            slot["announced"] = True
            if sink is not None:
                sink.tool_name(slot["name"])
    if "arguments" in function and function["arguments"] is not None:
        argument = function["arguments"]
        if isinstance(argument, str):
            slot["arguments"] += argument
        else:
            slot["arguments"] += json.dumps(argument)


def _error_text(error: object) -> str:
    if isinstance(error, dict):
        message = error.get("message")
        if message is not None:
            return str(message)
        return json.dumps(error, ensure_ascii=False)
    if isinstance(error, str):
        return error
    return json.dumps(error, ensure_ascii=False)
