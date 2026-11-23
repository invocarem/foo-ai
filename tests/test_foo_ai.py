import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from foo_ai.agent import Agent
from foo_ai.cli import confirm_shell
from foo_ai.client import ChatError, Client, _read_sse, assistant_message
from foo_ai.config import ConfigError, McpServer, Provider, load_mcp_servers, load_provider
from foo_ai.mcp import McpHost, format_tool_result, model_tool_name
from foo_ai.tools import TOOL_OUTPUT_LIMIT, run_tool, workspace_path
from foo_ai.usage import format_tokens, format_usage_line, parse_usage
from foo_ai.context import (
    PRUNE_MARKER,
    accept_token_ratio,
    discard_to_trigger,
    estimate_tokens,
    hard_input_ceiling,
    prune_tool_results,
    summary_prefix,
    tail_budget_tokens,
    transcript_for_summary,
    trigger_tokens,
    with_summary,
)
from foo_ai.session import close_open_tool_calls, load_session


class RequestBodyTest(unittest.TestCase):
    def test_default_provider_and_extra_body(self):
        provider = Provider()
        self.assertEqual(provider.name, "home")
        self.assertEqual(provider.base_url, "http://127.0.0.1:8100/v1")
        self.assertEqual(provider.model, "DeepSeek-V4.1-Flash-1017")
        body = Client(provider).build_body([{"role": "user", "content": "hi"}], [])
        self.assertEqual(body["model"], "DeepSeek-V4.1-Flash-1017")
        self.assertEqual(
            body["chat_template_kwargs"],
            {"reasoning_effort": "max", "thinking": True},
        )
        self.assertNotIn("thinking", body)
        self.assertTrue(body["stream"])
        self.assertEqual(body["stream_options"], {"include_usage": True})
        self.assertIn("tools", body)

    def test_summary_request_omits_tools(self):
        body = Client(Provider()).build_body([{"role": "user", "content": "hi"}], None)
        self.assertNotIn("tools", body)

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

    def test_builtin_deepseek_flash_provider(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.toml"
            provider = load_provider(missing, name="deepseek-flash_1")
        self.assertEqual(provider.name, "deepseek-flash_1")
        self.assertEqual(provider.model, "deepseek-flash")
        self.assertEqual(provider.chat_url, "https://api.deepseek.com/v1/chat/completions")
        self.assertEqual(provider.extra_body, {})
        self.assertEqual(provider.api_key, "")
        full = Provider(base_url="https://api.deepseek.com/v1/chat/completions", extra_body={})
        self.assertEqual(full.chat_url, "https://api.deepseek.com/v1/chat/completions")

    def test_providers_keep_separate_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "foo-ai.toml"
            path.write_text(
                '[provider]\nname = "home"\napi_key = "home-key"\n\n'
                '[providers.deepseek-flash_1]\napi_key = "flash-key"\n',
                encoding="utf-8",
            )
            home = load_provider(path)
            flash = load_provider(path, name="deepseek-flash_1")
        self.assertEqual(home.name, "home")
        self.assertEqual(home.api_key, "home-key")
        self.assertIn("chat_template_kwargs", home.extra_body)
        self.assertEqual(flash.api_key, "flash-key")
        self.assertEqual(flash.model, "deepseek-flash")
        self.assertEqual(flash.extra_body, {})

    def test_default_provider_and_unknown_name(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "foo-ai.toml"
            path.write_text('default_provider = "deepseek-flash_1"\n', encoding="utf-8")
            selected = load_provider(path)
            with self.assertRaises(ConfigError):
                load_provider(path, name="missing")
        self.assertEqual(selected.name, "deepseek-flash_1")
        self.assertEqual(selected.context_window, 1_000_000)

    def test_context_window_override(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "foo-ai.toml"
            path.write_text("[providers.home]\ncontext_window = 131072\n", encoding="utf-8")
            provider = load_provider(path)
        self.assertEqual(provider.context_window, 131072)


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

    def test_edit_replaces_one_match_and_reports_a_diff(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_tool("write_file", {"path": "a.txt", "content": "hello\n"}, root)
            edited = run_tool(
                "edit_file",
                {"path": "a.txt", "old_string": "hello", "new_string": "hello world"},
                root,
            )
            self.assertIn("+hello world", edited)
            self.assertEqual((root / "a.txt").read_text(encoding="utf-8"), "hello world\n")

    def test_edit_rejects_ambiguous_and_missing_matches(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "a.txt"
            path.write_text("same\nsame\n", encoding="utf-8")
            ambiguous = run_tool(
                "edit_file",
                {"path": "a.txt", "old_string": "same", "new_string": "other"},
                root,
            )
            self.assertIn("matched 2 times", ambiguous)
            self.assertEqual(path.read_text(encoding="utf-8"), "same\nsame\n")
            missing = run_tool(
                "edit_file",
                {"path": "a.txt", "old_string": "absent", "new_string": "other"},
                root,
            )
            self.assertIn("not found", missing)

    def test_edit_matches_crlf_text_from_the_numbered_view(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "a.txt"
            path.write_bytes(b"alpha\r\nbeta\r\n")
            edited = run_tool(
                "edit_file",
                {"path": "a.txt", "old_string": "alpha\nbeta", "new_string": "alpha\ngamma"},
                root,
            )
            self.assertIn("edited a.txt", edited)
            self.assertEqual(path.read_bytes(), b"alpha\r\ngamma\r\n")

    def test_write_refuses_an_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_tool("write_file", {"path": "a.txt", "content": "keep\n"}, root)
            refused = run_tool("write_file", {"path": "a.txt", "content": "replace\n"}, root)
            self.assertIn("edit_file", refused)
            self.assertEqual((root / "a.txt").read_text(encoding="utf-8"), "keep\n")

    def test_edit_rejects_a_path_outside_the_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            escaped = run_tool(
                "edit_file",
                {"path": "../outside.txt", "old_string": "a", "new_string": "b"},
                root,
            )
            self.assertIn("escapes", escaped)

    def test_shell_waits_for_approval(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            denied = run_tool(
                "shell",
                {"command": "echo hi > ran.txt"},
                root,
                approve_shell=lambda _command: False,
            )
            self.assertIn("not approved", denied)
            self.assertFalse((root / "ran.txt").exists())
            approved = run_tool("shell", {"command": "echo hi"}, root, approve_shell=lambda _command: True)
            self.assertIn("hi", approved)
            self.assertIn("[exit 0]", approved)


class ConfirmTest(unittest.TestCase):
    def test_yes_flag_and_answers(self):
        with redirect_stdout(StringIO()):
            self.assertTrue(confirm_shell("echo hi", assume_yes=True, is_tty=False))
            self.assertFalse(confirm_shell("echo hi", assume_yes=False, is_tty=False))
            self.assertTrue(confirm_shell("echo hi", assume_yes=False, is_tty=True, read_line=lambda _prompt: "y"))
            self.assertFalse(confirm_shell("echo hi", assume_yes=False, is_tty=True, read_line=lambda _prompt: "no"))


class ContextTest(unittest.TestCase):
    def test_trigger_is_eighty_percent_of_the_window(self):
        self.assertEqual(trigger_tokens(1_000_000), 800_000)
        self.assertEqual(tail_budget_tokens(1_000_000), 160_000)
        self.assertEqual(hard_input_ceiling(1_000_000), 999_744)
        self.assertEqual(800_000 - 178_100, 621_900)
        self.assertEqual(format_tokens(800_000), "800K")
        self.assertEqual(format_tokens(621_900), "621.9K")
        self.assertEqual(trigger_tokens(0), 0)
        self.assertIsNone(accept_token_ratio(178_100, 0))
        self.assertEqual(accept_token_ratio(100, 100), 1.0)
        self.assertIsNone(accept_token_ratio(100, 10_000))

    def test_prune_cuts_the_middle_of_every_long_tool_result(self):
        body = "A" * 5000 + "MIDDLE" + "B" * 4000
        messages = [
            {"role": "assistant", "content": "reading", "reasoning_content": "keep"},
            {"role": "tool", "name": "read_file", "content": "A" * 8000},
        ]
        self.assertIs(prune_tool_results(messages), messages)
        messages[1]["content"] = body + "C" * 200
        pruned = prune_tool_results(messages)
        self.assertEqual(messages[1]["content"][:6], "AAAAAA")
        self.assertNotIn(PRUNE_MARKER, messages[1]["content"])
        self.assertTrue(pruned[1]["content"].startswith("A" * 4096))
        self.assertIn(PRUNE_MARKER, pruned[1]["content"])
        self.assertTrue(pruned[1]["content"].endswith("C" * 200))
        self.assertNotIn("MIDDLE", pruned[1]["content"])
        self.assertEqual(pruned[0]["reasoning_content"], "keep")

    def test_summary_folds_earlier_turns_and_keeps_reasoning_out(self):
        messages = [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "Q" * 2000},
            {
                "role": "assistant",
                "content": "reading",
                "reasoning_content": "R" * 500,
                "tool_calls": [{"id": "c1", "function": {"name": "read_file", "arguments": "{}"}}],
            },
            {"role": "tool", "tool_call_id": "c1", "name": "read_file", "content": "body" * 200},
            {"role": "user", "content": "continue"},
        ]
        self.assertIsNone(summary_prefix(messages, context_window=1_000_000))
        prefix = summary_prefix(messages, context_window=700)
        self.assertIsNotNone(prefix)
        assert prefix is not None
        self.assertEqual(prefix[0]["content"], "Q" * 2000)
        rendered = transcript_for_summary(prefix)
        self.assertNotIn("R" * 80, rendered)
        self.assertIn("…[snipped,", rendered)
        folded = with_summary(messages, "Renamed the function.", prefix)
        encoded = json.dumps(folded)
        self.assertIn("Renamed the function.", encoded)
        self.assertNotIn("R" * 80, encoded)
        self.assertNotIn("Q" * 100, encoded)
        self.assertEqual(folded[-1]["content"], "continue")

    def test_live_turn_is_not_summarized_when_it_is_the_bulk(self):
        messages = [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "earlier"},
            {"role": "assistant", "content": "ok", "reasoning_content": "short"},
            {"role": "user", "content": "Z" * 5000},
        ]
        self.assertIsNone(summary_prefix(messages, context_window=1500))

    def test_discard_drops_oldest_groups_only_at_the_hard_ceiling(self):
        messages = [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "Q" * 10000},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "c1", "function": {"name": "read_file", "arguments": "{}"}}],
            },
            {"role": "tool", "tool_call_id": "c1", "name": "read_file", "content": "Z" * 10000},
            {"role": "user", "content": "continue"},
        ]
        self.assertIs(discard_to_trigger(messages, context_window=20_000), messages)
        discarded = discard_to_trigger(messages, context_window=4_000)
        self.assertEqual(discarded[1]["content"], "Earlier turns were removed to fit the context window.")
        self.assertEqual(discarded[-1]["content"], "continue")
        self.assertNotIn("Q" * 100, json.dumps(discarded))
        self.assertLess(estimate_tokens(discarded), trigger_tokens(4_000))


class SessionTest(unittest.TestCase):
    def test_resume_archive_and_broken_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = ScriptedClient([{"role": "assistant", "content": "done", "reasoning_content": ""}])
            Agent(client, root).ask("hello secret-token")
            resumed = Agent(ScriptedClient([]), root)
            self.assertIn("Resumed", resumed.session_note)
            self.assertEqual(resumed.messages[-1]["content"], "done")
            hidden = run_tool("grep", {"pattern": "secret-token"}, root)
            self.assertEqual(hidden, "[no matches]")
            note = resumed.reset()
            self.assertIn("Archived .foo-ai/session-", note)
            self.assertFalse((root / ".foo-ai" / "session.jsonl").exists())
            self.assertTrue(list((root / ".foo-ai").glob("session-*.jsonl")))

            broken = root / ".foo-ai" / "session.jsonl"
            broken.write_text("{not json\n", encoding="utf-8")
            fresh = Agent(ScriptedClient([]), root)
            self.assertIn("unreadable", fresh.session_note)
            self.assertTrue((root / ".foo-ai" / "session.jsonl.broken").is_file())
            self.assertEqual(len(fresh.messages), 1)

    def test_open_tool_call_is_closed_before_the_next_user_turn(self):
        call = {"id": "c1", "type": "function", "function": {"name": "shell", "arguments": "{}"}}
        repaired = close_open_tool_calls(
            [
                {"role": "user", "content": "go"},
                {"role": "assistant", "content": None, "tool_calls": [call]},
                {"role": "user", "content": "next"},
            ]
        )
        self.assertEqual(repaired[2]["role"], "tool")
        self.assertEqual(repaired[2]["tool_call_id"], "c1")
        self.assertIn("interrupted", repaired[2]["content"])
        self.assertEqual(repaired[3]["content"], "next")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            directory_path = root / ".foo-ai"
            directory_path.mkdir()
            lines = [
                {"role": "system", "content": "old"},
                {"role": "assistant", "content": None, "tool_calls": [call]},
            ]
            payload = "\n".join(json.dumps(line) for line in lines) + "\n"
            (directory_path / "session.jsonl").write_text(payload, encoding="utf-8")
            messages, _note = load_session(root, "current")
            self.assertEqual(messages[0]["content"], "current")
            self.assertEqual(messages[-1]["role"], "tool")


class AgentLoopTest(unittest.TestCase):
    def test_below_the_trigger_the_transcript_is_sent_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = ScriptedClient([{"role": "assistant", "content": "next", "reasoning_content": "keep-me"}])
            agent = Agent(client, root)
            agent.messages = [
                {"role": "system", "content": "s"},
                {"role": "user", "content": "first"},
                {
                    "role": "assistant",
                    "content": "",
                    "reasoning_content": "keep-me",
                    "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}],
                },
                {"role": "tool", "tool_call_id": "c1", "name": "read_file", "content": "Z" * 9000},
                {"role": "assistant", "content": "read it", "reasoning_content": ""},
            ]
            self.assertEqual(agent.ask("continue"), "next")
            sent = json.dumps(client.calls[0]["messages"])
            self.assertIn("Z" * 9000, sent)
            self.assertIn("keep-me", sent)
            self.assertNotIn(PRUNE_MARKER, sent)
            saved = (root / ".foo-ai" / "session.jsonl").read_text(encoding="utf-8")
            self.assertIn("Z" * 9000, saved)
            self.assertEqual(len(client.calls), 1)

    def test_compression_is_saved_and_skips_the_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = ScriptedClient([{"role": "assistant", "content": "next", "reasoning_content": ""}])
            agent = Agent(client, root)
            agent.context_window = 2500
            agent.messages = [
                {"role": "system", "content": "s"},
                {"role": "user", "content": "first"},
                {"role": "assistant", "content": "reading", "reasoning_content": "keep-me"},
                {"role": "tool", "tool_call_id": "c1", "name": "read_file", "content": "Z" * 9000},
            ]
            events = []
            self.assertEqual(agent.ask("continue", events.append), "next")
            self.assertEqual(len(client.calls), 1)
            self.assertIsNotNone(client.calls[0]["tools"])
            sent = json.dumps(client.calls[0]["messages"])
            self.assertIn(PRUNE_MARKER, sent)
            self.assertNotIn("Z" * 9000, sent)
            self.assertIn("keep-me", sent)
            saved = (root / ".foo-ai" / "session.jsonl").read_text(encoding="utf-8")
            self.assertIn(PRUNE_MARKER, saved)
            self.assertNotIn("Z" * 9000, saved)
            self.assertEqual([event["text"] for event in events if event["type"] == "status"], ["Compressed old tool results."])

    def test_one_summary_replaces_earlier_turns(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = ScriptedClient(
                [
                    {"role": "assistant", "content": "User wanted a rename.", "reasoning_content": ""},
                    {"role": "assistant", "content": "Finished.", "reasoning_content": "small"},
                ]
            )
            agent = Agent(client, root)
            agent.context_window = 4000
            agent.messages = [
                {"role": "system", "content": "s"},
                {"role": "user", "content": "rename it"},
                {
                    "role": "assistant",
                    "content": "reading",
                    "reasoning_content": "R" * 5000,
                    "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}],
                },
                {"role": "tool", "tool_call_id": "c1", "name": "read_file", "content": "Z" * 8000},
                {"role": "assistant", "content": "I read it", "reasoning_content": ""},
            ]
            events = []
            answer = agent.ask("what next", events.append)
            self.assertEqual(answer, "Finished.")
            self.assertIsNone(client.calls[0]["tools"])
            self.assertNotIn("R" * 1000, json.dumps(client.calls[0]["messages"]))
            self.assertNotIn("Z" * 1000, json.dumps(client.calls[0]["messages"]))
            answer_request = json.dumps(client.calls[1]["messages"])
            self.assertIn("Earlier conversation, summarized:", answer_request)
            self.assertIn("what next", answer_request)
            self.assertNotIn("R" * 1000, answer_request)
            self.assertEqual([event["text"] for event in events if event["type"] == "status"], ["Summarizing earlier turns."])
            saved = (root / ".foo-ai" / "session.jsonl").read_text(encoding="utf-8")
            self.assertIn("User wanted a rename.", saved)
            self.assertNotIn("R" * 1000, saved)
            self.assertEqual(len(client.calls), 2)

    def test_summary_failure_below_the_ceiling_keeps_the_transcript(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = FailingSummaryClient()
            agent = Agent(client, root)
            agent.context_window = 1550
            agent.messages = [
                {"role": "system", "content": "s"},
                {"role": "user", "content": "rename it"},
                {"role": "assistant", "content": "reading", "reasoning_content": "R" * 5000},
            ]
            events = []
            answer = agent.ask("what next", events.append)
            self.assertEqual(answer, "still here")
            self.assertIn("R" * 5000, json.dumps(agent.messages))
            self.assertTrue(any(event["type"] == "status" and event["text"].startswith("Summary skipped:") for event in events))
            self.assertFalse(any(event["text"] == "Discarded earlier turns." for event in events if event["type"] == "status"))

    def test_summary_failure_at_the_ceiling_discards_earlier_turns(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = FailingSummaryClient()
            agent = Agent(client, root)
            agent.context_window = 4000
            agent.messages = [
                {"role": "system", "content": "s"},
                {"role": "user", "content": "rename it"},
                {"role": "assistant", "content": "reading", "reasoning_content": "R" * 20000},
            ]
            events = []
            answer = agent.ask("what next", events.append)
            self.assertEqual(answer, "still here")
            saved = json.dumps(agent.messages)
            self.assertNotIn("R" * 1000, saved)
            self.assertIn("Earlier turns were removed to fit the context window.", saved)
            self.assertIn("what next", saved)
            statuses = [event["text"] for event in events if event["type"] == "status"]
            self.assertEqual(
                statuses,
                ["Summarizing earlier turns.", "Summary skipped: HTTP 500 from summary", "Discarded earlier turns."],
            )


class UsageTest(unittest.TestCase):
    def test_compact_counts_and_cache_rate(self):
        self.assertEqual(format_tokens(178100), "178.1K")
        self.assertEqual(format_tokens(1_000_000), "1M")
        self.assertEqual(format_tokens(142000), "142K")
        self.assertEqual(format_tokens(999), "999")
        usage = parse_usage(
            {
                "prompt_tokens": 178100,
                "completion_tokens": 624,
                "prompt_cache_hit_tokens": 175641,
                "prompt_cache_miss_tokens": 2459,
                "completion_tokens_details": {"reasoning_tokens": 100},
            }
        )
        self.assertEqual(format_usage_line(usage, 1_000_000), "context 178.1K/1M · usage total 178100 624 · cache 98.62%")

    def test_nested_cache_tokens_fill_the_miss(self):
        usage = parse_usage({"prompt_tokens": 1000, "completion_tokens": 10, "prompt_tokens_details": {"cached_tokens": 600}})
        self.assertEqual(usage["cache_hit_tokens"], 600)
        self.assertEqual(usage["cache_miss_tokens"], 400)
        self.assertEqual(format_usage_line(usage, 0), "context 1K · usage total 1000 10 · cache 60.00%")

    def test_stream_usage_survives_an_empty_choice(self):
        events = list(
            _read_sse(
                [
                    b'data: {"choices":[{"delta":{"content":"hi"}}]}\n',
                    b"\n",
                    b'data: {"choices":[],"usage":{"prompt_tokens":178100,"completion_tokens":624,"prompt_cache_hit_tokens":175641,"prompt_cache_miss_tokens":2459}}\n',
                    b"data: [DONE]\n",
                ]
            )
        )
        self.assertEqual(events[0]["text"], "hi")
        self.assertEqual(events[1]["usage"]["prompt_tokens"], 178100)
        self.assertEqual(events[2]["message"]["content"], "hi")

    def test_agent_prints_the_usage_line(self):
        with tempfile.TemporaryDirectory() as directory:
            client = ScriptedClient(
                [
                    {
                        "role": "assistant",
                        "content": "done",
                        "reasoning_content": "",
                        "usage": {
                            "prompt_tokens": 178100,
                            "completion_tokens": 624,
                            "cache_hit_tokens": 175641,
                            "cache_miss_tokens": 2459,
                        },
                    }
                ]
            )
            events = []
            answer = Agent(client, Path(directory)).ask("hello", events.append)
        self.assertEqual(answer, "done")
        self.assertEqual(
            [event["text"] for event in events if event["type"] == "usage"],
            ["context 178.1K/1M · usage total 178100 624 · cache 98.62%"],
        )


class McpTest(unittest.TestCase):
    def test_names_and_tool_result_text(self):
        self.assertEqual(model_tool_name("whitaker", "analyze_latin"), "mcp__whitaker__analyze_latin")
        self.assertEqual(model_tool_name("a b", "c/d"), "mcp__a_b__c_d")
        long_name = model_tool_name("server", "t" * 80)
        self.assertLessEqual(len(long_name), 64)
        self.assertRegex(long_name, r"^[A-Za-z0-9_-]+$")
        self.assertNotEqual(long_name, model_tool_name("server", "u" * 80))
        self.assertEqual(
            format_tool_result(
                {
                    "content": [
                        {"type": "text", "text": "a"},
                        {"type": "image", "data": "xx"},
                        {"type": "resource", "resource": {"text": "body"}},
                    ]
                }
            ),
            "a\n[image]\nbody",
        )
        self.assertEqual(format_tool_result({"content": [], "isError": True}), "error: tool failed")
        self.assertEqual(
            format_tool_result({"isError": True, "content": [{"type": "text", "text": "error: already"}]}),
            "error: already",
        )
        self.assertEqual(format_tool_result({"content": []}), "[empty tool result]")

    def test_toml_servers_and_provider_still_load(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "foo-ai.toml"
            path.write_text(
                'default_provider = "deepseek-flash_1"\n\n'
                "[mcp.whitaker]\n"
                "enabled = true\n"
                'command = "docker"\n'
                'args = ["exec", "-i", "whitaker-mcp", "python", "-u", "/app/whitaker_server.py"]\n'
                "timeout_seconds = 15\n"
                'cwd = "C:/work"\n\n'
                "[mcp.whitaker.env]\n"
                'PYTHONUNBUFFERED = "1"\n\n'
                "[mcp.off]\n"
                'command = "nope"\n'
                "enabled = false\n\n"
                "[mcp.later]\n"
                'command = "later"\n',
                encoding="utf-8",
            )
            servers = load_mcp_servers(path)
            provider = load_provider(path)
            missing = load_mcp_servers(Path(directory) / "absent.toml")
            with self.assertRaises(ConfigError):
                bad = Path(directory) / "bad.toml"
                bad.write_text('[mcp.bad]\nenabled = true\ncommand = "x"\nargs = 1\n', encoding="utf-8")
                load_mcp_servers(bad)
        self.assertEqual(missing, [])
        self.assertEqual(provider.name, "deepseek-flash_1")
        self.assertEqual(len(servers), 1)
        self.assertEqual(servers[0].name, "whitaker")
        self.assertEqual(servers[0].args[0], "exec")
        self.assertEqual(servers[0].timeout_seconds, 15)
        self.assertEqual(servers[0].cwd, "C:/work")
        self.assertEqual(servers[0].env, {"PYTHONUNBUFFERED": "1"})
        bare = McpServer(name="local", command="python")
        self.assertEqual(bare.timeout_seconds, 60)

    def test_stdio_session_lists_pages_and_calls_tools(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "fake_mcp.py"
            script.write_text(FAKE_MCP, encoding="utf-8")
            host = McpHost(
                [
                    McpServer(
                        name="demo",
                        command=sys.executable,
                        args=["-u", str(script)],
                        cwd=str(root),
                        env={"FOO_AI_MCP_TEST": "yes"},
                        timeout_seconds=5,
                    )
                ]
            )
            try:
                notes = host.connect()
                self.assertEqual(host.connect(), notes)
                self.assertEqual(len(notes), 1)
                self.assertTrue(notes[0].startswith("mcp demo:"))
                self.assertIn("clock", notes[0])
                self.assertIn("echo", notes[0])
                names = [schema["function"]["name"] for schema in host.schemas()]
                self.assertEqual(names, ["mcp__demo__clock", "mcp__demo__echo"])
                self.assertTrue(host.schemas()[1]["function"]["description"].startswith("[demo]"))
                clock = host.call("mcp__demo__clock", {})
                folder, flag = clock.split("|", 1)
                self.assertEqual(flag, "yes")
                self.assertEqual(os.path.normcase(str(Path(folder).resolve())), os.path.normcase(str(root.resolve())))
                self.assertEqual(host.call("mcp__demo__echo", {"text": "ave"}), "echo:ave")
                self.assertEqual(host.call("mcp__demo__echo", '{"text": "ave"}'), "echo:ave")
                self.assertTrue(host.call("mcp__demo__echo", {"text": "fail"}).startswith("error:"))
                big = host.call("mcp__demo__echo", {"text": "big"})
                self.assertTrue(big.endswith("…[truncated]"))
                self.assertLess(len(big), TOOL_OUTPUT_LIMIT + 20)
            finally:
                host.close()

    def test_unavailable_server_is_skipped(self):
        host = McpHost([McpServer(name="missing", command="foo-ai-no-such-binary-xyz")])
        try:
            notes = host.connect()
        finally:
            host.close()
        self.assertIn("unavailable", notes[0])
        self.assertEqual(host.schemas(), [])

        with tempfile.TemporaryDirectory() as directory:
            host = McpHost(
                [
                    McpServer(
                        name="gone",
                        command=sys.executable,
                        args=["-c", "raise SystemExit(3)"],
                        timeout_seconds=5,
                    )
                ]
            )
            try:
                notes = host.connect()
            finally:
                host.close()
        self.assertIn("unavailable", notes[0])

    def test_call_timeout_is_a_tool_error(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "slow_mcp.py"
            script.write_text(SLOW_MCP, encoding="utf-8")
            host = McpHost(
                [McpServer(name="slow", command=sys.executable, args=["-u", str(script)], timeout_seconds=1)]
            )
            try:
                notes = host.connect()
                self.assertIn("sleep", notes[0])
                result = host.call("mcp__slow__sleep", {})
            finally:
                host.close()
        self.assertIn("timed out", result)

    def test_agent_runs_mcp_and_builtin_tools(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "fake_mcp.py"
            script.write_text(FAKE_MCP, encoding="utf-8")
            (root / "a.txt").write_text("hello from file\n", encoding="utf-8")
            host = McpHost(
                [McpServer(name="demo", command=sys.executable, args=["-u", str(script)], timeout_seconds=5)]
            )
            try:
                host.connect()
                client = ScriptedClient(
                    [
                        {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [_tool_call("c1", "mcp__demo__echo", {"text": "ave"})],
                        },
                        {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [_tool_call("c2", "read_file", {"path": "a.txt"})],
                        },
                        {"role": "assistant", "content": "done", "reasoning_content": ""},
                    ]
                )
                answer = Agent(client, root, mcp=host).ask("check")
                self.assertEqual(answer, "done")
                sent = json.dumps(client.calls[0]["tools"])
                self.assertIn("mcp__demo__echo", sent)
                self.assertIn("read_file", sent)
                again = [tool["function"]["name"] for tool in client.calls[1]["tools"]]
                self.assertIn("mcp__demo__echo", again)
                saved = (root / ".foo-ai" / "session.jsonl").read_text(encoding="utf-8")
                self.assertIn("echo:ave", saved)
                self.assertIn("hello from file", saved)
            finally:
                host.close()


def _tool_call(call_id: str, name: str, arguments: dict) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


FAKE_MCP = r"""
import json
import os
import sys

def send(message):
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()

def main():
    sys.stdout.write("not-json\n")
    sys.stdout.flush()
    while True:
        line = sys.stdin.readline()
        if not line:
            return
        text = line.strip()
        if not text:
            continue
        message = json.loads(text)
        method = message.get("method")
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": 9001, "method": "ping"})
            reply = sys.stdin.readline()
            if not reply:
                return
            if json.loads(reply).get("id") != 9001:
                sys.stderr.write("bad ping reply\n")
                return
            send({
                "jsonrpc": "2.0",
                "id": message["id"],
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "fake", "version": "0"},
                },
            })
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            cursor = (message.get("params") or {}).get("cursor")
            if cursor == "2":
                result = {"tools": [{
                    "name": "echo",
                    "description": "Echo text.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"],
                    },
                }]}
            else:
                result = {
                    "tools": [{
                        "name": "clock",
                        "description": "Report the working directory.",
                        "inputSchema": {"type": "object", "properties": {}},
                    }],
                    "nextCursor": "2",
                }
            send({"jsonrpc": "2.0", "id": message["id"], "result": result})
        elif method == "tools/call":
            params = message.get("params") or {}
            name = params.get("name")
            arguments = params.get("arguments") or {}
            if name == "clock":
                text = os.getcwd() + "|" + os.environ.get("FOO_AI_MCP_TEST", "")
            elif name == "echo":
                text = str(arguments.get("text", ""))
                if text == "fail":
                    send({
                        "jsonrpc": "2.0",
                        "id": message["id"],
                        "result": {"isError": True, "content": [{"type": "text", "text": "nope"}]},
                    })
                    continue
                text = ("x" * 25000) if text == "big" else "echo:" + text
            else:
                send({
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "error": {"code": -32602, "message": "unknown tool"},
                })
                continue
            send({
                "jsonrpc": "2.0",
                "id": message["id"],
                "result": {"content": [{"type": "text", "text": text}]},
            })
        elif method == "ping":
            send({"jsonrpc": "2.0", "id": message["id"], "result": {}})

if __name__ == "__main__":
    main()
"""

SLOW_MCP = r"""
import json
import sys
import time

def send(message):
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()

def main():
    while True:
        line = sys.stdin.readline()
        if not line:
            return
        message = json.loads(line)
        method = message.get("method")
        if method == "initialize":
            send({
                "jsonrpc": "2.0",
                "id": message["id"],
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "serverInfo": {"name": "slow", "version": "0"},
                },
            })
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            send({
                "jsonrpc": "2.0",
                "id": message["id"],
                "result": {"tools": [{
                    "name": "sleep",
                    "description": "Sleep.",
                    "inputSchema": {"type": "object", "properties": {}},
                }]},
            })
        elif method == "tools/call":
            time.sleep(30)

if __name__ == "__main__":
    main()
"""


class ScriptedClient:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def stream(self, messages, tools):
        self.calls.append({"messages": messages, "tools": tools})
        reply = self.replies.pop(0)
        if reply.get("content"):
            yield {"type": "content", "text": reply["content"]}
        if reply.get("usage"):
            yield {"type": "usage", "usage": reply["usage"]}
        yield {"type": "message", "message": reply}


class FailingSummaryClient:
    def stream(self, messages, tools):
        if tools is None:
            raise ChatError("HTTP 500 from summary")
        yield {"type": "message", "message": {"role": "assistant", "content": "still here", "reasoning_content": ""}}


if __name__ == "__main__":
    unittest.main()
