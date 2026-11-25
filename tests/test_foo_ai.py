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
import tomllib
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from foo_ai import __version__
from foo_ai.agent import Agent
from foo_ai.cli import build_parser, main
from foo_ai.client import ChatError, Client, Completion, _normalize_calls
from foo_ai.config import (
    ConfigError,
    Provider,
    completions_url,
    known_providers,
    load_config,
    load_page_settings,
    load_page_workspace,
    merge_reasoning,
    save_page_settings,
    save_page_workspace,
    user_config_path,
    write_provider_settings,
)
from foo_ai.context import Context, transcript_chars
from foo_ai.mcp import McpPool, advertise_name, render_tool_result
from foo_ai.prompt import SYSTEM_PROMPT
from foo_ai.session import Session
from foo_ai.skill import RestrictedToolbox, command_allowed, load_skills, skills_banner, system_prompt
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
        self.assertIsNone(build_parser().get_default("max_steps"))
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
        result = run_cli(["--max-steps", "0"], Path(tempfile.mkdtemp()))
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr, "max_steps must be a positive integer\n")

    def test_workspace_config_error_exits_before_banner(self):
        with tempfile.TemporaryDirectory() as launch, tempfile.TemporaryDirectory() as workspace:
            (Path(workspace) / ".foo-ai.toml").write_text("[mcp.bad]\nenabled = true\n", encoding="utf-8")
            result = run_cli(["--workspace", workspace], Path(launch))
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr, "mcp bad: command is required\n")
        self.assertNotIn("Foo AI", result.stdout)

    def test_interactive_exit_and_reset(self):
        with tempfile.TemporaryDirectory() as directory:
            result = run_cli([], Path(directory), stdin="/reset\n/exit\n")
        self.assertEqual(result.returncode, 0)
        self.assertIn("Foo AI · home · DeepSeek-V4.1-Flash-1017\n", result.stdout)
        self.assertIn("Workspace ", result.stdout)
        self.assertIn("/exit to quit, /reset to clear the conversation.\n", result.stdout)
        self.assertIn("Skills: /explore, /review.\n", result.stdout)
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

    def test_workspace_mcp_overlay(self):
        launch = """
default_provider = "home"

[providers.home]
model = "from-launch"

[mcp.whitaker]
enabled = false
command = "docker"
args = ["exec", "-i", "whitaker-mcp", "python"]
timeout_seconds = 120

[mcp.demo]
enabled = true
command = "python"
args = ["demo.py"]

[mcp.demo.env]
KEEP = "1"

[mcp.broken]
enabled = true
command = "python"
args = [true]
"""
        overlay = """
default_provider = "deepseek-flash_1"

[providers.home]
model = "from-workspace"

[mcp.whitaker]
enabled = true

[mcp.demo.env]
ADDED = "3"

[mcp.broken]
enabled = false

[mcp.extra]
enabled = true
command = "python"
args = ["extra.py"]
timeout_seconds = 15

[mcp.skip]
enabled = 1
args = [true]
"""
        with tempfile.TemporaryDirectory() as launch_dir, tempfile.TemporaryDirectory() as workspace_dir:
            launch_path = Path(launch_dir)
            workspace = Path(workspace_dir)
            (launch_path / "foo-ai.toml").write_text(
                '[mcp.demo]\nenabled = true\ncommand = "python"\nargs = ["demo.py"]\n',
                encoding="utf-8",
            )
            untouched = load_config(directory=launch_path, workspace=workspace)
            self.assertEqual([server.name for server in untouched.mcp_servers], ["demo"])
            (launch_path / "foo-ai.toml").write_text(launch, encoding="utf-8")
            (workspace / ".foo-ai.toml").write_text(overlay, encoding="utf-8")
            config = load_config(directory=launch_path, workspace=workspace)
        self.assertEqual(config.provider.name, "home")
        self.assertEqual(config.provider.model, "from-launch")
        self.assertEqual([server.name for server in config.mcp_servers], ["whitaker", "demo", "extra"])
        whitaker = config.mcp_servers[0]
        self.assertEqual(whitaker.command, "docker")
        self.assertEqual(whitaker.args, ["exec", "-i", "whitaker-mcp", "python"])
        self.assertEqual(whitaker.timeout, 120)
        self.assertEqual(config.mcp_servers[1].env, {"ADDED": "3"})
        self.assertEqual(config.mcp_servers[2].args, ["extra.py"])
        self.assertEqual(config.mcp_servers[2].timeout, 15)

    def test_workspace_mcp_overlay_errors(self):
        with tempfile.TemporaryDirectory() as launch_dir, tempfile.TemporaryDirectory() as workspace_dir:
            launch_path = Path(launch_dir)
            workspace = Path(workspace_dir)
            (launch_path / "foo-ai.toml").write_text('default_provider = "home"\n', encoding="utf-8")
            (workspace / ".foo-ai.toml").write_text("this is not toml {\n", encoding="utf-8")
            with self.assertRaises(ConfigError) as caught:
                load_config(directory=launch_path, workspace=workspace)
            self.assertTrue(str(caught.exception).startswith(".foo-ai.toml: "))
            (workspace / ".foo-ai.toml").write_text("mcp = []\n", encoding="utf-8")
            with self.assertRaises(ConfigError) as caught:
                load_config(directory=launch_path, workspace=workspace)
            self.assertEqual(
                str(caught.exception),
                ".foo-ai.toml: mcp must be a table of servers, for example [mcp.whitaker]",
            )
            (workspace / ".foo-ai.toml").write_text("[mcp.whitaker]\nenabled = true\n", encoding="utf-8")
            with self.assertRaises(ConfigError) as caught:
                load_config(directory=launch_path, workspace=workspace)
            self.assertEqual(str(caught.exception), "mcp whitaker: command is required")

    def test_max_steps_from_files(self):
        with tempfile.TemporaryDirectory() as launch_dir, tempfile.TemporaryDirectory() as workspace_dir:
            launch = Path(launch_dir)
            workspace = Path(workspace_dir)
            self.assertEqual(load_config(directory=launch, workspace=workspace).max_steps, 200)
            (launch / "foo-ai.toml").write_text("max_steps = 50\n", encoding="utf-8")
            self.assertEqual(load_config(directory=launch, workspace=workspace).max_steps, 50)
            (workspace / ".foo-ai.toml").write_text("max_steps = 80\n", encoding="utf-8")
            self.assertEqual(load_config(directory=launch, workspace=workspace).max_steps, 80)
            (workspace / ".foo-ai.toml").write_text("max_steps = 0\n", encoding="utf-8")
            with self.assertRaises(ConfigError) as caught:
                load_config(directory=launch, workspace=workspace)
            self.assertEqual(str(caught.exception), ".foo-ai.toml: max_steps must be a positive integer")
            (launch / "foo-ai.toml").write_text("max_steps = true\n", encoding="utf-8")
            with self.assertRaises(ConfigError) as caught:
                load_config(directory=launch)
            self.assertEqual(str(caught.exception), "max_steps must be a positive integer")

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


class SettingsFileTests(unittest.TestCase):
    """The file the desktop page writes: where it sits, what it overrides, how it is written."""

    def test_the_path_follows_the_environment(self):
        with tempfile.TemporaryDirectory() as folder:
            with mock.patch.dict(os.environ, {"FOO_AI_CONFIG_DIR": folder}):
                self.assertEqual(user_config_path(), Path(folder) / "foo-ai.toml")

    def test_the_page_settings_override_the_launch_file(self):
        with tempfile.TemporaryDirectory() as folder:
            launch = Path(folder)
            (launch / "foo-ai.toml").write_text(
                'default_provider = "home"\n[providers.home]\nmodel = "from-launch"\n',
                encoding="utf-8",
                newline="\n",
            )
            settings = launch / "settings.toml"
            settings.write_text(
                'default_provider = "home"\n'
                '[providers.home]\nbase_url = "http://127.0.0.1:9/v1"\nmodel = "from-page"\n',
                encoding="utf-8",
                newline="\n",
            )
            config = load_config(directory=launch, user_config=settings)
        self.assertEqual(config.provider.name, "home")
        self.assertEqual(config.provider.model, "from-page")
        self.assertEqual(config.provider.base_url, "http://127.0.0.1:9/v1")

    def test_a_provider_the_page_does_not_name_still_comes_from_the_launch_file(self):
        with tempfile.TemporaryDirectory() as folder:
            launch = Path(folder)
            (launch / "foo-ai.toml").write_text(
                '[providers.deepseek-flash_1]\nmodel = "from-launch"\n',
                encoding="utf-8",
                newline="\n",
            )
            settings = launch / "settings.toml"
            settings.write_text(
                '[providers.home]\nmodel = "from-page"\n', encoding="utf-8", newline="\n"
            )
            deepseek = load_config("deepseek-flash_1", directory=launch, user_config=settings)
            home = load_config("home", directory=launch, user_config=settings)
        self.assertEqual(deepseek.provider.model, "from-launch")
        self.assertEqual(home.provider.model, "from-page")

    def test_the_cli_does_not_read_the_page_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            page = home / "page-settings"
            page.mkdir()
            (page / "foo-ai.toml").write_text(
                'default_provider = "deepseek-flash_1"\n'
                '[providers.deepseek-flash_1]\nmodel = "from-page"\n',
                encoding="utf-8",
                newline="\n",
            )
            with mock.patch.dict(os.environ, {"FOO_AI_CONFIG_DIR": str(page)}):
                config = load_config(directory=home)
        self.assertEqual(config.provider.name, "home")
        self.assertEqual(config.provider.model, "DeepSeek-V4.1-Flash-1017")

    def test_a_missing_settings_file_changes_nothing(self):
        with tempfile.TemporaryDirectory() as folder:
            config = load_config(directory=Path(folder), user_config=Path(folder) / "nothing.toml")
        self.assertEqual(config.provider.name, "home")
        self.assertEqual(config.provider.model, "DeepSeek-V4.1-Flash-1017")

    def test_the_settings_file_carries_no_mcp_servers_and_no_budget(self):
        with tempfile.TemporaryDirectory() as folder:
            settings = Path(folder) / "settings.toml"
            settings.write_text(
                'max_steps = 7\n[mcp.demo]\nenabled = true\ncommand = "python"\n',
                encoding="utf-8",
                newline="\n",
            )
            config = load_config(directory=Path(folder), user_config=settings)
        self.assertEqual(config.mcp_servers, [])
        self.assertEqual(config.max_steps, 200)

    def test_a_broken_settings_file_names_the_file(self):
        with tempfile.TemporaryDirectory() as folder:
            settings = Path(folder) / "settings.toml"
            settings.write_text("providers = 3\n", encoding="utf-8", newline="\n")
            with self.assertRaises(ConfigError) as caught:
                load_config(directory=Path(folder), user_config=settings)
        self.assertIn(str(settings), str(caught.exception))
        self.assertIn("providers must be a table", str(caught.exception))

    def test_the_settings_file_names_the_providers_it_adds(self):
        with tempfile.TemporaryDirectory() as folder:
            settings = Path(folder) / "settings.toml"
            settings.write_text('[providers.work]\nmodel = "m"\n', encoding="utf-8", newline="\n")
            self.assertEqual(
                known_providers(Path(folder), user_config=settings),
                ["deepseek-flash_1", "home", "work"],
            )

    def test_writing_a_provider_round_trips(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "nested" / "foo-ai.toml"
            write_provider_settings(
                path,
                "deepseek-flash_1",
                base_url="https://api.deepseek.com/v1",
                model="deepseek-flash",
                context_window=128_000,
                api_key="sk-test",
            )
            text = path.read_text(encoding="utf-8")
            config = load_config(directory=Path(folder), user_config=path)
        self.assertEqual(tomllib.loads(text)["default_provider"], "deepseek-flash_1")
        self.assertEqual(config.provider.name, "deepseek-flash_1")
        self.assertEqual(config.provider.base_url, "https://api.deepseek.com/v1")
        self.assertEqual(config.provider.model, "deepseek-flash")
        self.assertEqual(config.provider.context_window, 128_000)
        self.assertEqual(config.provider.api_key, "sk-test")
        self.assertNotIn("extra_body", tomllib.loads(text)["providers"]["deepseek-flash_1"])

    def test_writing_home_records_the_thinking_flags_the_request_sends(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "foo-ai.toml"
            write_provider_settings(
                path,
                "home",
                base_url="http://100.109.56.33:8100/v1",
                model="DeepSeek-V4.1-Flash-1017",
                context_window=1_000_000,
                extra_body={"chat_template_kwargs": {"reasoning_effort": "max", "thinking": True}},
            )
            saved = tomllib.loads(path.read_text(encoding="utf-8"))
            write_provider_settings(
                path,
                "home",
                base_url="http://100.109.56.33:8100/v1",
                model="DeepSeek-V4.1-Flash-1017",
                context_window=1_000_000,
                extra_body={"chat_template_kwargs": {"reasoning_effort": "low", "thinking": False}},
            )
            kept = tomllib.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(
            saved["providers"]["home"]["extra_body"],
            {"chat_template_kwargs": {"reasoning_effort": "max", "thinking": True}},
        )
        self.assertEqual(kept["providers"]["home"]["extra_body"], saved["providers"]["home"]["extra_body"])

    def test_merge_reasoning_edits_only_the_two_fields_the_sheet_shows(self):
        body = {
            "temperature": 0.2,
            "chat_template_kwargs": {
                "reasoning_effort": "max",
                "thinking": True,
                "enable_thinking": False,
            },
        }
        merged = merge_reasoning(body, {"reasoning_effort": "low", "thinking": False})
        self.assertEqual(
            merged,
            {
                "temperature": 0.2,
                "chat_template_kwargs": {
                    "reasoning_effort": "low",
                    "thinking": False,
                    "enable_thinking": False,
                },
            },
        )
        # The caller's body is left as it was.
        self.assertEqual(body["chat_template_kwargs"]["reasoning_effort"], "max")

    def test_merge_reasoning_drops_a_blank_effort(self):
        body = {"chat_template_kwargs": {"reasoning_effort": "max", "thinking": True}}
        merged = merge_reasoning(body, {"reasoning_effort": "", "thinking": True})
        self.assertEqual(merged, {"chat_template_kwargs": {"thinking": True}})
        only_thinking = merge_reasoning({}, {"thinking": False})
        self.assertEqual(only_thinking, {"chat_template_kwargs": {"thinking": False}})

    def test_writing_the_edited_body_replaces_the_stored_one(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "foo-ai.toml"
            path.write_text(
                "[providers.home.extra_body]\n"
                'stream_options = { include_usage = true }\n\n'
                "[providers.home.extra_body.chat_template_kwargs]\n"
                "thinking = true\n",
                encoding="utf-8",
                newline="\n",
            )
            write_provider_settings(
                path,
                "home",
                base_url="http://127.0.0.1:9/v1",
                model="m",
                context_window=1000,
                extra_body={
                    "stream_options": {"include_usage": True},
                    "chat_template_kwargs": {"reasoning_effort": "low", "thinking": False},
                },
                replace_extra_body=True,
            )
            saved = tomllib.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(
            saved["providers"]["home"]["extra_body"],
            {
                "stream_options": {"include_usage": True},
                "chat_template_kwargs": {"reasoning_effort": "low", "thinking": False},
            },
        )

    def test_writing_keeps_the_other_tables_and_the_stored_key(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "foo-ai.toml"
            path.write_text(
                'default_provider = "home"\n\n'
                '[providers.home]\nmodel = "kept"\napi_key = "kept-key"\n\n'
                "[providers.home.extra_body.chat_template_kwargs]\nthinking = true\n",
                encoding="utf-8",
                newline="\n",
            )
            write_provider_settings(
                path,
                "home",
                base_url="http://127.0.0.1:9/v1",
                model="changed",
                context_window=0,
            )
            kept = tomllib.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(kept["providers"]["home"]["api_key"], "kept-key")
            self.assertEqual(
                kept["providers"]["home"]["extra_body"],
                {"chat_template_kwargs": {"thinking": True}},
            )
            write_provider_settings(
                path,
                "home",
                base_url="http://127.0.0.1:9/v1",
                model="changed",
                context_window=0,
                api_key="",
            )
            cleared = tomllib.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(cleared["default_provider"], "home")
        self.assertEqual(cleared["providers"]["home"]["model"], "changed")
        self.assertEqual(cleared["providers"]["home"]["api_key"], "")

    def test_the_page_options_round_trip(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "foo-ai.toml"
            save_page_settings(path, thinking=True, verbose=False, yes=True)
            self.assertEqual(
                load_page_settings(path),
                {"thinking": True, "verbose": False, "yes": True},
            )
            write_provider_settings(
                path,
                "home",
                base_url="http://127.0.0.1:9/v1",
                model="m",
                context_window=1,
            )
            kept = tomllib.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(kept["page"], {"thinking": True, "verbose": False, "yes": True})
            self.assertEqual(kept["providers"]["home"]["model"], "m")
            save_page_settings(path, thinking=False, verbose=True, yes=False)
            self.assertEqual(
                load_page_settings(path),
                {"thinking": False, "verbose": True, "yes": False},
            )
            saved = tomllib.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["providers"]["home"]["model"], "m")

    def test_a_missing_settings_file_leaves_the_options_off(self):
        with tempfile.TemporaryDirectory() as folder:
            missing = Path(folder) / "nothing.toml"
            self.assertEqual(
                load_page_settings(missing),
                {"thinking": False, "verbose": False, "yes": False},
            )
        self.assertEqual(load_page_settings(None)["thinking"], False)

    def test_a_non_boolean_option_stays_off(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "foo-ai.toml"
            path.write_text(
                '[page]\nthinking = "yes"\nverbose = 1\nyes = true\n',
                encoding="utf-8",
                newline="\n",
            )
            self.assertEqual(
                load_page_settings(path),
                {"thinking": False, "verbose": False, "yes": True},
            )

    def test_a_page_that_is_not_a_table_names_the_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "foo-ai.toml"
            path.write_text("page = 3\n", encoding="utf-8", newline="\n")
            with self.assertRaises(ConfigError) as caught:
                load_page_settings(path)
        self.assertIn(str(path), str(caught.exception))
        self.assertIn("page must be a table", str(caught.exception))

    def test_the_page_workspace_round_trips_beside_the_providers(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "foo-ai.toml"
            workspace = Path(folder) / "project"
            workspace.mkdir()
            self.assertIsNone(load_page_workspace(path))
            save_page_workspace(path, workspace)
            self.assertEqual(load_page_workspace(path), workspace)
            write_provider_settings(
                path,
                "home",
                base_url="http://127.0.0.1:9/v1",
                model="m",
                context_window=1,
            )
            save_page_settings(path, thinking=True, verbose=False, yes=True)
            self.assertEqual(load_page_workspace(path), workspace)
            kept = tomllib.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(Path(kept["workspace"]), workspace)
            self.assertEqual(kept["default_provider"], "home")
            self.assertEqual(kept["page"], {"thinking": True, "verbose": False, "yes": True})

    def test_a_workspace_value_that_is_not_a_folder_name_reads_as_none(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "foo-ai.toml"
            self.assertIsNone(load_page_workspace(None))
            for text in ('workspace = "   "\n', "workspace = 7\n", 'workspace = ""\n'):
                path.write_text(text, encoding="utf-8", newline="\n")
                self.assertIsNone(load_page_workspace(path), text)

    def test_a_quote_and_a_newline_in_a_value_round_trip(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "foo-ai.toml"
            write_provider_settings(
                path,
                "home",
                base_url='https://api.example.com/v1?q="x"',
                model='line\n"two"',
                context_window=1,
            )
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["providers"]["home"]["base_url"], 'https://api.example.com/v1?q="x"')
        self.assertEqual(data["providers"]["home"]["model"], 'line\n"two"')


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
        self.assertEqual(
            list(names),
            ["read_file", "list_dir", "glob", "grep", "write_file", "edit_file", "shell", "todo_write", "ask"],
        )
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

    def test_glob_matches_files(self):
        (self.workspace / "src").mkdir()
        (self.workspace / "src" / "b.py").write_text("b\n", encoding="utf-8", newline="\n")
        (self.workspace / "src" / "a.py").write_text("a\n", encoding="utf-8", newline="\n")
        (self.workspace / "src" / "a.txt").write_text("t\n", encoding="utf-8", newline="\n")
        (self.workspace / "src" / "sub").mkdir()
        (self.workspace / "src" / "sub" / "c.py").write_text("c\n", encoding="utf-8", newline="\n")
        (self.workspace / "A.txt").write_text("A\n", encoding="utf-8", newline="\n")
        (self.workspace / "b.txt").write_text("b\n", encoding="utf-8", newline="\n")
        (self.workspace / ".gitignore").write_text("*\n", encoding="utf-8", newline="\n")
        (self.workspace / ".git").mkdir()
        (self.workspace / ".git" / "hidden.txt").write_text("secret\n", encoding="utf-8", newline="\n")
        (self.workspace / ".foo-ai").mkdir()
        (self.workspace / ".foo-ai" / "session.jsonl").write_text("secret\n", encoding="utf-8", newline="\n")
        (self.workspace / "pkg").mkdir()
        (self.workspace / "file1.txt").write_text("1\n", encoding="utf-8", newline="\n")
        (self.workspace / "file10.txt").write_text("10\n", encoding="utf-8", newline="\n")
        (self.workspace / "a.c").write_text("c\n", encoding="utf-8", newline="\n")
        (self.workspace / "a.h").write_text("h\n", encoding="utf-8", newline="\n")
        (self.workspace / "a.py").write_text("py\n", encoding="utf-8", newline="\n")

        self.assertEqual(self.box.run("glob", {"pattern": ""}), "error: pattern is required")
        self.assertEqual(self.box.run("glob", {"pattern": "/etc/passwd"}), "error: pattern must be relative")
        self.assertEqual(self.box.run("glob", {"pattern": "C:/Windows"}), "error: pattern must be relative")
        self.assertEqual(self.box.run("glob", {"pattern": "../secret"}), "error: pattern must not contain '..'")
        self.assertEqual(self.box.run("glob", {"pattern": "*.py", "path": "a.py"}), "error: not a directory: a.py")
        self.assertEqual(self.box.run("glob", {"pattern": "*.py", "path": "missing"}), "error: not a directory: missing")
        self.assertTrue(self.box.run("glob", {"pattern": "*.py", "path": "../"}).startswith("error: path escapes the workspace:"))

        top_py = self.box.run("glob", {"pattern": "*.py"})
        self.assertEqual(top_py, "a.py")
        self.assertEqual(self.box.run("glob", {"pattern": "**/*.py"}), "a.py\nsrc/a.py\nsrc/b.py\nsrc/sub/c.py")
        self.assertEqual(self.box.run("glob", {"pattern": "src/*"}), "src/a.py\nsrc/a.txt\nsrc/b.py")
        self.assertEqual(self.box.run("glob", {"pattern": "src\\*.py"}), "src/a.py\nsrc/b.py")
        self.assertEqual(self.box.run("glob", {"pattern": "src//a.py"}), "src/a.py")
        self.assertEqual(self.box.run("glob", {"pattern": "*.txt", "path": "src"}), "src/a.txt")
        self.assertEqual(self.box.run("glob", {"pattern": "pkg"}), "[no matches]")
        self.assertEqual(self.box.run("glob", {"pattern": "*.[ch]"}), "a.c\na.h")
        self.assertEqual(self.box.run("glob", {"pattern": "file?.txt"}), "file1.txt")
        self.assertEqual(self.box.run("glob", {"pattern": "[!a].py"}), "src/b.py\nsrc/sub/c.py")
        self.assertEqual(self.box.run("glob", {"pattern": "[!a].c"}), "[no matches]")
        self.assertNotIn("src/a.txt", self.box.run("glob", {"pattern": "*.txt"}))
        listed = self.box.run("glob", {"pattern": "*.txt"})
        self.assertEqual(listed.splitlines()[:2], ["A.txt", "b.txt"])
        self.assertNotIn(".git/hidden.txt", self.box.run("glob", {"pattern": "**/*"}))
        self.assertNotIn(".foo-ai/session.jsonl", self.box.run("glob", {"pattern": "**/*"}))
        self.assertIn(".gitignore", self.box.run("glob", {"pattern": "*"}).splitlines())

        (self.workspace / "a.py").unlink()
        self.assertEqual(
            self.box.run("glob", {"pattern": "*.py"}),
            "src/a.py\nsrc/b.py\nsrc/sub/c.py",
        )

    def test_glob_match_limit_and_outside_symlink(self):
        for index in range(201):
            (self.workspace / f"f{index:03}.txt").write_text("x\n", encoding="utf-8", newline="\n")
        limited = self.box.run("glob", {"pattern": "*.txt"})
        self.assertTrue(limited.endswith("\n…[match limit reached]"))
        self.assertEqual(len(limited.splitlines()) - 1, 200)
        self.assertIn("f000.txt", limited)
        self.assertNotIn("f200.txt", limited)

        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        target = Path(outside.name) / "secret.txt"
        target.write_text("secret\n", encoding="utf-8", newline="\n")
        try:
            os.symlink(target, self.workspace / "link.txt")
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are not available here")
        result = self.box.run("glob", {"pattern": "link.txt"})
        self.assertEqual(result, "[no matches]")

    def test_grep_match_limit_and_skips(self):
        text = "\n".join(["hit"] * 60) + "\n"
        (self.workspace / "hits.txt").write_text(text, encoding="utf-8", newline="\n")
        result = self.box.run("grep", {"pattern": "hit", "path": "hits.txt"})
        self.assertEqual(result.count("\n"), 50)
        self.assertTrue(result.endswith("\n…[match limit reached]"))
        self.assertEqual(len(result.splitlines()) - 1, 50)

    def test_grep_skips_symlinks_that_leave_the_workspace(self):
        (self.workspace / "inside.txt").write_text("needle\n", encoding="utf-8", newline="\n")
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        target = Path(outside.name) / "secret.txt"
        target.write_text("needle\n", encoding="utf-8", newline="\n")
        try:
            os.symlink(target, self.workspace / "link.txt")
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are not available here")
        result = self.box.run("grep", {"pattern": "needle"})
        self.assertIn("inside.txt:1:needle", result.replace("\\", "/"))
        self.assertNotIn("link.txt", result)

    def test_edit_file_checks_the_path_before_the_strings(self):
        self.assertEqual(
            self.box.run("edit_file", {"path": "missing.txt", "old_string": "", "new_string": "x"}),
            "error: not a file: missing.txt",
        )

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

    def test_todo_write_replaces_the_list(self):
        written = self.box.run(
            "todo_write",
            {
                "todos": [
                    {"content": "  inspect  ", "status": "completed", "note": "ignored"},
                    {"content": "ship", "status": "in_progress"},
                ]
            },
        )
        self.assertEqual(
            json.loads(written),
            {
                "todos": [
                    {"content": "inspect", "status": "completed"},
                    {"content": "ship", "status": "in_progress"},
                ],
                "counts": {"total": 2, "pending": 0, "in_progress": 1, "completed": 1},
            },
        )
        self.assertEqual(self.box.todos[0]["content"], "inspect")
        invalid = self.box.run("todo_write", {"todos": [{"content": "ship", "status": "later"}]})
        self.assertEqual(
            invalid,
            "error: todos[0].status 'later' is invalid; use pending, in_progress, or completed",
        )
        self.assertEqual(len(self.box.todos), 2)
        duplicate = self.box.run(
            "todo_write",
            {"todos": [{"content": "ship", "status": "pending"}, {"content": " ship ", "status": "completed"}]},
        )
        self.assertIn("duplicates todos[0].content 'ship'", duplicate)
        self.assertEqual(self.box.run("todo_write", {}), "error: todos is required")
        self.assertEqual(self.box.run("todo_write", {"todos": "no"}), "error: todos must be an array")
        cleared = json.loads(self.box.run("todo_write", {"todos": []}))
        self.assertEqual(cleared["todos"], [])
        self.assertEqual(cleared["counts"]["total"], 0)
        self.assertEqual(self.box.todos, [])
        from foo_ai.tools import latest_todos

        listed = json.dumps({"todos": [{"content": "ship", "status": "pending"}]})
        self.assertEqual(
            latest_todos(
                [
                    {"role": "tool", "name": "todo_write", "content": listed},
                    {"role": "tool", "name": "todo_write", "content": "error: todos must be an array"},
                ]
            ),
            [{"content": "ship", "status": "pending"}],
        )
        self.assertEqual(latest_todos([{"role": "tool", "name": "todo_write", "content": '{"todos": []}'}]), [])
        self.assertIsNone(latest_todos([]))

    def test_ask_choices_and_fallback(self):
        from foo_ai.tools import ASK_DISMISSED, ASK_NO_USER

        payload = {
            "questions": [
                {
                    "header": " Library ",
                    "question": " Which library? ",
                    "options": [
                        {"label": " requests ", "description": " small "},
                        {"label": "httpx"},
                    ],
                }
            ]
        }
        self.assertEqual(self.box.run("ask", payload), ASK_NO_USER)
        self.assertFalse((self.workspace / ".foo-ai" / "decisions.json").exists())
        seen = []

        def asker(questions):
            seen.append(questions)
            return [{"id": "q1", "selected": ["requests", "httpx"]}]

        dismissed = Toolbox(self.workspace, confirm=lambda command: True, asker=lambda questions: [])
        self.assertEqual(dismissed.run("ask", payload), ASK_DISMISSED)
        self.assertFalse((self.workspace / ".foo-ai" / "decisions.json").exists())
        box = Toolbox(self.workspace, confirm=lambda command: True, asker=asker)
        answered = box.run("ask", payload)
        self.assertTrue(answered.startswith("The user answered:\n- Library: requests\n\ndecision_id: dec-"))
        decision_id = answered.split("decision_id: ", 1)[1]
        self.assertEqual(seen[0][0]["header"], "Library")
        self.assertEqual(seen[0][0]["question"], "Which library?")
        self.assertEqual(seen[0][0]["options"][0]["description"], "small")
        self.assertFalse(seen[0][0]["multiSelect"])
        self.assertEqual(box.run("ask", payload), (
            f"Host reused accepted decision {decision_id} for the same ambiguity. "
            "The user already chose: The user answered:\n- Library: requests. "
            f"Continue with that decision; to reopen it, cite decision_id {decision_id} and supply new_evidence."
        ))
        self.assertEqual(len(seen), 1)
        box.asker = lambda questions: [{"id": "q1", "selected": ["httpx"]}]
        reopened = box.run("ask", {**payload, "decision_id": decision_id, "new_evidence": "httpx is installed"})
        self.assertTrue(reopened.startswith("The user answered:\n- Library: httpx\n\ndecision_id: " + decision_id))
        self.assertIn(
            "unknown decision_id 'dec-nope'",
            box.run("ask", {**payload, "decision_id": "dec-nope"}),
        )
        own = Toolbox(self.workspace, confirm=lambda command: True, asker=lambda questions: [
            {"id": "q1", "selected": ["  use sqlalchemy  "]},
        ])
        custom = own.run(
            "ask",
            {
                "questions": [
                    {
                        "header": "Library",
                        "question": "Which other library?",
                        "options": [{"label": "requests"}, {"label": "httpx"}],
                    }
                ]
            },
        )
        self.assertTrue(custom.startswith("The user answered:\n- Library: use sqlalchemy\n\ndecision_id: "))
        self.assertEqual(box.run("ask", {}), "error: questions is required")
        self.assertEqual(
            box.run(
                "ask",
                {"questions": [{"header": "H", "question": "Q", "options": [{"label": "only"}]}]},
            ),
            "error: questions[0].options must contain 2 to 4 items",
        )
        both = {
            "questions": [
                {
                    "header": "Extras",
                    "question": "Which extras?",
                    "multiSelect": True,
                    "options": [{"label": "tests"}, {"label": "docs"}],
                }
            ]
        }
        box.asker = lambda questions: [{"id": "q1", "selected": ["docs", "tests", "docs"]}]
        extras = box.run("ask", both)
        self.assertTrue(extras.startswith("The user answered:\n- Extras: docs, tests\n\ndecision_id: dec-"))

    def test_a_blank_decision_id_asks_instead_of_failing(self):
        payload = {
            "questions": [
                {
                    "header": "Library",
                    "question": "Which library?",
                    "options": [{"label": "requests"}, {"label": "httpx"}],
                }
            ]
        }
        seen = []

        def asker(questions):
            seen.append(questions)
            return [
                {"id": question["id"], "selected": [question["options"][0]["label"]]}
                for question in questions
            ]

        box = Toolbox(self.workspace, confirm=lambda command: True, asker=asker)
        answered = box.run("ask", {**payload, "decision_id": ""})
        self.assertTrue(answered.startswith("The user answered:\n- Library: requests\n\ndecision_id: dec-"))
        self.assertEqual(len(seen), 1)
        decision_id = answered.split("decision_id: ", 1)[1]
        self.assertTrue(
            box.run("ask", {**payload, "decision_id": ""}).startswith(
                f"Host reused accepted decision {decision_id} for the same ambiguity."
            )
        )
        scope = {
            "questions": [
                {
                    "header": "Scope",
                    "question": "Which scope?",
                    "options": [{"label": "one"}, {"label": "two"}],
                }
            ]
        }
        self.assertTrue(
            box.run("ask", {**scope, "decision_id": "   "}).startswith("The user answered:\n- Scope: one")
        )
        self.assertEqual(len(seen), 2)
        self.assertEqual(box.run("ask", {**payload, "decision_id": 7}), "error: decision_id must be a string")

    def test_an_unwritable_decision_file_keeps_the_answer(self):
        payload = {
            "questions": [
                {
                    "header": "Library",
                    "question": "Which library?",
                    "options": [{"label": "requests"}, {"label": "httpx"}],
                }
            ]
        }
        store = self.workspace / ".foo-ai" / "decisions.json"
        store.mkdir(parents=True)
        box = Toolbox(
            self.workspace,
            confirm=lambda command: True,
            asker=lambda questions: [{"id": "q1", "selected": ["requests"]}],
        )
        answered = box.run("ask", payload)
        self.assertTrue(answered.startswith("The user answered:\n- Library: requests\n\ndecision_id: dec-"))
        decision_id = answered.split("decision_id: ", 1)[1]
        self.assertEqual(box.decisions.get(decision_id)["answer"], "The user answered:\n- Library: requests")
        self.assertFalse((self.workspace / ".foo-ai" / "decisions.json.tmp").exists())
        self.assertTrue(store.is_dir())
        self.assertTrue(
            box.run("ask", payload).startswith(f"Host reused accepted decision {decision_id} for the same ambiguity.")
        )


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
        reloaded.decisions.remember("dec-a", "dec-a", "The user answered:\n- Library: httpx")
        again = Session(self.workspace)
        again.load()
        self.assertEqual(again.decisions.get("dec-a")["answer"], "The user answered:\n- Library: httpx")
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
        (directory / "decisions.json").write_text('{"decisions": []}\n', encoding="utf-8")
        path.write_text("not json\n", encoding="utf-8")
        self.assertIn("unreadable", Session(self.workspace).load())
        self.assertFalse((directory / "decisions.json").exists())
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
            toolbox=Toolbox(workspace, confirm=lambda command: yes, decisions=session.decisions),
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

    def test_closing_round_after_the_step_limit(self):
        agent, session, sink, client, workspace = self._agent(
            [
                tool_call("read_file", "{\"path\": \"note.txt\"}"),
                answer("Reviewed.", {"prompt_tokens": 30, "completion_tokens": 4}),
            ],
            max_steps=1,
        )
        (workspace / "note.txt").write_text("hello\n", encoding="utf-8", newline="\n")
        self.assertEqual(agent.ask("review"), "Reviewed.")
        self.assertIsNone(client.seen[1]["tools"])
        self.assertEqual(session.messages[-1]["content"], "Reviewed.")
        self.assertNotIn("tool_calls", session.messages[-1])
        agent, session, sink, client, workspace = self._agent(
            [tool_call("read_file", "{\"path\": \"note.txt\"}"), tool_call("read_file", "{\"path\": \"note.txt\"}")],
            max_steps=1,
        )
        self.assertEqual(agent.ask("review"), "Stopped: tool-call limit reached before a final answer.")
        self.assertIsNone(client.seen[1]["tools"])
        stored = [json.loads(line) for line in (workspace / ".foo-ai" / "session.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(stored[-1]["role"], "tool")

    def test_open_todos_are_continued_then_cleared(self):
        from foo_ai.agent import todo_nudge

        pending = json.dumps({"todos": [{"content": "ship", "status": "in_progress"}]})
        done = json.dumps({"todos": [{"content": "ship", "status": "completed"}]})
        agent, session, sink, client, workspace = self._agent(
            [
                tool_call("todo_write", pending),
                answer("not yet"),
                tool_call("todo_write", done),
                answer("Done."),
                answer("next"),
            ]
        )
        lists = []
        sink.todos = lists.append
        self.assertEqual(agent.ask("ship it"), "Done.")
        notes = [
            message["content"]
            for message in session.messages
            if message["role"] == "user" and str(message["content"]).startswith("[todo]")
        ]
        self.assertEqual(notes, [todo_nudge([{"content": "ship", "status": "in_progress"}])])
        self.assertEqual(lists[-1], [{"content": "ship", "status": "completed"}])
        self.assertEqual(agent.ask("another"), "next")
        self.assertEqual(agent.toolbox.todos, [])
        self.assertEqual(lists[-1], [])
        self.assertEqual(client.seen[-1]["messages"][-1]["content"], "another")

    def test_two_nudges_let_the_answer_stand(self):
        pending = json.dumps({"todos": [{"content": "ship", "status": "pending"}]})
        agent, session, sink, client, workspace = self._agent(
            [
                tool_call("todo_write", pending),
                answer("one"),
                answer("two"),
                answer("three"),
                answer("unused"),
            ]
        )
        self.assertEqual(agent.ask("ship it"), "three")
        self.assertEqual(len(client.rounds), 1)
        notes = [
            message
            for message in session.messages
            if message["role"] == "user" and str(message["content"]).startswith("[todo]")
        ]
        self.assertEqual(len(notes), 2)

    def test_dismissed_ask_stops_and_an_answer_continues(self):
        from foo_ai.tools import ASK_DISMISSED, ASK_NO_USER

        raw = json.dumps(
            {
                "questions": [
                    {
                        "header": "Library",
                        "question": "Which library?",
                        "options": [{"label": "requests"}, {"label": "httpx"}],
                    }
                ]
            }
        )
        agent, session, sink, client, workspace = self._agent(
            [tool_call("ask", raw), answer("should not run")]
        )
        agent.toolbox.asker = lambda questions: []
        self.assertEqual(agent.ask("pick"), ASK_DISMISSED)
        self.assertEqual(len(client.rounds), 1)
        self.assertEqual(session.messages[-1]["content"], ASK_DISMISSED)
        agent, session, sink, client, workspace = self._agent(
            [tool_call("ask", raw), answer("Using httpx.")]
        )
        self.assertEqual(agent.ask("pick"), "Using httpx.")
        self.assertEqual(session.messages[-2]["content"], ASK_NO_USER)
        agent, session, sink, client, workspace = self._agent(
            [tool_call("ask", raw), answer("Using httpx.")]
        )
        agent.toolbox.asker = lambda questions: [{"id": "q1", "selected": ["httpx"]}]
        self.assertEqual(agent.ask("pick"), "Using httpx.")
        self.assertTrue(
            session.messages[-2]["content"].startswith("The user answered:\n- Library: httpx\n\ndecision_id: dec-")
        )

    def test_dismiss_skips_tools_that_follow_and_keeps_those_before(self):
        from foo_ai.tools import ASK_DISMISSED, ASK_SKIPPED

        raw = json.dumps(
            {
                "questions": [
                    {
                        "header": "Library",
                        "question": "Which library?",
                        "options": [{"label": "requests"}, {"label": "httpx"}],
                    }
                ]
            }
        )
        agent, session, sink, client, workspace = self._agent(
            [
                Completion(
                    content="",
                    reasoning_content="",
                    tool_calls=[
                        {
                            "id": "call_0",
                            "type": "function",
                            "function": {
                                "name": "write_file",
                                "arguments": json.dumps({"path": "kept.txt", "content": "yes"}),
                            },
                        },
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "ask", "arguments": raw},
                        },
                        {
                            "id": "call_2",
                            "type": "function",
                            "function": {
                                "name": "write_file",
                                "arguments": json.dumps({"path": "skipped.txt", "content": "no"}),
                            },
                        },
                    ],
                ),
                answer("should not run"),
            ]
        )
        agent.toolbox.asker = lambda questions: []
        self.assertEqual(agent.ask("pick"), ASK_DISMISSED)
        self.assertEqual((workspace / "kept.txt").read_text(encoding="utf-8"), "yes")
        self.assertFalse((workspace / "skipped.txt").exists())
        self.assertEqual(session.messages[-1]["name"], "write_file")
        self.assertEqual(session.messages[-1]["content"], ASK_SKIPPED)
        self.assertEqual(len(client.rounds), 1)

    def test_an_answered_ask_does_not_nudge_open_todos(self):
        pending = json.dumps({"todos": [{"content": "ship", "status": "in_progress"}]})
        raw = json.dumps(
            {
                "questions": [
                    {
                        "header": "Library",
                        "question": "Which library?",
                        "options": [{"label": "requests"}, {"label": "httpx"}],
                    }
                ]
            }
        )
        agent, session, sink, client, workspace = self._agent(
            [
                tool_call("todo_write", pending),
                tool_call("ask", raw),
                answer("Stopping to check with you."),
                answer("unused"),
            ]
        )
        agent.toolbox.asker = lambda questions: [{"id": "q1", "selected": ["httpx"]}]
        self.assertEqual(agent.ask("ship it"), "Stopping to check with you.")
        notes = [
            message
            for message in session.messages
            if message["role"] == "user" and str(message["content"]).startswith("[todo]")
        ]
        self.assertEqual(notes, [])
        self.assertEqual(len(client.rounds), 1)

    def test_prompt_questions_and_hub_answer(self):
        from foo_ai.cli import make_asker, prompt_questions
        from foo_ai.gui.hub import Hub

        questions = [
            {
                "id": "q1",
                "header": "Library",
                "question": "Which library?",
                "multiSelect": False,
                "options": [
                    {"label": "requests", "description": "small"},
                    {"label": "httpx", "description": ""},
                ],
            },
            {
                "id": "q2",
                "header": "Extras",
                "question": "Which extras?",
                "multiSelect": True,
                "options": [
                    {"label": "tests", "description": ""},
                    {"label": "docs", "description": "guides"},
                ],
            },
        ]
        stdout = io.StringIO()
        answers = prompt_questions(questions, io.StringIO("9\n2\n1, 2\n"), stdout)
        self.assertEqual(
            answers,
            [
                {"id": "q1", "selected": ["httpx"]},
                {"id": "q2", "selected": ["tests", "docs"]},
            ],
        )
        self.assertIn("? Library\n", stdout.getvalue())
        self.assertIn("  1. requests - small\n", stdout.getvalue())
        self.assertIn("Enter a number from 1 to 2.", stdout.getvalue())
        self.assertIn("or type your own answer:", stdout.getvalue())
        self.assertEqual(prompt_questions(questions[:1], io.StringIO(""), io.StringIO()), [])
        own = prompt_questions(questions[:1], io.StringIO("use sqlalchemy\n"), io.StringIO())
        self.assertEqual(own, [{"id": "q1", "selected": ["use sqlalchemy"]}])
        with mock.patch("sys.stdin.isatty", return_value=False):
            self.assertIsNone(make_asker()(questions))

        hub = Hub(Path("."))
        self.assertEqual(hub.answer([]), {"ok": False, "error": "no question is waiting for an answer"})
        pending = {"questions": [{"id": "q1"}], "event": threading.Event(), "answers": None}
        hub._question = pending
        hub._todos = [{"content": "ship", "status": "pending"}]
        self.assertEqual(hub.state()["todos"], [{"content": "ship", "status": "pending"}])
        self.assertEqual(hub.events_after(0)["question"], [{"id": "q1"}])
        self.assertEqual(hub.answer([{"id": "q1", "selected": ["requests"]}]), {"ok": True})
        self.assertEqual(pending["answers"], [{"id": "q1", "selected": ["requests"]}])
        self.assertTrue(pending["event"].is_set())

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
                self.assertEqual(len(body["tools"]), 9)
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

    def test_json_body_reaches_the_sink(self):
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
                                    "reasoning_content": "because",
                                    "tool_calls": [
                                        {"type": "function", "function": {"name": "shell", "arguments": "{}"}}
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
        provider = Provider("home", f"http://127.0.0.1:{port}/v1", "m", 1_000_000, "", {})
        sink = Sink()
        completion = Client(provider).complete([{"role": "user", "content": "hi"}], None, sink)
        self.assertEqual(completion.content, "Done.")
        self.assertEqual(sink.contents, ["Done."])
        self.assertEqual(sink.reasonings, ["because"])
        self.assertEqual(sink.names, ["shell"])

    def test_stream_read_error_becomes_a_chat_error(self):
        def reply(body, requests):
            return 200, "text/event-stream", sse(json.dumps({"choices": [{"delta": {"content": "Hi"}}]}))

        server = HttpScript(reply)
        port = server.start_and_port()
        self.addCleanup(server.stop)
        provider = Provider("home", f"http://127.0.0.1:{port}/v1", "m", 1_000_000, "", {})
        with mock.patch("foo_ai.client._read_sse", side_effect=ConnectionResetError("reset")):
            with self.assertRaises(ChatError) as caught:
                Client(provider).complete([{"role": "user", "content": "hi"}], None, None)
        self.assertIn("cannot reach ", str(caught.exception))
        self.assertIn("reset", str(caught.exception))

    def test_fallback_ids_count_kept_calls(self):
        calls = _normalize_calls([None, {"function": {"name": "shell", "arguments": "{}"}}])
        self.assertEqual([call["id"] for call in calls], ["call_0"])


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


class SkillTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name).resolve()

    def tearDown(self):
        self.tmp.cleanup()

    def _write_skill(self, name: str, text: str) -> None:
        directory = self.workspace / ".foo-ai" / "skills" / name
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text(text, encoding="utf-8", newline="\n")

    def _agent(self, rounds, catalog=None, max_steps=8):
        catalog = load_skills(self.workspace) if catalog is None else catalog
        session = Session(self.workspace, prompt=system_prompt(catalog))
        sink = Sink()
        client = ScriptedClient(rounds)
        provider = Provider("home", "http://127.0.0.1:9/v1", "test", 1_000_000, "", {})
        toolbox = Toolbox(self.workspace, confirm=lambda command: True, skills=catalog)
        agent = Agent(
            provider=provider,
            session=session,
            toolbox=toolbox,
            client=client,
            sink=sink,
            max_steps=max_steps,
        )
        return agent, session, sink, client, catalog

    def test_builtin_review_and_project_override(self):
        catalog = load_skills(self.workspace)
        self.assertEqual([skill.name for skill in catalog.entries], ["explore", "review"])
        explore = catalog.get("explore")
        self.assertEqual(explore.run, "subagent")
        self.assertEqual(explore.tools, ("read_file", "list_dir", "glob", "grep"))
        self.assertEqual(explore.shell, ())
        self.assertEqual(explore.task, "")
        self.assertTrue(explore.body.startswith("You are a read-only exploration subagent."))
        review = catalog.get("review")
        self.assertEqual(review.run, "subagent")
        self.assertEqual(review.tools, ("read_file", "list_dir", "grep", "shell"))
        self.assertEqual(review.shell, ("git status", "git diff", "git log", "git show"))
        self.assertEqual(review.task, "Review the uncommitted changes.")
        self.assertTrue(review.body.startswith("You are a read-only code reviewer."))
        prompt = system_prompt(catalog)
        self.assertFalse(prompt.endswith("\n"))
        self.assertIn(
            "\n\nSkills:\n- explore (subagent): Investigate repository structure or behavior in a read-only subagent.\n- review (subagent): Review the working tree for correctness, security, and missing tests.",
            prompt,
        )
        self.assertEqual(
            [item["function"]["name"] for item in Toolbox(self.workspace, lambda command: True, skills=catalog).schemas()],
            [
                "read_file",
                "list_dir",
                "glob",
                "grep",
                "write_file",
                "edit_file",
                "shell",
                "todo_write",
                "ask",
                "run_skill",
            ],
        )
        self._write_skill(
            "review",
            "---\nname: review\ndescription: Project review.\nrun: inline\n---\n\nProject body.\n",
        )
        self._write_skill(
            "check",
            "---\nname: check\ndescription: Check a module.\nrun: inline\n---\n\nCheck it.\n",
        )
        self._write_skill("Nope", "---\nname: Nope\n---\n\nHi.\n")
        self._write_skill("note", "hello\n")
        replaced = load_skills(self.workspace)
        self.assertEqual([skill.name for skill in replaced.entries], ["check", "explore", "review"])
        self.assertEqual(replaced.get("review").description, "Project review.")
        self.assertEqual(replaced.get("review").body, "Project body.")
        self.assertEqual(replaced.get("check").run, "inline")
        self.assertEqual(
            replaced.skipped,
            [
                "Skill skipped: .foo-ai/skills/Nope/SKILL.md: invalid skill name\n",
                "Skill skipped: .foo-ai/skills/note/SKILL.md: frontmatter is missing\n",
            ],
        )
        self.assertEqual(skills_banner(replaced), "Skills: /check, /explore, /review.")

    def test_skipped_reasons_and_shell_guard(self):
        self._write_skill(
            "badsh",
            "---\nname: badsh\ndescription: Bad.\nrun: subagent\ntools: shell\nshell:\n  - git status | head\n---\n\nBody.\n",
        )
        catalog = load_skills(self.workspace)
        self.assertIn(
            "Skill skipped: .foo-ai/skills/badsh/SKILL.md: shell prefix contains a forbidden character\n",
            catalog.skipped,
        )
        self.assertTrue(command_allowed("git diff --stat", ("git diff",)))
        self.assertFalse(command_allowed("git difftool", ("git diff",)))
        self.assertFalse(command_allowed("git status && echo hi", ("git status",)))
        script = self.workspace / "say.py"
        script.write_text("print(123)\n", encoding="utf-8", newline="\n")
        command = f'"{sys.executable}" "{script}"'
        prefix = f'"{sys.executable}"'

        def confirm(value: str) -> bool:
            raise AssertionError(value)

        box = RestrictedToolbox(Toolbox(self.workspace, confirm), ("shell",), (prefix,))
        self.assertIn("123", box.run("shell", {"command": command}))
        self.assertEqual(box.run("shell", {"command": "echo hi"}), "error: command is not allowed for this skill")
        self.assertEqual(box.run("edit_file", {}), "error: unknown tool 'edit_file'")

    def test_subagent_stays_out_of_the_parent_session(self):
        agent, session, sink, client, catalog = self._agent(
            [
                tool_call("run_skill", json.dumps({"name": "review", "arguments": "focus"})),
                answer("verdict", {"prompt_tokens": 10, "completion_tokens": 2}),
                answer("done"),
            ]
        )
        agent.context.ratio = 0.5
        self.assertEqual(agent.ask("please"), "done")
        self.assertEqual(agent.context.ratio, 0.5)
        child = client.seen[1]
        self.assertTrue(child["messages"][0]["content"].startswith("You are a read-only code reviewer."))
        self.assertEqual(child["messages"][1], {"role": "user", "content": "focus"})
        self.assertEqual(len(child["messages"]), 2)
        self.assertEqual(
            [item["function"]["name"] for item in child["tools"]],
            ["read_file", "list_dir", "grep", "shell"],
        )
        self.assertIn("\n[review] context ", "".join(sink.statuses))
        stored = (self.workspace / ".foo-ai" / "session.jsonl").read_text(encoding="utf-8")
        self.assertIn("verdict", stored)
        self.assertNotIn("You are a read-only code reviewer.", stored)
        self.assertEqual(
            [json.loads(line)["role"] for line in stored.splitlines()],
            ["system", "user", "assistant", "tool", "assistant"],
        )
        self.assertIn(
            "Skills:\n- explore (subagent): Investigate repository structure or behavior in a read-only subagent.\n- review (subagent):",
            session.messages[0]["content"],
        )

    def test_inline_unknown_and_required_arguments(self):
        self._write_skill(
            "check",
            "---\nname: check\ndescription: Check a module.\nrun: inline\n---\n\nCheck it.\n",
        )
        agent, session, sink, client, catalog = self._agent(
            [
                tool_call("run_skill", json.dumps({"name": "check", "arguments": "the parser"})),
                answer("done"),
            ]
        )
        self.assertEqual(agent.ask("please"), "done")
        self.assertEqual(len(client.seen), 2)
        self.assertEqual(sink.results[0][1], "Check it.\n\nArguments:\nthe parser")
        box = agent.toolbox
        self.assertEqual(box.run("run_skill", json.dumps({"name": "missing"})), "error: unknown skill 'missing'")
        self.assertEqual(box.run("run_skill", json.dumps({"name": "review"})), "error: arguments are required for subagent skill 'review'")
        self.assertEqual(
            box.run("run_skill", json.dumps({"name": "review", "arguments": 1})),
            "error: arguments must be a string",
        )

    def test_child_error_verbose_trace_and_step_cap(self):
        agent, session, sink, client, catalog = self._agent(
            [
                tool_call("run_skill", json.dumps({"name": "review", "arguments": "focus"})),
                ChatError("HTTP 500 from http://x: boom"),
                answer("continued"),
            ]
        )
        self.assertEqual(agent.ask("please"), "continued")
        self.assertEqual(sink.results[0][1], "error: HTTP 500 from http://x: boom")
        (self.workspace / "note.txt").write_text("hello\n", encoding="utf-8", newline="\n")
        agent, session, sink, client, catalog = self._agent(
            [
                tool_call("run_skill", json.dumps({"name": "review", "arguments": "focus"})),
                tool_call("read_file", json.dumps({"path": "note.txt"})),
                answer("verdict"),
                answer("done"),
            ]
        )
        sink.verbose = True
        self.assertEqual(agent.ask("please"), "done")
        printed = "".join(sink.contents)
        self.assertIn("\n[review] → read_file", printed)
        self.assertIn("\n[review] 1|hello\n[1 lines shown, 1 total]\n", printed)
        agent, session, sink, client, catalog = self._agent(
            [
                tool_call("run_skill", json.dumps({"name": "review", "arguments": "focus"})),
                tool_call("read_file", json.dumps({"path": "note.txt"})),
                answer("verdict"),
                answer("done"),
            ],
            max_steps=1,
        )
        self.assertEqual(agent.ask("please"), "done")
        self.assertEqual(sink.results[0][1], "verdict")
        self.assertIsNone(client.seen[2]["tools"])

    def test_slash_review_and_resume_prompt(self):
        bare, session, sink, client, catalog = self._agent([])
        self.assertIsNone(bare.run_slash("/explore", "explore", ""))
        self.assertEqual(sink.statuses, ["error: arguments are required for subagent skill 'explore'\n"])
        self.assertEqual(len(session.messages), 1)
        agent, session, sink, client, catalog = self._agent([answer("Looks good.")])
        self.assertEqual(agent.run_slash("/review", "review", ""), "Looks good.")
        self.assertEqual(sink.contents, ["Looks good."])
        self.assertEqual(session.messages[1]["content"], "/review")
        self.assertEqual(session.messages[2]["content"], "Looks good.")
        self.assertEqual(session.messages[2]["reasoning_content"], "")
        self.assertNotIn("tool_calls", session.messages[2])
        self.assertEqual(client.seen[0]["messages"][1]["content"], "Review the uncommitted changes.")
        self._write_skill(
            "scan",
            "---\nname: scan\ndescription: Scan.\nrun: subagent\n---\n\nLook.\n",
        )
        self._write_skill(
            "find",
            "---\nname: find\ndescription: Find.\nrun: subagent\ntools: grep, glob\n---\n\nFind.\n",
        )
        loaded = load_skills(self.workspace)
        self.assertEqual(loaded.get("scan").tools, ("read_file", "list_dir", "glob", "grep"))
        self.assertEqual(loaded.get("find").tools, ("glob", "grep"))
        agent, session, sink, client, catalog = self._agent([])
        self.assertIsNone(agent.run_slash("/scan", "scan", ""))
        self.assertEqual(sink.statuses, ["error: arguments are required for subagent skill 'scan'\n"])
        self.assertEqual(len(session.messages), 1)
        session.add_user("hi")
        session.save()
        fresh = Session(self.workspace, prompt="custom prompt")
        self.assertEqual(fresh.load(), "Resumed .foo-ai/session.jsonl (1 messages).")
        self.assertEqual(fresh.messages[0]["content"], "custom prompt")
        fresh.reset()
        self.assertEqual(fresh.messages[0]["content"], "custom prompt")

    def test_cli_slash_is_interactive_only(self):
        captured = {}

        def reply(body, requests):
            captured.setdefault("bodies", []).append(body)
            payload = sse(json.dumps({"choices": [{"delta": {"content": "Looks good."}}]}))
            return 200, "text/event-stream", payload

        server = HttpScript(reply)
        port = server.start_and_port()
        self.addCleanup(server.stop)
        env = {"FOO_AI_BASE_URL": f"http://127.0.0.1:{port}/v1", "FOO_AI_MODEL": "test-model"}
        interactive = run_cli([], self.workspace, stdin="/review\n/exit\n", env=env)
        self.assertEqual(interactive.returncode, 0, interactive.stderr)
        self.assertIn("Looks good.", interactive.stdout)
        self.assertIn("Skills: /explore, /review.\n", interactive.stdout)
        child = captured["bodies"][0]
        self.assertTrue(child["messages"][0]["content"].startswith("You are a read-only code reviewer."))
        self.assertEqual(child["messages"][1]["content"], "Review the uncommitted changes.")
        self.assertEqual(
            [item["function"]["name"] for item in child["tools"]],
            ["read_file", "list_dir", "grep", "shell"],
        )
        stored = (self.workspace / ".foo-ai" / "session.jsonl").read_text(encoding="utf-8")
        self.assertIn('"content": "/review"', stored)
        self.assertNotIn("You are a read-only code reviewer.", stored)
        one_shot = run_cli(["/review"], self.workspace, env=env)
        self.assertEqual(one_shot.returncode, 0, one_shot.stderr)
        parent = captured["bodies"][1]
        self.assertTrue(parent["messages"][0]["content"].startswith("You are Foo AI"))
        self.assertEqual(parent["messages"][-1]["content"], "/review")


if __name__ == "__main__":
    unittest.main()
