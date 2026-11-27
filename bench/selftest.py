"""Offline checks for ``bench/run.py``: no endpoint and no probe run.

Run it from the repository root with ``python -m bench.selftest``. It is not a
``test_*.py`` file, and ``tests/`` never imports the bench: the package does not
install this folder, so the suite has no way to import it, and the suite stays
offline and deterministic while a probe run needs a live endpoint.

Every check here uses a scripted completion instead of a client, so the scoring
rules are covered without a token and without a network.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from bench.run import (
    Probe,
    ProbeError,
    Step,
    build_messages,
    dry_run,
    load_probe,
    load_probes,
    matcher_ok,
    report,
    row,
    run_probe,
    schema_sha,
    score,
    treatment,
    type_ok,
    write_report,
)
from foo_ai.client import Completion
from foo_ai.config import Config, Provider
from foo_ai.skill import SkillCatalog, load_skills
from foo_ai.tools import TOOL_SCHEMAS, Toolbox

PROBE = """\
prompt = "Show me the first 40 lines of src/app.py."

[expect]
tool = "read_file"

[expect.args]
path = "src/app.py"

[expect.types]
offset = "integer"
"""

HISTORY_PROBE = """\
prompt = "Change it."

[[history]]
tool = "read_file"
arguments = { path = "src/app.py" }
result = "1|a"

[expect]
tool = "edit_file"
"""


def _probe(**overrides) -> Probe:
    fields: dict = {"id": "p", "prompt": "do it", "tool": "read_file"}
    fields.update(overrides)
    return Probe(**fields)


def _call(name: str, arguments: object) -> dict:
    """One tool call as the wire sends it: the arguments are a JSON string."""
    raw = arguments if isinstance(arguments, str) else json.dumps(arguments)
    return {"function": {"name": name, "arguments": raw}}


def _completion(calls: list[dict] | None = None, *, content: str = "", usage=None) -> Completion:
    return Completion(
        content=content,
        reasoning_content="",
        tool_calls=list(calls or []),
        usage=usage,
    )


class ScriptedClient:
    """A client whose one round the test writes down."""

    def __init__(self, completion: Completion):
        self.completion = completion
        self.seen: dict | None = None

    def complete(self, messages, tools, sink):
        self.seen = {"messages": messages, "tools": tools}
        return self.completion


class ProbeFileTests(unittest.TestCase):
    def write(self, folder: str, name: str, text: str) -> Path:
        path = Path(folder) / name
        path.write_text(text, encoding="utf-8", newline="\n")
        return path

    def test_a_probe_file_is_read(self):
        with tempfile.TemporaryDirectory() as folder:
            probe = load_probe(self.write(folder, "01-read-file.toml", PROBE))
        self.assertEqual(probe.id, "01-read-file")
        self.assertEqual(probe.tool, "read_file")
        self.assertEqual(probe.args, {"path": "src/app.py"})
        self.assertEqual(probe.types, {"offset": "integer"})
        self.assertEqual(probe.forbid, [])
        self.assertEqual(probe.check, "")

    def test_a_bad_file_names_itself(self):
        cases = {
            "unknown key": PROBE + '\nnote2 = "x"\n',
            "no expect": 'prompt = "x"\n',
            "no tool": 'prompt = "x"\n[expect]\nforbid = ["shell"]\n',
            "no prompt": '[expect]\ntool = "read_file"\n',
            "bad regex": 'prompt = "x"\n[expect]\ntool = "read_file"\n[expect.args]\npath = "re:("\n',
            "bad type": 'prompt = "x"\n[expect]\ntool = "read_file"\n[expect.types]\npath = "text"\n',
            "bad check": 'prompt = "x"\n[expect]\ntool = "ask"\ncheck = "decisions"\n',
            "bad forbid": 'prompt = "x"\n[expect]\ntool = "read_file"\nforbid = "shell"\n',
        }
        for name, text in cases.items():
            with tempfile.TemporaryDirectory() as folder:
                path = self.write(folder, "p.toml", text)
                with self.subTest(name):
                    with self.assertRaises(ProbeError) as caught:
                        load_probe(path)
                    self.assertIn(str(path), str(caught.exception))

    def test_a_folder_of_probes_keeps_file_order_and_takes_a_filter(self):
        with tempfile.TemporaryDirectory() as folder:
            self.write(folder, "02-second.toml", PROBE)
            self.write(folder, "01-first.toml", PROBE)
            self.write(folder, "notes.txt", "not a probe")
            probes = load_probes(Path(folder))
            self.assertEqual([probe.id for probe in probes], ["01-first", "02-second"])
            self.assertEqual([probe.id for probe in load_probes(Path(folder), ["second"])], ["02-second"])
            with self.assertRaises(ProbeError) as caught:
                load_probes(Path(folder), ["nothing"])
            self.assertIn("no probe matches nothing", str(caught.exception))
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ProbeError) as caught:
                load_probes(Path(folder))
            self.assertIn("no probe files", str(caught.exception))


class HistoryTests(unittest.TestCase):
    """The seeded calls, and the transcript they make."""

    def write(self, folder: str, text: str) -> Path:
        path = Path(folder) / "p.toml"
        path.write_text(text, encoding="utf-8", newline="\n")
        return path

    def test_a_history_block_is_read(self):
        with tempfile.TemporaryDirectory() as folder:
            probe = load_probe(self.write(folder, HISTORY_PROBE))
        self.assertEqual(len(probe.history), 1)
        step = probe.history[0]
        self.assertEqual(step.tool, "read_file")
        self.assertEqual(step.arguments, {"path": "src/app.py"})
        self.assertEqual(step.result, "1|a")

    def test_a_bad_history_names_itself(self):
        cases = {
            "not a list": 'prompt = "x"\nhistory = "no"\n\n[expect]\ntool = "ask"\n',
            "no tool": 'prompt = "x"\n\n[[history]]\nresult = "y"\n\n[expect]\ntool = "ask"\n',
            "no result": 'prompt = "x"\n\n[[history]]\ntool = "read_file"\n\n[expect]\ntool = "ask"\n',
            "not a table": 'prompt = "x"\n\n[[history]]\ntool = "read_file"\narguments = "no"\nresult = "y"\n\n[expect]\ntool = "ask"\n',
            "unknown key": 'prompt = "x"\n\n[[history]]\ntool = "read_file"\nresult = "y"\nnote = "z"\n\n[expect]\ntool = "ask"\n',
        }
        for name, text in cases.items():
            with tempfile.TemporaryDirectory() as folder:
                path = self.write(folder, text)
                with self.subTest(name):
                    with self.assertRaises(ProbeError) as caught:
                        load_probe(path)
                    self.assertIn(str(path), str(caught.exception))

    def test_the_seeded_calls_follow_the_turn(self):
        probe = _probe(prompt="change it", tool="edit_file")
        probe.history = [Step(tool="read_file", arguments={"path": "src/app.py"}, result="1|a")]
        messages = build_messages(probe, SkillCatalog(entries=[], skipped=[]))
        roles = [message["role"] for message in messages]
        self.assertEqual(roles, ["system", "user", "assistant", "tool"])
        self.assertEqual(messages[1]["content"], "change it")
        self.assertIsNone(messages[2]["content"])
        call = messages[2]["tool_calls"][0]
        self.assertEqual(call["function"]["name"], "read_file")
        self.assertEqual(json.loads(call["function"]["arguments"]), {"path": "src/app.py"})
        self.assertEqual(messages[3]["tool_call_id"], call["id"])
        self.assertEqual(messages[3]["name"], "read_file")
        self.assertEqual(messages[3]["content"], "1|a")

    def test_two_seeded_calls_keep_their_order_and_their_ids(self):
        probe = _probe()
        probe.history = [
            Step(tool="list_dir", arguments={"path": "."}, result="src/"),
            Step(tool="read_file", arguments={"path": "src/app.py"}, result="1|a"),
        ]
        messages = build_messages(probe, SkillCatalog(entries=[], skipped=[]))
        roles = [message["role"] for message in messages]
        self.assertEqual(roles, ["system", "user", "assistant", "tool", "assistant", "tool"])
        ids = [messages[index]["tool_calls"][0]["id"] for index in (2, 4)]
        self.assertEqual(ids, ["call_1", "call_2"])
        self.assertEqual([messages[index]["tool_call_id"] for index in (3, 5)], ids)

    def test_a_dry_run_shows_the_seeded_calls(self):
        probe = _probe(id="02-edit-existing", tool="edit_file")
        probe.history = [Step(tool="read_file", arguments={"path": "src/app.py"}, result="1|a")]
        header = {"model": "m", "reasoning_effort": "max", "thinking": True,
                  "schema_sha": "abc", "commit": "def", "tools": ["read_file"]}
        text = dry_run([probe], header)
        self.assertIn('after read_file {"path": "src/app.py"}', text)


class MatcherTests(unittest.TestCase):
    def test_a_matcher_is_any_the_exact_text_or_a_search(self):
        self.assertTrue(matcher_ok({"anything": 1}, "*"))
        self.assertTrue(matcher_ok("src/app.py", "src/app.py"))
        self.assertFalse(matcher_ok("src/app.py", "app.py"))
        self.assertTrue(matcher_ok("return None here", "re:return\\s+None"))
        self.assertFalse(matcher_ok("return 0", "re:return\\s+None"))
        self.assertTrue(matcher_ok(40, "40"))
        self.assertTrue(matcher_ok(1.5, "1.5"))
        self.assertFalse(matcher_ok(True, "true"))
        self.assertFalse(matcher_ok(None, "null"))
        self.assertFalse(matcher_ok(["a"], "a"))
        self.assertFalse(matcher_ok({"a": 1}, "re:."))

    def test_a_type_check_holds_the_json_type(self):
        self.assertTrue(type_ok("x", "string"))
        self.assertTrue(type_ok(3, "integer"))
        self.assertTrue(type_ok(3.5, "number"))
        self.assertTrue(type_ok(3, "number"))
        self.assertTrue(type_ok(True, "boolean"))
        self.assertTrue(type_ok([1], "array"))
        self.assertTrue(type_ok({}, "object"))
        self.assertFalse(type_ok(True, "integer"))
        self.assertFalse(type_ok(3, "boolean"))
        self.assertFalse(type_ok("x", "array"))
        self.assertFalse(type_ok({}, "nonsense"))


class ScoreTests(unittest.TestCase):
    def test_the_wanted_tool_with_every_check_is_a_pass(self):
        probe = _probe(args={"path": "src/app.py"}, types={"offset": "integer"})
        result = score(probe, _completion([_call("read_file", {"path": "src/app.py", "offset": 2})]))
        self.assertEqual(result.score, "pass")
        self.assertEqual(result.problems, [])
        self.assertEqual(result.tool, "read_file")

    def test_a_check_that_does_not_hold_is_a_partial(self):
        probe = _probe(args={"path": "src/app.py"})
        result = score(probe, _completion([_call("read_file", {"path": "src/other.py"})]))
        self.assertEqual(result.score, "partial")
        self.assertEqual(result.problems, ['path is "src/other.py", wanted src/app.py'])

    def test_a_missing_argument_is_a_partial(self):
        probe = _probe(args={"path": "src/app.py"}, types={"offset": "integer"})
        result = score(probe, _completion([_call("read_file", {})]))
        self.assertEqual(result.score, "partial")
        self.assertEqual(result.problems, ["path is missing", "offset is missing"])

    def test_another_tool_or_no_tool_is_a_fail(self):
        probe = _probe(tool="edit_file", forbid=["write_file"])
        wrong = score(probe, _completion([_call("read_file", {"path": "x"})]))
        self.assertEqual(wrong.score, "fail")
        self.assertEqual(wrong.problems, ["called read_file, wanted edit_file"])
        forbidden = score(probe, _completion([_call("write_file", {"path": "x"})]))
        self.assertEqual(forbidden.problems, ["called write_file, which this probe forbids"])
        silent = score(probe, _completion(content="I would rather explain."))
        self.assertEqual(silent.score, "fail")
        self.assertEqual(silent.problems, ["no tool call"])
        self.assertEqual(silent.content, "I would rather explain.")

    def test_arguments_that_are_not_json_are_a_fail(self):
        result = score(_probe(), _completion([_call("read_file", "{not json")]))
        self.assertEqual(result.score, "fail")
        self.assertTrue(result.problems[0].startswith("error: arguments are not JSON"))

    def test_arguments_already_parsed_by_the_server_are_read(self):
        call = {"function": {"name": "read_file", "arguments": {"path": "src/app.py"}}}
        result = score(_probe(args={"path": "src/app.py"}), _completion([call]))
        self.assertEqual(result.score, "pass")

    def test_the_wanted_call_counts_wherever_it_sits_in_the_round(self):
        calls = [
            _call("read_file", {"path": "src/app.py"}),
            _call("edit_file", {"path": "src/app.py", "old_string": "a", "new_string": "b"}),
        ]
        probe = _probe(tool="edit_file", args={"path": "src/app.py"})
        result = score(probe, _completion(calls))
        self.assertEqual(result.score, "pass")
        self.assertEqual(result.calls, 2)
        self.assertEqual(result.tools_called, ["read_file", "edit_file"])
        self.assertEqual(result.tool, "edit_file")
        self.assertEqual(result.args["old_string"], "a")

    def test_a_forbidden_call_beside_the_wanted_one_is_a_partial(self):
        calls = [
            _call("read_file", {"path": "src/app.py"}),
            _call("edit_file", {"path": "src/app.py"}),
        ]
        probe = _probe(tool="edit_file", forbid=["read_file"], args={"path": "src/app.py"})
        result = score(probe, _completion(calls))
        self.assertEqual(result.score, "partial")
        self.assertEqual(result.problems, ["also called read_file, which this probe forbids"])

    def test_a_round_without_the_wanted_call_names_what_it_called(self):
        calls = [_call("list_dir", {"path": "."}), _call("read_file", {"path": "x"})]
        result = score(_probe(tool="ask"), _completion(calls))
        self.assertEqual(result.score, "fail")
        self.assertEqual(result.problems, ["called list_dir, wanted ask"])
        self.assertEqual(result.tools_called, ["list_dir", "read_file"])

    def test_the_ask_payload_has_to_be_one_the_asker_can_show(self):
        probe = _probe(tool="ask", check="questions")
        empty = score(probe, _completion([_call("ask", {"questions": []})]))
        self.assertEqual(empty.score, "partial")
        self.assertEqual(empty.problems, ["error: questions must contain 1 to 3 items"])
        thin = score(probe, _completion([_call("ask", {"questions": [{"header": "H", "question": "Q"}]})]))
        self.assertEqual(thin.problems, ["error: questions[0].options must contain 2 to 4 items"])
        questions = [
            {
                "header": "Library",
                "question": "Which library?",
                "options": [{"label": "requests"}, {"label": "httpx"}],
            }
        ]
        good = score(probe, _completion([_call("ask", {"questions": questions})]))
        self.assertEqual(good.score, "pass")

    def test_the_tokens_and_the_reasoning_are_counted(self):
        usage = {"prompt_tokens": 1200, "completion_tokens": 42, "prompt_tokens_details": {"cached_tokens": 900}}
        result = score(_probe(), _completion([_call("read_file", {"path": "x"})], usage=usage))
        self.assertEqual(result.prompt_tokens, 1200)
        self.assertEqual(result.completion_tokens, 42)


class RunTests(unittest.TestCase):
    def test_a_round_sends_the_system_prompt_and_the_schemas(self):
        catalog = load_skills(Path.cwd())
        client = ScriptedClient(_completion([_call("read_file", {"path": "src/app.py"})]))
        probe = _probe(prompt="  read it\n")
        result = run_probe(client, probe, TOOL_SCHEMAS, catalog)
        self.assertEqual(result.score, "pass")
        self.assertGreaterEqual(result.ms, 0.0)
        self.assertIs(client.seen["tools"], TOOL_SCHEMAS)
        messages = client.seen["messages"]
        self.assertEqual([message["role"] for message in messages], ["system", "user"])
        self.assertIn("Skills:", messages[0]["content"])
        self.assertEqual(messages[1]["content"], "read it")

    def test_a_catalog_without_skills_leaves_the_skills_block_out(self):
        catalog = SkillCatalog(entries=[], skipped=[])
        messages = build_messages(_probe(prompt="read it"), catalog)
        self.assertNotIn("Skills:", messages[0]["content"])

    def test_the_schema_fingerprint_follows_the_schemas(self):
        first = schema_sha(TOOL_SCHEMAS)
        self.assertEqual(first, schema_sha(json.loads(json.dumps(TOOL_SCHEMAS))))
        self.assertEqual(len(first), 12)
        changed = json.loads(json.dumps(TOOL_SCHEMAS))
        changed[0]["function"]["description"] += " Changed."
        self.assertNotEqual(schema_sha(changed), first)

    def test_the_treatment_carries_the_measurement_and_never_the_key(self):
        provider = Provider(
            name="home",
            base_url="http://127.0.0.1:8100/v1",
            model="m",
            context_window=1000000,
            api_key="sk-secret",
            extra_body={"chat_template_kwargs": {"reasoning_effort": "max", "thinking": True}},
        )
        header = treatment(Config(provider=provider), TOOL_SCHEMAS)
        self.assertEqual(header["provider"], "home")
        self.assertEqual(header["model"], "m")
        self.assertEqual(header["reasoning_effort"], "max")
        self.assertIs(header["thinking"], True)
        self.assertEqual(header["schema_sha"], schema_sha(TOOL_SCHEMAS))
        self.assertIn("read_file", header["tools"])
        self.assertNotIn("sk-secret", json.dumps(header))

    def test_a_treatment_without_a_reasoning_block_says_so(self):
        provider = Provider(
            name="cloud",
            base_url="https://api.example/v1",
            model="m",
            context_window=0,
            api_key="",
            extra_body={},
        )
        header = treatment(Config(provider=provider), TOOL_SCHEMAS)
        self.assertEqual(header["reasoning_effort"], "")
        self.assertIsNone(header["thinking"])


class ReportTests(unittest.TestCase):
    def header(self) -> dict:
        provider = Provider(
            name="home",
            base_url="http://127.0.0.1:8100/v1",
            model="m",
            context_window=0,
            api_key="",
            extra_body={},
        )
        return treatment(Config(provider=provider), TOOL_SCHEMAS)

    def test_the_report_names_every_probe_and_counts_the_scores(self):
        results = [
            score(_probe(id="01-read-file"), _completion([_call("read_file", {"path": "x"})])),
            score(_probe(id="02-edit", tool="edit_file"), _completion([_call("read_file", {"path": "x"})])),
        ]
        text = report(results, self.header(), Path("bench/results/x.json"))
        self.assertIn("01-read-file", text)
        self.assertIn("02-edit", text)
        self.assertIn("called read_file, wanted edit_file", text)
        self.assertIn('    args: {"path": "x"}', text)
        self.assertIn("2 probes: 1 pass, 0 partial, 1 fail", text)
        self.assertIn(str(Path("bench/results/x.json")), text)
        one = report(results[:1], self.header(), Path("bench/results/x.json"))
        self.assertIn("1 probe: 1 pass, 0 partial, 0 fail", one)

    def test_the_json_report_keeps_the_treatment_and_one_object_per_probe(self):
        results = [score(_probe(), _completion([_call("read_file", {"path": "x"})], content="ok"))]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "nested" / "run.json"
            write_report(path, self.header(), results)
            payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["treatment"]["model"], "m")
        self.assertEqual(len(payload["results"]), 1)
        self.assertEqual(payload["results"][0]["probe"], "p")
        self.assertEqual(payload["results"][0]["score"], "pass")
        self.assertEqual(payload["results"][0]["content"], "ok")
        self.assertEqual(payload["results"][0]["tools_called"], ["read_file"])
        self.assertEqual(payload["results"][0]["args"], {"path": "x"})
        self.assertEqual(row(results[0])["want"], "read_file")

    def test_a_multi_call_round_shows_every_call_it_made(self):
        calls = [_call("read_file", {"path": "x"}), _call("shell", {"command": "ls"})]
        text = report([score(_probe(), _completion(calls))], self.header(), Path("r.json"))
        self.assertIn("the round called: read_file, shell", text)

    def test_a_dry_run_prints_the_plan_and_sends_nothing(self):
        probe = _probe(id="01-read-file", forbid=["shell"], note="The control.")
        text = dry_run([probe], self.header())
        self.assertIn("tools: read_file, list_dir, glob, grep, write_file, edit_file, shell, todo_write, ask", text)
        self.assertIn("01-read-file: wants read_file (forbid: shell)", text)
        self.assertIn("    # The control.", text)
        self.assertIn("    do it", text)


def _slashes(text: str) -> str:
    """Compare paths the same way on Windows and elsewhere.

    A probe writes its seeded result with forward slashes, and a tool that names
    a file joins the path with the platform's separator.
    """
    return text.replace("\\", "/")


class FixtureTests(unittest.TestCase):
    """A seeded result has to be what the tool returns for ``bench/workspace``.

    This is the check that keeps a probe honest: change the fixture or a tool's
    output format, and every seeded result that no longer matches fails here
    instead of quietly scoring a stale transcript. Path separators are compared
    with ``/``, because a seeded result is written that way.
    """

    def test_every_seeded_result_matches_the_tool(self):
        root = Path(__file__).resolve().parent
        box = Toolbox(root / "workspace", lambda command: False)
        seeded = 0
        for probe in load_probes(root / "probes"):
            for index, step in enumerate(probe.history):
                rendered = box.run(step.tool, step.arguments)
                with self.subTest(probe=probe.id, step=index):
                    self.assertEqual(_slashes(step.result), _slashes(rendered))
                seeded += 1
        self.assertGreaterEqual(seeded, 3)


if __name__ == "__main__":
    unittest.main()
