"""Stdio client for Model Context Protocol servers.

A server is a subprocess. Each JSON-RPC message is one line. Lines that are
not JSON are ignored, so a log line on stdout does not break the session.
Tool names are read once at connect and kept for the process.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import threading
from foo_ai import __version__
from foo_ai.config import McpServer
from foo_ai.tools import TOOL_OUTPUT_LIMIT, tool_schemas

PROTOCOL_VERSION = "2024-11-05"
_NAME_LIMIT = 64


class McpError(RuntimeError):
    """The MCP server could not be started or did not answer a request."""


def model_tool_name(server: str, tool: str) -> str:
    """OpenAI function names are letters, digits, `_`, and `-`, at most 64 characters."""
    visible = f"mcp__{_safe(server)}__{_safe(tool)}"
    if len(visible) <= _NAME_LIMIT:
        return visible
    digest = hashlib.sha256(visible.encode()).hexdigest()[:8]
    return visible[:55].rstrip("_") + "_" + digest


def format_tool_result(result: object) -> str:
    if not isinstance(result, dict):
        return "error: tool result was not an object"
    parts = _content_parts(result.get("content"))
    text = "\n".join(parts).strip()
    if result.get("isError"):
        if not text:
            text = "tool failed"
        if not text.startswith("error:"):
            text = f"error: {text}"
        return text
    return text or "[empty tool result]"


class McpHost:
    """The MCP servers connected for one Foo AI process."""

    def __init__(self, servers: list[McpServer]):
        self.servers = servers
        self.sessions: list[McpSession] = []
        self._tools: dict[str, tuple[McpSession, str, dict]] = {}
        self._notes: list[str] = []
        self._connected = False

    def connect(self) -> list[str]:
        if self._connected:
            return list(self._notes)
        self._connected = True
        builtin = _builtin_names()
        notes: list[str] = []
        for server in self.servers:
            session = McpSession(server)
            try:
                tools = session.start()
            except McpError as exc:
                session.close()
                notes.append(f"mcp {server.name}: unavailable ({_one_line(str(exc))})")
                continue
            self.sessions.append(session)
            added = self._add_tools(server.name, session, tools, builtin)
            if added:
                notes.append(f"mcp {server.name}: {', '.join(added)}")
            elif any(isinstance(tool, dict) and tool.get("name") for tool in tools):
                notes.append(f"mcp {server.name}: connected, tool names already in use")
            else:
                notes.append(f"mcp {server.name}: connected, no tools")
        self._notes = notes
        return list(notes)

    def schemas(self) -> list[dict]:
        return [entry[2] for entry in self._tools.values()]

    def owns(self, name: str) -> bool:
        return name in self._tools

    def call(self, name: str, arguments: str | dict) -> str:
        try:
            session, raw, _schema = self._tools[name]
        except KeyError:
            return f"error: unknown tool {name!r}"
        try:
            parsed = _arguments(arguments)
        except json.JSONDecodeError as exc:
            return f"error: arguments are not JSON ({exc})"
        except ValueError as exc:
            return f"error: {exc}"
        try:
            text = session.call_tool(raw, parsed)
        except McpError as exc:
            text = f"error: {exc}"
        return _truncate(text)

    def close(self) -> None:
        sessions = self.sessions
        self.sessions = []
        for session in sessions:
            session.close()

    def _add_tools(self, server_name: str, session: McpSession, tools: list[dict], builtin: set[str]) -> list[str]:
        added: list[str] = []
        for tool in tools:
            raw = tool.get("name")
            if not isinstance(raw, str) or not raw.strip():
                continue
            visible = model_tool_name(server_name, raw)
            if visible in self._tools or visible in builtin:
                continue
            self._tools[visible] = (session, raw, _function_schema(visible, server_name, tool))
            added.append(raw)
        return added


class McpSession:
    def __init__(self, server: McpServer):
        self.server = server
        self.proc: subprocess.Popen[str] | None = None
        self._next_id = 1
        self._waiters: dict[int, tuple[threading.Event, dict]] = {}
        self._state_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._dead = ""
        self._stderr = ""
        self._stdout_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None

    def start(self) -> list[dict]:
        self._spawn()
        self._request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "foo-ai", "version": __version__},
            },
        )
        self._notify("notifications/initialized")
        return self._list_tools()

    def call_tool(self, name: str, arguments: dict) -> str:
        return format_tool_result(self._request("tools/call", {"name": name, "arguments": arguments}))

    def close(self) -> None:
        proc = self.proc
        if proc is None:
            return
        self.proc = None
        self._fail_all("server closed")
        if proc.stdin is not None:
            try:
                proc.stdin.close()
            except OSError:
                pass
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
        if self._stdout_thread is not None:
            self._stdout_thread.join(timeout=1)
        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=1)
        for stream in (proc.stdout, proc.stderr):
            if stream is not None and not stream.closed:
                try:
                    stream.close()
                except OSError:
                    pass

    def _spawn(self) -> None:
        if not self.server.command.strip():
            raise McpError("command is required")
        # Merge onto the parent environment. Replacing it drops PATH and SystemRoot on Windows.
        env = None
        if self.server.env:
            env = os.environ.copy()
            env.update(self.server.env)
        try:
            self.proc = subprocess.Popen(
                [self.server.command, *self.server.args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                cwd=self.server.cwd or None,
                env=env,
            )
        except OSError as exc:
            raise McpError(f"cannot start {self.server.command}: {exc}") from exc
        self._stdout_thread = threading.Thread(target=self._read_stdout, name=f"mcp-{self.server.name}-out", daemon=True)
        self._stderr_thread = threading.Thread(target=self._read_stderr, name=f"mcp-{self.server.name}-err", daemon=True)
        self._stdout_thread.start()
        self._stderr_thread.start()

    def _list_tools(self) -> list[dict]:
        tools: list[dict] = []
        cursor: str | None = None
        seen: set[str] = set()
        for _ in range(20):
            params: dict = {} if cursor is None else {"cursor": cursor}
            result = self._request("tools/list", params)
            if not isinstance(result, dict):
                raise McpError("tools/list returned no result")
            batch = result.get("tools") or []
            if isinstance(batch, list):
                tools.extend(item for item in batch if isinstance(item, dict))
            next_cursor = result.get("nextCursor")
            if not isinstance(next_cursor, str) or not next_cursor or next_cursor in seen:
                break
            seen.add(next_cursor)
            cursor = next_cursor
        return tools

    def _request(self, method: str, params: dict) -> object:
        request_id = self._next_id
        self._next_id += 1
        event = threading.Event()
        box: dict = {}
        with self._state_lock:
            if self._dead:
                raise McpError(self._dead)
            self._waiters[request_id] = (event, box)
        send_error: McpError | None = None
        try:
            try:
                self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
            except McpError as exc:
                send_error = exc
            wait = 0.5 if send_error else self.server.timeout_seconds
            if not event.wait(wait):
                if send_error is not None:
                    raise send_error
                raise McpError(f"timed out waiting for {method}")
            if "error" in box:
                raise McpError(str(box["error"]))
            if send_error is not None:
                raise send_error
            return box.get("result")
        finally:
            with self._state_lock:
                self._waiters.pop(request_id, None)

    def _notify(self, method: str) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": {}})

    def _send(self, message: dict) -> None:
        payload = json.dumps(message, ensure_ascii=False) + "\n"
        with self._write_lock:
            proc = self.proc
            if proc is None or proc.stdin is None:
                raise McpError(self._dead or "server is not running")
            try:
                proc.stdin.write(payload)
                proc.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise McpError(str(exc)) from exc

    def _read_stdout(self) -> None:
        proc = self.proc
        stdout = proc.stdout if proc is not None else None
        try:
            if stdout is not None:
                for line in stdout:
                    self._dispatch(line)
        except (OSError, ValueError):
            return
        finally:
            if self._stderr_thread is not None:
                self._stderr_thread.join(timeout=0.3)
            self._fail_all(self._failure("server closed stdout"))

    def _read_stderr(self) -> None:
        proc = self.proc
        stderr = proc.stderr if proc is not None else None
        if stderr is None:
            return
        try:
            for line in stderr:
                with self._state_lock:
                    self._stderr = (self._stderr + line)[-4000:]
        except (OSError, ValueError):
            return

    def _dispatch(self, line: str) -> None:
        text = line.strip()
        if not text:
            return
        try:
            message = json.loads(text)
        except json.JSONDecodeError:
            return
        if not isinstance(message, dict):
            return
        method = message.get("method")
        if isinstance(method, str) and "id" in message:
            self._respond(message)
            return
        if "id" not in message:
            return
        with self._state_lock:
            waiter = self._waiters.get(message["id"])
        if waiter is None:
            return
        event, box = waiter
        if "error" in message:
            box["error"] = _rpc_error(message["error"])
        else:
            box["result"] = message.get("result")
        event.set()

    def _respond(self, message: dict) -> None:
        method = str(message.get("method") or "")
        if method == "ping":
            body: dict = {"jsonrpc": "2.0", "id": message["id"], "result": {}}
        else:
            body = {
                "jsonrpc": "2.0",
                "id": message["id"],
                "error": {"code": -32601, "message": f"method not found: {method}"},
            }
        try:
            self._send(body)
        except McpError:
            return

    def _fail_all(self, message: str) -> None:
        with self._state_lock:
            if not self._dead:
                self._dead = message
            message = self._dead
            waiters = list(self._waiters.values())
        for event, box in waiters:
            box.setdefault("error", message)
            event.set()

    def _failure(self, fallback: str) -> str:
        with self._state_lock:
            err = self._stderr.strip()
        code = self.proc.poll() if self.proc is not None else None
        if err and code is not None:
            text = f"exit {code}: {err}"
        elif err:
            text = err
        elif code is not None:
            text = f"exit {code}"
        else:
            text = fallback
        return _one_line(text)


def _builtin_names() -> set[str]:
    names = set()
    for schema in tool_schemas():
        function = schema.get("function") or {}
        if function.get("name"):
            names.add(function["name"])
    return names


def _function_schema(visible: str, server: str, tool: dict) -> dict:
    description = tool.get("description")
    if not isinstance(description, str) or not description.strip():
        description = str(tool.get("name") or visible)
    description = f"[{server}] {description.strip()}"
    if len(description) > 2000:
        description = description[:2000] + "…"
    return {
        "type": "function",
        "function": {
            "name": visible,
            "description": description,
            "parameters": _parameters(tool.get("inputSchema")),
        },
    }


def _parameters(schema: object) -> dict:
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}}
    params = {key: value for key, value in schema.items() if key != "$schema"}
    params.setdefault("type", "object")
    params.setdefault("properties", {})
    return params


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
            else:
                uri = resource.get("uri") if isinstance(resource, dict) else ""
                parts.append(f"[resource {uri}]" if uri else "[resource]")
        elif isinstance(kind, str) and kind:
            parts.append(f"[{kind}]")
    return parts


def _arguments(arguments: str | dict) -> dict:
    if isinstance(arguments, dict):
        return arguments
    if not arguments or not str(arguments).strip():
        return {}
    parsed = json.loads(arguments)
    if not isinstance(parsed, dict):
        raise ValueError("arguments must be a JSON object")
    return parsed


def _safe(value: str) -> str:
    cleaned = []
    previous = False
    for char in value:
        ok = char.isascii() and (char.isalnum() or char in {"_", "-"})
        if ok:
            cleaned.append(char)
            previous = False
        elif not previous:
            cleaned.append("_")
            previous = True
    return "".join(cleaned).strip("_") or "tool"


def _truncate(text: str) -> str:
    if len(text) > TOOL_OUTPUT_LIMIT:
        return text[:TOOL_OUTPUT_LIMIT] + "\n…[truncated]"
    return text


def _one_line(text: str) -> str:
    text = " ".join(text.split())
    if len(text) > 200:
        return text[:200] + "…"
    return text


def _rpc_error(error: object) -> str:
    if isinstance(error, dict):
        return str(error.get("message") or error)
    return str(error)
