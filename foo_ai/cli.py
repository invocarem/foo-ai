"""Arguments, printing, prompts, and process exit codes."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from foo_ai.agent import Agent
from foo_ai.client import ChatError, Client
from foo_ai.config import ConfigError, load_config
from foo_ai.mcp import McpPool
from foo_ai.session import Session
from foo_ai.skill import load_skills, match_slash, skills_banner, system_prompt
from foo_ai.tools import Toolbox


class CliSink:
    def __init__(self, show_thinking: bool, verbose: bool):
        self.show_thinking = show_thinking
        self.verbose = verbose

    def content(self, text: str) -> None:
        sys.stdout.write(text)
        sys.stdout.flush()

    def reasoning(self, text: str) -> None:
        if self.show_thinking:
            sys.stderr.write(text)
            sys.stderr.flush()

    def tool_name(self, name: str) -> None:
        if self.show_thinking:
            sys.stderr.write("\n")
            sys.stderr.flush()
        sys.stdout.write(f"\n→ {name}")
        sys.stdout.flush()

    def tool_result(self, name: str, result: str) -> None:
        if self.verbose or name in {"edit_file", "write_file"}:
            sys.stdout.write("\n" + result + "\n")
        else:
            sys.stdout.write(f"\n← {name} ({len(result)} chars)\n")
        sys.stdout.flush()

    def status(self, text: str) -> None:
        sys.stderr.write(text)
        sys.stderr.flush()

    def todos(self, items: list[dict]) -> None:
        if not items:
            sys.stdout.write("\nTodos: (none)\n")
        else:
            marks = {"pending": " ", "in_progress": ">", "completed": "x"}
            lines = ["\nTodos:"]
            for item in items:
                lines.append(f" [{marks[item['status']]}] {item['content']}")
            sys.stdout.write("\n".join(lines) + "\n")
        sys.stdout.flush()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="foo-ai", allow_abbrev=False)
    parser.add_argument("prompt", nargs="*")
    parser.add_argument("--workspace", default=None)
    parser.add_argument("--max-steps", type=int, default=None)
    parser.add_argument("--show-thinking", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--provider", default=None)
    parser.add_argument("--version", action="store_true")
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
    if args.version:
        print("Foo AI 0.1.0", flush=True)
        return 0
    if args.max_steps is not None and args.max_steps <= 0:
        sys.stderr.write("max_steps must be a positive integer\n")
        return 1
    workspace = Path(args.workspace).expanduser().resolve() if args.workspace else Path.cwd().resolve()
    try:
        config = load_config(args.provider, workspace=workspace)
    except ConfigError as exc:
        sys.stderr.write(f"{exc}\n")
        return 1
    pool = McpPool(config.mcp_servers)
    try:
        try:
            mcp_lines = pool.connect()
        except KeyboardInterrupt:
            sys.stdout.write("\n")
            sys.stdout.flush()
            return 1
        skills = load_skills(workspace)
        for line in skills.skipped:
            sys.stderr.write(line)
        session = Session(workspace, prompt=system_prompt(skills))
        note = session.load()
        if note:
            print(note, flush=True)
        print(f"Foo AI · {config.provider.name} · {config.provider.model}", flush=True)
        for line in mcp_lines:
            print(line, flush=True)
        agent = Agent(
            provider=config.provider,
            session=session,
            toolbox=Toolbox(workspace, _confirmer(args.yes), pool, skills, asker=make_asker()),
            client=Client(config.provider),
            sink=CliSink(args.show_thinking, args.verbose),
            max_steps=args.max_steps if args.max_steps is not None else config.max_steps,
        )
        if args.prompt:
            return _one_shot(agent, " ".join(args.prompt))
        print(f"Workspace {workspace}", flush=True)
        print("/exit to quit, /reset to clear the conversation.", flush=True)
        banner = skills_banner(skills)
        if banner:
            print(banner, flush=True)
        return _interactive(agent)
    finally:
        pool.close()


def make_asker():
    """The ask tool's reader. A pipe returns None so the model states an assumption."""

    def ask(questions: list[dict]) -> list[dict] | None:
        if not sys.stdin.isatty():
            return None
        return prompt_questions(questions, sys.stdin, sys.stdout)

    return ask


def prompt_questions(questions: list[dict], stdin, stdout) -> list[dict]:
    """Print numbered choices and read one answer line per question.

    EOF dismisses the whole batch and returns ``[]``. A blank line leaves that
    question unanswered.
    """
    answers: list[dict] = []
    for question in questions:
        stdout.write(f"\n? {question['header']}\n")
        stdout.write(question["question"] + "\n")
        for index, option in enumerate(question["options"], start=1):
            line = f"  {index}. {option['label']}"
            description = option.get("description") or ""
            if description:
                line += f" - {description}"
            stdout.write(line + "\n")
        count = len(question["options"])
        prompt = _choice_prompt(count, question["multiSelect"])
        stdout.write(prompt)
        stdout.flush()
        while True:
            line = stdin.readline()
            if line == "":
                stdout.write("\n")
                stdout.flush()
                return []
            picked, error = _parse_choice(line, count, question["multiSelect"])
            if error:
                stdout.write(error + "\n")
                stdout.write(prompt)
                stdout.flush()
                continue
            labels = [question["options"][number - 1]["label"] for number in picked]
            answers.append({"id": question["id"], "selected": labels})
            break
    return answers


def _choice_prompt(count: int, multi: bool) -> str:
    if multi:
        return f"Choose any [1-{count}], comma-separated: "
    return f"Choose one [1-{count}]: "


def _choice_error(count: int, multi: bool) -> str:
    if multi:
        return f"Enter numbers from 1 to {count}, separated by commas."
    return f"Enter a number from 1 to {count}."


def _parse_choice(line: str, count: int, multi: bool) -> tuple[list[int], str | None]:
    text = line.strip()
    if text == "":
        return [], None
    parts = [part for part in re.split(r"[\s,]+", text) if part]
    numbers: list[int] = []
    for part in parts:
        if not part.isdigit():
            return [], _choice_error(count, multi)
        number = int(part)
        if number < 1 or number > count:
            return [], _choice_error(count, multi)
        if number not in numbers:
            numbers.append(number)
    if not numbers:
        return [], _choice_error(count, multi)
    if not multi and len(numbers) != 1:
        return [], f"Enter one number from 1 to {count}."
    return numbers, None


def _confirmer(yes: bool):
    def confirm(command: str) -> bool:
        sys.stdout.write(f"\n$ {command}")
        sys.stdout.flush()
        if yes:
            return True
        if not sys.stdin.isatty():
            sys.stdout.write("\nSkipped: pass --yes to run shell commands.\n")
            sys.stdout.flush()
            return False
        sys.stdout.write("\n[y/N] ")
        sys.stdout.flush()
        answer = sys.stdin.readline()
        if answer == "":
            sys.stdout.write("\n")
            sys.stdout.flush()
            return False
        return answer.strip().lower() in {"y", "yes"}

    return confirm


def _one_shot(agent: Agent, text: str) -> int:
    try:
        agent.ask(text)
    except ChatError as exc:
        sys.stderr.write(f"{exc}\n")
        return 1
    except KeyboardInterrupt as exc:
        sys.stderr.write("\n")
        message = str(exc)
        if message:
            sys.stderr.write(message + "\n")
        return 1
    if text.strip():
        sys.stdout.write("\n")
        sys.stdout.flush()
    return 0


def _interactive(agent: Agent) -> int:
    while True:
        try:
            line = input("\n> ")
        except EOFError:
            sys.stdout.write("\n")
            sys.stdout.flush()
            return 0
        except KeyboardInterrupt:
            sys.stdout.write("\n")
            sys.stdout.flush()
            return 0
        command = line.strip()
        if command == "":
            continue
        if command in {"/exit", "/quit"}:
            return 0
        if command == "/reset":
            print(agent.reset(), flush=True)
            continue
        matched = match_slash(command, agent.toolbox.skills)
        if matched is not None:
            name, arguments = matched
            try:
                outcome = agent.run_slash(command, name, arguments)
            except ChatError as exc:
                sys.stderr.write(f"\n{exc}\n")
                continue
            except KeyboardInterrupt:
                sys.stdout.write("\nInterrupted.\n")
                sys.stdout.flush()
                continue
            if outcome is None:
                continue
            sys.stdout.write("\n")
            sys.stdout.flush()
            continue
        try:
            agent.ask(line)
        except ChatError as exc:
            sys.stderr.write(f"\n{exc}\n")
            continue
        except KeyboardInterrupt:
            sys.stdout.write("\nInterrupted.\n")
            sys.stdout.flush()
            continue
        sys.stdout.write("\n")
        sys.stdout.flush()
