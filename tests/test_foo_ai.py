import json
import tempfile
import unittest
from pathlib import Path

from foo_ai.client import Client, assistant_message
from foo_ai.config import Provider, load_provider
from foo_ai.tools import run_tool, workspace_path


class RequestBodyTest(unittest.TestCase):
    def test_default_provider_and_extra_body(self):
        provider = Provider()
        self.assertEqual(provider.name, "home")
        self.assertEqual(provider.base_url, "http://100.109.56.33:81/v1")
        self.assertEqual(provider.model, "DeepSeek-V4.1-Flash-1017")
        body = Client(provider).build_body([{"role": "user", "content": "hi"}], [])
        self.assertEqual(body["model"], "DeepSeek-V4.1-Flash-1017")
        self.assertEqual(
            body["chat_template_kwargs"],
            {"reasoning_effort": "max", "thinking": True},
        )
        self.assertNotIn("thinking", body)
        self.assertTrue(body["stream"])

    def test_toml_override(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "foo-ai.toml"
            path.write_text(
                '[provider]\nname = "lab"\nmodel = "other"\n\n'
                '[provider.extra_body.chat_template_kwargs]\nreasoning_effort = "high"\nthinking = false\n',
                encoding="utf-8",
            )
            provider = load_provider(path)
        self.assertEqual(provider.name, "lab")
        self.assertEqual(provider.model, "other")
        self.assertEqual(provider.extra_body["chat_template_kwargs"]["reasoning_effort"], "high")
        self.assertIs(provider.extra_body["chat_template_kwargs"]["thinking"], False)


class HistoryTest(unittest.TestCase):
    def test_tool_turn_replays_reasoning_content(self):
        stored = assistant_message(
            {
                "content": None,
                "reasoning_content": "We need to read the file.",
                "tool_calls": [
                    {"id": "call_1", "type": "function", "function": {"name": "read_file", "arguments": "{\"path\":\"a.py\"}"}}
                ],
            }
        )
        encoded = json.dumps(stored)
        self.assertIn('"reasoning_content": "We need to read the file."', encoded)
        self.assertIn('"content": null', encoded)
        self.assertNotIn("tool_call_id", encoded)
        self.assertEqual(stored["tool_calls"][0]["function"]["arguments"], '{"path":"a.py"}')


class ToolTest(unittest.TestCase):
    def test_path_escape_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(ValueError):
                workspace_path(root, "../outside.txt")

    def test_read_and_write_stay_in_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            message = run_tool("write_file", {"path": "notes/a.txt", "content": "hello\n"}, root)
            self.assertIn("notes/a.txt", message.replace("\\", "/"))
            text = run_tool("read_file", {"path": "notes/a.txt"}, root)
            self.assertIn("hello", text)
            listing = run_tool("list_dir", {"path": "notes"}, root)
            self.assertIn("a.txt", listing)


if __name__ == "__main__":
    unittest.main()
