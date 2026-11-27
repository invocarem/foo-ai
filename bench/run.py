"""Probe the model with the tool schemas foo-ai actually sends.

One round, no agent loop, and no tool is executed. Each probe sends the real
system prompt, the real ``Toolbox.schemas()``, its user turn, and the calls it
seeds to stand in for the context phase, then scores the tool call that comes
back: the right tool, with arguments the tool would accept. A generic tool
benchmark measures a model against its own schemas. This measures it against this
program's, which is the only place a foo-ai-shaped mistake shows: the wrong one
of ``edit_file`` and ``write_file``, an ``old_string`` that matches twice, an
``ask`` payload the asker cannot show.

The program does not import this module, this module runs no tool, and it writes
only under ``bench/results``. ``bench/README.md`` has the probe format, the
seeded history, the treatment fields, and the boundary rules.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
import tomllib
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from foo_ai.client import ChatError, Client, Completion
from foo_ai.config import Config, ConfigError, Provider, load_config, local_reasoning, user_config_path
from foo_ai.skill import SkillCatalog, load_skills, system_prompt
from foo_ai.tools import Toolbox, normalize_questions, parse_arguments
from foo_ai.usage import parse_usage

_TOP_KEYS = {"prompt", "note", "expect", "history"}
_EXPECT_KEYS = {"tool", "forbid", "args", "types", "check"}
_STEP_KEYS = {"tool", "arguments", "result"}
_TYPE_NAMES = ("array", "boolean", "integer", "number", "object", "string")
_DEFAULT_TIMEOUT = 120.0


class ProbeError(Exception):
    """A probe file, or the folder of them, cannot be used. The text names it."""


@dataclass
class Step:
    """A call the agent loop already made before the probe's round.

    The result is what the tool would have answered, so a probe can start from
    the state the loop is in after a read: the file's text is in the transcript,
    and the next call should be the edit.
    """

    tool: str
    arguments: dict = field(default_factory=dict)
    result: str = ""


@dataclass
class Probe:
    """One turn to send and the tool call that would be right for it."""

    id: str
    prompt: str
    tool: str
    note: str = ""
    forbid: list[str] = field(default_factory=list)
    args: dict[str, str] = field(default_factory=dict)
    types: dict[str, str] = field(default_factory=dict)
    check: str = ""
    history: list[Step] = field(default_factory=list)


@dataclass
class Result:
    """One probe scored, with what the model did and what it cost."""

    probe: Probe
    score: str
    tool: str = ""
    args: dict = field(default_factory=dict)
    tools_called: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    content: str = ""
    reasoning_chars: int = 0
    calls: int = 0
    ms: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0


# ---------------------------------------------------------------- the probes


def load_probes(directory: Path, only: list[str] | None = None) -> list[Probe]:
    """Every ``*.toml`` probe in the folder, in file-name order.

    ``only`` keeps the probes whose id contains one of its words.
    """
    try:
        paths = sorted(path for path in directory.glob("*.toml") if path.is_file())
    except OSError as exc:
        raise ProbeError(f"cannot read {directory}: {exc}") from None
    if not paths:
        raise ProbeError(f"no probe files in {directory}")
    probes = [load_probe(path) for path in paths]
    if not only:
        return probes
    kept = [probe for probe in probes if any(word in probe.id for word in only)]
    if not kept:
        raise ProbeError(f"no probe matches {', '.join(only)}")
    return kept


def load_probe(path: Path) -> Probe:
    """One probe file. A bad key or a missing field names the file it came from."""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ProbeError(f"cannot read {path}: {exc}") from None
    except tomllib.TOMLDecodeError as exc:
        raise ProbeError(f"{path}: {exc}") from None
    unknown = sorted(set(data) - _TOP_KEYS)
    if unknown:
        raise ProbeError(f"{path}: unknown key {unknown[0]}")
    prompt = data.get("prompt")
    if not isinstance(prompt, str) or prompt.strip() == "":
        raise ProbeError(f"{path}: prompt must be a non-empty string")
    note = data.get("note", "")
    if not isinstance(note, str):
        raise ProbeError(f"{path}: note must be a string")
    expect = data.get("expect")
    if not isinstance(expect, dict):
        raise ProbeError(f"{path}: expect must be a table")
    unknown = sorted(set(expect) - _EXPECT_KEYS)
    if unknown:
        raise ProbeError(f"{path}: unknown expect key {unknown[0]}")
    tool = expect.get("tool")
    if not isinstance(tool, str) or tool.strip() == "":
        raise ProbeError(f"{path}: expect.tool must be a non-empty string")
    check = expect.get("check", "")
    if not isinstance(check, str):
        raise ProbeError(f"{path}: expect.check must be a string")
    if check and check not in _CHECKS:
        raise ProbeError(f"{path}: unknown check {check!r}")
    return Probe(
        id=path.stem,
        prompt=prompt,
        tool=tool,
        note=note,
        forbid=_names(path, "forbid", expect.get("forbid", [])),
        args=_matchers(path, "args", expect.get("args", {})),
        types=_types(path, "types", expect.get("types", {})),
        check=check,
        history=_steps(path, data.get("history", [])),
    )


def _names(path: Path, key: str, raw: object) -> list[str]:
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise ProbeError(f"{path}: expect.{key} must be a list of tool names")
    return list(raw)


def _matchers(path: Path, key: str, raw: object) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise ProbeError(f"{path}: expect.{key} must be a table")
    matchers: dict[str, str] = {}
    for name, matcher in raw.items():
        if not isinstance(matcher, str):
            raise ProbeError(f"{path}: expect.{key}.{name} must be a string")
        if matcher.startswith("re:"):
            try:
                re.compile(matcher[3:])
            except re.error as exc:
                raise ProbeError(f"{path}: expect.{key}.{name}: {exc}") from None
        matchers[name] = matcher
    return matchers


def _types(path: Path, key: str, raw: object) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise ProbeError(f"{path}: expect.{key} must be a table")
    types: dict[str, str] = {}
    for name, kind in raw.items():
        if not isinstance(kind, str) or kind not in _TYPE_NAMES:
            allowed = ", ".join(_TYPE_NAMES)
            raise ProbeError(f"{path}: expect.{key}.{name} must be one of {allowed}")
        types[name] = kind
    return types


def _steps(path: Path, raw: object) -> list[Step]:
    """The seeded calls, each with the result the tool would have given."""
    if not isinstance(raw, list):
        raise ProbeError(f"{path}: history must be a list of tables")
    steps: list[Step] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ProbeError(f"{path}: history[{index}] must be a table")
        unknown = sorted(set(item) - _STEP_KEYS)
        if unknown:
            raise ProbeError(f"{path}: unknown history[{index}] key {unknown[0]}")
        tool = item.get("tool")
        if not isinstance(tool, str) or tool.strip() == "":
            raise ProbeError(f"{path}: history[{index}].tool must be a non-empty string")
        arguments = item.get("arguments", {})
        if not isinstance(arguments, dict):
            raise ProbeError(f"{path}: history[{index}].arguments must be a table")
        result = item.get("result")
        if not isinstance(result, str):
            raise ProbeError(f"{path}: history[{index}].result must be a string")
        steps.append(Step(tool=tool, arguments=dict(arguments), result=result))
    return steps


# ------------------------------------------------------------------ matching


def matcher_ok(value: object, matcher: str) -> bool:
    """One ``[expect.args]`` entry.

    ``*`` takes any value. A ``re:`` matcher searches the text, so a probe can
    ask for a phrase anywhere in an argument. Anything else is the exact text,
    and a number matches its own digits. A list or an object never matches a
    bare string: write a type check for those.
    """
    if matcher == "*":
        return True
    if matcher.startswith("re:"):
        return isinstance(value, str) and re.search(matcher[3:], value) is not None
    if isinstance(value, str):
        return value == matcher
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, (int, float)):
        return str(value) == matcher
    return False


def type_ok(value: object, name: str) -> bool:
    """One ``[expect.types]`` entry. A boolean is not an integer here."""
    if name == "string":
        return isinstance(value, str)
    if name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if name == "boolean":
        return isinstance(value, bool)
    if name == "array":
        return isinstance(value, list)
    if name == "object":
        return isinstance(value, dict)
    return False


def _check_questions(args: dict) -> str:
    """The asker's own validator: the payload has to be one it can show."""
    questions, error = normalize_questions(args.get("questions"))
    return "" if questions is not None else error


_CHECKS = {"questions": _check_questions}


# ------------------------------------------------------------------- scoring


def score(probe: Probe, completion: Completion) -> Result:
    """Pass, partial, or fail, with the reasons that decided it.

    The agent runs every call in a round, in order, so a probe scores the wanted
    call wherever it sits in the round rather than only the first one. A
    forbidden call beside it is a partial: that call ran too, and it cost a round
    the probe did not ask for. ``pass`` is the wanted call with every check met,
    ``partial`` is the wanted call with a check that did not hold, and ``fail`` is
    anything else: no call, another tool, or arguments that are not JSON.
    """
    calls = [call for call in completion.tool_calls if isinstance(call, dict)]
    usage = parse_usage(completion.usage)
    result = Result(
        probe=probe,
        score="fail",
        content=(completion.content or "").strip()[:200],
        reasoning_chars=len(completion.reasoning_content or ""),
        calls=len(calls),
        tools_called=[_name(call) for call in calls],
        prompt_tokens=usage.prompt,
        completion_tokens=usage.completion,
    )
    if not calls:
        result.problems.append("no tool call")
        return result
    result.tool = result.tools_called[0]
    wanted = next((call for call in calls if _name(call) == probe.tool), None)
    if wanted is None:
        sent = parse_arguments(_function(calls[0]).get("arguments"))
        if isinstance(sent, dict):
            result.args = sent
        if result.tool in probe.forbid:
            result.problems.append(f"called {result.tool}, which this probe forbids")
        else:
            called = result.tool or "no tool name"
            result.problems.append(f"called {called}, wanted {probe.tool}")
        return result
    result.tool = probe.tool
    args = parse_arguments(_function(wanted).get("arguments"))
    if isinstance(args, str):
        result.problems.append(args)
        return result
    result.args = args
    for name in result.tools_called:
        if name in probe.forbid:
            result.problems.append(f"also called {name}, which this probe forbids")
            break
    for key, matcher in probe.args.items():
        if key not in args:
            result.problems.append(f"{key} is missing")
        elif not matcher_ok(args[key], matcher):
            text = json.dumps(args[key])[:80]
            result.problems.append(f"{key} is {text}, wanted {matcher}")
    for key, kind in probe.types.items():
        if key not in args:
            result.problems.append(f"{key} is missing")
        elif not type_ok(args[key], kind):
            result.problems.append(f"{key} is not {kind}")
    if probe.check:
        error = _CHECKS[probe.check](args)
        if error:
            result.problems.append(error)
    if not result.problems:
        result.score = "pass"
    else:
        result.score = "partial"
    return result


def _function(call: dict) -> dict:
    """The ``function`` block of a tool call, or an empty one."""
    function = call.get("function")
    return function if isinstance(function, dict) else {}


def _name(call: dict) -> str:
    """The tool name of a call, or an empty string when it carries none."""
    name = _function(call).get("name")
    return name if isinstance(name, str) else ""


def run_probe(client: Client, probe: Probe, schemas: list[dict], catalog: SkillCatalog) -> Result:
    """Send one turn and score the round that comes back."""
    started = time.perf_counter()
    completion = client.complete(build_messages(probe, catalog), schemas, _QUIET)
    ms = (time.perf_counter() - started) * 1000
    result = score(probe, completion)
    result.ms = ms
    return result


def build_messages(probe: Probe, catalog: SkillCatalog) -> list[dict]:
    """The system prompt, the probe's turn, then the calls the loop already made.

    A seeded step is the transcript after that call: the assistant turn that made
    it, then the tool turn that answered it. The assistant record carries a
    ``None`` content and no reasoning, the way ``Session.add_assistant`` writes a
    turn that only called a tool.
    """
    messages = [
        {"role": "system", "content": system_prompt(catalog)},
        {"role": "user", "content": probe.prompt.strip()},
    ]
    for index, step in enumerate(probe.history):
        call_id = f"call_{index + 1}"
        messages.append(
            {
                "role": "assistant",
                "content": None,
                "reasoning_content": "",
                "tool_calls": [
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": step.tool,
                            "arguments": json.dumps(step.arguments),
                        },
                    }
                ],
            }
        )
        messages.append(
            {
                "role": "tool",
                "tool_call_id": call_id,
                "name": step.tool,
                "content": step.result,
            }
        )
    return messages


class _QuietSink:
    """The client streams into a sink. A probe scores the round after it lands."""

    def reasoning(self, text: str) -> None:
        return None

    def content(self, text: str) -> None:
        return None

    def tool_name(self, name: str) -> None:
        return None


_QUIET = _QuietSink()


# ---------------------------------------------------------------- the report


def schema_sha(schemas: list[dict]) -> str:
    """A short fingerprint of the schemas a run sent."""
    text = json.dumps(schemas, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def commit_sha() -> str:
    """The checkout the run measured, or ``unknown`` when git cannot say."""
    try:
        done = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=Path(__file__).resolve().parent,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return done.stdout.strip() if done.returncode == 0 else "unknown"


def treatment(config: Config, schemas: list[dict]) -> dict:
    """What a result row is only comparable against.

    The API key is not here and never goes into a report. The effort and the
    thinking flag come from the provider block, because a row is a different
    measurement when either one moves.
    """
    provider: Provider = config.provider
    reasoning = local_reasoning(provider.extra_body) or {}
    return {
        "started": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "provider": provider.name,
        "model": provider.model,
        "base_url": provider.base_url,
        "reasoning_effort": reasoning.get("reasoning_effort", ""),
        "thinking": reasoning.get("thinking", None),
        "schema_sha": schema_sha(schemas),
        "tools": [schema["function"]["name"] for schema in schemas],
        "commit": commit_sha(),
    }


def row(result: Result) -> dict:
    """One result as the JSON report writes it."""
    return {
        "probe": result.probe.id,
        "score": result.score,
        "tool": result.tool,
        "want": result.probe.tool,
        "args": result.args,
        "tools_called": list(result.tools_called),
        "problems": list(result.problems),
        "content": result.content,
        "reasoning_chars": result.reasoning_chars,
        "calls": result.calls,
        "ms": round(result.ms, 1),
        "prompt_tokens": result.prompt_tokens,
        "completion_tokens": result.completion_tokens,
    }


def shown(value: object) -> str:
    """A treatment value as the console shows it: a dash when it is not there."""
    if value is None or value == "":
        return "-"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def report(results: list[Result], header: dict, where: Path) -> str:
    """The console table: the treatment, one line per probe, then the totals.

    The table is plain ASCII, so a run redirected to a file on Windows still
    writes it. The model's own text stays in the JSON report.
    """
    lines = [
        f"{header['model']} | effort {shown(header['reasoning_effort'])} "
        f"| thinking {shown(header['thinking'])} | schema {header['schema_sha']} "
        f"| commit {header['commit']}",
        "",
        f"{'probe':24} {'score':8} {'tool':16} {'ms':>6} {'tokens':>13}",
    ]
    for result in results:
        tokens = f"{result.prompt_tokens}/{result.completion_tokens}"
        tool = result.tool or "-"
        lines.append(
            f"{result.probe.id:24} {result.score:8} {tool:16} {result.ms:6.0f} {tokens:>13}"
        )
        for problem in result.problems:
            lines.append(f"    {problem}")
        if result.args:
            lines.append(f"    args: {json.dumps(result.args)[:160]}")
        if result.calls > 1:
            lines.append(f"    the round called: {', '.join(result.tools_called)}")
    counts = Counter(result.score for result in results)
    label = "probe" if len(results) == 1 else "probes"
    lines.append("")
    lines.append(
        f"{len(results)} {label}: {counts['pass']} pass, "
        f"{counts['partial']} partial, {counts['fail']} fail"
    )
    lines.append(f"results: {where}")
    return "\n".join(lines)


def write_report(path: Path, header: dict, results: list[Result]) -> None:
    """Write the run as JSON: the treatment once, then one object per probe."""
    payload = {"treatment": header, "results": [row(result) for result in results]}
    text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
    except OSError as exc:
        raise ProbeError(f"cannot write {path}: {exc}") from None


# --------------------------------------------------------------------- main


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bench",
        description="Probe the model with the tool schemas foo-ai sends.",
        allow_abbrev=False,
    )
    parser.add_argument("--probes", default=None, metavar="DIR",
                        help="the probe folder (default: bench/probes)")
    parser.add_argument("--provider", default=None, metavar="NAME")
    parser.add_argument("--config", default=None, metavar="PATH",
                        help="settings file the page writes (default: the user's config directory)")
    parser.add_argument("--workspace", default=None, metavar="DIR",
                        help="the workspace whose skills go in the system prompt (default: this repository)")
    parser.add_argument("--only", action="append", default=None, metavar="WORD",
                        help="keep the probes whose id contains this word; repeatable")
    parser.add_argument("--out", default=None, metavar="PATH",
                        help="the JSON report to write (default: bench/results/<UTC stamp>.json)")
    parser.add_argument("--timeout", type=float, default=_DEFAULT_TIMEOUT, metavar="SECONDS",
                        help="give up on a round that sends nothing for this long (default: 120)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the plan and send nothing")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        code = exc.code
        if code is None:
            return 0
        if isinstance(code, int):
            return code
        return 2
    if args.timeout <= 0:
        sys.stderr.write("timeout must be a positive number of seconds\n")
        return 1
    root = Path(__file__).resolve().parent
    probes_dir = Path(args.probes).expanduser() if args.probes else root / "probes"
    workspace = Path(args.workspace).expanduser().resolve() if args.workspace else root / "workspace"
    try:
        probes = load_probes(probes_dir, args.only)
    except ProbeError as exc:
        sys.stderr.write(f"{exc}\n")
        return 1
    user_config = Path(args.config).expanduser() if args.config else user_config_path()
    try:
        config = load_config(args.provider, workspace=workspace, user_config=user_config)
    except ConfigError as exc:
        sys.stderr.write(f"{exc}\n")
        return 1
    catalog = load_skills(workspace)
    schemas = Toolbox(workspace, _no_confirm, skills=catalog).schemas()
    header = treatment(config, schemas)
    if args.dry_run:
        print(dry_run(probes, header), flush=True)
        return 0
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = Path(args.out).expanduser() if args.out else root / "results" / f"{stamp}.json"
    client = Client(config.provider, timeout=args.timeout)
    results: list[Result] = []
    for probe in probes:
        try:
            results.append(run_probe(client, probe, schemas, catalog))
        except ChatError as exc:
            sys.stderr.write(f"{probe.id}: {exc}\n")
            return 1
    try:
        write_report(out, header, results)
    except ProbeError as exc:
        sys.stderr.write(f"{exc}\n")
        return 1
    print(report(results, header, out), flush=True)
    return 0


def dry_run(probes: list[Probe], header: dict) -> str:
    """The plan a run would send, without a client and without a token."""
    names = ", ".join(header["tools"])
    lines = [
        f"{header['model']} | effort {shown(header['reasoning_effort'])} "
        f"| thinking {shown(header['thinking'])} | schema {header['schema_sha']}",
        f"tools: {names}",
        f"probes: {len(probes)}",
        "",
    ]
    for probe in probes:
        wanted = probe.tool
        if probe.forbid:
            wanted += f" (forbid: {', '.join(probe.forbid)})"
        lines.append(f"{probe.id}: wants {wanted}")
        if probe.note:
            lines.append(f"    # {probe.note}")
        for line in probe.prompt.strip().splitlines():
            lines.append(f"    {line}")
        for step in probe.history:
            lines.append(f"    after {step.tool} {json.dumps(step.arguments)}")
        lines.append("")
    return "\n".join(lines).rstrip()


def _no_confirm(command: str) -> bool:
    """A probe executes no tool, so the shell gate is never reached."""
    return False


if __name__ == "__main__":
    raise SystemExit(main())
