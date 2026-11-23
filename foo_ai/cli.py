"""Arguments, printing, prompts, and process exit codes."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from foo_ai.agent import Agent
from foo_ai.client import ChatError, Client
from foo_ai.config import ConfigError, load_config
from foo_ai.mcp import McpPool
from foo_ai.session import Session
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="foo-ai", allow_abbrev=False)
    parser.add_argument("prompt", nargs="*")
    parser.add_argument("--workspace", default=None)
    parser.add_argument("--max-steps", type=int, default=16)
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
    try:
        config = load_config(args.provider)
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
        workspace = Path(args.workspace).expanduser().resolve() if args.workspace else Path.cwd().resolve()
        session = Session(workspace)
        note = session.load()
        if note:
            print(note, flush=True)
        print(f"Foo AI · {config.provider.name} · {config.provider.model}", flush=True)
        for line in mcp_lines:
            print(line, flush=True)
        agent = Agent(
            provider=config.provider,
            session=session,
            toolbox=Toolbox(workspace, _confirmer(args.yes), pool),
            client=Client(config.provider),
            sink=CliSink(args.show_thinking, args.verbose),
            max_steps=args.max_steps,
        )
        if args.prompt:
            return _one_shot(agent, " ".join(args.prompt))
        print(f"Workspace {workspace}", flush=True)
        print("/exit to quit, /reset to clear the conversation.", flush=True)
        return _interactive(agent)
    finally:
        pool.close()


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
