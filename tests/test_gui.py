"""The desktop page: the context line, the hub, the HTTP API, and the files served."""

from __future__ import annotations

import http.client
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from foo_ai.client import ChatError, Client, Completion
from foo_ai.config import ConfigError, Provider, known_providers, load_page_workspace, save_page_workspace
from foo_ai.gui.hub import Hub, usage_from_line
from foo_ai.gui.server import STATIC, build_parser, make_server, resolve_workspace

ROOT = Path(__file__).resolve().parent.parent

# The page's settings file: every command test points FOO_AI_CONFIG_DIR here,
# so a developer's own providers never reach a test.
_SETTINGS_HOME = tempfile.TemporaryDirectory(prefix="foo-ai-settings-")

SKILL = """---
name: note
description: Turn the arguments into one line.
run: inline
---

Write the arguments as one line.
"""


def clean_env() -> dict:
    env = os.environ.copy()
    for key in list(env):
        if key.startswith("FOO_AI_"):
            del env[key]
    env["FOO_AI_CONFIG_DIR"] = _SETTINGS_HOME.name
    env["PYTHONPATH"] = str(ROOT)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    return env


class ScriptedClient:
    """A client whose rounds the test writes down."""

    def __init__(self, rounds=None):
        self.rounds = list(rounds or [])
        self.seen: list[dict] = []

    def complete(self, messages, tools, sink):
        self.seen.append({"messages": json.loads(json.dumps(messages)), "tools": tools})
        item = self.rounds.pop(0)
        if isinstance(item, Exception):
            raise item
        if sink is not None and item.reasoning_content:
            sink.reasoning(item.reasoning_content)
        if sink is not None and item.content:
            sink.content(item.content)
        for call in item.tool_calls:
            name = call["function"]["name"]
            if sink is not None and name:
                sink.tool_name(name)
        return item


def answer(text: str) -> Completion:
    return Completion(content=text, reasoning_content="", tool_calls=[])


class SilentServer(threading.Thread):
    """Accept connections and never answer: a model server that stalls."""

    def __init__(self):
        super().__init__(daemon=True)
        self.socket = socket.socket()
        self.socket.bind(("127.0.0.1", 0))
        self.socket.listen(4)
        self.port = self.socket.getsockname()[1]
        self.connections: list[socket.socket] = []

    def run(self):
        while True:
            try:
                connection, _ = self.socket.accept()
            except OSError:
                return
            self.connections.append(connection)

    def close(self):
        for connection in self.connections:
            try:
                connection.close()
            except OSError:
                pass
        self.socket.close()


def tool_call(name: str, arguments: dict, call_id: str = "call_0") -> Completion:
    return Completion(
        content="",
        reasoning_content="",
        tool_calls=[
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }
        ],
    )


def request(base: str, path: str, body: dict | None = None, headers: dict | None = None):
    """One HTTP call. Returns the status and the body bytes, never raises."""
    data = None if body is None else json.dumps(body).encode("utf-8")
    sent = dict(headers or {})
    if data is not None and "Content-Type" not in sent:
        sent["Content-Type"] = "application/json"
    probe = urllib.request.Request(base + path, data=data, headers=sent)
    try:
        with urllib.request.urlopen(probe, timeout=15) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def get_json(base: str, path: str) -> dict:
    status, body = request(base, path)
    assert status == 200, (status, body)
    return json.loads(body)


def poll(base: str, stop, timeout: float = 20.0) -> tuple[dict, list[dict]]:
    """Collect events until ``stop(events)`` is true, then return the last reply."""
    deadline = time.monotonic() + timeout
    seq = 0
    events: list[dict] = []
    while time.monotonic() < deadline:
        data = get_json(base, f"/api/events?after={seq}")
        seq = data["seq"]
        events.extend(data["events"])
        if stop(events):
            return data, events
        time.sleep(0.02)
    raise AssertionError(f"timed out; events were {events}")


def finished(events: list[dict]):
    return any(event["kind"] == "done" for event in events)


def kinds(events: list[dict]) -> list[str]:
    return [event["kind"] for event in events]


class UsageLineTests(unittest.TestCase):
    def test_context_line_becomes_the_meter(self):
        usage = usage_from_line("context 178.1K/1M · usage total 178100 624 · cache 98.62%")
        self.assertIsNotNone(usage)
        self.assertEqual(usage["used"], "178.1K")
        self.assertEqual(usage["window"], "1M")
        self.assertAlmostEqual(usage["fraction"], 0.1781, places=4)
        self.assertEqual(usage["prompt"], 178100)
        self.assertEqual(usage["completion"], 624)
        self.assertAlmostEqual(usage["cache"], 98.62)

    def test_a_window_can_be_missing(self):
        usage = usage_from_line("context 800 · usage total 800 12")
        self.assertEqual(usage["window"], "")
        self.assertEqual(usage["fraction"], 0.0)
        self.assertIsNone(usage["cache"])

    def test_another_line_is_not_a_context_line(self):
        self.assertIsNone(usage_from_line("Summarizing earlier turns."))
        self.assertIsNone(usage_from_line("context nonsense"))


class KnownProvidersTests(unittest.TestCase):
    def test_the_catalog_and_the_launch_file_are_both_listed(self):
        with tempfile.TemporaryDirectory() as folder:
            launch = Path(folder)
            self.assertEqual(known_providers(launch), ["deepseek-flash_1", "home"])
            (launch / "foo-ai.toml").write_text(
                'default_provider = "work"\n[providers.work]\nmodel = "m"\n',
                encoding="utf-8",
                newline="\n",
            )
            self.assertEqual(known_providers(launch), ["deepseek-flash_1", "home", "work"])


def provider_at(port: int) -> Provider:
    return Provider(
        name="silent",
        base_url=f"http://127.0.0.1:{port}/v1",
        model="m",
        context_window=1000,
        api_key="",
        extra_body={},
    )


class StallTests(unittest.TestCase):
    """A provider that says nothing must not leave the page on "working" forever."""

    def test_a_silent_server_becomes_a_chat_error(self):
        server = SilentServer()
        server.start()
        try:
            client = Client(provider_at(server.port), timeout=0.4)
            with self.assertRaises(ChatError) as caught:
                client.complete([{"role": "user", "content": "hi"}], None, None)
            self.assertIn("cannot reach", str(caught.exception))
        finally:
            server.close()

    def test_the_default_client_carries_the_stall_bound(self):
        with tempfile.TemporaryDirectory() as folder:
            provider = provider_at(9)
            hub = Hub(Path(folder), read_timeout=42.0)
            client = hub._make_client(provider)
            self.assertIsInstance(client, Client)
            self.assertEqual(client.timeout, 42.0)
            self.assertIsNone(Hub(Path(folder), read_timeout=None)._make_client(provider).timeout)
            sentinel = object()
            factory = Hub(Path(folder), client_factory=lambda item: sentinel)
            self.assertIs(factory._make_client(provider), sentinel)


class WorkspaceStartTests(unittest.TestCase):
    """The folder the page opens: the flag, then the saved folder, then here."""

    def test_the_flag_wins_and_is_not_written(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            settings = root / "settings" / "foo-ai.toml"
            saved = root / "saved"
            saved.mkdir()
            flag = root / "flag"
            flag.mkdir()
            save_page_workspace(settings, saved)
            self.assertEqual(resolve_workspace(str(flag), settings), flag.resolve())
            self.assertEqual(load_page_workspace(settings), saved)

    def test_the_saved_folder_is_used_when_no_flag_names_one(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            settings = root / "settings" / "foo-ai.toml"
            saved = root / "saved"
            saved.mkdir()
            save_page_workspace(settings, saved)
            self.assertEqual(resolve_workspace(None, settings), saved.resolve())

    def test_a_saved_folder_that_is_gone_falls_back_to_here(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            settings = root / "settings" / "foo-ai.toml"
            save_page_workspace(settings, root / "gone")
            self.assertEqual(resolve_workspace(None, settings), Path.cwd().resolve())

    def test_no_settings_file_opens_the_current_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            settings = Path(folder) / "nothing.toml"
            self.assertEqual(resolve_workspace(None, settings), Path.cwd().resolve())
            self.assertEqual(resolve_workspace(None, None), Path.cwd().resolve())

    def test_a_broken_settings_file_names_itself(self):
        with tempfile.TemporaryDirectory() as folder:
            settings = Path(folder) / "foo-ai.toml"
            settings.write_text("workspace = [\n", encoding="utf-8", newline="\n")
            with self.assertRaises(ConfigError) as caught:
                resolve_workspace(None, settings)
            self.assertIn(str(settings), str(caught.exception))


class GuiServerTests(unittest.TestCase):
    """One hub and one server per test, with a client the test scripts."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name).resolve()
        skill = self.workspace / ".foo-ai" / "skills" / "note"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(SKILL, encoding="utf-8", newline="\n")
        self.cwd = os.getcwd()
        os.chdir(self.workspace)
        self.settings = self.workspace / "settings" / "foo-ai.toml"
        self.client = ScriptedClient()
        self.hub = Hub(
            self.workspace,
            client_factory=lambda provider: self.client,
            user_config=self.settings,
        )
        self.hub.start()
        self.server = make_server(self.hub, "127.0.0.1", 0)
        self.port = self.server.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.hub.close()
        os.chdir(self.cwd)
        self.tmp.cleanup()

    # --------------------------------------------------------------- files

    def test_the_page_and_its_files_are_served(self):
        status, body = request(self.base, "/")
        self.assertEqual(status, 200)
        self.assertIn(b"Foo AI", body)
        status, body = request(self.base, "/static/styles.css")
        self.assertEqual(status, 200)
        self.assertIn(b"--accent", body)
        status, body = request(self.base, "/static/app.js")
        self.assertEqual(status, 200)
        self.assertIn(b"/api/events", body)
        status, body = request(self.base, "/favicon.svg")
        self.assertEqual(status, 200)
        self.assertIn(b"<svg", body)

    def test_a_file_outside_static_is_not_served(self):
        self.assertEqual(request(self.base, "/static/hub.py")[0], 404)
        self.assertEqual(request(self.base, "/static/..%2fhub.py")[0], 404)
        self.assertEqual(request(self.base, "/static/missing.css")[0], 404)
        self.assertEqual(request(self.base, "/api/nothing")[0], 404)

    def test_the_static_files_are_in_the_package(self):
        for name in ("index.html", "styles.css", "app.js", "favicon.svg"):
            self.assertTrue((STATIC / name).is_file(), name)
        self.assertIn("gui/static/*", (ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    # --------------------------------------------------------------- state

    def test_state_reports_the_session(self):
        state = get_json(self.base, "/api/state")
        self.assertEqual(state["workspace"], str(self.workspace))
        self.assertEqual(state["provider"]["name"], "home")
        self.assertEqual(state["provider"]["model"], "DeepSeek-V4.1-Flash-1017")
        self.assertEqual(state["max_steps"], 200)
        names = [skill["name"] for skill in state["skills"]]
        self.assertEqual(names, ["explore", "note", "review"])
        self.assertEqual(state["session"]["file"], ".foo-ai/session.jsonl")
        self.assertEqual(state["session"]["messages"], 0)
        self.assertIn("home", state["providers"])
        self.assertEqual(state["status"], "idle")
        self.assertFalse(state["yes"])

    def test_history_is_empty_before_the_first_request(self):
        self.assertEqual(get_json(self.base, "/api/history"), {"messages": []})

    # ------------------------------------------------------------ requests

    def test_ask_streams_an_answer(self):
        self.client.rounds = [answer("hello there")]
        status, body = request(self.base, "/api/ask", {"text": "hi"})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])
        data, events = poll(self.base, finished)
        self.assertEqual(data["status"], "idle")
        self.assertIn("user", kinds(events))
        self.assertLess(kinds(events).index("start"), kinds(events).index("content"))
        content = "".join(event["text"] for event in events if event["kind"] == "content")
        self.assertEqual(content, "hello there")
        history = get_json(self.base, "/api/history")["messages"]
        self.assertEqual(history[0], {"role": "user", "text": "hi"})
        self.assertEqual(history[1]["role"], "assistant")
        self.assertEqual(history[1]["text"], "hello there")

    def test_an_empty_prompt_is_refused(self):
        status, body = request(self.base, "/api/ask", {"text": "   "})
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(body)["ok"])
        self.assertEqual(self.hub.status, "idle")

    def test_a_chat_error_is_an_error_event(self):
        from foo_ai.client import ChatError

        self.client.rounds = [ChatError("HTTP 401 from nowhere: nope")]
        request(self.base, "/api/ask", {"text": "hi"})
        data, events = poll(self.base, finished)
        self.assertEqual(data["status"], "idle")
        error = [event for event in events if event["kind"] == "error"]
        self.assertEqual(error[0]["text"], "HTTP 401 from nowhere: nope")

    def test_a_shell_command_waits_for_the_page(self):
        script = self.workspace / "say.py"
        script.write_text("print(123)\n", encoding="utf-8", newline="\n")
        command = f'"{sys.executable}" "{script}"'
        self.client.rounds = [
            tool_call("shell", {"command": command}),
            answer("ran it"),
        ]
        request(self.base, "/api/ask", {"text": "run it"})
        data, events = poll(self.base, lambda seen: any(e["kind"] == "confirm" for e in seen))
        self.assertEqual(data["status"], "approving")
        self.assertEqual(data["approval"], command)
        self.assertEqual(self.hub.approve(True), {"ok": True})
        data, events = poll(self.base, finished)
        results = [event for event in events if event["kind"] == "tool_result"]
        self.assertEqual(results[0]["name"], "shell")
        self.assertIn("123", results[0]["text"])
        self.assertFalse(results[0]["full"])
        self.assertEqual(results[0]["chars"], len(results[0]["text"]))
        self.assertIn("confirm_done", kinds(events))

    def test_an_edit_result_is_always_shown_in_full(self):
        (self.workspace / "note.txt").write_text("one\ntwo\n", encoding="utf-8", newline="\n")
        self.client.rounds = [
            tool_call("edit_file", {"path": "note.txt", "old_string": "two", "new_string": "three"}),
            answer("edited"),
        ]
        request(self.base, "/api/ask", {"text": "edit it"})
        _, events = poll(self.base, finished)
        results = [event for event in events if event["kind"] == "tool_result"]
        self.assertTrue(results[0]["full"])
        self.assertIn("-two", results[0]["text"])

    def test_verbose_shows_every_result_in_full(self):
        (self.workspace / "note.txt").write_text("one\n", encoding="utf-8", newline="\n")
        self.hub.set_settings(verbose=True)
        self.client.rounds = [tool_call("read_file", {"path": "note.txt"}), answer("read")]
        request(self.base, "/api/ask", {"text": "read it"})
        _, events = poll(self.base, finished)
        results = [event for event in events if event["kind"] == "tool_result"]
        self.assertTrue(results[0]["full"])
        self.assertIn("1|one", results[0]["text"])

    def test_a_denied_shell_command_is_a_tool_error(self):
        self.client.rounds = [
            tool_call("shell", {"command": "echo hi"}),
            answer("fine"),
        ]
        request(self.base, "/api/ask", {"text": "run it"})
        poll(self.base, lambda seen: any(e["kind"] == "confirm" for e in seen))
        self.hub.approve(False)
        data, events = poll(self.base, finished)
        results = [event for event in events if event["kind"] == "tool_result"]
        self.assertEqual(results[0]["text"], "error: command not approved")
        self.assertFalse(results[0]["full"])

    def test_auto_approve_does_not_ask(self):
        self.hub.set_settings(yes=True)
        self.client.rounds = [tool_call("shell", {"command": "echo hi"}), answer("fine")]
        request(self.base, "/api/ask", {"text": "run it"})
        data, events = poll(self.base, finished)
        confirm = [event for event in events if event["kind"] == "confirm"]
        self.assertEqual(len(confirm), 1)
        self.assertTrue(confirm[0]["auto"])
        self.assertIsNone(data["approval"])

    def test_stop_interrupts_a_waiting_request(self):
        self.client.rounds = [tool_call("shell", {"command": "echo hi"}), answer("never")]
        request(self.base, "/api/ask", {"text": "run it"})
        poll(self.base, lambda seen: any(e["kind"] == "confirm" for e in seen))
        self.assertEqual(self.hub.cancel(), {"ok": True})
        data, events = poll(self.base, finished)
        self.assertEqual(data["status"], "idle")
        statuses = [event["text"] for event in events if event["kind"] == "status"]
        self.assertIn("Interrupted.", statuses)

    def test_a_second_request_is_refused_while_one_waits(self):
        self.client.rounds = [tool_call("shell", {"command": "echo hi"}), answer("fine")]
        request(self.base, "/api/ask", {"text": "run it"})
        poll(self.base, lambda seen: any(e["kind"] == "confirm" for e in seen))
        status, body = request(self.base, "/api/ask", {"text": "and again"})
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(body)["ok"])
        self.hub.approve(False)
        poll(self.base, finished)

    def test_reset_archives_the_conversation(self):
        self.client.rounds = [answer("hello")]
        request(self.base, "/api/ask", {"text": "hi"})
        poll(self.base, finished)
        status, body = request(self.base, "/api/reset", {})
        self.assertEqual(status, 200)
        result = json.loads(body)
        self.assertTrue(result["ok"])
        self.assertIn("Archived", result["note"])
        self.assertEqual(get_json(self.base, "/api/history")["messages"], [])
        archived = list((self.workspace / ".foo-ai").glob("session-*.jsonl"))
        self.assertEqual(len(archived), 1)

    def test_a_slash_command_runs_a_loaded_skill(self):
        self.client.rounds = [answer("noted")]
        status, body = request(self.base, "/api/slash", {"command": "/note one line"})
        self.assertTrue(json.loads(body)["ok"])
        _, events = poll(self.base, finished)
        content = "".join(event["text"] for event in events if event["kind"] == "content")
        self.assertEqual(content, "noted")
        status, body = request(self.base, "/api/slash", {"command": "/missing"})
        self.assertFalse(json.loads(body)["ok"])

    # ------------------------------------------------------------ ask, todos

    def test_a_question_waits_for_the_page(self):
        questions = [
            {
                "header": "Library",
                "question": "Which library?",
                "options": [
                    {"label": "requests", "description": "small"},
                    {"label": "httpx"},
                ],
            }
        ]
        self.client.rounds = [
            tool_call("ask", {"questions": questions}),
            answer("Using httpx."),
        ]
        request(self.base, "/api/ask", {"text": "pick one"})
        data, _ = poll(self.base, lambda seen: any(e["kind"] == "ask" for e in seen))
        self.assertEqual(data["status"], "asking")
        asked = data["question"]
        self.assertEqual(asked[0]["id"], "q1")
        self.assertEqual(asked[0]["header"], "Library")
        self.assertEqual(asked[0]["question"], "Which library?")
        self.assertEqual([option["label"] for option in asked[0]["options"]], ["requests", "httpx"])
        self.assertEqual(asked[0]["options"][0]["description"], "small")
        self.assertFalse(asked[0]["multiSelect"])

        status, body = request(
            self.base,
            "/api/answer",
            {"answers": [{"id": "q1", "selected": ["httpx"]}]},
        )
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])
        data, events = poll(self.base, finished)
        self.assertEqual(data["status"], "idle")
        self.assertIsNone(data["question"])
        self.assertIn("ask_done", kinds(events))
        results = [event for event in events if event["kind"] == "tool_result"]
        self.assertEqual(results[0]["name"], "ask")
        self.assertTrue(
            results[0]["text"].startswith("The user answered:\n- Library: httpx\n\ndecision_id: dec-")
        )

    def test_a_multi_select_question_takes_every_pick(self):
        self.client.rounds = [
            tool_call(
                "ask",
                {
                    "questions": [
                        {
                            "header": "Extras",
                            "question": "Which extras?",
                            "multiSelect": True,
                            "options": [{"label": "tests"}, {"label": "docs"}],
                        }
                    ]
                },
            ),
            answer("Both."),
        ]
        request(self.base, "/api/ask", {"text": "pick"})
        data, _ = poll(self.base, lambda seen: any(e["kind"] == "ask" for e in seen))
        self.assertTrue(data["question"][0]["multiSelect"])
        request(
            self.base,
            "/api/answer",
            {"answers": [{"id": "q1", "selected": ["tests", "docs"]}]},
        )
        _, events = poll(self.base, finished)
        results = [event for event in events if event["kind"] == "tool_result"]
        self.assertTrue(results[0]["text"].startswith("The user answered:\n- Extras: tests, docs\n\ndecision_id: dec-"))

    def test_dismissing_a_question_ends_the_turn(self):
        self.client.rounds = [
            tool_call(
                "ask",
                {
                    "questions": [
                        {
                            "header": "Extras",
                            "question": "Which extras?",
                            "options": [{"label": "tests"}, {"label": "docs"}],
                        }
                    ]
                },
            ),
            answer("should not run"),
        ]
        request(self.base, "/api/ask", {"text": "pick"})
        poll(self.base, lambda seen: any(e["kind"] == "ask" for e in seen))
        status, body = request(self.base, "/api/answer", {"answers": []})
        self.assertTrue(json.loads(body)["ok"])
        data, events = poll(self.base, finished)
        self.assertEqual(data["status"], "idle")
        results = [event for event in events if event["kind"] == "tool_result"]
        self.assertTrue(results[0]["text"].startswith("The user dismissed the question"))
        self.assertEqual([event for event in events if event["kind"] == "content"], [])
        self.assertEqual(len(self.client.rounds), 1)

    def test_an_answer_without_a_question_is_refused(self):
        status, body = request(self.base, "/api/answer", {"answers": []})
        self.assertEqual(status, 200)
        self.assertEqual(
            json.loads(body),
            {"ok": False, "error": "no question is waiting for an answer"},
        )
        status, body = request(self.base, "/api/answer", {"answers": "no"})
        self.assertFalse(json.loads(body)["ok"])
        self.assertIn("answers must be a list", json.loads(body)["error"])

    def test_stop_cancels_a_waiting_question(self):
        self.client.rounds = [
            tool_call(
                "ask",
                {
                    "questions": [
                        {
                            "header": "H",
                            "question": "Q?",
                            "options": [{"label": "a"}, {"label": "b"}],
                        }
                    ]
                },
            ),
            answer("unused"),
        ]
        request(self.base, "/api/ask", {"text": "pick"})
        poll(self.base, lambda seen: any(e["kind"] == "ask" for e in seen))
        self.assertEqual(self.hub.cancel(), {"ok": True})
        data, events = poll(self.base, finished)
        self.assertEqual(data["status"], "idle")
        self.assertIsNone(data["question"])
        statuses = [event["text"] for event in events if event["kind"] == "status"]
        self.assertIn("Interrupted.", statuses)

    def test_todos_reach_the_page_and_clear_on_the_next_request(self):
        written = [{"content": "read the parser", "status": "completed"}]
        self.client.rounds = [
            tool_call("todo_write", {"todos": written}),
            answer("done"),
        ]
        request(self.base, "/api/ask", {"text": "go"})
        _, events = poll(self.base, finished)
        todos = [event for event in events if event["kind"] == "todos"]
        self.assertTrue(todos)
        self.assertEqual(todos[-1]["todos"], written)
        self.assertEqual(get_json(self.base, "/api/state")["todos"], written)

        self.client.rounds = [answer("again")]
        request(self.base, "/api/ask", {"text": "again"})
        _, events = poll(self.base, finished)
        todos = [event for event in events if event["kind"] == "todos"]
        self.assertEqual(todos[-1]["todos"], [])
        self.assertEqual(get_json(self.base, "/api/state")["todos"], [])

    def test_a_new_page_restores_the_task_list(self):
        written = [
            {"content": "read the parser", "status": "in_progress"},
            {"content": "ship", "status": "pending"},
        ]
        self.client.rounds = [
            tool_call("todo_write", {"todos": written}),
            answer("done"),
        ]
        request(self.base, "/api/ask", {"text": "go"})
        poll(self.base, finished)
        other = Hub(
            self.workspace,
            client_factory=lambda provider: ScriptedClient(),
            user_config=self.settings,
        )
        other.start()
        try:
            self.assertEqual(other.state()["todos"], written)
        finally:
            other.close()

    # ------------------------------------------------------------ settings

    def test_settings_are_kept_on_the_hub(self):
        status, body = request(
            self.base,
            "/api/settings",
            {"thinking": True, "verbose": True, "yes": True},
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            json.loads(body),
            {"ok": True, "thinking": True, "verbose": True, "yes": True},
        )
        state = get_json(self.base, "/api/state")
        self.assertTrue(state["thinking"])
        self.assertTrue(state["verbose"])
        self.assertTrue(state["yes"])

    def test_turn_options_survive_a_restart(self):
        request(self.base, "/api/settings", {"thinking": True, "verbose": True, "yes": True})
        other = Hub(
            self.workspace,
            client_factory=lambda provider: self.client,
            user_config=self.settings,
        )
        other.start()
        try:
            state = other.state()
            self.assertTrue(state["thinking"])
            self.assertTrue(state["verbose"])
            self.assertTrue(state["yes"])
        finally:
            other.close()
        request(self.base, "/api/settings", {"thinking": False})
        again = Hub(
            self.workspace,
            client_factory=lambda provider: self.client,
            user_config=self.settings,
        )
        again.start()
        try:
            self.assertFalse(again.state()["thinking"])
            self.assertTrue(again.state()["verbose"])
            self.assertTrue(again.state()["yes"])
        finally:
            again.close()

    def test_a_launch_flag_and_a_saved_option_both_hold(self):
        self.hub.set_settings(thinking=False, verbose=False, yes=False)
        flagged = Hub(
            self.workspace,
            client_factory=lambda provider: self.client,
            thinking=True,
            user_config=self.settings,
        )
        flagged.start()
        try:
            self.assertTrue(flagged.state()["thinking"])
            self.assertFalse(flagged.state()["verbose"])
        finally:
            flagged.close()

    def test_an_unknown_provider_is_refused(self):
        status, body = request(self.base, "/api/provider", {"name": "nowhere"})
        self.assertFalse(json.loads(body)["ok"])
        self.assertIn("unknown provider", json.loads(body)["error"])
        status, body = request(self.base, "/api/provider", {"name": "deepseek-flash_1"})
        self.assertTrue(json.loads(body)["ok"])
        state = get_json(self.base, "/api/state")
        self.assertEqual(state["provider"]["name"], "deepseek-flash_1")
        self.assertEqual(state["provider"]["model"], "deepseek-flash")

    def test_a_workspace_change_reads_that_session(self):
        other = Path(tempfile.mkdtemp(dir=self.workspace))
        folder = other / ".foo-ai"
        folder.mkdir()
        (folder / "session.jsonl").write_text(
            json.dumps({"role": "user", "content": "earlier question"}) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        status, body = request(self.base, "/api/workspace", {"path": str(other)})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])
        state = get_json(self.base, "/api/state")
        self.assertEqual(state["workspace"], str(other))
        self.assertEqual(state["session"]["messages"], 1)
        history = get_json(self.base, "/api/history")["messages"]
        self.assertEqual(history, [{"role": "user", "text": "earlier question"}])

    def test_a_missing_workspace_is_refused(self):
        status, body = request(self.base, "/api/workspace", {"path": str(self.workspace / "nope")})
        self.assertFalse(json.loads(body)["ok"])
        self.assertIn("not a directory", json.loads(body)["error"])
        self.assertEqual(self.hub.workspace, self.workspace)
        self.assertFalse(self.settings.exists())

    def test_a_workspace_switch_is_remembered_for_the_next_launch(self):
        other = Path(tempfile.mkdtemp(dir=self.workspace))
        status, body = request(self.base, "/api/workspace", {"path": str(other)})
        self.assertTrue(json.loads(body)["ok"], body)
        self.assertEqual(load_page_workspace(self.settings), other.resolve())
        self.assertEqual(resolve_workspace(None, self.settings), other.resolve())
        kept = tomllib.loads(self.settings.read_text(encoding="utf-8"))
        self.assertEqual(Path(kept["workspace"]), other.resolve())

    # ------------------------------------------------------- workspace picker

    def browse(self, path: str | None = None) -> dict:
        query = "" if path is None else "?path=" + urllib.parse.quote(path)
        return get_json(self.base, "/api/browse" + query)

    def test_the_picker_lists_folders_only(self):
        base = self.workspace / "picker"
        (base / "beta").mkdir(parents=True)
        (base / "alpha").mkdir()
        (base / ".hidden").mkdir()
        (base / "notes.txt").write_text("x", encoding="utf-8")
        listing = self.browse(str(base))
        self.assertTrue(listing["ok"])
        self.assertEqual(listing["path"], str(base))
        self.assertEqual(listing["parent"], str(base.parent))
        self.assertEqual([entry["name"] for entry in listing["entries"]], ["alpha", "beta"])
        self.assertEqual(listing["entries"][0]["path"], str(base / "alpha"))

    def test_the_picker_starts_at_the_open_workspace(self):
        listing = self.browse()
        self.assertTrue(listing["ok"])
        self.assertEqual(listing["path"], str(self.workspace))
        self.assertNotIn(".foo-ai", [entry["name"] for entry in listing["entries"]])

    def test_a_file_names_its_own_folder(self):
        folder = self.workspace / "picker"
        folder.mkdir()
        file = folder / "notes.txt"
        file.write_text("x", encoding="utf-8")
        listing = self.browse(str(file))
        self.assertTrue(listing["ok"])
        self.assertEqual(listing["path"], str(folder))

    def test_a_missing_folder_is_refused(self):
        listing = self.browse(str(self.workspace / "nope"))
        self.assertFalse(listing["ok"])
        self.assertIn("no such folder", listing["error"])

    def test_the_top_of_a_drive_has_no_parent(self):
        listing = self.browse(str(Path(self.workspace.anchor)))
        self.assertTrue(listing["ok"])
        self.assertIsNone(listing["parent"])
        for entry in listing["entries"]:
            self.assertTrue(Path(entry["path"]).is_dir())

    # ------------------------------------------------------ provider settings

    def test_saving_a_provider_writes_the_file_and_uses_it(self):
        status, body = request(
            self.base,
            "/api/provider/save",
            {
                "name": "deepseek-flash_1",
                "base_url": "https://api.deepseek.com/v1",
                "model": "deepseek-flash",
                "context_window": "128_000",
                "api_key": "sk-page",
            },
        )
        self.assertEqual(status, 200)
        result = json.loads(body)
        self.assertTrue(result["ok"])
        self.assertEqual(result["provider"]["api_key_set"], True)
        state = get_json(self.base, "/api/state")
        self.assertEqual(state["provider"]["name"], "deepseek-flash_1")
        self.assertEqual(state["provider"]["base_url"], "https://api.deepseek.com/v1")
        self.assertEqual(state["provider"]["context_window"], 128_000)
        self.assertTrue(state["provider"]["api_key_set"])
        self.assertEqual(state["settings_file"], str(self.settings))
        self.assertEqual(self.hub.agent.provider.api_key, "sk-page")
        saved = self.settings.read_text(encoding="utf-8")
        self.assertIn('default_provider = "deepseek-flash_1"', saved)
        self.assertIn('api_key = "sk-page"', saved)
        status, raw = request(self.base, "/api/state")
        self.assertNotIn(b"sk-page", raw)

    def test_the_saved_provider_is_read_again_by_a_new_session(self):
        self.hub.save_provider(
            "deepseek-flash_1",
            base_url="https://api.deepseek.com/v1",
            model="deepseek-flash",
            context_window=128_000,
            api_key="sk-page",
        )
        other = Hub(
            self.workspace,
            client_factory=lambda provider: self.client,
            user_config=self.settings,
        )
        other.start()
        try:
            state = other.state()
            self.assertEqual(state["provider"]["name"], "deepseek-flash_1")
            self.assertEqual(state["provider"]["model"], "deepseek-flash")
            self.assertEqual(state["provider"]["context_window"], 128_000)
            self.assertTrue(state["provider"]["api_key_set"])
        finally:
            other.close()

    def test_a_saved_key_is_kept_and_cleared_on_request(self):
        save = {
            "name": "home",
            "base_url": "http://127.0.0.1:9/v1",
            "model": "kept-model",
            "context_window": 1000,
        }
        status, body = request(self.base, "/api/provider/save", {**save, "api_key": "sk-page"})
        self.assertTrue(json.loads(body)["ok"])
        status, body = request(self.base, "/api/provider/save", save)
        self.assertTrue(json.loads(body)["ok"])
        self.assertTrue(get_json(self.base, "/api/state")["provider"]["api_key_set"])
        self.assertIn("sk-page", self.settings.read_text(encoding="utf-8"))
        saved = tomllib.loads(self.settings.read_text(encoding="utf-8"))
        self.assertEqual(
            saved["providers"]["home"]["extra_body"],
            {"chat_template_kwargs": {"reasoning_effort": "max", "thinking": True}},
        )
        status, body = request(self.base, "/api/provider/save", {**save, "api_key": ""})
        self.assertTrue(json.loads(body)["ok"])
        self.assertFalse(get_json(self.base, "/api/state")["provider"]["api_key_set"])
        self.assertNotIn("sk-page", self.settings.read_text(encoding="utf-8"))

    def test_the_local_reasoning_block_is_reported_and_saved(self):
        state = get_json(self.base, "/api/state")
        self.assertEqual(
            state["provider"]["chat_template_kwargs"],
            {"reasoning_effort": "max", "thinking": True},
        )
        status, body = request(
            self.base,
            "/api/provider/save",
            {
                "name": "home",
                "base_url": "http://127.0.0.1:9/v1",
                "model": "kept-model",
                "context_window": 1000,
                "chat_template_kwargs": {"reasoning_effort": " low ", "thinking": False},
            },
        )
        self.assertTrue(json.loads(body)["ok"], body)
        saved = tomllib.loads(self.settings.read_text(encoding="utf-8"))
        self.assertEqual(
            saved["providers"]["home"]["extra_body"],
            {"chat_template_kwargs": {"reasoning_effort": "low", "thinking": False}},
        )
        self.assertEqual(
            get_json(self.base, "/api/state")["provider"]["chat_template_kwargs"],
            {"reasoning_effort": "low", "thinking": False},
        )
        self.assertEqual(
            self.hub.agent.provider.extra_body,
            {"chat_template_kwargs": {"reasoning_effort": "low", "thinking": False}},
        )

    def test_a_blank_reasoning_effort_leaves_that_key_out(self):
        result = self.hub.save_provider(
            "home",
            base_url="http://127.0.0.1:9/v1",
            model="m",
            context_window=1,
            chat_template_kwargs={"reasoning_effort": "  ", "thinking": True},
        )
        self.assertTrue(result["ok"], result)
        saved = tomllib.loads(self.settings.read_text(encoding="utf-8"))
        self.assertEqual(
            saved["providers"]["home"]["extra_body"],
            {"chat_template_kwargs": {"thinking": True}},
        )

    def test_a_reasoning_save_keeps_the_rest_of_the_body(self):
        self.settings.parent.mkdir(parents=True)
        self.settings.write_text(
            "[providers.home.extra_body]\n"
            "stream_options = { include_usage = true }\n\n"
            "[providers.home.extra_body.chat_template_kwargs]\n"
            "thinking = true\n",
            encoding="utf-8",
            newline="\n",
        )
        result = self.hub.save_provider(
            "home",
            base_url="http://127.0.0.1:9/v1",
            model="m",
            context_window=1,
            chat_template_kwargs={"reasoning_effort": "low", "thinking": False},
        )
        self.assertTrue(result["ok"], result)
        saved = tomllib.loads(self.settings.read_text(encoding="utf-8"))
        self.assertEqual(
            saved["providers"]["home"]["extra_body"],
            {
                "stream_options": {"include_usage": True},
                "chat_template_kwargs": {"reasoning_effort": "low", "thinking": False},
            },
        )

    def test_a_launch_file_body_survives_a_reasoning_save(self):
        (self.workspace / "foo-ai.toml").write_text(
            "[providers.home.extra_body]\n"
            "temperature = 0.2\n\n"
            "[providers.home.extra_body.chat_template_kwargs]\n"
            'reasoning_effort = "max"\n'
            "thinking = true\n",
            encoding="utf-8",
            newline="\n",
        )
        result = self.hub.save_provider(
            "home",
            base_url="http://127.0.0.1:9/v1",
            model="m",
            context_window=1,
            chat_template_kwargs={"reasoning_effort": "low", "thinking": True},
        )
        self.assertTrue(result["ok"], result)
        saved = tomllib.loads(self.settings.read_text(encoding="utf-8"))
        self.assertEqual(
            saved["providers"]["home"]["extra_body"],
            {
                "temperature": 0.2,
                "chat_template_kwargs": {"reasoning_effort": "low", "thinking": True},
            },
        )
        # The request still sends the temperature the launch file set.
        self.assertEqual(self.hub.agent.provider.extra_body["temperature"], 0.2)

    def test_a_block_key_the_sheet_does_not_show_passes_through(self):
        self.settings.parent.mkdir(parents=True)
        self.settings.write_text(
            "[providers.home.extra_body.chat_template_kwargs]\n"
            'reasoning_effort = "max"\n'
            "enable_thinking = false\n",
            encoding="utf-8",
            newline="\n",
        )
        result = self.hub.save_provider(
            "home",
            base_url="http://127.0.0.1:9/v1",
            model="m",
            context_window=1,
            chat_template_kwargs={"reasoning_effort": "high", "thinking": True},
        )
        self.assertTrue(result["ok"], result)
        saved = tomllib.loads(self.settings.read_text(encoding="utf-8"))
        self.assertEqual(
            saved["providers"]["home"]["extra_body"]["chat_template_kwargs"],
            {"reasoning_effort": "high", "enable_thinking": False, "thinking": True},
        )

    def test_a_body_without_the_block_hides_it_in_the_state(self):
        self.settings.parent.mkdir(parents=True)
        self.settings.write_text(
            "[providers.home.extra_body]\ntemperature = 0.2\n",
            encoding="utf-8",
            newline="\n",
        )
        status, body = request(self.base, "/api/provider", {"name": "home"})
        self.assertTrue(json.loads(body)["ok"], body)
        self.assertIsNone(get_json(self.base, "/api/state")["provider"]["chat_template_kwargs"])

    def test_a_cloud_provider_has_no_local_reasoning_block(self):
        status, body = request(self.base, "/api/provider", {"name": "deepseek-flash_1"})
        self.assertTrue(json.loads(body)["ok"], body)
        self.assertIsNone(get_json(self.base, "/api/state")["provider"]["chat_template_kwargs"])
        result = self.hub.save_provider(
            "deepseek-flash_1",
            base_url="https://api.deepseek.com/v1",
            model="deepseek-flash",
            context_window=128_000,
            chat_template_kwargs={"reasoning_effort": "max", "thinking": True},
        )
        self.assertFalse(result["ok"])
        self.assertIn("no chat_template_kwargs", result["error"])
        self.assertFalse(self.settings.exists())

    def test_a_bad_reasoning_block_is_refused(self):
        cases = [
            ("max", "chat_template_kwargs must be an object"),
            ({"reasoning_effort": 3}, "reasoning_effort must be a string"),
            ({"thinking": "yes"}, "thinking must be true or false"),
        ]
        for value, message in cases:
            status, raw = request(
                self.base,
                "/api/provider/save",
                {
                    "name": "home",
                    "base_url": "http://127.0.0.1:9/v1",
                    "model": "m",
                    "context_window": 1,
                    "chat_template_kwargs": value,
                },
            )
            self.assertEqual(status, 200)
            self.assertFalse(json.loads(raw)["ok"], value)
            self.assertIn(message, json.loads(raw)["error"])
        self.assertFalse(self.settings.exists())

    def test_a_bad_provider_save_is_refused(self):
        cases = [
            (
                {"name": "nowhere", "base_url": "https://x/v1", "model": "m", "context_window": 1},
                "unknown provider",
            ),
            (
                {"name": "home", "base_url": "ftp://x/v1", "model": "m", "context_window": 1},
                "http or https",
            ),
            (
                {"name": "home", "base_url": "https://x/v1", "model": " ", "context_window": 1},
                "model is empty",
            ),
            (
                {"name": "home", "base_url": "https://x/v1", "model": "m", "context_window": "many"},
                "context_window must be zero or a positive integer",
            ),
            (
                {"name": "home", "base_url": "https://x/v1", "model": "m", "context_window": 1, "api_key": 5},
                "api_key must be a string",
            ),
        ]
        for body, message in cases:
            status, raw = request(self.base, "/api/provider/save", body)
            self.assertEqual(status, 200)
            self.assertFalse(json.loads(raw)["ok"], body)
            self.assertIn(message, json.loads(raw)["error"])
        self.assertFalse(self.settings.exists())

    def test_a_provider_save_is_refused_while_a_request_runs(self):
        self.client.rounds = [tool_call("shell", {"command": "echo hi"}), answer("never")]
        request(self.base, "/api/ask", {"text": "run it"})
        poll(self.base, lambda seen: any(e["kind"] == "confirm" for e in seen))
        result = self.hub.save_provider(
            "home", base_url="http://127.0.0.1:9/v1", model="m", context_window=1
        )
        self.assertFalse(result["ok"])
        self.assertIn("a request is running", result["error"])
        self.assertFalse(self.settings.exists())
        self.hub.cancel()
        poll(self.base, finished)

    def test_a_page_without_a_settings_file_refuses_a_save(self):
        other = Hub(self.workspace, client_factory=lambda provider: self.client)
        other.start()
        try:
            result = other.save_provider(
                "home", base_url="http://127.0.0.1:9/v1", model="m", context_window=1
            )
        finally:
            other.close()
        self.assertFalse(result["ok"])
        self.assertIn("no settings file", result["error"])

    # --------------------------------------------------------------- guards

    def test_another_host_name_is_refused(self):
        status, _ = request(self.base, "/api/state", headers={"Host": "evil.example:80"})
        self.assertEqual(status, 403)
        status, _ = request(self.base, "/", headers={"Host": "evil.example:80"})
        self.assertEqual(status, 403)

    def test_another_origin_is_refused(self):
        status, _ = request(
            self.base,
            "/api/ask",
            {"text": "hi"},
            headers={"Origin": "http://evil.example"},
        )
        self.assertEqual(status, 403)
        status, _ = request(
            self.base,
            "/api/ask",
            {"text": "hi"},
            headers={"Origin": self.base},
        )
        self.assertEqual(status, 200)

    def test_a_body_that_is_not_json_is_refused(self):
        probe = urllib.request.Request(
            self.base + "/api/ask",
            data=b"text=hi",
            headers={"Content-Type": "text/plain"},
        )
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(probe, timeout=15)
        self.assertEqual(caught.exception.code, 415)
        probe = urllib.request.Request(
            self.base + "/api/ask",
            data=b"not json",
            headers={"Content-Type": "application/json"},
        )
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(probe, timeout=15)
        self.assertEqual(caught.exception.code, 400)

    def test_a_refused_write_drains_its_body(self):
        """A refused POST must not leave bytes that the next request would parse."""
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        try:
            connection.request(
                "POST",
                "/api/ask",
                body=b"text=hi",
                headers={"Content-Type": "text/plain"},
            )
            first = connection.getresponse()
            self.assertEqual(first.status, 415)
            first.read()
            connection.request("GET", "/api/state")
            second = connection.getresponse()
            self.assertEqual(second.status, 200)
            self.assertEqual(json.loads(second.read())["workspace"], str(self.workspace))
        finally:
            connection.close()


class GuiCommandLineTests(unittest.TestCase):
    def test_the_config_flag_is_accepted(self):
        args = build_parser().parse_args(["--config", "settings/foo-ai.toml"])
        self.assertEqual(args.config, "settings/foo-ai.toml")
        self.assertIsNone(build_parser().parse_args([]).config)

    def test_version_prints_and_exits_zero(self):
        with tempfile.TemporaryDirectory() as folder:
            result = subprocess.run(
                [sys.executable, "-m", "foo_ai.gui", "--version"],
                cwd=folder,
                capture_output=True,
                text=True,
                encoding="utf-8",
                env=clean_env(),
            )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "Foo AI 0.1.0\n")

    def test_a_bad_step_budget_exits_one(self):
        with tempfile.TemporaryDirectory() as folder:
            result = subprocess.run(
                [sys.executable, "-m", "foo_ai.gui", "--max-steps", "0"],
                cwd=folder,
                capture_output=True,
                text=True,
                encoding="utf-8",
                env=clean_env(),
            )
        self.assertEqual(result.returncode, 1)
        self.assertIn("max_steps must be a positive integer", result.stderr)

    def test_a_bad_port_exits_one(self):
        with tempfile.TemporaryDirectory() as folder:
            result = subprocess.run(
                [sys.executable, "-m", "foo_ai.gui", "--port", "70000"],
                cwd=folder,
                capture_output=True,
                text=True,
                encoding="utf-8",
                env=clean_env(),
            )
        self.assertEqual(result.returncode, 1)
        self.assertIn("outside 0..65535", result.stderr)

    def test_a_negative_read_timeout_exits_one(self):
        with tempfile.TemporaryDirectory() as folder:
            result = subprocess.run(
                [sys.executable, "-m", "foo_ai.gui", "--read-timeout=-1", "--no-browser"],
                cwd=folder,
                capture_output=True,
                text=True,
                encoding="utf-8",
                env=clean_env(),
            )
        self.assertEqual(result.returncode, 1)
        self.assertIn("read_timeout must be zero or a positive number of seconds", result.stderr)

    def test_a_config_error_exits_one(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "foo-ai.toml").write_text(
                'max_steps = "many"\n', encoding="utf-8", newline="\n"
            )
            result = subprocess.run(
                [sys.executable, "-m", "foo_ai.gui", "--no-browser"],
                cwd=folder,
                capture_output=True,
                text=True,
                encoding="utf-8",
                env=clean_env(),
            )
        self.assertEqual(result.returncode, 1)
        self.assertIn("max_steps must be a positive integer", result.stderr)

    def test_the_command_announces_the_url_it_serves(self):
        with tempfile.TemporaryDirectory() as folder:
            proc = subprocess.Popen(
                [sys.executable, "-m", "foo_ai.gui", "--no-browser", "--port", "0"],
                cwd=folder,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                env=clean_env(),
            )
            try:
                line = proc.stdout.readline()
                self.assertTrue(line.startswith("foo-ai-desktop:"), line)
                url = line.split(":", 1)[1].strip()
                self.assertTrue(url.startswith("http://127.0.0.1:"), url)
                self.assertTrue(url.endswith("/"), url)
                status, body = request(url, "api/state")
                self.assertEqual(status, 200)
                state = json.loads(body)
                self.assertEqual(state["provider"]["name"], "home")
                self.assertEqual(Path(state["workspace"]), Path(folder).resolve())
            finally:
                proc.terminate()
                try:
                    proc.communicate(timeout=15)
                except subprocess.TimeoutExpired:  # pragma: no cover - defensive
                    proc.kill()
                    proc.communicate()


    def test_the_config_file_feeds_the_page(self):
        with tempfile.TemporaryDirectory() as folder:
            settings = Path(folder) / "settings" / "foo-ai.toml"
            settings.parent.mkdir()
            settings.write_text(
                'default_provider = "deepseek-flash_1"\n\n'
                "[providers.deepseek-flash_1]\n"
                'base_url = "https://api.deepseek.com/v1"\n'
                'model = "deepseek-flash"\n'
                "context_window = 128000\n",
                encoding="utf-8",
                newline="\n",
            )
            proc = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "foo_ai.gui",
                    "--no-browser",
                    "--port",
                    "0",
                    "--config",
                    str(settings),
                ],
                cwd=folder,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                env=clean_env(),
            )
            try:
                line = proc.stdout.readline()
                self.assertTrue(line.startswith("foo-ai-desktop:"), line)
                url = line.split(":", 1)[1].strip()
                state = get_json(url, "api/state")
                self.assertEqual(state["provider"]["name"], "deepseek-flash_1")
                self.assertEqual(state["provider"]["base_url"], "https://api.deepseek.com/v1")
                self.assertEqual(state["provider"]["context_window"], 128_000)
                self.assertEqual(state["settings_file"], str(settings))
            finally:
                proc.terminate()
                try:
                    proc.communicate(timeout=15)
                except subprocess.TimeoutExpired:  # pragma: no cover - defensive
                    proc.kill()
                    proc.communicate()

    def test_the_saved_workspace_feeds_the_page(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            saved = root / "project"
            saved.mkdir()
            settings = root / "settings" / "foo-ai.toml"
            save_page_workspace(settings, saved)
            proc = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "foo_ai.gui",
                    "--no-browser",
                    "--port",
                    "0",
                    "--config",
                    str(settings),
                ],
                cwd=str(root),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                env=clean_env(),
            )
            try:
                line = proc.stdout.readline()
                self.assertTrue(line.startswith("foo-ai-desktop:"), line)
                url = line.split(":", 1)[1].strip()
                state = get_json(url, "api/state")
                self.assertEqual(Path(state["workspace"]), saved.resolve())
                self.assertEqual(state["settings_file"], str(settings))
            finally:
                proc.terminate()
                try:
                    proc.communicate(timeout=15)
                except subprocess.TimeoutExpired:  # pragma: no cover - defensive
                    proc.kill()
                    proc.communicate()


if __name__ == "__main__":
    unittest.main()
