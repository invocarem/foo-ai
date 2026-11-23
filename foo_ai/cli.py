"""Foo AI command line."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from foo_ai import __version__
from foo_ai.agent import Agent
from foo_ai.client import ChatError, Client
from foo_ai.config import load_provider


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="foo-ai", description="Foo AI, a small coding agent.")
    parser.add_argument("prompt", nargs="*", help="One-shot request. Omit it to start a session.")
    parser.add_argument("--version", action="version", version=f"Foo AI {__version__}")
    parser.add_argument("--workspace", type=Path, default=Path.cwd(), help="Directory tools may read and write.")
    parser.add_argument("--max-steps", type=int, default=16, help="Maximum model rounds for one request.")
    parser.add_argument("--show-thinking", action="store_true", help="Print reasoning text as it streams.")
    parser.add_argument("--verbose", action="store_true", help="Print tool results.")
    args = parser.parse_args(argv)

    provider = load_provider()
    agent = Agent(Client(provider), args.workspace.resolve(), max_steps=args.max_steps)
    banner = f"Foo AI · {provider.name} · {provider.model}"

    def on_event(event: dict) -> None:
        kind = event["type"]
        if kind == "reasoning" and args.show_thinking:
            print(event["text"], end="", file=sys.stderr, flush=True)
        elif kind == "content":
            print(event["text"], end="", flush=True)
        elif kind == "tool_start":
            if args.show_thinking:
                print(file=sys.stderr)
            print(f"\n→ {event['name']}", flush=True)
        elif kind == "tool_result":
            if args.verbose:
                print(event["content"])
            else:
                size = len(event["content"])
                print(f"← {event['name']} ({size} chars)")

    if args.prompt:
        print(banner)
        try:
            agent.ask(" ".join(args.prompt), on_event)
        except (ChatError, KeyboardInterrupt) as exc:
            print(f"\n{exc}", file=sys.stderr)
            return 1
        print()
        return 0

    print(f"{banner}\nWorkspace {agent.workspace}\n/exit to quit, /reset to clear the conversation.")
    while True:
        try:
            line = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not line:
            continue
        if line in {"/exit", "/quit"}:
            return 0
        if line == "/reset":
            agent.reset()
            print("Conversation cleared.")
            continue
        try:
            agent.ask(line, on_event)
        except ChatError as exc:
            print(f"\n{exc}", file=sys.stderr)
            continue
        except KeyboardInterrupt:
            print("\nInterrupted.")
            continue
        print()


if __name__ == "__main__":
    raise SystemExit(main())
