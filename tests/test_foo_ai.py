"""Behavior checks for the Foo AI specification."""

from __future__ import annotations

import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from foo_ai import __version__
from foo_ai.agent import Agent
from foo_ai.cli import main
from foo_ai.client import ChatError, Client, Completion
from foo_ai.config import ConfigError, Provider, completions_url, load_config
from foo_ai.context import Context, transcript_chars
from foo_ai.mcp import McpPool, advertise_name, render_tool_result
from foo_ai.prompt import SYSTEM_PROMPT
from foo_ai.session import Session
from foo_ai.tools import TOOL_SCHEMAS, Toolbox, parse_arguments
from foo_ai.usage import compact, format_usage_line, parse_usage

ECHO_MCP = Path(__file__).with_name("echo_mcp.py")


def clean_env(extra: dict | None = None) -> dict:
    env = os.environ.copy()
    for key in list(env):
        if key.startswith("FOO_AI_"):
            del env[key]
    env["PYTHONPATH"] = str(ROOT)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    if extra:
        env.update(extra)
    return env


def run_cli(args: list[str], cwd: Path, stdin: str | None = None, env: dict | None = None):
    return subprocess.run(
        [sys.executable, "-m", "foo_ai", *args],
        cwd=cwd,
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=clean_env(env),
    )


class Sink:
    def __init__(self):
        self.contents: list[str] = []
        self.reasonings: list[str] = []
        self.names: list[str] = []
        self.results: list[tuple[str, str]] = []
        self.statuses: list[str] = []

    def content(self, text: str) -> None:
        self.contents.append(text)

    def reasoning(self, text: str) -> None:
        self.reasonings.append(text)

    def tool_name(self, name: str) -> None:
        self.names.append(name)

    def tool_result(self, name: str, result: str) -> None:
        self.results.append((name, result))

    def status(self, text: str) -> None:
        self.statuses.append(text)


class ScriptedClient:
    def __init__(self, rounds):
        self.rounds = list(rounds)
        self.seen = []

    def complete(self, messages, tools, sink):
        self.seen.append({"messages": json.loads(json.dumps(messages)), "tools": tools, "sink": sink})
        item = self.rounds.pop(0)
        if isinstance(item, Exception):
            raise item
        if sink is not None and item.content:
            sink.content(item.content)
        for call in item.tool_calls:
            name = call["function"]["name"]
            if sink is not None and name:
                sink.tool_name(name)
        return item


def answer(text: str, usage: dict | None = None) -> Completion:
    return Completion(content=text, reasoning_content="", tool_calls=[], usage=usage)


def tool_call(name: str, arguments: str, call_id: str = "call_0") -> Completion:
    return Completion(
        content="",
        reasoning_content="thinking",
        tool_calls=[
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": arguments},
            }
        ],
    )


class HttpScript(threading.Thread):
    def __init__(self, reply):
        super().__init__(daemon=True)
        self.reply = reply
        self.requests = []
        self._server = None
        self.ready = threading.Event()

    def run(self):
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length)
                body = json.loads(raw.decode("utf-8"))
                parent.requests.append(
                    {
                        "path": self.path,
                        "headers": {key.lower(): value for key, value in self.headers.items()},
                        "body": body,
                    }
                )
                status, content_type, payload = parent.reply(body, parent.requests)
                data = payload if isinstance(payload, bytes) else payload.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, fmt, *args):
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        self.ready.set()
        self._server.serve_forever()

    def start_and_port(self) -> int:
        self.start()
        if not self.ready.wait(5):
            raise RuntimeError("test server did not start")
        return self.port

    def stop(self):
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()


def sse(*events: str) -> bytes:
    lines = []
    for event in events:
        lines.append(f"data: {event}")
    lines.append("data: [DONE]")
    lines.append("")
    return ("\n".join(lines)).encode("utf-8")


class PackageTests(unittest.TestCase):
    def test_version_and_prompt(self):
        self.assertEqual(__version__, "0.1.0")
        self.assertFalse(SYSTEM_PROMPT.endswith("\n"))
        self.assertTrue(SYSTEM_PROMPT.startswith("You are Foo AI, a coding agent.\n"))
        self.assertIn("Use write_file only to create files.", SYSTEM_PROMPT)

    def test_cli_version_and_unknown_flag(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch("sys.stdout", stdout), mock.patch("sys.stderr", stderr):
            code = main(["--version"])
        self.assertEqual(code, 0)
        self.assertEqual(stdout.getvalue(), "Foo AI 0.1.0\n")
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "foo-ai.toml").write_text("this is not toml {\n", encoding="utf-8")
            result = run_cli(["--version"], Path(directory))
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "Foo AI 0.1.0\n")
        result = run_cli(["--not-a-flag"], Path(tempfile.mkdtemp()))
        self.assertEqual(result.returncode, 2)

    def test_interactive_exit_and_reset(self):
        with tempfile.TemporaryDirectory() as directory:
            result = run_cli([], Path(directory), stdin="/reset\n/exit\n")
        self.assertEqual(result.returncode, 0)
        self.assertIn("Foo AI · home · DeepSeek-V4.1-Flash-1017\n", result.stdout)
        self.assertIn("Workspace ", result.stdout)
        self.assertIn("/exit to quit, /reset to clear the conversation.\n", result.stdout)
        self.assertIn("\n> ", result.stdout)
        self.assertIn("Conversation cleared.\n", result.stdout)


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self._env = os.environ.copy()
        for key in list(os.environ):
            if key.startswith("FOO_AI_"):
                del os.environ[key]

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)

    def test_unknown_provider_is_sorted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "foo-ai.toml").write_text('default_provider = "nope"\n', encoding="utf-8")
            with self.assertRaises(ConfigError) as caught:
                load_config(directory=path)
        self.assertEqual(
            str(caught.exception),
            "unknown provider 'nope'; known providers: deepseek-flash_1, home",
        )

    def test_file_env_and_context_window(self):
        text = """
default_provider = "local"

[providers.local]
base_url = "http://127.0.0.1:9/v1"
model = "custom-model"
context_window = "1_000_000"
api_key = "secret"

[providers.blank]
context_window = true

[providers.neg]
context_window = -4

[providers.frac]
context_window = 10.9
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "foo-ai.toml").write_text(text, encoding="utf-8")
            config = load_config(directory=path)
            self.assertEqual(config.provider.name, "local")
            self.assertEqual(config.provider.model, "custom-model")
            self.assertEqual(config.provider.context_window, 1_000_000)
            self.assertEqual(config.provider.api_key, "secret")
            self.assertEqual(
                config.provider.extra_body,
                {"chat_template_kwargs": {"reasoning_effort": "max", "thinking": True}},
            )
            os.environ["FOO_AI_MODEL"] = "override-model"
            os.environ["FOO_AI_BASE_URL"] = "http://127.0.0.1:9/v1"
            os.environ["FOO_AI_API_KEY"] = ""
            os.environ["FOO_AI_PROVIDER"] = "deepseek-flash_1"
            selected = load_config(directory=path)
            self.assertEqual(selected.provider.name, "deepseek-flash_1")
            self.assertEqual(selected.provider.model, "override-model")
            self.assertEqual(selected.provider.api_key, "")
            self.assertEqual(selected.provider.extra_body, {})
            flagged = load_config("home", directory=path)
            self.assertEqual(flagged.provider.name, "home")
            catalog = load_config("blank", directory=path)
            self.assertEqual(catalog.provider.context_window, 0)
            self.assertEqual(load_config("neg", directory=path).provider.context_window, 0)
            self.assertEqual(load_config("frac", directory=path).provider.context_window, 10)

    def test_legacy_provider_and_mcp_table(self):
        text = """
[providers.home]
model = "from-table"

[provider]
name = "legacy"
model = "from-legacy"

[mcp.skip]
enabled = 1
args = [true]

[mcp.off]
enabled = false

[mcp.demo]
enabled = true
command = "python"
args = "server.py"
timeout_seconds = 60.9

[mcp.demo.env]
PYTHONUNBUFFERED = "1"
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "foo-ai.toml").write_text(text, encoding="utf-8")
            config = load_config(directory=path)
        self.assertEqual(config.provider.name, "legacy")
        self.assertEqual(config.provider.model, "from-legacy")
        self.assertEqual(len(config.mcp_servers), 1)
        server = config.mcp_servers[0]
        self.assertEqual(server.name, "demo")
        self.assertEqual(server.args, ["server.py"])
        self.assertEqual(server.timeout, 60)
        self.assertEqual(server.env, {"PYTHONUNBUFFERED": "1"})

    def test_mcp_config_errors(self):
        cases = {
            "mcp = []\n": "mcp must be a table of servers, for example [mcp.whitaker]",
            "[mcp.bad]\nenabled = true\ncommand = 1\n": "mcp bad: command is required",
            "[mcp.bad]\nenabled = true\ncommand = \"x\"\nargs = [true]\n": "mcp bad: args must be an array of strings",
            "[mcp.bad]\nenabled = true\ncommand = \"x\"\ntimeout_seconds = 0\n": "mcp bad: timeout_seconds must be a positive number of seconds",
            "[mcp.bad]\nenabled = true\ncommand = \"x\"\ntimeout_seconds = true\n": "mcp bad: timeout_seconds must be a positive number of seconds",
            "[mcp.bad]\nenabled = true\ncommand = \"x\"\nenv = \"nope\"\n": "mcp bad: env must be a table of strings",
            "mcp = {bad = \"x\"}\n": "mcp bad: expected a table with command and args",
        }
        for text, message in cases.items():
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory)
                (path / "foo-ai.toml").write_text(text, encoding="utf-8")
                with self.assertRaises(ConfigError) as caught:
                    load_config(directory=path)
            self.assertEqual(str(caught.exception), message)

    def test_completions_url(self):
        self.assertEqual(
            completions_url("https://api.deepseek.com/v1"),
            "https://api.deepseek.com/v1/chat/completions",
        )
        self.assertEqual(
            completions_url("http://127.0.0.1:8100/v1/"),
            "http://127.0.0.1:8100/v1/chat/completions",
        )
        self.assertEqual(
            completions_url("http://127.0.0.1:8100/v1/chat/completions"),
            "http://127.0.0.1:8100/v1/chat/completions",
        )
        self.assertEqual(
            completions_url("http://127.0.0.1:8100/v1/chat/completions/"),
            "http://127.0.0.1:8100/v1/chat/completions",
        )


class UsageTests(unittest.TestCase):
    def test_sample_lines(self):
        usage = parse_usage(
            {
                "prompt_tokens": 178100,
                "completion_tokens": 624,
                "prompt_cache_hit_tokens": 175641,
                "prompt_cache_miss_tokens": 2459,
                "completion_tokens_details": {"reasoning_tokens": 10},
            }
        )
        self.assertEqual(
            format_usage_line(usage, 1_000_000),
            "context 178.1K/1M · usage total 178100 624 · cache 98.62%",
        )
        details = parse_usage(
            {
                "prompt_tokens": 1000,
                "completion_tokens": 10,
                "prompt_tokens_details": {"cached_tokens": 600},
            }
        )
        self.assertEqual(
            format_usage_line(details, 0),
            "context 1K · usage total 1000 10 · cache 60.00%",
        )
        self.assertIsNone(format_usage_line(parse_usage({"prompt_tokens": True, "completion_tokens": False}), 100))
        self.assertEqual(parse_usage({"prompt_tokens": 10.9, "completion_tokens": " 12 "}).prompt, 10)
        self.assertEqual(parse_usage({"prompt_tokens": 10.9, "completion_tokens": " 12 "}).completion, 12)
        self.assertEqual(parse_usage({"prompt_tokens": -5, "input_tokens": 3}).prompt, 3)
        self.assertEqual(compact(0), "0")
        self.assertEqual(compact(999), "999")
        self.assertEqual(compact(142000), "142K")
        self.assertEqual(compact(178100), "178.1K")
        self.assertEqual(compact(1_000_000), "1M")
        self.assertEqual(compact(1_500_000), "1.5M")


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name).resolve()
        self.box = Toolbox(self.workspace, confirm=lambda command: True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_schemas_match_the_spec(self):
        names = [item["function"]["name"] for item in TOOL_SCHEMAS]
        self.assertEqual(list(names), ["read_file", "list_dir", "grep", "write_file", "edit_file", "shell"])
        self.assertNotIn("required", TOOL_SCHEMAS[1]["function"]["parameters"])
        self.assertEqual(TOOL_SCHEMAS[0]["function"]["parameters"]["required"], ["path"])

    def test_argument_errors(self):
        self.assertEqual(
            self.box.run("read_file", "not-json"),
            "error: arguments are not JSON (Expecting value: line 1 column 1 (char 0))",
        )
        self.assertEqual(
            self.box.run("read_file", "[1]"),
            "error: arguments are not JSON (arguments must be a JSON object: line 1 column 1 (char 0))",
        )
        self.assertEqual(self.box.run("missing", "{}"), "error: unknown tool 'missing'")
        self.assertEqual(parse_arguments("[1]", mcp=True), "error: arguments must be a JSON object")
        self.assertEqual(parse_arguments("", mcp=False), {})

    def test_read_list_grep_write_edit(self):
        (self.workspace / "sub").mkdir()
        (self.workspace / "sub" / "b.txt").write_bytes(b"B\n")
        (self.workspace / "C").write_text("c\n", encoding="utf-8", newline="\n")
        (self.workspace / ".git").mkdir()
        (self.workspace / ".git" / "hidden.txt").write_text("secret\n", encoding="utf-8")
        (self.workspace / ".foo-ai").mkdir()
        (self.workspace / ".foo-ai" / "session.jsonl").write_text("secret\n", encoding="utf-8")
        listing = self.box.run("list_dir", {"path": "."})
        self.assertEqual(listing.splitlines()[0], ".foo-ai/")
        self.assertIn(".git/", listing.splitlines())
        self.assertLess(listing.splitlines().index(".git/"), listing.splitlines().index("C"))
        written = self.box.run("write_file", {"path": "a.txt", "content": "hello\n"})
        self.assertEqual(
            written,
            "wrote a.txt (6 bytes)\n--- a/a.txt\n+++ b/a.txt\n@@ -0,0 +1 @@\n+hello",
        )
        self.assertEqual((self.workspace / "a.txt").read_bytes(), b"hello\n")
        again = self.box.run("write_file", {"path": "a.txt", "content": "nope"})
        self.assertEqual(again, "error: file exists; use edit_file to change it")
        self.assertEqual((self.workspace / "a.txt").read_bytes(), b"hello\n")
        read = self.box.run("read_file", {"path": "a.txt"})
        self.assertEqual(read, "1|hello\n[1 lines shown, 1 total]")
        edited = self.box.run(
            "edit_file",
            {"path": "a.txt", "old_string": "hello", "new_string": "hello world"},
        )
        self.assertEqual(
            edited,
            "edited a.txt\n--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-hello\n+hello world",
        )
        crlf = self.workspace / "crlf.txt"
        crlf.write_bytes(b"hello\r\nworld\r\n")
        self.box.run(
            "edit_file",
            {"path": "crlf.txt", "old_string": "hello\nworld", "new_string": "hello\nthere"},
        )
        self.assertEqual(crlf.read_bytes(), b"hello\r\nthere\r\n")
        self.assertEqual(
            self.box.run("edit_file", {"path": "a.txt", "old_string": "missing", "new_string": "x"}),
            "error: old_string not found",
        )
        self.assertEqual(
            self.box.run("edit_file", {"path": "a.txt", "old_string": "hello", "new_string": "hello"}),
            "error: old_string and new_string are the same",
        )
        self.assertTrue(self.box.run("grep", {"pattern": "secret"}).startswith("[no matches]"))
        found = self.box.run("grep", {"pattern": "hello", "path": "a.txt"})
        self.assertIn("a.txt:1:hello world", found.replace("\\", "/"))
        self.assertEqual(self.box.run("grep", {"pattern": ""}), "error: pattern is required")
        self.assertTrue(self.box.run("grep", {"pattern": "["}).startswith("error: "))
        escaped = self.box.run("read_file", {"path": "../outside.txt"})
        self.assertTrue(escaped.startswith("error: path escapes the workspace:"))

    def test_read_window_and_truncation(self):
        lines = "\n".join(f"line-{index}" for index in range(1, 6)) + "\n"
        (self.workspace / "many.txt").write_text(lines, encoding="utf-8", newline="\n")
        window = self.box.run("read_file", {"path": "many.txt", "offset": 2, "limit": 2})
        self.assertEqual(window, "2|line-2\n3|line-3\n[2 lines shown, 5 total]")
        self.assertEqual(
            self.box.run("read_file", {"path": "many.txt", "offset": 0, "limit": 0}),
            self.box.run("read_file", {"path": "many.txt"}),
        )
        long = self.box.run("read_file", {"path": "many.txt", "limit": 5000})
        self.assertIn("[5 lines shown, 5 total]", long)
        (self.workspace / "huge.txt").write_text("x" * 25000, encoding="utf-8", newline="\n")
        capped = self.box.run("read_file", {"path": "huge.txt"})
        self.assertTrue(capped.endswith("\n…[truncated]"))
        self.assertEqual(len(capped.split("\n…[truncated]")[0]), 20000)
        empty = self.workspace / "empty.txt"
        empty.write_text("", encoding="utf-8")
        self.assertEqual(self.box.run("read_file", {"path": "empty.txt"}), "[empty file, 0 total]")
        self.assertEqual(self.box.run("read_file", {"path": "missing.txt"}), "error: not a file: missing.txt")
        self.assertEqual(self.box.run("grep", {"pattern": "z", "path": "missing.txt"}), "error: missing.txt")

    def test_grep_match_limit_and_skips(self):
        text = "\n".join(["hit"] * 60) + "\n"
        (self.workspace / "hits.txt").write_text(text, encoding="utf-8", newline="\n")
        result = self.box.run("grep", {"pattern": "hit", "path": "hits.txt"})
        self.assertEqual(result.count("\n"), 50)
        self.assertTrue(result.endswith("\n…[match limit reached]"))
        self.assertEqual(len(result.splitlines()) - 1, 50)

    def test_shell_runs_and_times_out(self):
        script = self.workspace / "say.py"
        script.write_text("print(123)\n", encoding="utf-8", newline="\n")
        command = f"\"{sys.executable}\" \"{script}\""
        result = self.box.run("shell", {"command": command})
        self.assertIn("123", result)
        self.assertTrue(result.endswith("\n[exit 0]"))
        with mock.patch("foo_ai.tools.SHELL_TIMEOUT_SECONDS", 1):
            slow = self.box.run(
                "shell",
                {"command": f"\"{sys.executable}\" -c \"import time; time.sleep(30)\""},
            )
        self.assertIn("timed out after 1 seconds", slow)
        denied = Toolbox(self.workspace, confirm=lambda command: False)
        self.assertEqual(denied.run("shell", {"command": "echo hi"}), "error: command not approved")
        self.assertEqual(self.box.run("shell", {"command": "  "}), "error: command is required")


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name).resolve()
        self.session = Session(self.workspace)

    def tearDown(self):
        self.tmp.cleanup()

    def test_round_trip_repair_and_reset(self):
        self.assertIsNone(self.session.load())
        self.session.add_user("café")
        self.session.add_assistant(
            "",
            "We need to read the file.",
            [
                {
                    "id": "call_0",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{\"path\": \"a.py\"}"},
                }
            ],
        )
        self.session.save()
        raw = self.session.path.read_bytes()
        self.assertNotIn(b"\r\n", raw)
        self.assertIn("café".encode(), raw)
        text = raw.decode("utf-8")
        stored = [json.loads(line) for line in text.splitlines()]
        self.assertEqual(stored[1]["content"], "café")
        self.assertIsNone(stored[2]["content"])
        self.assertEqual(stored[2]["reasoning_content"], "We need to read the file.")
        reloaded = Session(self.workspace)
        note = reloaded.load()
        self.assertEqual(note, "Resumed .foo-ai/session.jsonl (3 messages).")
        self.assertEqual(reloaded.messages[-1]["content"], "error: interrupted before the tool result was saved")
        self.assertEqual(self.session.path.read_text(encoding="utf-8"), text)
        with mock.patch("foo_ai.session.time.strftime", return_value="20261123-140000"):
            cleared = reloaded.reset()
        self.assertEqual(cleared, "Conversation cleared. Archived .foo-ai/session-20261123-140000.jsonl.")
        self.assertFalse(reloaded.path.exists())
        archive = self.workspace / ".foo-ai" / "session-20261123-140000.jsonl"
        self.assertTrue(archive.is_file())
        reloaded.add_user("again")
        reloaded.save()
        with mock.patch("foo_ai.session.time.strftime", return_value="20261123-140000"):
            second = reloaded.reset()
        self.assertIn("session-20261123-140000-2.jsonl", second)

    def test_broken_and_system_replacement(self):
        directory = self.workspace / ".foo-ai"
        directory.mkdir()
        path = directory / "session.jsonl"
        path.write_text('{"role":"system","content":"old","extra":1}\nnot json\n', encoding="utf-8")
        (directory / "session.jsonl.broken").write_text("previous\n", encoding="utf-8")
        note = self.session.load()
        self.assertEqual(note, "Session file was unreadable and was renamed to .foo-ai/session.jsonl.broken.")
        self.assertFalse(path.exists())
        self.assertIn("not json", (directory / "session.jsonl.broken").read_text(encoding="utf-8"))
        path.write_text(
            json.dumps({"extra": 1, "content": "old", "role": "system"}) + "\n"
            + json.dumps({"content": "hi", "role": "user"}) + "\n",
            encoding="utf-8",
        )
        fresh = Session(self.workspace)
        note = fresh.load()
        self.assertEqual(note, "Resumed .foo-ai/session.jsonl (1 messages).")
        self.assertEqual(fresh.messages[0], {"role": "system", "content": SYSTEM_PROMPT})
        empty = directory / "session.jsonl"
        empty.write_text("\n\n", encoding="utf-8")
        quiet = Session(self.workspace)
        self.assertIsNone(quiet.load())
        self.assertFalse(empty.exists())


class ContextTests(unittest.TestCase):
    def test_compression_summary_and_discard(self):
        with tempfile.TemporaryDirectory() as directory:
            session = Session(Path(directory))
            session.add_user("x" * 40000)
            session.add_assistant(
                "",
                "",
                [{"id": "call_0", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}],
            )
            session.add_tool("call_0", "read_file", "y" * 40000)
            session.add_user("latest")
            session.save()
            context = Context()
            sink = Sink()
            client = ScriptedClient([answer("Renamed the function.")])
            context.fit(session, 10_000, client, sink)
            self.assertTrue(any("Compressed old tool results." in line for line in sink.statuses))
            self.assertTrue(any("Summarizing earlier turns." in line for line in sink.statuses))
            self.assertEqual(session.messages[1]["content"], "Earlier conversation, summarized:\nRenamed the function.")
            self.assertEqual(session.messages[-1]["content"], "latest")
            self.assertIsNone(client.seen[0]["tools"])
            self.assertEqual(context.ratio, 0.25)

    def test_summary_failure_discards_above_the_hard_ceiling(self):
        with tempfile.TemporaryDirectory() as directory:
            session = Session(Path(directory))
            session.add_user("x" * 40000)
            session.add_user("latest")
            context = Context()
            sink = Sink()
            client = ScriptedClient([ChatError("boom")])
            context.fit(session, 10_000, client, sink)
            self.assertTrue(any("Summary skipped: boom" in line for line in sink.statuses))
            self.assertTrue(any("Discarded earlier turns." in line for line in sink.statuses))
            self.assertEqual(session.messages[1]["content"], "Earlier turns were removed to fit the context window.")
            self.assertEqual(session.messages[-1]["content"], "latest")

    def test_below_hard_ceiling_keeps_the_transcript(self):
        with tempfile.TemporaryDirectory() as directory:
            session = Session(Path(directory))
            session.add_user("x" * 35000)
            session.add_user("latest")
            context = Context()
            sink = Sink()
            client = ScriptedClient([ChatError("boom")])
            context.fit(session, 10_000, client, sink)
            self.assertFalse(any("Discarded" in line for line in sink.statuses))
            self.assertEqual(session.messages[1]["content"], "x" * 35000)

    def test_ratio_bounds(self):
        context = Context()
        messages = [{"role": "user", "content": "x" * 10000}]
        context.observe(messages, {"prompt_tokens": 510})
        self.assertAlmostEqual(context.ratio, 0.051)
        context.ratio = 0.25
        context.observe(messages, {"prompt_tokens": 500})
        self.assertEqual(context.ratio, 0.25)
        context.observe(messages, {"prompt_tokens": 20000})
        self.assertEqual(context.ratio, 0.25)
        context.observe([], {"prompt_tokens": 10})
        self.assertEqual(context.ratio, 0.25)
        self.assertGreater(transcript_chars(messages), 0)


class AgentTests(unittest.TestCase):
    def _agent(self, rounds, max_steps=16, yes=True):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        workspace = Path(tmp.name).resolve()
        session = Session(workspace)
        sink = Sink()
        client = ScriptedClient(rounds)
        provider = Provider("home", "http://127.0.0.1:9/v1", "test", 1_000_000, "", {})
        agent = Agent(
            provider=provider,
            session=session,
            toolbox=Toolbox(workspace, confirm=lambda command: yes),
            client=client,
            sink=sink,
            max_steps=max_steps,
        )
        return agent, session, sink, client, workspace

    def test_empty_limit_and_tool_round(self):
        agent, session, sink, client, workspace = self._agent([])
        self.assertEqual(agent.ask("   "), "")
        self.assertFalse((workspace / ".foo-ai" / "session.jsonl").exists())
        agent, session, sink, client, workspace = self._agent([], max_steps=0)
        self.assertEqual(agent.ask("hello"), "Stopped: tool-call limit reached before a final answer.")
        self.assertEqual(sink.contents, ["Stopped: tool-call limit reached before a final answer."])
        stored = [json.loads(line) for line in (workspace / ".foo-ai" / "session.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([item["role"] for item in stored], ["system", "user"])
        agent, session, sink, client, workspace = self._agent(
            [
                tool_call("read_file", "{\"path\": \"note.txt\"}"),
                answer("Done.", {"prompt_tokens": 20, "completion_tokens": 2}),
            ]
        )
        (workspace / "note.txt").write_text("hello\n", encoding="utf-8", newline="\n")
        self.assertEqual(agent.ask("read it"), "Done.")
        self.assertEqual(sink.names, ["read_file"])
        self.assertEqual(client.seen[0]["tools"], client.seen[1]["tools"])
        self.assertTrue(client.seen[0]["tools"])
        self.assertIn("1|hello", sink.results[0][1])
        self.assertTrue(any("context " in line for line in sink.statuses))

    def test_provider_error_does_not_save_a_partial_answer(self):
        class Partial(ChatError):
            pass

        agent, session, sink, client, workspace = self._agent([ChatError("HTTP 500 from http://x: boom")])

        def fail(messages, tools, sink):
            if sink is not None:
                sink.content("partial")
            raise ChatError("HTTP 500 from http://x: boom")

        client.complete = fail
        with self.assertRaises(ChatError):
            agent.ask("hello")
        text = (workspace / ".foo-ai" / "session.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("partial", text)
        self.assertEqual(json.loads(text.splitlines()[-1])["role"], "user")

    def test_repair_is_saved_before_the_new_user_message(self):
        agent, session, sink, client, workspace = self._agent([answer("ok")])
        session.messages.append(
            {
                "role": "assistant",
                "content": None,
                "reasoning_content": "",
                "tool_calls": [
                    {"id": "call_0", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}
                ],
            }
        )
        saves = []
        original = session.save

        def wrapped():
            original()
            saves.append(json.loads(json.dumps(session.messages)))

        session.save = wrapped
        agent.ask("continue")
        self.assertEqual(saves[0][-1]["content"], "error: interrupted before the tool result was saved")
        self.assertNotIn("continue", json.dumps(saves[0]))
        self.assertEqual(saves[1][-1]["content"], "continue")


class ClientTests(unittest.TestCase):
    def test_stream_tools_usage_and_errors(self):
        def reply(body, requests):
            if len(requests) == 1:
                self.assertEqual(body["model"], "DeepSeek-V4.1-Flash-1017")
                self.assertTrue(body["stream"])
                self.assertEqual(body["stream_options"], {"include_usage": True})
                self.assertEqual(
                    body["chat_template_kwargs"],
                    {"reasoning_effort": "max", "thinking": True},
                )
                self.assertEqual(len(body["tools"]), 6)
                payload = sse(
                    json.dumps({"choices": [{"delta": {"reasoning_content": "think "}}]}),
                    json.dumps({"choices": [{"delta": {"content": "Hi"}}]}),
                    json.dumps(
                        {
                            "choices": [
                                {
                                    "delta": {
                                        "tool_calls": [
                                            {"index": 1, "id": "a", "function": {"name": "read"}},
                                            {"index": 0, "function": {"name": "list_", "arguments": "{\"path\":"}},
                                        ]
                                    }
                                }
                            ]
                        }
                    ),
                    json.dumps(
                        {
                            "choices": [
                                {
                                    "delta": {
                                        "tool_calls": [
                                            {"index": 1, "function": {"name": "_file", "arguments": 1}},
                                            {"index": 0, "function": {"arguments": "\\\"a\\\"}"}},
                                        ]
                                    }
                                }
                            ]
                        }
                    ),
                    json.dumps(
                        {
                            "choices": [],
                            "usage": {"prompt_tokens": 178100, "completion_tokens": 624},
                        }
                    ),
                )
                return 200, "text/event-stream", payload
            return 500, "text/plain", "x" * 2500

        server = HttpScript(reply)
        port = server.start_and_port()
        self.addCleanup(server.stop)
        provider = Provider(
            "home",
            f"http://127.0.0.1:{port}/v1",
            "DeepSeek-V4.1-Flash-1017",
            1_000_000,
            "",
            {"chat_template_kwargs": {"reasoning_effort": "max", "thinking": True}},
        )
        sink = Sink()
        completion = Client(provider).complete([{"role": "user", "content": "hi"}], TOOL_SCHEMAS, sink)
        self.assertEqual(completion.content, "Hi")
        self.assertEqual(completion.reasoning_content, "think ")
        self.assertEqual([call["function"]["name"] for call in completion.tool_calls], ["read_file", "list_"])
        self.assertEqual(completion.tool_calls[0]["id"], "a")
        self.assertEqual(completion.tool_calls[0]["function"]["arguments"], "1")
        self.assertEqual(sink.names, ["read", "list_"])
        self.assertEqual(completion.usage["prompt_tokens"], 178100)
        with self.assertRaises(ChatError) as caught:
            Client(provider).complete([{"role": "user", "content": "hi"}], None, sink)
        self.assertIn("HTTP 500 from ", str(caught.exception))
        self.assertEqual(len(str(caught.exception).split(": ", 1)[1]), 2000)
        self.assertNotIn("tools", server.requests[1]["body"])
        self.assertNotIn("authorization", server.requests[0]["headers"])

    def test_json_body_and_connection_error(self):
        def reply(body, requests):
            return (
                200,
                "application/json",
                json.dumps(
                    {
                        "choices": [
                            {
                                "message": {
                                    "content": "Done.",
                                    "tool_calls": [
                                        {"type": "function", "function": {"name": "shell", "arguments": "{\"command\": \"echo\"}"}}
                                    ],
                                }
                            }
                        ],
                        "usage": {"prompt_tokens": 4, "completion_tokens": 1},
                    }
                ),
            )

        server = HttpScript(reply)
        port = server.start_and_port()
        self.addCleanup(server.stop)
        provider = Provider(
            "home",
            f"http://127.0.0.1:{port}/v1/",
            "DeepSeek-V4.1-Flash-1017",
            1_000_000,
            "secret",
            {},
        )
        completion = Client(provider).complete([{"role": "user", "content": "hi"}], None, None)
        self.assertEqual(completion.content, "Done.")
        self.assertEqual(completion.tool_calls[0]["id"], "call_0")
        self.assertEqual(server.requests[0]["path"], "/v1/chat/completions")
        self.assertEqual(server.requests[0]["headers"]["authorization"], "Bearer secret")
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        closed = sock.getsockname()[1]
        sock.close()
        provider.base_url = f"http://127.0.0.1:{closed}/v1"
        with self.assertRaises(ChatError) as caught:
            Client(provider).complete([], None, None)
        self.assertIn(f"cannot reach http://127.0.0.1:{closed}/v1/chat/completions:", str(caught.exception))


class McpTests(unittest.TestCase):
    def _pool(self, mode: str, name: str = "demo", **extra) -> McpPool:
        command = sys.executable
        args = ["-u", str(ECHO_MCP)]
        env = clean_env({"FOO_MCP_MODE": mode})
        from foo_ai.config import McpSpec

        spec = McpSpec(name=name, command=command, args=args, timeout=5, cwd=None, env=env)
        if extra.get("blank"):
            spec.command = " "
        pool = McpPool([spec])
        self.addCleanup(pool.close)
        return pool

    def test_connect_call_and_names(self):
        pool = self._pool("echo")
        lines = pool.connect()
        self.assertEqual(lines, ["mcp demo: echo"])
        self.assertEqual(pool.connect(), lines)
        self.assertEqual(len(pool.servers), 1)
        schema = pool.schemas()[0]["function"]
        self.assertEqual(schema["name"], "mcp__demo__echo")
        self.assertEqual(schema["description"], "[demo] Echo.")
        self.assertNotIn("$schema", schema["parameters"])
        self.assertEqual(pool.call("mcp__demo__echo", {"text": "ave"}), "ave")
        parts = pool.call("mcp__demo__echo", json.dumps({"kind": "parts"}))
        self.assertEqual(parts, "a\n[image]\nbody")
        self.assertEqual(pool.call("mcp__demo__echo", json.dumps({"kind": "error"})), "error: nope")
        self.assertEqual(pool.call("mcp__demo__echo", json.dumps({"kind": "empty"})), "[empty tool result]")
        self.assertEqual(pool.call("mcp__demo__echo", json.dumps({"kind": "fail-empty"})), "error: tool failed")
        self.assertEqual(pool.call("mcp__demo__echo", "[1]"), "error: arguments must be a JSON object")
        self.assertEqual(advertise_name("a b", "c/d"), "mcp__a_b__c_d")
        long_tool = "t" * 80
        advertised = advertise_name("server", long_tool)
        self.assertLessEqual(len(advertised), 64)
        self.assertRegex(advertised, r"^[A-Za-z0-9_-]+$")
        other = advertise_name("server", "t" * 79 + "u")
        self.assertNotEqual(advertised, other)

    def test_pages_collision_and_failures(self):
        pages = self._pool("pages", name="demo")
        self.assertEqual(pages.connect(), ["mcp demo: one, two"])
        empty = self._pool("empty", name="demo")
        self.assertEqual(empty.connect(), ["mcp demo: connected, no tools"])
        collide = self._pool("collide", name="demo")
        self.assertEqual(collide.connect(), ["mcp demo: a b"])
        missing = self._pool("echo", name="demo", blank=True)
        self.assertEqual(missing.connect(), ["mcp demo: unavailable (command is required)"])
        dead = self._pool("exit", name="other")
        line = dead.connect()[0]
        self.assertTrue(line.startswith("mcp other: unavailable ("))
        self.assertIn("exit 3", line)
        gone = McpPool(
            [
                __import__("foo_ai.config", fromlist=["McpSpec"]).McpSpec(
                    name="nope",
                    command="definitely-not-a-real-foo-ai-command",
                    args=[],
                    timeout=5,
                    cwd=None,
                    env=None,
                )
            ]
        )
        self.addCleanup(gone.close)
        self.assertIn("cannot start definitely-not-a-real-foo-ai-command", gone.connect()[0])

    def test_render_resource_without_text(self):
        self.assertEqual(
            render_tool_result({"content": [{"type": "resource", "resource": {"uri": "file://a"}}]}),
            "[resource file://a]",
        )


class CliIntegrationTests(unittest.TestCase):
    def test_one_shot_answer_tools_and_provider_error(self):
        state = {"calls": 0}

        def reply(body, requests):
            state["calls"] += 1
            if state["calls"] == 1:
                payload = sse(
                    json.dumps(
                        {
                            "choices": [
                                {
                                    "delta": {
                                        "tool_calls": [
                                            {
                                                "index": 0,
                                                "id": "call_0",
                                                "function": {
                                                    "name": "write_file",
                                                    "arguments": json.dumps({"path": "a.txt", "content": "hello\n"}),
                                                },
                                            }
                                        ]
                                    }
                                }
                            ],
                            "usage": {
                                "prompt_tokens": 178100,
                                "completion_tokens": 624,
                                "prompt_cache_hit_tokens": 175641,
                                "prompt_cache_miss_tokens": 2459,
                            },
                        }
                    )
                )
                return 200, "text/event-stream", payload
            payload = sse(
                json.dumps({"choices": [{"delta": {"content": "Done."}}], "usage": {"prompt_tokens": 10, "completion_tokens": 1}})
            )
            return 200, "text/event-stream", payload

        server = HttpScript(reply)
        port = server.start_and_port()
        self.addCleanup(server.stop)
        with tempfile.TemporaryDirectory() as directory:
            result = run_cli(
                ["--yes", "create a file"],
                Path(directory),
                env={"FOO_AI_BASE_URL": f"http://127.0.0.1:{port}/v1", "FOO_AI_MODEL": "test-model"},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Foo AI · home · test-model\n", result.stdout)
            self.assertNotIn("Workspace ", result.stdout)
            self.assertIn("\n→ write_file", result.stdout)
            self.assertIn("wrote a.txt (6 bytes)", result.stdout)
            self.assertIn("Done.\n", result.stdout)
            self.assertIn("context 178.1K/1M · usage total 178100 624 · cache 98.62%", result.stderr)
            self.assertEqual((Path(directory) / "a.txt").read_bytes(), b"hello\n")
            stored = (Path(directory) / ".foo-ai" / "session.jsonl").read_text(encoding="utf-8")
            self.assertIn('"name": "write_file"', stored)
            self.assertIn('"content": "Done."', stored)
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        closed = sock.getsockname()[1]
        sock.close()
        with tempfile.TemporaryDirectory() as directory:
            failed = run_cli(
                ["say hi"],
                Path(directory),
                env={"FOO_AI_BASE_URL": f"http://127.0.0.1:{closed}/v1"},
            )
        self.assertEqual(failed.returncode, 1)
        self.assertIn("cannot reach ", failed.stderr)
        self.assertNotIn("\n\n", failed.stdout[-2:])

    def test_shell_is_skipped_when_stdin_is_not_a_terminal(self):
        def reply(body, requests):
            if len(requests) == 1:
                payload = sse(
                    json.dumps(
                        {
                            "choices": [
                                {
                                    "delta": {
                                        "tool_calls": [
                                            {
                                                "index": 0,
                                                "id": "call_0",
                                                "function": {
                                                    "name": "shell",
                                                    "arguments": json.dumps(
                                                        {
                                                            "command": f"\"{sys.executable}\" -c \"open('ran.txt','w').write('x')\""
                                                        }
                                                    ),
                                                },
                                            }
                                        ]
                                    }
                                }
                            ]
                        }
                    )
                )
                return 200, "text/event-stream", payload
            return 200, "text/event-stream", sse(json.dumps({"choices": [{"delta": {"content": "skipped"}}]}))

        server = HttpScript(reply)
        port = server.start_and_port()
        self.addCleanup(server.stop)
        with tempfile.TemporaryDirectory() as directory:
            result = run_cli(
                ["run it"],
                Path(directory),
                stdin="",
                env={"FOO_AI_BASE_URL": f"http://127.0.0.1:{port}/v1"},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Skipped: pass --yes to run shell commands.", result.stdout)
            self.assertIn("← shell (", result.stdout)
            self.assertFalse((Path(directory) / "ran.txt").exists())
            self.assertIn("skipped\n", result.stdout)


if __name__ == "__main__":
    unittest.main()
