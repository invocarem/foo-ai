"""Stdio MCP client. One session per enabled server, no per-server special cases."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import threading

from foo_ai import __version__
from foo_ai.config import McpSpec
from foo_ai.tools import TOOL_NAMES, parse_arguments


class _Problem(Exception):
    pass


class _Timeout(_Problem):
    pass


class _Dead(_Problem):
    pass


class _RpcError(_Problem):
    pass


def one_line(text: str) -> str:
    collapsed = " ".join(str(text).split())
    if len(collapsed) > 200:
        return collapsed[:200] + "…"
    return collapsed


def _sanitize(part: str) -> str:
    pieces: list[str] = []
    replaced = False
    for char in part:
        if char.isascii() and (char.isalnum() or char in "_-"):
            pieces.append(char)
            replaced = False
        elif not replaced:
            pieces.append("_")
            replaced = True
    text = "".join(pieces).strip("_")
    return text or "tool"


def advertise_name(server: str, tool: str) -> str:
    full = f"mcp__{_sanitize(server)}__{_sanitize(tool)}"
    if len(full) <= 64:
        return full
    digest = hashlib.sha256(full.encode("utf-8")).hexdigest()[:8]
    prefix = full[:55].rstrip("_")
    return f"{prefix}_{digest}"


def _parameters(tool: dict) -> dict:
    schema = tool.get("inputSchema")
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}}
    cleaned = {key: value for key, value in schema.items() if key != "$schema"}
    if "type" not in cleaned:
        cleaned["type"] = "object"
    if "properties" not in cleaned:
        cleaned["properties"] = {}
    return cleaned


def _description(server_name: str, raw_name: str, tool: dict) -> str:
    description = tool.get("description")
    if isinstance(description, str) and description.strip():
        body = description.strip()
    else:
        body = raw_name
    text = f"[{server_name}] {body}"
    if len(text) > 2000:
        return text[:2000] + "…"
    return text


def _schema(server_name: str, raw_name: str, advertised: str, tool: dict) -> dict:
    return {
        "type": "function",
        "function": {
            "name": advertised,
            "description": _description(server_name, raw_name, tool),
            "parameters": _parameters(tool),
        },
    }


def render_tool_result(result: object) -> str:
    if not isinstance(result, dict):
        return "error: tool result was not an object"
    body = "\n".join(_content_parts(result.get("content"))).strip()
    if result.get("isError"):
        if body == "":
            body = "tool failed"
        if not body.startswith("error:"):
            body = "error: " + body
    elif body == "":
        body = "[empty tool result]"
    return body


def _content_parts(content: object) -> list[str]:
    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        return []
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
            continue
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text" and isinstance(block.get("text"), str):
            parts.append(block["text"])
        elif kind == "image":
            parts.append("[image]")
        elif kind == "resource":
            resource = block.get("resource")
            if isinstance(resource, dict) and isinstance(resource.get("text"), str):
                parts.append(resource["text"])
            elif isinstance(resource, dict) and isinstance(resource.get("uri"), str):
                parts.append(f"[resource {resource['uri']}]")
            else:
                parts.append("[resource]")
        elif isinstance(kind, str) and kind:
            parts.append(f"[{kind}]")
    return parts


def _rpc_error_text(error: object) -> str:
    if isinstance(error, dict):
        message = error.get("message")
        if message is not None:
            return str(message)
        return json.dumps(error, ensure_ascii=False)
    return str(error)


class McpProcess:
    def __init__(self, spec: McpSpec):
        self.spec = spec
        self.proc: subprocess.Popen | None = None
        self.failure: str | None = None
        self.next_id = 1
        self.pending: dict[object, dict] = {}
        self.stderr_text = ""
        self.state_lock = threading.Lock()
        self.write_lock = threading.Lock()
        self.stdout_thread: threading.Thread | None = None
        self.stderr_thread: threading.Thread | None = None

    def start(self) -> tuple[list[dict] | None, str | None]:
        if not isinstance(self.spec.command, str) or self.spec.command.strip() == "":
            text = one_line("command is required")
            self.failure = text
            return None, text
        env = None
        if self.spec.env is not None:
            env = os.environ.copy()
            env.update(self.spec.env)
        try:
            self.proc = subprocess.Popen(
                [self.spec.command, *self.spec.args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=self.spec.cwd,
                env=env,
                bufsize=0,
            )
        except OSError as exc:
            text = one_line(f"cannot start {self.spec.command}: {exc}")
            self.failure = text
            return None, text
        self._start_threads()
        try:
            self.request(
                "initialize",
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "foo-ai", "version": __version__},
                },
            )
            self.notify("notifications/initialized", {})
            tools = self._list_tools()
        except _Problem as exc:
            text = one_line(str(exc))
            self.failure = text
            return None, text
        return tools, None

    def call_tool(self, name: str, arguments: dict) -> str:
        if self.failure:
            return f"error: {self.failure}"
        try:
            result = self.request("tools/call", {"name": name, "arguments": arguments})
        except _Timeout:
            text = one_line("timed out waiting for tools/call")
            self.failure = text
            return f"error: {text}"
        except _Dead as exc:
            text = one_line(str(exc))
            self.failure = text
            return f"error: {text}"
        except _RpcError as exc:
            return f"error: {exc}"
        return render_tool_result(result)

    def close(self) -> None:
        proc = self.proc
        if proc is None:
            return
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                proc.terminate()
            except Exception:
                pass
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except Exception:
                    pass
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
        if self.stdout_thread is not None:
            self.stdout_thread.join(timeout=2)
        if self.stderr_thread is not None:
            self.stderr_thread.join(timeout=2)
        for stream in (proc.stdout, proc.stderr, proc.stdin):
            if stream is None:
                continue
            try:
                stream.close()
            except Exception:
                pass

    def request(self, method: str, params: dict):
        proc = self.proc
        if proc is None or proc.stdin is None:
            raise _Dead(self.failure or "server closed stdout")
        with self.state_lock:
            request_id = self.next_id
            self.next_id += 1
            waiter = {"event": threading.Event(), "message": None, "error": None}
            self.pending[request_id] = waiter
        self._write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        if not waiter["event"].wait(self.spec.timeout):
            with self.state_lock:
                self.pending.pop(request_id, None)
            raise _Timeout(f"timed out waiting for {method}")
        if waiter["error"]:
            raise _Dead(str(waiter["error"]))
        message = waiter["message"]
        if not isinstance(message, dict):
            raise _Dead(self.failure or "server closed stdout")
        if "error" in message:
            raise _RpcError(_rpc_error_text(message["error"]))
        return message.get("result")

    def notify(self, method: str, params: dict) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def _list_tools(self) -> list[dict]:
        params: dict = {}
        seen: set[str] = set()
        tools: list[dict] = []
        for _ in range(20):
            result = self.request("tools/list", params)
            if not isinstance(result, dict):
                raise _RpcError("tools/list returned no result")
            batch = result.get("tools")
            if isinstance(batch, list):
                for item in batch:
                    if isinstance(item, dict):
                        tools.append(item)
            cursor = result.get("nextCursor")
            if not isinstance(cursor, str) or cursor == "" or cursor in seen:
                break
            seen.add(cursor)
            params = {"cursor": cursor}
        return tools

    def _write(self, obj: dict) -> None:
        data = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
        with self.write_lock:
            try:
                assert self.proc is not None and self.proc.stdin is not None
                self.proc.stdin.write(data)
                self.proc.stdin.flush()
                return
            except Exception:
                pass
        self._fail()
        raise _Dead(self.failure or "server closed stdout")

    def _start_threads(self) -> None:
        self.stdout_thread = threading.Thread(target=self._stdout_loop, name="foo-ai-mcp-stdout", daemon=True)
        self.stderr_thread = threading.Thread(target=self._stderr_loop, name="foo-ai-mcp-stderr", daemon=True)
        self.stdout_thread.start()
        self.stderr_thread.start()

    def _stdout_loop(self) -> None:
        try:
            assert self.proc is not None and self.proc.stdout is not None
            while True:
                raw = self.proc.stdout.readline()
                if not raw:
                    self._fail()
                    return
                self._on_line(raw)
        except Exception:
            self._fail()

    def _stderr_loop(self) -> None:
        try:
            assert self.proc is not None and self.proc.stderr is not None
            while True:
                raw = self.proc.stderr.readline()
                if not raw:
                    return
                text = raw.decode("utf-8", "replace")
                with self.state_lock:
                    self.stderr_text = (self.stderr_text + text)[-4000:]
        except Exception:
            return

    def _on_line(self, raw: bytes) -> None:
        line = raw.decode("utf-8", "replace").strip()
        if not line:
            return
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            return
        if not isinstance(message, dict):
            return
        if "method" in message and "id" in message:
            self._answer(message)
            return
        if "id" not in message:
            return
        with self.state_lock:
            waiter = self.pending.pop(message["id"], None)
        if waiter is not None:
            waiter["message"] = message
            waiter["event"].set()

    def _answer(self, message: dict) -> None:
        method = message.get("method")
        if method == "ping":
            response = {"jsonrpc": "2.0", "id": message["id"], "result": {}}
        else:
            response = {
                "jsonrpc": "2.0",
                "id": message["id"],
                "error": {"code": -32601, "message": f"method not found: {method}"},
            }
        try:
            self._write(response)
        except _Dead:
            return

    def _fail(self, text: str | None = None) -> None:
        if text is None:
            text = self._closed_text()
        text = one_line(text)
        with self.state_lock:
            if self.failure is None:
                self.failure = text
            waiters = list(self.pending.values())
            self.pending.clear()
        for waiter in waiters:
            waiter["error"] = self.failure
            waiter["event"].set()

    def _closed_text(self) -> str:
        if self.proc is not None and self.proc.poll() is None:
            try:
                self.proc.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                pass
        if self.stderr_thread is not None and self.stderr_thread is not threading.current_thread():
            self.stderr_thread.join(timeout=0.5)
        with self.state_lock:
            stderr = self.stderr_text.strip()
        code = self.proc.poll() if self.proc is not None else None
        if stderr and code is not None:
            return f"exit {code}: {stderr}"
        if stderr:
            return stderr
        if code is not None:
            return f"exit {code}"
        return "server closed stdout"


class McpPool:
    def __init__(self, specs: list[McpSpec]):
        self.specs = specs
        self.servers: list[McpProcess] = []
        self._schemas: list[dict] = []
        self._by_name: dict[str, tuple[McpProcess, str]] = {}
        self._lines: list[str] | None = None

    def connect(self) -> list[str]:
        if self._lines is not None:
            return list(self._lines)
        lines: list[str] = []
        used = set(TOOL_NAMES)
        for spec in self.specs:
            server = McpProcess(spec)
            self.servers.append(server)
            tools, error = server.start()
            if error is not None:
                lines.append(f"mcp {spec.name}: unavailable ({error})")
                continue
            lines.append(self._register(spec.name, server, tools or [], used))
        self._lines = lines
        return list(lines)

    def schemas(self) -> list[dict]:
        return list(self._schemas)

    def knows(self, name: str) -> bool:
        return name in self._by_name

    def call(self, name: str, raw: object) -> str:
        found = self._by_name.get(name)
        if found is None:
            return f"error: unknown tool {name!r}"
        server, original = found
        parsed = parse_arguments(raw, mcp=True)
        if isinstance(parsed, str):
            return parsed
        return server.call_tool(original, parsed)

    def close(self) -> None:
        for server in self.servers:
            server.close()

    def _register(self, server_name: str, server: McpProcess, tools: list[dict], used: set[str]) -> str:
        named: list[str] = []
        added: list[str] = []
        for tool in tools:
            raw_name = tool.get("name")
            if not isinstance(raw_name, str) or raw_name == "":
                continue
            named.append(raw_name)
            advertised = advertise_name(server_name, raw_name)
            if advertised in used:
                continue
            used.add(advertised)
            added.append(raw_name)
            self._by_name[advertised] = (server, raw_name)
            self._schemas.append(_schema(server_name, raw_name, advertised, tool))
        if not named:
            return f"mcp {server_name}: connected, no tools"
        if not added:
            return f"mcp {server_name}: connected, tool names already in use"
        return f"mcp {server_name}: {', '.join(added)}"
