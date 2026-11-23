"""Stdio MCP test server. The mode is FOO_MCP_MODE."""

from __future__ import annotations

import json
import os
import sys


def send(obj: dict) -> None:
    sys.stdout.buffer.write((json.dumps(obj) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()


def read() -> dict | None:
    while True:
        line = sys.stdin.buffer.readline()
        if line == b"":
            return None
        text = line.decode("utf-8", "replace").strip()
        if not text:
            continue
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value


def tools_for(mode: str, cursor: str | None) -> dict:
    if mode == "pages" and not cursor:
        return {
            "tools": [
                {
                    "name": "one",
                    "description": "First.",
                    "inputSchema": {"type": "object", "properties": {}},
                }
            ],
            "nextCursor": "c1",
        }
    if mode == "pages":
        return {"tools": [{"name": "two", "description": "Second."}]}
    if mode == "empty":
        return {"tools": []}
    if mode == "noname":
        return {"tools": [{"description": "missing name"}]}
    if mode == "collide":
        return {
            "tools": [
                {"name": "a b", "description": "One."},
                {"name": "a_b", "description": "Two."},
            ]
        }
    return {
        "tools": [
            {
                "name": "echo",
                "description": "Echo.",
                "inputSchema": {
                    "$schema": "http://json-schema.org/draft-07/schema#",
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                },
            }
        ]
    }


def call_result(arguments: dict) -> dict:
    kind = arguments.get("kind")
    if kind == "parts":
        return {
            "content": [
                {"type": "text", "text": "a"},
                {"type": "image"},
                {"type": "resource", "resource": {"text": "body"}},
            ]
        }
    if kind == "error":
        return {"content": [{"type": "text", "text": "nope"}], "isError": True}
    if kind == "empty":
        return {"content": []}
    if kind == "fail-empty":
        return {"isError": True, "content": []}
    if kind == "plain":
        return {"content": arguments.get("text", "")}
    return {"content": [{"type": "text", "text": str(arguments.get("text", ""))}]}


def main() -> None:
    mode = os.environ.get("FOO_MCP_MODE", "echo")
    if mode == "exit":
        sys.stderr.write("boom failure\n")
        raise SystemExit(3)
    while True:
        message = read()
        if message is None:
            return
        method = message.get("method")
        if method == "initialize":
            if mode != "nopi":
                send({"jsonrpc": "2.0", "id": 99, "method": "ping"})
                read()
            if mode == "die":
                return
            send(
                {
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "result": {"protocolVersion": "2024-11-05"},
                }
            )
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            params = message.get("params") if isinstance(message.get("params"), dict) else {}
            cursor = params.get("cursor") if isinstance(params.get("cursor"), str) else None
            send({"jsonrpc": "2.0", "id": message["id"], "result": tools_for(mode, cursor)})
        elif method == "tools/call":
            params = message.get("params") if isinstance(message.get("params"), dict) else {}
            arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
            send({"jsonrpc": "2.0", "id": message["id"], "result": call_result(arguments)})


if __name__ == "__main__":
    main()
